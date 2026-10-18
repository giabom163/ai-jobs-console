"""预警中心：规则表 + 告警表 + 60 秒调度线程 + 查询/确认。仅标准库，不联网、不调用模型。

口径说明（可审计）：
- 命中对象是旧 RSS 材料表 records（keyword 匹配 title/summary；source 匹配 feeds.name；
  sentiment 匹配 records.sentiment），不是人工候选 p1_evidence。
- 时间窗口口径与 storage.overview_stats 一致：coalesce(published_at, fetched_at)，缺时不补造。
- 冷却按规则维度（同规则最近一条告警的 last_seen + cooldown_minutes）判断；冷却期内不再新建。
- 告警 ID 由 storage.digest 生成；同一时刻重复 evaluate 用 INSERT OR IGNORE 幂等，不重复计数。
"""
from datetime import datetime, timedelta, timezone
import json
import re
import sys
import threading
import time

import storage

MATCH_KINDS = ("keyword", "source", "sentiment")
LEVELS = ("info", "warn", "critical")
STATUSES = ("open", "acked", "closed")
RULE_FIELDS = {"name", "match_kind", "match_value", "window_minutes", "threshold", "level", "cooldown_minutes"}
LIST_LIMIT = 500
EVIDENCE_SAMPLE = 20
WINDOW_MAX = 10080          # 7 天，分钟
COOLDOWN_MAX = 10080
THRESHOLD_MAX = 100000
SCHEDULER = {"started": False}
SCHEDULER_LOCK = threading.Lock()

SCHEMA = """
  CREATE TABLE IF NOT EXISTS alert_rules(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, match_kind TEXT NOT NULL CHECK(match_kind IN ('keyword','source','sentiment')), match_value TEXT NOT NULL, window_minutes INTEGER NOT NULL DEFAULT 60, threshold INTEGER NOT NULL DEFAULT 1, level TEXT NOT NULL DEFAULT 'info' CHECK(level IN ('info','warn','critical')), cooldown_minutes INTEGER NOT NULL DEFAULT 30, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY, rule_id INTEGER NOT NULL, level TEXT NOT NULL, metrics TEXT, evidence TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acked','closed')));
"""


def init(db):
    """建表（IF NOT EXISTS，幂等）。服务与调度线程每次连接都会调用。"""
    db.executescript(SCHEMA)


def moment_of(value):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("moment 必须是带时区的 datetime")
    return value.astimezone(timezone.utc)


def keyword_like(value):
    """字面量包含匹配：转义 LIKE 的通配符，避免 % 与 _ 被当作模式。"""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def match_clause(kind, value):
    """返回 (SQL 片段, 参数) 或 (None, ())；片段只引用 records 别名 r 与 feeds 别名 f。"""
    if kind == "keyword":
        pattern = keyword_like(value)
        return "(r.title LIKE ? ESCAPE '\\' OR r.summary LIKE ? ESCAPE '\\')", (pattern, pattern)
    if kind == "source":
        return "f.name = ?", (value,)
    if kind == "sentiment":
        if value == "偏负面":
            return "r.sentiment IN ('偏负面','强负面')", ()
        if value == "偏正面":
            return "r.sentiment IN ('偏正面','强正面')", ()
        return "r.sentiment = ?", (value,)
    return None, ()


def in_cooldown(db, rule, moment):
    """同规则最近一条告警是否仍在冷却期。按插入顺序（rowid）取最近一条，避免字符串时间比较。"""
    cooldown = rule["cooldown_minutes"]
    if type(cooldown) is not int or cooldown <= 0:
        return False
    last = db.execute("SELECT last_seen FROM alerts WHERE rule_id=? ORDER BY rowid DESC LIMIT 1",
                      (rule["id"],)).fetchone()
    if last is None:
        return False
    return moment - datetime.fromisoformat(last[0]) < timedelta(minutes=cooldown)


def rule_hits(db, rule, since):
    """窗口内命中记录数 + 命中的 record id（前 EVIDENCE_SAMPLE 条）。"""
    where, args = match_clause(rule["match_kind"], rule["match_value"])
    if where is None:
        return None, []
    base = (" FROM records r JOIN feeds f ON f.id=r.feed_id"
            " WHERE coalesce(r.published_at,r.fetched_at)>=? AND " + where)
    total = db.execute("SELECT count(*)" + base, (since,) + args).fetchone()[0]
    if total <= 0:
        return total, []
    ids = [row[0] for row in db.execute(
        "SELECT r.id" + base + " ORDER BY r.id LIMIT ?", (since,) + args + (EVIDENCE_SAMPLE,))]
    return total, ids


