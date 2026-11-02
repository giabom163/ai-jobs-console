"""Shared ingestion invariants and transaction-bound source-row batch accounting."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from .ai_lexicon import classify
from .dedup_candidates import canonical_title
from .job_states import STATUSES


def standard_fields(title, description, department=''):
    tier, confidence, reason = classify(title, description, department)
    return dict(normalized_title=canonical_title(title), ai_relevance=tier,
                ai_confidence=confidence, ai_reason=reason)


def validate_record(record):
    for field in ('source', 'external_id', 'title', 'location', 'first_seen', 'last_seen', 'url'):
        if not str(record.get(field) or '').strip():
            raise ValueError('missing required field: ' + field)
    if str(record['external_id']).lower() in ('none', 'null'):
        raise ValueError('invalid source identity')
    for field in ('first_seen', 'last_seen'):
        value = datetime.fromisoformat(record[field].replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError(field + ' requires timezone')
    if datetime.fromisoformat(record['last_seen'].replace('Z', '+00:00')) < datetime.fromisoformat(record['first_seen'].replace('Z', '+00:00')):
        raise ValueError('last_seen precedes first_seen')
    url = urlsplit(record['url'])
    if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
        raise ValueError('invalid public source URL')
    return record


def _rows(con):
    cursor = con.execute('SELECT * FROM jobs ORDER BY id')
    columns = [c[0] for c in cursor.description]
    return {row[0]: dict(zip(columns, row)) for row in cursor}


def _failure(batch, error):
    root = Path(__file__).resolve().parent.parent / 'reports' / 'batch-failures'
    root.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in batch.items() if k not in {'before'}}
    # Preserve actionable diagnostics while dropping credentials and URL queries.
    message = re.sub(r'https?://[^\s\"<>]+', lambda m: m.group().split('?')[0].split('#')[0].rsplit('@',1)[-1], str(error))
    message = re.sub(r'(?i)(authorization|cookie|api[_-]?key|token|password)[=: ]+[^\s,;]+', r'\1=[redacted]', message)
    message = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[redacted]', message)
    payload.update(outcome='rolled_back', error_summary=message[:500], failure_stage=batch.get('stage','business'), error_type=type(error).__name__,
                   failed_at=datetime.now(timezone.utc).isoformat())
    (root / (batch['batch_id'] + '.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def record_external_failure(origin,error,request_events):
    batch=dict(batch_id='batch-'+uuid4().hex,origin=origin,stage='source_collection',
               request_events=list(request_events),started_at=datetime.now(timezone.utc).isoformat())
    _failure(batch,error)
    return batch['batch_id']


def abort_batch(con, batch, error):
    con.rollback()
    if batch:
        _failure(batch, error)


def _initialize(con):
    # DDL is a separate, backed-up migration transaction, never an executescript
    # implicit commit inside a business batch.
    schema = Path(__file__).resolve().parent.parent / 'schema' / 'ai_employment.sql'
    statement = ''
    for line in schema.read_text(encoding='utf-8').splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ''
    if statement.strip() and any(not x.strip().startswith('--') for x in statement.splitlines() if x.strip()):
        raise ValueError('incomplete schema statement')
    from .job_fields import ensure_columns
    ensure_columns(con)
    have = {row[1] for row in con.execute('PRAGMA table_info(jobs)')}
    for name in ('scored_at', 'presence_status', 'presence_checked_at', 'source_response_sha256'):
        if name not in have:
            con.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' TEXT')
    con.execute("""CREATE TABLE IF NOT EXISTS ingestion_batches (
        batch_id TEXT PRIMARY KEY, origin TEXT NOT NULL, started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL, input_sha256 TEXT, accounting_json TEXT NOT NULL)""")
    con.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
    con.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?,?)', ('fix12-contract-v1', datetime.now(timezone.utc).isoformat()))
    document_schema=Path(__file__).resolve().parent.parent/'schema'/'deepseek_discovery.sql'
    statement=''
    for line in document_schema.read_text(encoding='utf8').splitlines(True):
        statement+=line
        if sqlite3.complete_statement(statement):
            con.execute(statement);statement=''
    if statement.strip():raise ValueError('incomplete discovery schema')
    con.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?,?)',('discovery-storage-v1',datetime.now(timezone.utc).isoformat()))
    con.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?,?)',('source-run-ledger-v1',datetime.now(timezone.utc).isoformat()))


def begin_batch(con, origin, input_sha256=None, existing_backup=None):
    if con.in_transaction:
        raise ValueError('begin batch before writing source records')
    batch = dict(batch_id='batch-' + uuid4().hex, origin=origin,
                 started_at=datetime.now(timezone.utc).isoformat(), input_sha256=input_sha256, stage='migration')
    try:
        main_path = next(row[2] for row in con.execute('PRAGMA database_list') if row[1] == 'main')
        backup_path = Path(existing_backup) if existing_backup else None
        if existing_backup and not backup_path.is_file():
            raise ValueError('existing backup must exist')
        if main_path and not existing_backup:
            database = Path(main_path)
            backup_path = database.parent / ('_backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8] + '-ingestion.db')
            target = sqlite3.connect(backup_path)
            try:
                con.backup(target)
            finally:
                target.close()
        batch['backup_path'] = str(backup_path) if backup_path else None
        if backup_path:
            batch['backup_sha256'] = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN IMMEDIATE')
        _initialize(con)
        con.commit()
        con.execute('BEGIN IMMEDIATE')
        batch['before'] = _rows(con)
        batch['stage'] = 'business'
        return batch
    except BaseException as error:
        con.rollback()
        _failure(batch, error)
        raise


def finish_batch(con, batch):
    after, before = _rows(con), batch['before']
    inserted = sorted(after.keys() - before.keys())
    updated = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    deleted = sorted(before.keys() - after.keys())
    if deleted:
        raise ValueError('ingestion cannot delete existing jobs')
    if any(row['status'] not in STATUSES for row in after.values()):
        raise ValueError('unknown lifecycle state')
    for key in before.keys() & after.keys():
        if before[key]['first_seen'] != after[key]['first_seen']:
            raise ValueError('ingestion cannot rewrite first_seen')
        if datetime.fromisoformat(after[key]['last_seen'].replace('Z','+00:00')) < datetime.fromisoformat(before[key]['last_seen'].replace('Z','+00:00')):
            raise ValueError('observation time cannot move backwards')
    facts = dict(document_ids=batch.get('document_ids',[]), requests=max((event.get('request_number',0) for event in batch.get('request_events',[])),default=0), request_events=batch.get('request_events', []), source_failures=batch.get('source_failures', []), backup_path=batch['backup_path'], baseline_total=len(before), final_total=len(after),
                 inserted_ids=inserted, updated_ids=updated, deleted_ids=deleted,
                 new_source_records=len(inserted),
                 new_ai_candidates=sum(after[k]['ai_relevance'] in ('build', 'adjacent') and
                                       after[k]['status'] != 'merged' for k in inserted),
                 confirmed_independent_new=None,
                 independent_new_note='Cross-source dedup review required; candidates are not confirmed independent jobs',
                 baseline_sha256=hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest())
    con.execute('INSERT INTO ingestion_batches VALUES(?,?,?,?,?,?)',
                (batch['batch_id'], batch['origin'], batch['started_at'],
                 datetime.now(timezone.utc).isoformat(), batch['input_sha256'],
                 json.dumps(facts, ensure_ascii=False)))
    return dict(batch_id=batch['batch_id'], **facts)
