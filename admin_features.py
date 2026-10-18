"""报告管理 / 系统设置 / 事件聚类（P3）。纯标准库，读写本机 sentiment.db。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
import sqlite3

import storage
import pipeline

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
        platforms TEXT NOT NULL, titles TEXT NOT NULL, first_seen TEXT,
        created_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS events_size ON events(records);
    """)


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


def generate_report(db, kind, trace_id=None, task_id=None):
    if kind not in ("daily", "weekly"):
        raise ValueError("kind 必须是 daily 或 weekly")
    now = datetime.now(timezone(timedelta(hours=8)))
    if kind == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"舆情日报 · {start.strftime('%Y-%m-%d')}"
    else:
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"舆情周报 · {start.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}"

    stats = storage.overview_stats(db, 1 if kind == "daily" else 7)
    window_days = 1 if kind == "daily" else 7
    kpi = stats["kpi"]
    platforms = stats["platforms"][:8]
    sentiment = stats["sentiment"]
    events = db.execute(
        "SELECT label,records,platforms FROM events WHERE records >= 2 ORDER BY records DESC, label LIMIT 5"
    ).fetchall()
    alerts = db.execute(
        "SELECT level, metrics, status, last_seen FROM alerts "
        "WHERE status='open' ORDER BY last_seen DESC LIMIT 5").fetchall()

    lines = [
        f"# {title}",
        "",
        f"- 生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（UTC+8）",
        f"- 统计窗口：近 {window_days} 天（{_fmt_local(stats.get('window_since'))} 起）",
        f"- 数据来源：本机采集库 sentiment.db（records / events / alerts）",
        "",
        "## 一、核心指标",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 记录总数 | {kpi['records']} 条 |",
        f"| 去重条目 | {kpi['canonical']} 条 |",
        f"| 来源数 | {kpi['feeds']} 个 |",
        f"| 时间缺失 | {round((kpi['unknown_time_ratio'] or 0) * 100, 1)}% |",
        f"| 情感待判 | {round((kpi['sentiment_pending_ratio'] or 0) * 100, 1)}% |",
        f"| 最近成功采集 | {_fmt_local(kpi.get('last_success_at'))} |",
        "",
        "## 二、平台分布",
        "",
        "| 来源 | 记录数 |",
        "|---|---|",
    ]
    lines += [f"| {p['label']} | {p['value']} |" for p in platforms] or ["| （无） | 0 |"]
    lines += ["", "## 三、情感分布", "", "| 情感 | 条数 |", "|---|---|"]
    lines += [f"| {s['label']} | {s['value']} |" for s in sentiment] or ["| （无） | 0 |"]
    lines += ["", "## 四、热点事件（相似标题聚类，≥2 条）", ""]
    if events:
        lines += ["| 事件 | 条数 | 涉及来源 |", "|---|---|---|"]
        for row in events:
            platforms_text = ", ".join(json.loads(row["platforms"]) or []) or "—"
            lines.append(f"| {row['label']} | {row['records']} | {platforms_text} |")
    else:
        lines.append("_本次未运行事件聚类，或没有 ≥2 条的聚类事件。_")
    lines += ["", "## 五、未处理预警", ""]
    if alerts:
        lines += ["| 级别 | 命中 | 最近出现 |", "|---|---|---|"]
        for row in alerts:
            try:
                metrics = json.loads(row["metrics"] or "{}")
            except ValueError:
                metrics = {}
            lines.append(f"| {row['level']} | {metrics.get('hit_count', '—')} | {_fmt_local(row['last_seen'])} |")
    else:
        lines.append("_当前没有未处理预警。_")
    lines += ["", "---", "",
              "> 本报告由本机规则与统计自动生成，不代表人工结论；情感为词典规则粗筛，时间口径为 published_at ?: fetched_at。"]

    body = "\n".join(lines) + "\n"
    stamp = now.strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # 同一天同 kind 只保留一份：重跑覆盖，避免流水线每次运行都新增日报
    existing = db.execute(
        "SELECT id,path FROM reports WHERE kind=? AND date(created_at,'+8 hours')=?",
        (kind, now.strftime("%Y-%m-%d"))).fetchone()
    if existing:
        report_id = existing["id"]
        path = Path(existing["path"])
    else:
        report_id = f"report-{kind}-{stamp}-{uuid.uuid4().hex[:6]}"
        path = REPORTS_DIR / f"{report_id}.md"
    task = pipeline.start(DB, "report", {"kind": kind, "report_id": report_id},
                          trace_id=trace_id, task_id=task_id)
    try:
        path.write_text(body, encoding="utf-8")
        size = path.stat().st_size
        with db:
            db.execute(
                "INSERT OR REPLACE INTO reports(id,kind,title,period_start,period_end,created_at,"
                "size_bytes,path,summary) VALUES(?,?,?,?,?,?,?,?,?)",
                (report_id, kind, title, start.isoformat(), now.isoformat(),
                 storage.now(), size, str(path), f"{kpi['records']} 条记录 / {len(events)} 个事件"))
    except Exception as exc:
        path.unlink(missing_ok=True)   # 写盘或入库失败都不留孤儿文件
        pipeline.fail(DB, task["id"], exc)
        raise
    pipeline.finish(DB, task["id"], {"id": report_id, "size": size})
    return {"id": report_id, "kind": kind, "title": title, "size_bytes": size,
            "created_at": storage.now(), "path": str(path)}


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
                        "INSERT OR REPLACE INTO events(id,label,records,platforms,titles,first_seen,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (event["event_id"], titles[0] if titles else "(无标题)", int(event.get("size") or 0),
                         json.dumps(event.get("feeds") or [], ensure_ascii=False),
                         json.dumps(titles[:20], ensure_ascii=False), event.get("first_seen"), stamp))
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
        "SELECT label, records, platforms, titles FROM events WHERE records >= 2 "
        "ORDER BY records DESC, label").fetchall()
    analyzed = db.execute("SELECT coalesce(sum(records),0) FROM events").fetchone()[0] or 0
    all_topics = []
    for index, ev in enumerate(events):
        try:
            titles = json.loads(ev["titles"] or "[]")
            platforms = json.loads(ev["platforms"] or "[]")
        except ValueError:
            titles, platforms = [], []
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
        label = ev["label"] or "(无标题)"
        all_topics.append({
            "name": label[:18] + ("…" if len(label) > 18 else ""),
            "full": label,
            "keywords": platforms,
            "count": int(ev["records"] or 0),
            "trend": trend,
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
            shared = set(topics[i]["keywords"]) & set(topics[j]["keywords"])
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

