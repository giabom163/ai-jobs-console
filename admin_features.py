"""报告管理 / 系统设置 / 事件聚类（P3）。纯标准库，读写本机 sentiment.db。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
import sqlite3

import storage
import pipeline
import reporting

ROOT = Path(__file__).resolve().parent
DB = ROOT / "sentiment.db"
EVENTS_DIR = ROOT / "events"
REPORTS_DIR = ROOT / "reports"
SETTINGS_KEYS = ("collect_interval", "collect_limit", "keywords", "fuzzy")
DEFAULT_SETTINGS = {"collect_interval": 20, "collect_limit": 20, "keywords": [], "fuzzy": True}
_EVENT_LOCK = threading.Lock()


def init(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS reports(
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
        period_start TEXT, period_end TEXT, created_at TEXT NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL, summary TEXT);
      CREATE INDEX IF NOT EXISTS reports_created ON reports(created_at);
      CREATE TABLE IF NOT EXISTS events(
        id TEXT PRIMARY KEY, label TEXT NOT NULL, records INTEGER NOT NULL,
        platforms TEXT NOT NULL, titles TEXT NOT NULL, keywords TEXT, first_seen TEXT,
        created_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS events_size ON events(records);
    """)
    cols = [row[1] for row in db.execute("PRAGMA table_info(events)")]
    if "keywords" not in cols:
        db.execute("ALTER TABLE events ADD COLUMN keywords TEXT")


def extract_keywords(titles, topk=5):
    """jieba TF-IDF 关键词；不可用时返回空列表（UI 回退为来源）。"""
    try:
        import jieba.analyse
    except Exception:
        return []
    try:
        text = "。".join([t for t in titles[:20] if t])
        return [w for w in jieba.analyse.extract_tags(text, topK=topk) if len(w) > 1][:topk]
    except Exception:
        return []


# ---------- 设置 ----------

def get_settings(db):
    out = dict(DEFAULT_SETTINGS)
    for row in db.execute("SELECT key, value FROM settings"):
        if row["key"] not in SETTINGS_KEYS:
            continue
        try:
            out[row["key"]] = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
    out["keywords"] = [k for k in out.get("keywords") or [] if isinstance(k, str)]
    return out


def set_keywords(db, items):
    """关键词唯一写入口：同时更新 keywords 表与 settings.keywords。"""
    cleaned = []
    for item in items or []:
        if not isinstance(item, str):
            continue
        text = item.strip()[:80]
        if text and text not in cleaned:
            cleaned.append(text)
    stamp = storage.now()
    with db:
        db.execute("DELETE FROM keywords")
        db.executemany("INSERT OR IGNORE INTO keywords(value) VALUES(?)", [(k,) for k in cleaned])
        db.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES('keywords',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (json.dumps(cleaned, ensure_ascii=False), stamp))
    return cleaned


def save_settings(db, payload):
    """只接受四个已知键；keywords 同步写入 keywords 表（检索/预警用）。"""
    merged = get_settings(db)
    if "collect_interval" in payload:
        value = payload["collect_interval"]
        if type(value) is not int or not 0 <= value <= 1440:
            raise ValueError("collect_interval 必须是 0–1440 的整数（分钟）")
        merged["collect_interval"] = value
    if "collect_limit" in payload:
        value = payload["collect_limit"]
        if type(value) is not int or not 1 <= value <= 50:
            raise ValueError("collect_limit 必须是 1–50 的整数")
        merged["collect_limit"] = value
    if "keywords" in payload:
        items = payload["keywords"]
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("keywords 必须是最多 100 项的数组")
    if "fuzzy" in payload:
        if not isinstance(payload["fuzzy"], bool):
            raise ValueError("fuzzy 必须是布尔值")
        merged["fuzzy"] = payload["fuzzy"]

    stamp = storage.now()
    with db:
        for key in ("collect_interval", "collect_limit", "fuzzy"):
            db.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(merged[key], ensure_ascii=False), stamp))
    if "keywords" in payload:
        merged["keywords"] = set_keywords(db, payload["keywords"])
    return merged


# ---------- 报告 ----------

