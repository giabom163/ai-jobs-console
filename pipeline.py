"""轻量任务流水线：pipeline_tasks 记录每个 stage 的状态与 trace，替代 Redis Stream 的记账部分。"""
from __future__ import annotations

import json
import sqlite3
import uuid

import storage

DB = None


def init(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS pipeline_tasks(
        id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, stage TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','ok','failed','dead')),
        payload TEXT, result TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
      CREATE INDEX IF NOT EXISTS pipeline_trace ON pipeline_tasks(trace_id);
      CREATE INDEX IF NOT EXISTS pipeline_created ON pipeline_tasks(created_at);
    """)


def _connect(db_path):
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    init(db)
    return db


def start(db_path, stage, payload=None, trace_id=None, task_id=None):
    """创建一个 running 任务；同一 task_id 再次 start 视为重试（attempts+1）。"""
    task_id = task_id or "task-" + uuid.uuid4().hex
    trace_id = trace_id or "trace-" + uuid.uuid4().hex
    stamp = storage.now()
    db = _connect(db_path)
    try:
        with db:
            db.execute(
                "INSERT INTO pipeline_tasks(id,trace_id,stage,status,payload,attempts,created_at,started_at) "
                "VALUES(?,?,?,'running',?,0,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status='running', attempts=attempts+1, "
                "started_at=excluded.started_at, finished_at=NULL, last_error=NULL",
                (task_id, trace_id, stage,
                 json.dumps(payload or {}, ensure_ascii=False)[:20000], stamp, stamp))
    finally:
        db.close()
    return {"id": task_id, "trace_id": trace_id, "stage": stage, "status": "running"}


def finish(db_path, task_id, result=None):
    db = _connect(db_path)
    try:
        with db:
            db.execute("UPDATE pipeline_tasks SET status='ok', result=?, finished_at=? "
                       "WHERE id=? AND status IN ('running','failed')",
                       (json.dumps(result or {}, ensure_ascii=False)[:20000], storage.now(), task_id))
    finally:
        db.close()


def fail(db_path, task_id, error):
    """失败累加 attempts；第 3 次失败进入 dead。只对 running 生效，避免覆盖已结束任务。"""
    db = _connect(db_path)
    try:
        with db:
            db.execute(
                "UPDATE pipeline_tasks SET attempts = attempts + 1, "
                "status = CASE WHEN attempts + 1 >= 3 THEN 'dead' ELSE 'failed' END, "
                "last_error=?, finished_at=? WHERE id=? AND status='running'",
                (str(error)[:1000], storage.now(), task_id))
    finally:
        db.close()


def recover(db_path, before):
    """启动恢复：上一个进程遗留的 running 任务标记为 failed，避免面板永远显示运行中。"""
    db = _connect(db_path)
    try:
        with db:
            db.execute("UPDATE pipeline_tasks SET status='failed', last_error='进程重启，任务中断', "
                       "finished_at=? WHERE status='running' AND started_at < ?",
                       (storage.now(), before))
    finally:
        db.close()


def list_tasks(db, limit=50):
    rows = db.execute(
        "SELECT id,trace_id,stage,status,attempts,last_error,created_at,started_at,finished_at "
        "FROM pipeline_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def trace(db, trace_id):
    rows = db.execute(
        "SELECT id,trace_id,stage,status,attempts,last_error,created_at,started_at,finished_at,payload,result "
        "FROM pipeline_tasks WHERE trace_id=? ORDER BY created_at", (trace_id,)).fetchall()
    return [dict(r) for r in rows]
