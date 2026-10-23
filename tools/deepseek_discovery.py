#!/usr/bin/env python
"""Bounded direct DeepSeek Search/Fetch discovery. No model calls or automatic approval.

Protocol reference: https://api-docs.deepseek.com/guides/search/
Use --preflight --limit 3 first. Credentials come only from DEEPSEEK_API_KEY.
Default: collect evidence files only; --write-db explicitly saves provisional candidates.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__:
    from .employment_signals import parse_time
else:
    from employment_signals import parse_time

ROOT = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=8))
API = 'https://api.deepseek.com/v1/tools/'
STATE_DIR = Path.home() / '.codex' / 'documents' / 'deepseek-discovery-runs'
PAID_FROM = datetime(2026, 11, 1, tzinfo=TZ)
AI = re.compile(r'大模型|人工智能|机器学习|深度学习|多模态|智能体|模型训练|模型推理|生成式|(?<![a-z])(?:ai|llm|ml|nlp|aigc|rag)(?![a-z])', re.I)
EVENT = re.compile(r'裁员|裁撤|缩编|缩招|停招|冻结招聘|停止招聘|招聘|招募|扩招|(?:layoffs?|hiring\s+freeze|headcount\s+reduction|hiring|recruiting)', re.I)
NEGATIVE = re.compile(r'否认|辟谣|不实|并未|未发生|denies?|no layoffs', re.I)
SPECULATION = re.compile(r'传闻|网传|据传|rumou?r|预测|预计|可能|或将|predict|may\s+(?:cut|replace)', re.I)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def public_url(value):
    u = urllib.parse.urlsplit(str(value))
    if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password:
        raise ValueError('expected a public HTTP(S) URL without embedded credentials')
    host = u.hostname.lower()
    if host == 'localhost' or host.endswith(('.local', '.localhost', '.internal')):
        raise ValueError('local targets are not accepted')
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not addr.is_global:
            raise ValueError('non-public IP targets are not accepted')
    if u.port not in {None, 80, 443}:
        raise ValueError('nonstandard target ports are not accepted')
    return urllib.parse.urlunsplit((u.scheme, u.netloc.lower(), u.path or '/', u.query, ''))


def candidate(item, query, *, fetched=False):
    """Require AI and employment in one sentence; all matches remain provisional."""
    url = public_url(item.get('url', ''))
    title = str(item.get('title') or '').strip()
    content = str(item.get('content') or '').strip()
    summary = str(item.get('summary') or item.get('snippet') or '').strip()
    field = 'content' if content else 'search_summary'
    text = content or summary
    # A search summary is discovery evidence, never a substitute for source verification.
    evidence_text = text
    found = None
    for match in re.finditer(r'[^。！？\n.!?]+[。！？.!?]?', evidence_text):
        sentence = match.group()
        a, e = AI.search(sentence), EVENT.search(sentence)
        if a and e:
            found = (match, a, e)
            break
    if found is None:
        return None, 'no_joint_ai_employment_evidence'
    sentence, ai, employment = found
    token = employment.group().lower()
    category = ('layoff' if re.search(r'裁员|裁撤|layoff', token) else
                'shrink' if re.search(r'缩编|缩招|reduction', token) else
                'freeze' if re.search(r'停招|冻结招聘|停止招聘|freeze', token) else 'hiring')
    hint = 'denial_candidate' if NEGATIVE.search(sentence.group()) else (
        'speculation_candidate' if SPECULATION.search(sentence.group()) else 'unverified')
    stamp = parse_time(item.get('date'))
    evidence = {
        'source_field': field, 'rule_version': 'ai-employment-discovery-v1',
        'ai': {'text': ai.group(), 'start': sentence.start()+ai.start(), 'end': sentence.start()+ai.end()},
        'employment': {'text': sentence.group().strip(), 'start': sentence.start(), 'end': sentence.end()},
        'fetch_received': fetched, 'source_verified': False,
    }
    # Offsets refer to the exact stored text, including sentence whitespace.
    evidence['employment']['text'] = sentence.group()
    payload = {
        'id': 'discovery-' + hashlib.sha256(url.encode()).hexdigest()[:24],
        'url': url, 'title': title or urllib.parse.urlsplit(url).hostname,
        'source': urllib.parse.urlsplit(url).hostname, 'content': evidence_text,
        'content_sha256': hashlib.sha256(evidence_text.encode()).hexdigest(),
        'published_at': stamp.isoformat() if stamp else None,
        'date_basis': 'search_index' if stamp else 'unknown',
        'query': query, 'category': category, 'claim_status': hint,
        'ai_scope': 'pending', 'review_status': 'pending', 'liveness': 'unknown',
        'evidence': evidence,
    }
    return payload, None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DirectClient:
    def __init__(self, key, run_dir, *, max_requests=2, state_dir=STATE_DIR, allow_paid=False):
        self.key, self.run_dir = key, run_dir
        self.allow_paid = allow_paid
        self.max_requests, self.used = max_requests, 0
        self.calls = []
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state = sqlite3.connect(state_dir / 'request-state.sqlite3', timeout=10)
        self.state.execute('CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY, last_started REAL, cooldown REAL)')
        self.state.execute('INSERT OR IGNORE INTO budget VALUES (1,0,0)')
        self.state.execute('CREATE TABLE IF NOT EXISTS denied (url TEXT PRIMARY KEY, status INTEGER)')
        self.state.commit()
        self.opener = urllib.request.build_opener(NoRedirect())

    def reserve(self, target):
        if self.used >= self.max_requests:
            raise RuntimeError('request budget exhausted; no extra requests are dispatched')
        while True:
            self.state.execute('BEGIN IMMEDIATE')
            last, cooldown = self.state.execute('SELECT last_started,cooldown FROM budget WHERE id=1').fetchone()
            denied = self.state.execute('SELECT status FROM denied WHERE url=?', (target,)).fetchone() if target else None
            current = time.time()
            if denied or cooldown > current:
                self.state.rollback()
                if denied:
                    raise RuntimeError('source previously rejected (403); supported recovery is required')
                raise RuntimeError('429 cooldown active until ' + datetime.fromtimestamp(cooldown, TZ).isoformat())
            delay = max(0, last+20-current)
            if delay:
                self.state.rollback()
                if delay > 60:
                    raise RuntimeError('clock moved backwards; wait before the next request')
                time.sleep(delay)
                continue
            self.state.execute('UPDATE budget SET last_started=? WHERE id=1', (current,))
            self.state.commit()
            self.used += 1
            return

    def post(self, action, payload):
        if datetime.now(TZ) >= PAID_FROM and not self.allow_paid:
            raise RuntimeError('free period ended; no request dispatched without --allow-paid')
        target = payload.get('url') if action == 'fetch' else None
        self.reserve(target)
        call = {'action': action, 'endpoint': API+action, 'started_at': now(), 'input': payload,
                'input_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        self.calls.append(call)
        request = urllib.request.Request(API+action, data=json.dumps(payload).encode(), method='POST',
            headers={'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json',
                     'User-Agent': 'ai-employment-monitor/direct-discovery-v1'})
        try:
            try:
                with self.opener.open(request, timeout=35) as response:
                    status, body = response.status, response.read(8*1024*1024+1)
            except urllib.error.HTTPError as error:
                status, body = error.code, error.read(8*1024*1024+1)
            raw = body.decode('utf-8', errors='replace')
            # Preserve response content, except any accidental credential echo.
            safe = raw.replace(self.key, '[REDACTED]')
            call.update(http_status=status, finished_at=now(), credential_echo_redacted=safe != raw)
            path = self.run_dir / f'{self.used:02d}-{action}-response.txt'
            path.write_text(safe, encoding='utf-8')
            call['response_file'] = str(path)
            call['response_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            if status == 429:
                self.state.execute('UPDATE budget SET cooldown=? WHERE id=1', (time.time()+300,))
                self.state.commit()
            if status == 403 and target:
                self.state.execute('INSERT OR REPLACE INTO denied VALUES (?,?)', (target, status))
                self.state.commit()
            if status != 200:
                raise RuntimeError(f'HTTP {status}; body saved in {path}')
            if len(body) > 8*1024*1024:
                raise RuntimeError('response exceeds 8 MiB; stored response is truncated, not a valid result')
            data = json.loads(safe)
            if not isinstance(data, dict):
                raise ValueError('expected a JSON object')
            if action == 'fetch' and (not isinstance(data.get('content'), str) or not data['content'].strip()):
                raise ValueError('Fetch response has no usable content; not a successful fetch')
            return data
        except Exception as error:
            call.update(finished_at=now(), error=str(error).replace(self.key, '[REDACTED]'))
            raise

    def close(self):
        self.state.close()


def save_candidates(db_path, items):
    if not db_path.is_file():
        raise ValueError('--db must point to an existing project database')
    con = sqlite3.connect(db_path)
    try:
        con.executescript((ROOT / 'schema' / 'deepseek_discovery.sql').read_text(encoding='utf-8'))
        with con:
            for item in items:
                if hashlib.sha256(item['content'].encode()).hexdigest() != item['content_sha256']:
                    raise ValueError('candidate content hash mismatch')
                if item['id'] != 'discovery-' + hashlib.sha256(item['url'].encode()).hexdigest()[:24]:
                    raise ValueError('candidate ID mismatch')
                if public_url(item['url']) != item['url'] or item['category'] not in {'hiring','layoff','shrink','freeze'}:
                    raise ValueError('invalid candidate URL/category')
                for span in [item['evidence']['ai'], item['evidence']['employment']]:
                    start, end = span['start'], span['end']
                    if not isinstance(start,int) or not isinstance(end,int) or not 0 <= start < end <= len(item['content']) or item['content'][start:end] != span['text']:
                        raise ValueError('evidence span does not match the stored source')
                fields = ['id','url','title','source','published_at','date_basis','content','content_sha256','query','category','claim_status']
                stamp = now()
                columns = fields + ['first_seen','last_seen','evidence_json']
                values = [item[k] for k in fields] + [stamp, stamp, json.dumps(item['evidence'], ensure_ascii=False)]
                updates = ','.join(f'{k}=excluded.{k}' for k in fields if k not in {'id','url'})
                con.execute('INSERT INTO discovery_documents ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+') '
                    'ON CONFLICT(url) DO UPDATE SET '+updates+",last_seen=excluded.last_seen,evidence_json=excluded.evidence_json,"
                    "review_status='pending',ai_scope='pending',liveness='unknown',last_verified_at=NULL", values)
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--import-run', type=Path, help='Offline import of an existing run.json; requires --write-db and makes no request')
    parser.add_argument('--query', help='Query before automatic date operators; maximum final length 128 characters')
    parser.add_argument('--kind', choices=['hiring','changes'], default='hiring')
    parser.add_argument('--since', help='Beijing start date inclusive; default 30 days for hiring, 7 for changes')
    parser.add_argument('--until', help='Beijing end date, exclusive; default tomorrow')
    parser.add_argument('--limit', type=int, default=3, choices=range(1,4), help='At most 3 search results/candidates')
    parser.add_argument('--max-requests', type=int, choices=[1,2], default=2)
    parser.add_argument('--fetch-first', action='store_true', help='Fetch only the first result if its content is absent')
    parser.add_argument('--fetch-url', help='One explicit public source URL instead of a search')
    parser.add_argument('--write-db', action='store_true', help='Append provisional documents, never jobs or approved events')
    parser.add_argument('--db', type=Path, default=ROOT/'ai_jobs.db')
    parser.add_argument('--output-dir', type=Path, default=STATE_DIR)
    parser.add_argument('--allow-paid', action='store_true', help='Explicitly permit calls on/after 2026-11-01')
    parser.add_argument('--preflight', action='store_true', help='Print nonsecret configuration; no request, DB write or state file')
    args = parser.parse_args()
    today = datetime.now(TZ).date()
    since = args.since or (today-timedelta(days=6 if args.kind=='changes' else 29)).isoformat()
    until = args.until or (today+timedelta(days=1)).isoformat()
    lower, upper = parse_time(since), parse_time(until)
    if len(since) != 10 or len(until) != 10 or lower is None or upper is None or lower >= upper:
        parser.error('date bounds must be YYYY-MM-DD with since < until')
    if args.fetch_url and (args.query or args.fetch_first):
        parser.error('--fetch-url is a standalone fetch; do not combine with --query/--fetch-first')
    try:
        target = public_url(args.fetch_url) if args.fetch_url else None
    except ValueError as error:
        parser.error(str(error))
    base_query = args.query or ('AI 大模型 团队 招聘' if args.kind=='hiring' else 'AI 裁员')
    if re.search(r'\b(?:after|before):', base_query):
        parser.error('use --since/--until instead of date operators in --query')
    query = f'{base_query} after:{(lower.date()-timedelta(days=1)).isoformat()} before:{until}'
    if not target and (not base_query.strip() or len(query)>128):
        parser.error('final query must have 1 to 128 characters')
    if args.import_run:
        if not args.write_db or args.fetch_url or args.query or args.fetch_first or args.preflight:
            parser.error('--import-run requires --write-db and no search/fetch/preflight options')
        try:
            previous = json.loads(args.import_run.read_text(encoding='utf-8'))
            items = previous.get('accepted')
            if not isinstance(items, list) or len(items) > args.limit:
                raise ValueError('import must contain an accepted list within --limit')
            save_candidates(args.db, items)
            print(json.dumps({'status':'PASS', 'status_scope':'offline_candidate_import_only',
                'imported':len(items), 'network_requests':0, 'input':str(args.import_run),
                'input_sha256':hashlib.sha256(args.import_run.read_bytes()).hexdigest()}, ensure_ascii=False))
            return 0
        except Exception as error:
            print(json.dumps({'status':'FAIL', 'network_requests':0, 'error':str(error)}, ensure_ascii=False))
            return 1
    key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    if args.preflight:
        print(json.dumps({'status': 'READY' if key else 'BLOCKED', 'credential_present':bool(key),
            'endpoint':API, 'operation':'fetch' if target else 'search', 'query':None if target else query,
            'limit':args.limit, 'max_requests':args.max_requests, 'interval_seconds':20,
            'cooldown_seconds_429':300, 'write_db':args.write_db,
            'paid_date_reached':datetime.now(TZ)>=PAID_FROM, 'network_requests':0}, ensure_ascii=False))
        return 0 if key else 2
    if not key:
        print('BLOCKED: DEEPSEEK_API_KEY is absent; no requests or database writes.', file=sys.stderr)
        return 2
    if datetime.now(TZ)>=PAID_FROM and not args.allow_paid:
        print('BLOCKED: configured free period ended; --allow-paid is required.', file=sys.stderr)
        return 2
    run_dir = args.output_dir / (datetime.now(TZ).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {'run_id':run_dir.name, 'status':'BLOCKED', 'started_at':now(), 'network_requests':0,
              'accepted':[], 'rejected':[], 'errors':[], 'db_written':False,
              'model':None, 'provider':'deepseek-direct-search-fetch', 'model_requests':0}
    client = DirectClient(key, run_dir, max_requests=args.max_requests, allow_paid=args.allow_paid)
    try:
        if target:
            page = client.post('fetch', {'url':target})
            items = [{'url':target, 'title':page.get('title'), 'content':page.get('content')}]
        else:
            data = client.post('search', {'query':query, 'count':args.limit, 'need_content':True})
            items = data.get('results')
            if not isinstance(items, list):
                raise ValueError('Search response is missing a results array')
            items = items[:args.limit]
            if args.fetch_first and items and not items[0].get('content'):
                url = public_url(items[0].get('url',''))
                try:
                    page = client.post('fetch', {'url':url})
                    items[0] = dict(items[0], content=page['content'], _fetched=True)
                except Exception as error:
                    report['errors'].append(str(error).replace(key, '[REDACTED]'))
        seen = set()
        for item in items:
            try:
                if not isinstance(item, dict):
                    raise ValueError('result item is not an object')
                entry, rejection = candidate(item, query if not target else 'explicit-fetch', fetched=bool(target or item.get('_fetched')))
                if entry and entry['url'] not in seen:
                    stamp = parse_time(entry['published_at'])
                    if not target and stamp and not lower <= stamp < upper:
                        report['rejected'].append({'url':entry['url'], 'reason':'outside_index_date_window'})
                        continue
                    seen.add(entry['url'])
                    report['accepted'].append(entry)
                elif rejection:
                    report['rejected'].append({'url':item.get('url'), 'reason':rejection})
            except ValueError as error:
                report['rejected'].append({'reason':str(error)})
        if args.write_db and report['accepted']:
            save_candidates(args.db, report['accepted'])
            report['db_written'] = True
        report['status'] = 'FAIL' if report['errors'] else 'PASS'  # Execution only, never fact approval.
        report['status_scope'] = 'bounded_discovery_execution_only'
        return_code = 1 if report['errors'] else 0
    except Exception as error:
        report['errors'].append(str(error).replace(key, '[REDACTED]'))
        report['status'] = 'FAIL'
        return_code = 1
    finally:
        report.update(finished_at=now(), network_requests=client.used, calls=client.calls)
        path = run_dir/'run.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        client.close()
    print(json.dumps({'status':report['status'], 'accepted':len(report['accepted']),
        'network_requests':report['network_requests'], 'db_written':report['db_written'],
        'errors':report['errors'], 'evidence':str(path)}, ensure_ascii=False))
    return return_code


if __name__ == '__main__':
    raise SystemExit(main())