def list_reports(db, limit=50):
    rows = db.execute(
        "SELECT id,kind,title,period_start,period_end,created_at,size_bytes,path,summary "
        "FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _fmt_local(value):
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:19]
    if dt.tzinfo is None:            # SQLite 里的 ISO 字符串是 UTC 且不带时区
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone(timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M:%S")


def _ledger_path(db, db_path=None):
    """pipeline 记账写回同一个库；显式传入优先，否则从连接读取真实文件路径。"""
    if db_path:
        return db_path
    try:
        for row in db.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return row[2]
    except sqlite3.Error:
        pass
    return DB


def _write_immutable(path, data):
    """不可变产物：唯一临时文件 + 原子 no-overwrite 落位。

    内容一致则不动；内容不同说明 ID 冲突，拒绝覆盖。并发下由硬链接 / O_EXCL
    保证只有一个写入者成功，避免 exists + 固定 tmp + os.replace 的竞争覆盖。
    """
    if path.exists():
        if path.read_bytes() == data:
            return False
        raise ValueError(f"报告产物已存在且内容不同，拒绝覆盖：{path.name}")
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)          # 原子、不覆盖的落位
        except FileExistsError:
            if path.read_bytes() == data:
                return False
            raise ValueError(f"报告产物已存在且内容不同，拒绝覆盖：{path.name}")
        except OSError:
            # 文件系统不支持硬链接时退化为独占创建（同样不覆盖）
            try:
                with open(path, "xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if path.read_bytes() == data:
                    return False
                raise ValueError(f"报告产物已存在且内容不同，拒绝覆盖：{path.name}")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return True


_NEW_REPORT_ID = re.compile(
    r"report-(?:" + "|".join(re.escape(kind) for kind in reporting.KINDS) + r")-\d{6,8}-[0-9a-f]{10}")


def _is_new_report_id(stem):
    """报告文件名是否为新版结构：report-<kind>-<period>-<10 位十六进制内容哈希>。"""
    return _NEW_REPORT_ID.fullmatch(stem) is not None


def load_verified_report(md_path):
    """读取报告 Markdown；存在同名 JSON 快照时校验两者一致后再返回。

    新版报告（ID 形如 report-<kind>-<period>-<10 位哈希>）必须带同名 JSON 快照：
    缺快照、JSON 损坏或 Markdown 被改动都不允许当作成功预览 / PDF / 下载。
    只有确认的旧版报告（文件名不是新版 ID 结构）无 sidecar 时才原样返回以保持兼容。
    """
    path = Path(md_path)
    body = path.read_text(encoding="utf-8")
    sidecar = path.with_name(path.stem + ".json")
    if not sidecar.exists():
        if _is_new_report_id(path.stem):
            raise ValueError("新版报告缺少同名 JSON 快照")
        return body
    if not sidecar.is_file():
        # sidecar 位置存在但不是文件（如目录）：绝不当成旧报告放行
        raise ValueError("报告快照路径不是文件")
    try:
        snapshot = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"报告快照无法解析：{exc}") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("报告快照格式不正确")
    digest = reporting.validate_snapshot(snapshot)
    if snapshot.get("content_sha256") != digest:
        raise ValueError("报告快照缺少有效内容哈希")
    if reporting.render_markdown(snapshot) != body:
        raise ValueError("报告 Markdown 与快照内容不一致")
    return body


