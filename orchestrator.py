"""分析流水线状态机：sentiment → topic → alert → report。

- pipeline_runs / pipeline_state 记录一次 run 的阶段与结果（checkpoint）；
- 每个阶段可重入：已 ok 的阶段在 resume 时跳过；
- 不引入 LangGraph/Redis，单进程顺序执行。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import storage

STAGES = ("sentiment", "topic", "alert", "report")


def init(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS pipeline_runs(
        trace_id TEXT PRIMARY KEY, status TEXT NOT NULL, current_stage TEXT,
        stages TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, error TEXT);
      CREATE TABLE IF NOT EXISTS pipeline_state(
        trace_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
        result TEXT, error TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(trace_id, stage));
    """)


def _connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    init(db)
    return db


def create_run(db, stages):
    trace_id = "trace-" + uuid.uuid4().hex
    stamp = storage.now()
    with db:
        db.execute("INSERT INTO pipeline_runs(trace_id,status,current_stage,stages,started_at) "
                   "VALUES(?,'running',NULL,?,?)",
                   (trace_id, json.dumps(list(stages), ensure_ascii=False), stamp))
    return trace_id


def list_runs(db, limit=10):
    runs = []
    for run in db.execute("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,)):
        item = dict(run)
        item["stages"] = [dict(r) for r in db.execute(
            "SELECT stage,status,result,error,updated_at FROM pipeline_state "
            "WHERE trace_id=? ORDER BY rowid", (run["trace_id"],))]
        runs.append(item)
    return runs


def state(db, trace_id):
    run = db.execute("SELECT * FROM pipeline_runs WHERE trace_id=?", (trace_id,)).fetchone()
    if not run:
        return None
    stages = [dict(r) for r in db.execute(
        "SELECT stage,status,result,error,updated_at FROM pipeline_state "
        "WHERE trace_id=? ORDER BY rowid", (trace_id,))]
    return {"run": dict(run), "stages": stages}


def _mark(db, trace_id, stage, status, result=None, error=None):
    stamp = storage.now()
    with db:
        db.execute("INSERT INTO pipeline_state(trace_id,stage,status,result,error,updated_at) "
                   "VALUES(?,?,?,?,?,?) ON CONFLICT(trace_id,stage) DO UPDATE SET "
                   "status=excluded.status, result=excluded.result, error=excluded.error, "
                   "updated_at=excluded.updated_at",
                   (trace_id, stage, status,
                    json.dumps(result, ensure_ascii=False)[:20000] if result is not None else None,
                    str(error)[:1000] if error else None, stamp))
        db.execute("UPDATE pipeline_runs SET current_stage=? WHERE trace_id=?", (stage, trace_id))


def _finish_run(db, trace_id, status, error=None):
    with db:
        db.execute("UPDATE pipeline_runs SET status=?, finished_at=?, error=? WHERE trace_id=?",
                   (status, storage.now(), str(error)[:1000] if error else None, trace_id))


def run(db_path, trace_id, handlers, stages):
    """顺序执行；已 ok 的阶段跳过；某阶段失败即停止，run 置 failed。"""
    db = _connect(db_path)
    try:
        done = {r["stage"] for r in db.execute(
            "SELECT stage FROM pipeline_state WHERE trace_id=? AND status='ok'", (trace_id,))}
        for stage in stages:
            if stage in done:
                continue
            handler = handlers.get(stage)
            if handler is None:
                _mark(db, trace_id, stage, "failed", error="no handler")
                _finish_run(db, trace_id, "failed", f"no handler for {stage}")
                return
            _mark(db, trace_id, stage, "running")
            try:
                result = handler()
            except Exception as exc:
                _mark(db, trace_id, stage, "failed", error=f"{type(exc).__name__}: {exc}")
                _finish_run(db, trace_id, "failed", exc)
                return
            _mark(db, trace_id, stage, "ok", result=result)
        _finish_run(db, trace_id, "ok")
    finally:
        db.close()


def recover(db, before):
    """进程重启：把上一个进程遗留的 running 置 interrupted。"""
    with db:
        db.execute("UPDATE pipeline_runs SET status='failed', error='进程重启，任务中断', "
                   "finished_at=? WHERE status='running' AND started_at < ?",
                   (storage.now(), before))
