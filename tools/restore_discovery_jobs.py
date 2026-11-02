"""Bounded offline import of source-backed discoveries as review records only.

Start with --limit 3 (preview). --apply backs up the DB before inserting.
No source requests, model calls, liveness promotion or physical deletion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .ingest_job_records import normalize, source_body
else:
    from ingest_job_records import normalize, source_body

from tools.ingestion_contract import begin_batch, finish_batch, abort_batch

ROOT = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=8))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def interleave(rows):
    groups = defaultdict(deque)
    for row in rows:
        groups[urlsplit(row.get('url', '')).hostname].append(row)
    while groups:
        for host in list(groups):
            yield groups[host].popleft()
            if not groups[host]:
                del groups[host]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', type=Path, required=True)
    ap.add_argument('--db', type=Path, default=ROOT / 'ai_jobs.db')
    ap.add_argument('--limit', type=int, default=3)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--json-out', type=Path, required=True)
    args = ap.parse_args()
    if not 1 <= args.limit <= 50:
        ap.error('limit must be 1..50; preview --limit 3 first')
    rows = json.loads(args.input.read_text(encoding='utf-8'))
    result = {'mode': 'apply_review' if args.apply else 'preview', 'limit': args.limit,
              'input_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest(),
              'accepted': [], 'existing': [], 'rejected': [], 'duplicates': [],
              'requests': 0, 'physically_deleted': 0}
    con = sqlite3.connect('file:' + args.db.resolve().as_posix() + '?mode=ro', uri=True)
    seen = set()
    today = datetime.now(TZ).date().isoformat()
    try:
        for row in interleave(rows):
            if len(result['accepted']) >= args.limit:
                break
            try:
                record = normalize(row)
                body = source_body(row)
                if not body or hashlib.sha256(body.encode()).hexdigest() != row.get('body_sha256'):
                    raise ValueError('raw source body hash differs from discovery evidence')
                if record['title'] not in body:
                    raise ValueError('complete job title not found in retained source body')
                if record.get('application_deadline') and record['application_deadline'] < today:
                    raise ValueError('advertised application deadline has passed')
                key = (record['source'], record['external_id'])
                if key in seen:
                    result['duplicates'].append({'url': row['url'], 'reason': 'same_source_id'})
                    continue
                seen.add(key)
                existing = con.execute('SELECT id,title,company_id FROM jobs WHERE source=? AND external_id=?', key).fetchone()
                if existing:
                    result['existing'].append({'id': existing[0], 'url': row['url']})
                    continue
                # Keep the source's duties and requirements, not just three AI hits.
                start = re.search(r'职位描述|岗位职责|工作职责|职位介绍|职责描述', body)
                description = body[start.start():] if start else body
                footer = re.search(r'公司简介|猎聘温馨提示|相关推荐|相似职位|公司信息|^(?:职位信息|申请职位|使用百度前必读)', description, re.M)
                if footer:
                    description = description[:footer.start()]
                record['description'] = description[:20000]
                record['raw_file'] = row['raw_file']
                record['body_sha256'] = row['body_sha256']
                result['accepted'].append(record)
            except (ValueError, TypeError, KeyError, OSError) as error:
                result['rejected'].append({'url': row.get('url'), 'reason': str(error)})
    finally:
        con.close()
    if args.apply and result['accepted']:
        backup = ROOT / ('_backup-' + datetime.now(TZ).strftime('%Y%m%d-%H%M%S') + '-multisource.db')
        if backup.exists():
            raise ValueError('backup already exists; refusing overwrite')
        result['before_database_sha256'] = hashlib.sha256(args.db.read_bytes()).hexdigest()
        con = sqlite3.connect(args.db)
        target = sqlite3.connect(backup)
        try:
            con.backup(target)
        finally:
            target.close()
        result['backup'] = str(backup)
        result['backup_sha256'] = hashlib.sha256(backup.read_bytes()).hexdigest()
        con.execute('PRAGMA foreign_keys=ON')
        batch = None
        try:
            with con:
                batch = begin_batch(con, 'restore_discovery', result['input_sha256'], existing_backup=result['backup'])
                for record in result['accepted']:
                    raw_file, body_hash = record['raw_file'], record['body_sha256']
                    company = con.execute('SELECT id FROM companies WHERE name=?', (record['company'],)).fetchone()
                    cid = company[0] if company else 'company-' + digest(record['company'])[:24]
                    owner = con.execute('SELECT name FROM companies WHERE id=?', (cid,)).fetchone()
                    if owner and owner[0] != record['company']:
                        raise ValueError('company ID collision')
                    con.execute('INSERT OR IGNORE INTO companies(id,name,ats_type,domain,created_at) VALUES(?,?,?,?,?)',
                                (cid, record['company'], 'official' if record['source'].startswith('official:') else 'platform',
                                 urlsplit(record['url']).hostname, record['first_seen']))
                    jid = 'job-' + digest(record['source'] + '|' + record['external_id'])[:24]
                    fields = {k: v for k, v in record.items() if k not in {'company', 'raw_file', 'body_sha256'}}
                    fields.update(id=jid, company_id=cid, status='review', liveness='uncertain',
                                  liveness_reason='公开来源快照，未确认当前仍在招',
                                  authenticity_level='待核验', content_hash=digest(record['title']+'\n'+record['location']+'\n'+record['description']))
                    con.execute('INSERT INTO jobs(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')', list(fields.values()))
                    con.execute('INSERT INTO job_evidence(job_id,source,url,seen_at) VALUES(?,?,?,?)',
                                (jid, record['source'], record['url'], record['first_seen']))
                    con.execute('INSERT INTO job_snapshots(job_id,seen_at,status,content_hash,location,raw_json) VALUES(?,?,?,?,?,?)',
                                (jid, record['first_seen'], 'review', fields['content_hash'], record['location'],
                                 json.dumps({'kind':'deepseek_discovery_snapshot','raw_file':raw_file,'body_sha256':body_hash}, ensure_ascii=False)))
                    con.execute('INSERT INTO job_changes(job_id,change_type,changed_at,new_hash,note) VALUES(?,?,?,?,?)',
                                (jid, 'new', record['first_seen'], fields['content_hash'], '离线恢复已采具体岗位，待复核，不等于当前在招'))
                    record['inserted_id'] = jid
                result['batch'] = finish_batch(con, batch)
        except BaseException as error:
            if batch:
                abort_batch(con, batch, error)
            raise
        finally:
            con.close()
        result['after_database_sha256'] = hashlib.sha256(args.db.read_bytes()).hexdigest()
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in result.items() if k not in {'input_sha256'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
