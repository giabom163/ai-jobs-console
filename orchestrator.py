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
    try:
        row = db.execute("SELECT value FROM schema_meta WHERE key='orchestrator_version'").fetchone()
    except sqlite3.Error:
        row = None
    if row and row[0] == "1":
        return
    db.executescript("""
      CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS pipeline_runs(
        trace_id TEXT PRIMARY KEY, status TEXT NOT NULL, current_stage TEXT,
        stages TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, error TEXT);
      CREATE TABLE IF NOT EXISTS pipeline_state(
        trace_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
        result TEXT, error TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(trace_id, stage));
    """)
    with db:
        db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('orchestrator_version','1')")


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


def fail_run(db_path, trace_id, error):
    """兜底：阶段循环之外的异常也要把 run 收尾，避免永久 running。"""
    db = _connect(db_path)
    try:
        _finish_run(db, trace_id, "failed", error)
    finally:
        db.close()


def recover(db, before):
    """进程重启：run 与遗留的 running 阶段一起收尾。"""
    with db:
        db.execute("UPDATE pipeline_runs SET status='failed', error='进程重启，任务中断', "
                   "finished_at=? WHERE status='running' AND started_at < ?",
                   (storage.now(), before))
        db.execute("UPDATE pipeline_state SET status='failed', error='进程重启，任务中断', updated_at=? "
                   "WHERE status='running' AND trace_id IN "
                   "(SELECT trace_id FROM pipeline_runs WHERE status='failed')",
                   (storage.now(),))