def generate_report(db, kind, trace_id=None, task_id=None, db_path=None):
    """统一报告入口：经发布闸门后持久化不可变 JSON 快照 + Markdown（不覆盖旧报告）。

    只读取数，不触发采集/标注/模型；闸门异常时抛 ValueError，不写成功产物或报告行。
    """
    if kind not in reporting.KINDS:
        raise ValueError("kind 必须是 daily / weekly / monthly")
    ledger = _ledger_path(db, db_path)
    task_id = task_id or ("task-report-" + uuid.uuid4().hex)
    task = pipeline.start(ledger, "report", {"kind": kind}, trace_id=trace_id, task_id=task_id)
    try:
        snapshot, markdown, report_id = reporting.build_report(db, kind)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        md_path = REPORTS_DIR / f"{report_id}.md"
        json_path = REPORTS_DIR / f"{report_id}.json"
        md_bytes = markdown.encode("utf-8")
        json_bytes = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                indent=1).encode("utf-8")
        # 先写快照再写 Markdown：任一步失败都不会留下可被当作旧报告预览的孤儿 Markdown。
        _write_immutable(json_path, json_bytes)
        _write_immutable(md_path, md_bytes)
        # 落位后回读校验，确认两产物一致且未被并发覆盖，再写成功行。
        if md_path.read_bytes() != md_bytes or json_path.read_bytes() != json_bytes:
            raise ValueError("报告产物落位校验失败")
        stored = json.loads(json_path.read_text(encoding="utf-8"))
        if reporting.validate_snapshot(stored) != stored.get("content_sha256"):
            raise ValueError("报告快照落位校验失败")
        if reporting.render_markdown(stored) != md_path.read_text(encoding="utf-8"):
            raise ValueError("报告 Markdown 与快照内容不一致")
        size = md_path.stat().st_size
        sample_total = snapshot["counts"]["sample_total"]
        summary = f"{sample_total} 条原帖线索 / {len(snapshot['topics'])} 类议题"
        with db:
            db.execute(
                "INSERT OR IGNORE INTO reports(id,kind,title,period_start,period_end,created_at,"
                "size_bytes,path,summary) VALUES(?,?,?,?,?,?,?,?,?)",
                (report_id, kind, snapshot["title"], snapshot["window"]["start"],
                 snapshot["window"]["end"], storage.now(), size, str(md_path), summary))
    except Exception as exc:
        pipeline.fail(ledger, task["id"], exc)
        raise
    pipeline.finish(ledger, task["id"], {"id": report_id, "size": size, "sample": sample_total})
    return {"id": report_id, "kind": kind, "title": snapshot["title"], "size_bytes": size,
            "created_at": snapshot["generated_at"], "path": str(md_path),
            "snapshot_path": str(json_path), "sample_total": sample_total}


# ---------- 事件聚类 ----------

def rebuild_events(limit=200, trace_id=None, task_id=None):
    """调用 tools/cluster_events.py --no-model，导入 events 表；失败保留旧数据。"""
    if not 10 <= limit <= 200:
        raise ValueError("limit 必须在 10–200 之间（聚类工具上限 200）")
    if not _EVENT_LOCK.acquire(blocking=False):
        raise RuntimeError("已有聚类任务在运行，请稍后再试")
    task = None
    try:
        task = pipeline.start(DB, "topic", {"limit": limit}, trace_id=trace_id, task_id=task_id)
        before = {p.name for p in EVENTS_DIR.glob("events-*.jsonl")}
        started_ts = time.time()
        cmd = [sys.executable, "-B", str(ROOT / "tools" / "cluster_events.py"),
               "--no-model", "--limit", str(limit), "--db", str(DB), "--out-dir", str(EVENTS_DIR)]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
        if proc.returncode != 0:
            detail = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()[:400]
            raise RuntimeError(f"聚类失败（退出码 {proc.returncode}）：{detail}")
        produced = [p for p in sorted(EVENTS_DIR.glob("events-*.jsonl"),
                                      key=lambda p: p.stat().st_mtime)
                    if p.name not in before or p.stat().st_mtime >= started_ts]
        if not produced:
            raise RuntimeError("本次聚类没有产出新的 events-*.jsonl，已保留旧数据")
        files = produced
        counts = 0
        stamp = storage.now()
        db = DB_CONNECT()
        try:
            with db:
                db.execute("DELETE FROM events")
                for line in files[-1].read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    titles = event.get("titles") or []
                    db.execute(
                        "INSERT OR REPLACE INTO events(id,label,records,platforms,titles,first_seen,created_at,keywords) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (event["event_id"], titles[0] if titles else "(无标题)", int(event.get("size") or 0),
                         json.dumps(event.get("feeds") or [], ensure_ascii=False),
                         json.dumps(titles[:20], ensure_ascii=False), event.get("first_seen"), stamp,
                         json.dumps(extract_keywords(titles), ensure_ascii=False)))
                    counts += 1
        finally:
            db.close()
        pipeline.finish(DB, task["id"], {"events": counts})
        return {"events": counts, "limit": limit, "source": str(files[-1])}
    except Exception as exc:
        if task:
            pipeline.fail(DB, task["id"], exc)
        raise
    finally:
        _EVENT_LOCK.release()


