"""关键词采集任务（C0/C1）：SQLite 任务表 + 单线程消费者。

- 生产者：按 关键词 × 平台 入队，去重 + 冷却；
- 消费者：调 tools/collect_opencli.py 的 *-search 端点，写 records；
- 失败：指数退避，3 次进入 dead。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage

ROOT = Path(__file__).resolve().parent
PLATFORM_ENDPOINTS = {
    "weibo": "weibo-search",
    "zhihu": "zhihu-search",
    "tieba": "tieba-search",
    "bilibili": "bilibili-search",
    "xiaohongshu": "xiaohongshu-search",
}
DEFAULT_PLATFORMS = tuple(PLATFORM_ENDPOINTS)
MAX_BUDGET = 120
KEYWORD_COOLDOWN_MIN = 60


def init(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS crawl_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id TEXT NOT NULL,
        keyword TEXT NOT NULL,
        platform TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','running','ok','failed','dead')),
        limit_n INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        returned INTEGER NOT NULL DEFAULT 0,
        added INTEGER NOT NULL DEFAULT 0,
        duplicates INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        next_retry_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS crawl_jobs_status ON crawl_jobs(status, next_retry_at);
      CREATE UNIQUE INDEX IF NOT EXISTS crawl_jobs_dedupe
        ON crawl_jobs(keyword, platform, status) WHERE status IN ('queued','running');
    """)


def enqueue(db, keywords, platforms=None, limit=5, trace_id=None):
    """按 关键词×平台 入队；同词同平台已有 queued/running 或 60 分钟内 ok 的跳过。"""
    platforms = [p for p in (platforms or DEFAULT_PLATFORMS) if p in PLATFORM_ENDPOINTS]
    if not platforms:
        raise ValueError("platforms 至少包含一个受支持的平台")
    keywords = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
    if not keywords or len(keywords) > 20:
        raise ValueError("keywords 必须是 1–20 个非空字符串")
    trace_id = trace_id or "trace-crawl-" + storage.digest(str(time.time()).encode())[:12]
    stamp = storage.now()
    created = 0
    with db:
        for keyword in keywords:
            for platform in platforms:
                busy = db.execute(
                    "SELECT 1 FROM crawl_jobs WHERE keyword=? AND platform=? "
                    "AND (status IN ('queued','running') OR (status='ok' AND updated_at >= ?))",
                    (keyword, platform, _minutes_ago(KEYWORD_COOLDOWN_MIN))).fetchone()
                if busy:
                    continue
                db.execute(
                    "INSERT INTO crawl_jobs(trace_id,keyword,platform,endpoint,status,limit_n,"
                    "next_retry_at,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?,?,?)",
                    (trace_id, keyword, platform, PLATFORM_ENDPOINTS[platform],
                     limit, stamp, stamp, stamp))
                created += 1
    return {"trace_id": trace_id, "queued": created, "keywords": keywords,
            "platforms": platforms, "limit": limit}


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def _minutes_ago(minutes):
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def list_jobs(db, limit=50):
    rows = db.execute("SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def claim(db):
    """原子认领一条到期任务；没有则返回 None。"""
    stamp = storage.now()
    with db:
        row = db.execute(
            "SELECT * FROM crawl_jobs WHERE status IN ('queued','failed') "
            "AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY id LIMIT 1",
            (stamp,)).fetchone()
        if not row:
            return None
        db.execute("UPDATE crawl_jobs SET status='running', attempts=attempts+1, updated_at=? "
                   "WHERE id=? AND status IN ('queued','failed')", (stamp, row["id"]))
    return dict(row)


def finish(db, job_id, report):
    result = (report.get("results") or [{}])[0]
    run = result.get("run") or {}
    with db:
        db.execute("UPDATE crawl_jobs SET status='ok', returned=?, added=?, duplicates=?, "
                   "last_error=NULL, updated_at=? WHERE id=?",
                   (result.get("returned_count") or 0, run.get("added") or 0,
                    run.get("duplicates") or 0, storage.now(), job_id))


def fail(db, job_id, message):
    stamp = storage.now()
    with db:
        row = db.execute("SELECT attempts FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
        attempts = (row["attempts"] if row else 1)
        status = "dead" if attempts >= 3 else "failed"
        delay = 30 * (2 ** max(0, attempts - 1))
        next_retry = _iso(datetime.now(timezone.utc) + timedelta(seconds=delay))
        db.execute("UPDATE crawl_jobs SET status=?, last_error=?, next_retry_at=?, updated_at=? "
                   "WHERE id=?", (status, str(message)[:800], next_retry, stamp, job_id))


def run_one(db_path, job, timeout=180, opencli=None):
    cmd = [sys.executable, "-B", str(ROOT / "tools" / "collect_opencli.py"),
           "--endpoint", job["endpoint"], "--keyword", job["keyword"],
           "--limit", str(job["limit_n"]), "--max-per-run", str(job["limit_n"]),
           "--min-interval", "300",
           # 搜索单独记账，避免被热榜采集的冷却整轮跳过
           "--throttle-file", str(ROOT / ".collector-throttle-search.json")]
    if opencli:
        cmd += ["--opencli", opencli]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    raw = (proc.stdout or "").strip()
    try:
        report = json.loads(raw)
    except ValueError:
        raise RuntimeError((proc.stderr or raw or "采集器无输出")[:500])
    if proc.returncode != 0 or report.get("status") == "failed":
        err = (report.get("error") or {}).get("message") or (proc.stderr or "")[:300]
        raise RuntimeError(err or f"采集器退出码 {proc.returncode}")
    if report.get("status") == "throttled":
        raise RuntimeError("平台冷却中，稍后重试")
    return report


def run_pending(db_path, max_jobs=3, opencli=None):
    """消费至多 max_jobs 条任务；返回处理摘要。"""
    from storage import connect
    done = []
    db = connect(db_path)
    try:
        init(db)
        for _ in range(max_jobs):
            job = claim(db)
            if not job:
                break
            try:
                report = run_one(db_path, job, opencli=opencli)
            except Exception as exc:
                fail(db, job["id"], exc)
                done.append({"id": job["id"], "status": "failed", "error": str(exc)[:200]})
                continue
            finish(db, job["id"], report)
            done.append({"id": job["id"], "status": "ok",
                         "added": (report.get("results") or [{}])[0].get("run", {}).get("added", 0)})
    finally:
        db.close()
    return done