def evaluate(db, moment=None):
    """对每条 enabled=1 规则统计窗口内命中数；达阈值且不在冷却期内则新建一条告警。

    返回本轮新建告警条数与统计；重复调用（同一 moment）不会重复新建同一条告警。
    """
    moment = moment_of(moment)
    checked = 0
    created = 0
    triggered = []
    for rule in [dict(row) for row in db.execute("SELECT * FROM alert_rules WHERE enabled=1 ORDER BY id")]:
        window = rule["window_minutes"]
        if type(window) is not int or window < 1:
            window = 60
        checked += 1
        since = (moment - timedelta(minutes=window)).isoformat()
        total, ids = rule_hits(db, rule, since)
        if not total or total < rule["threshold"]:
            continue
        if in_cooldown(db, rule, moment):
            continue
        metrics = {"hit_count": total, "threshold": rule["threshold"], "window_minutes": window,
                   "match_kind": rule["match_kind"], "match_value": rule["match_value"]}
        stamp = moment.isoformat()
        alert_id = "alert-" + storage.digest(storage.json_bytes(
            [rule["id"], stamp, rule["match_kind"], rule["match_value"], total, ids]))
        with db:
            cursor = db.execute("""INSERT OR IGNORE INTO alerts(id,rule_id,level,metrics,evidence,first_seen,last_seen,status)
                                   VALUES(?,?,?,?,?,?,?,'open')""",
                                (alert_id, rule["id"], rule["level"], storage.json_bytes(metrics).decode(),
                                 storage.json_bytes(ids).decode(), stamp, stamp))
        # 同一时刻重复 evaluate 命中同一条告警时 IGNORE，不计入新建条数。
        if cursor.rowcount != 1:
            continue
        created += 1
        triggered.append({"alert_id": alert_id, "rule_id": rule["id"], "name": rule["name"],
                          "level": rule["level"], "hit_count": total})
    return {"created": created, "checked_rules": checked, "evaluated_at": moment.isoformat(),
            "triggered": triggered}


def rules(db):
    """全部规则（含 enabled=0），按 id 升序。"""
    return [dict(row) for row in db.execute("SELECT * FROM alert_rules ORDER BY id")]


def alert_out(row):
    item = dict(row)
    for field in ("metrics", "evidence"):
        if item.get(field):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, json.JSONDecodeError):
                pass
    return item


def alerts(db, limit=100):
    """告警列表，按新建时间倒序（rowid）。"""
    if type(limit) is not int or not 1 <= limit <= LIST_LIMIT:
        raise ValueError(f"limit 必须为 1–{LIST_LIMIT} 的整数")
    return [alert_out(row) for row in db.execute("SELECT * FROM alerts ORDER BY rowid DESC LIMIT ?", (limit,))]


def ack(db, alert_id):
    """确认告警：status 改为 acked，last_seen 不变。仅 open 可确认。"""
    if not isinstance(alert_id, str) or not alert_id.strip():
        raise ValueError("alert_id 必须为非空文本")
    alert_id = alert_id.strip()
    row = db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if row is None:
        raise ValueError("告警不存在")
    if row["status"] != "open":
        raise ValueError("仅未确认（open）的告警可确认")
    with db:
        db.execute("UPDATE alerts SET status='acked' WHERE id=? AND status='open'", (alert_id,))
    return alert_out(db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone())


def int_field(payload, name, default, low, high):
    value = payload.get(name, default)
    if value is None:
        value = default
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} 必须为 {low}–{high} 的整数")
    return value


def create_rule(db, payload):
    """校验并新增规则；非法字段抛 ValueError（服务端映射为 400）。"""
    if not isinstance(payload, dict):
        raise ValueError("请求格式错误")
    unknown = set(payload) - RULE_FIELDS
    if unknown:
        raise ValueError("规则含未声明字段：" + "、".join(sorted(unknown)))
    name = payload.get("name")
    if not isinstance(name, str):
        raise ValueError("name 必须为文本")
    name = re.sub(r"\s+", " ", name).strip()
    if not 1 <= len(name) <= 100:
        raise ValueError("name 必须为 1–100 字符的文本")
    kind = payload.get("match_kind")
    if kind not in MATCH_KINDS:
        raise ValueError("match_kind 必须为 keyword/source/sentiment")
    value = payload.get("match_value")
    if not isinstance(value, str):
        raise ValueError("match_value 必须为文本")
    value = value.strip()
    if not 1 <= len(value) <= 200:
        raise ValueError("match_value 必须为 1–200 字符的文本")
    window = int_field(payload, "window_minutes", 60, 1, WINDOW_MAX)
    threshold = int_field(payload, "threshold", 1, 1, THRESHOLD_MAX)
    level = payload.get("level", "info")
    if level is None:
        level = "info"
    if level not in LEVELS:
        raise ValueError("level 必须为 info/warn/critical")
    cooldown = int_field(payload, "cooldown_minutes", 30, 0, COOLDOWN_MAX)
    with db:
        cursor = db.execute("""INSERT INTO alert_rules(name,enabled,match_kind,match_value,window_minutes,
                               threshold,level,cooldown_minutes,created_at) VALUES(?,1,?,?,?,?,?,?,?)""",
                            (name, kind, value, window, threshold, level, cooldown, storage.now()))
        rule_id = cursor.lastrowid
    return dict(db.execute("SELECT * FROM alert_rules WHERE id=?", (rule_id,)).fetchone())


def _loop(db_path, interval_seconds):
    interval = max(1, int(interval_seconds))
    while True:
        time.sleep(interval)
        try:
            db = storage.connect(db_path)
            try:
                init(db)
                evaluate(db)
            finally:
                db.close()
        except Exception as exc:                      # 调度线程任何异常只记录，不外抛、不终止循环
            print(f"[alertcenter] scheduler evaluate failed: {exc!r}", file=sys.stderr, flush=True)


def start_scheduler(db_path, interval_seconds=60):
    """启动一个 daemon 线程，每 interval_seconds 用 storage.connect 打开库跑一次 evaluate。

    幂等：已启动时直接返回 False，不重复起线程。异常只在 _loop 内记录到 stderr。
    """
    global SCHEDULER
    with SCHEDULER_LOCK:
        if SCHEDULER["started"]:
            return False
        SCHEDULER["started"] = True
    thread = threading.Thread(target=_loop, args=(db_path, interval_seconds),
                              name="alertcenter-scheduler", daemon=True)
    thread.start()
    return True