def DB_CONNECT():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def list_events(db, limit=50):
    rows = db.execute(
        "SELECT id,label,records,platforms,titles,first_seen FROM events "
        "ORDER BY records DESC, label LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def active_topics(db, min_records=2):
    return db.execute("SELECT count(*) FROM events WHERE records >= ?", (min_records,)).fetchone()[0]


def topic_analysis(db, window_days=7):
    """话题分析：以 events（相似标题聚类）为话题，按本地日统计趋势与共现关系。"""
    if window_days not in (7, 14, 30):
        raise ValueError("window 只接受 7 / 14 / 30")
    now = datetime.now(timezone(timedelta(hours=8)))
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(window_days - 1, -1, -1)]
    start = (now - timedelta(days=window_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since = start.astimezone(timezone.utc).isoformat()

    rows = db.execute("SELECT title, fetched_at FROM records WHERE fetched_at >= ?", (since,)).fetchall()
    total = len(rows)
    day_of = {}
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["fetched_at"]).astimezone(timezone(timedelta(hours=8)))
            day = dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
        day_of.setdefault(row["title"], []).append(day)

    events = db.execute(
        "SELECT label, records, platforms, titles, keywords FROM events WHERE records >= 2 "
        "ORDER BY records DESC, label").fetchall()
    analyzed = db.execute("SELECT coalesce(sum(records),0) FROM events").fetchone()[0] or 0
    all_topics = []
    for index, ev in enumerate(events):
        try:
            titles = json.loads(ev["titles"] or "[]")
            platforms = json.loads(ev["platforms"] or "[]")
        except ValueError:
            titles, platforms = [], []
        try:
            keywords = json.loads(ev["keywords"] or "[]")
        except ValueError:
            keywords = []
        keywords = [k for k in keywords if isinstance(k, str)]
        series = [0] * len(days)
        for title in titles:
            for day in day_of.get(title, []):
                if day in days:
                    series[days.index(day)] += 1
        last = series[-1] if series else 0
        prev = series[-2] if len(series) > 1 else 0
        if prev == 0:
            trend = 0 if last == 0 else 100.0
        else:
            trend = round((last - prev) / prev * 100, 1)
        baseline = series[:-1]
        if len(baseline) >= 3 and last > 0:
            ordered = sorted(baseline)
            mid = len(ordered) // 2
            median = (ordered[mid - 1] + ordered[mid]) / 2
            deviations = sorted(abs(v - median) for v in baseline)
            mid = len(deviations) // 2
            mad = (deviations[mid - 1] + deviations[mid]) / 2
            if max(baseline) == 0:
                # 真正从零起量：量级足够大才判异常
                zscore = 99.0 if last >= 5 else 0.0
            else:
                zscore = round(0.6745 * (last - median) / max(mad, 1.0), 2)
        else:
            zscore = 0.0
        anomaly = bool(last >= 2 and zscore >= 3.5)
        label = ev["label"] or "(无标题)"
        all_topics.append({
            "name": label[:18] + ("…" if len(label) > 18 else ""),
            "full": label,
            "keywords": keywords,
            "platforms": platforms,
            "count": int(ev["records"] or 0),
            "trend": trend,
            "zscore": zscore,
            "anomaly": anomaly,
            "series": series,
            "index": index,
        })

    topics = all_topics[:6]
    clustered = sum(t["count"] for t in all_topics)
    other = max(0, analyzed - sum(t["count"] for t in topics))
    coverage = round(clustered / analyzed * 100, 1) if analyzed else 0.0
    rising = sum(1 for t in all_topics if t["trend"] > 0)

    edges = []
    for i in range(len(topics)):
        for j in range(i + 1, len(topics)):
            shared = set(topics[i]["platforms"]) & set(topics[j]["platforms"])
            if shared:
                edges.append({"a": i, "b": j, "weight": len(shared)})

    return {
        "window_days": window_days,
        "days": days,
        "kpi": {"topics": len(all_topics), "coverage": coverage, "rising": rising,
                "total": total, "analyzed": analyzed, "clustered": clustered, "other": other},
        "topics": topics,
        "other": {"name": "其他话题", "count": other, "trend": 0.0,
                  "series": [0] * len(days)},
        "edges": edges,
        "generated_at": storage.now(),
    }

