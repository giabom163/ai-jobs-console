"""Durable, bounded source-run ledger for real collection attempts."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {'succeeded', 'partial', 'failed', 'preview', 'interrupted'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _connect(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError('source ledger database does not exist')
    con = sqlite3.connect('file:' + path.as_posix() + '?mode=rw', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def ensure_schema(db_path):
    """Apply only the source-run ledger schema with a recoverable pre-migration copy."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError('source ledger database does not exist')
    con = _connect(path)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        has_version = ('schema_migrations' in tables and con.execute(
            "SELECT 1 FROM schema_migrations WHERE version='source-run-ledger-v1' LIMIT 1").fetchone() is not None)
        if {'source_registry', 'crawler_runs'} <= tables and 'crawler_runs_source_started' in indexes and has_version:
            return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = path.with_name(f'_backup-{stamp}-{uuid4().hex[:8]}-source-runs.db')
        target = sqlite3.connect(backup)
        try:
            con.backup(target)
        finally:
            target.close()
        try:
            con.execute('BEGIN IMMEDIATE')
            con.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )''')
            con.execute('''CREATE TABLE IF NOT EXISTS source_registry (
                source_id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                enabled INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )''')
            con.execute('''CREATE TABLE IF NOT EXISTS crawler_runs (
                run_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES source_registry(source_id),
                run_kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running','succeeded','partial','failed','preview','interrupted')),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count >= 0),
                records_seen INTEGER NOT NULL DEFAULT 0 CHECK(records_seen >= 0),
                records_added INTEGER NOT NULL DEFAULT 0 CHECK(records_added >= 0),
                records_updated INTEGER NOT NULL DEFAULT 0 CHECK(records_updated >= 0),
                error_summary TEXT,
                details_json TEXT NOT NULL DEFAULT '{}'
            )''')
            con.execute('''CREATE INDEX IF NOT EXISTS crawler_runs_source_started
                ON crawler_runs(source_id, started_at DESC)''')
            con.execute('INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?,?)',
                        ('source-run-ledger-v1', now()))
            con.commit()
        except BaseException:
            con.rollback()
            raise
    finally:
        con.close()


def source_id(source_key):
    return 'source-' + hashlib.sha256(str(source_key).encode('utf-8')).hexdigest()[:20]


def start_run(db_path, source_key, name, source_type, *, enabled=True, run_kind='collection', details=None):
    if run_kind not in {'collection', 'verification', 'discovery'}:
        raise ValueError('unsupported source run kind')
    if not enabled:
        raise ValueError('disabled sources cannot start a collection run')
    ensure_schema(db_path)
    sid, stamp, rid = source_id(source_key), now(), 'run-' + uuid4().hex
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO source_registry(source_id,source_key,name,source_type,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET "
            "name=excluded.name,source_type=excluded.source_type,enabled=excluded.enabled,updated_at=excluded.updated_at",
            (sid, str(source_key), str(name), str(source_type), int(bool(enabled)), stamp, stamp))
        # Resolve the persisted ID so an earlier registry row cannot create a broken FK.
        sid = con.execute('SELECT source_id FROM source_registry WHERE source_key=?', (str(source_key),)).fetchone()[0]
        con.execute(
            "INSERT INTO crawler_runs(run_id,source_id,run_kind,status,started_at,details_json) VALUES(?,?,?,'running',?,?)",
            (rid, sid, run_kind, stamp, json.dumps(details or {}, ensure_ascii=False, sort_keys=True)))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    return rid


def _safe_error(error):
    text = re.sub(r'https?://[^\s"<>]+', lambda m: urlunsplit(urlsplit(m.group())._replace(query='', fragment='')), str(error))
    text = re.sub(r'(?i)(authorization|cookie|api[_-]?key|token|password)[=: ]+[^\s,;]+', r'\1=[redacted]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[redacted]', text)
    return text[:500]


def finish_run(db_path, run_id, status, *, request_count=0, records_seen=0,
               records_added=0, records_updated=0, error=None, details=None):
    if status not in TERMINAL:
        raise ValueError('invalid terminal source-run status')
    counts = (request_count, records_seen, records_added, records_updated)
    if any(not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError('source-run counters must be nonnegative integers')
    if status == 'succeeded' and request_count < 1:
        raise ValueError('a successful source run requires at least one dispatched request')
    con = _connect(db_path)
    try:
        cursor = con.execute(
            "UPDATE crawler_runs SET status=?,completed_at=?,request_count=?,records_seen=?,records_added=?,records_updated=?,error_summary=?,details_json=? "
            "WHERE run_id=? AND status='running'",
            (status, now(), *counts, _safe_error(error) if error else None,
             json.dumps(details or {}, ensure_ascii=False, sort_keys=True), run_id))
        if cursor.rowcount != 1:
            raise ValueError('source run missing or already finalized')
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def ats_source_key(company):
    return 'configured:' + str(company['type']) + ':' + str(company.get('token') or company['name'])
