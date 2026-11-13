#!/usr/bin/env python3
"""Fetch one approved public social post and preserve a reviewable receipt."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_ROOT = Path.home() / '.codex' / 'documents' / 'employment-signal-evidence'
EXTRACTOR = 'public-social-html-v2'


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript', 'svg'}:
            self.hidden += 1
        elif tag in {'p', 'div', 'br', 'li', 'article', 'h1', 'h2', 'h3'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript', 'svg'} and self.hidden:
            self.hidden -= 1
        elif tag in {'p', 'div', 'li', 'article'} and not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def source_for(host):
    mapping = {
        'www.v2ex.com': ('social:v2ex', 'v2ex'),
        'www.bilibili.com': ('social:bilibili', 'bilibili'),
        'www.douyin.com': ('social:douyin', 'douyin'),
        'www.youtube.com': ('social:youtube', 'youtube'),
    }
    try:
        return mapping[host]
    except KeyError as exc:
        raise ValueError('host is not an approved public social source') from exc


def fetch(url: str, *, state_path=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('use a public HTTPS URL without credentials')
    source_id, label = source_for(parsed.hostname.lower())
    sys.path.insert(0, str(ROOT))
    from tools.request_policy import RequestBudget
    budget = RequestBudget(limit=1, state_path=state_path)
    request = Request(url, headers={'User-Agent': 'ai-employment-monitor/public-evidence/1.0',
                                    'Accept': 'text/html,application/xhtml+xml',
                                    'Accept-Encoding': 'identity'})
    budget.authorize(request, source_id=source_id, operation='fetch')
    with budget.open(request, timeout=30, allow_redirects=True,
                     source_id=source_id, operation='fetch') as response:
        status = response.status
        raw = response.read(8 * 1024 * 1024 + 1)
        final_url = response.geturl()
        headers = dict(response.headers.items())
    if status != 200:
        raise RuntimeError(f'public source returned HTTP {status}; request stopped')
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeError('source page exceeded 8 MiB; evidence was not accepted')
    content_encoding = headers.get('Content-Encoding', '').strip().lower()
    if content_encoding == 'gzip':
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise RuntimeError('source gzip body could not be decoded; evidence was not accepted') from exc
    elif content_encoding not in ('', 'identity'):
        raise RuntimeError(f'unsupported source content encoding {content_encoding!r}; evidence was not accepted')
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeError('decoded source page exceeded 8 MiB; evidence was not accepted')
    encoding = 'utf-8'
    match = re.search(r'charset=([\w-]+)', headers.get('Content-Type', ''), re.I)
    if match:
        encoding = match.group(1)
    html = raw.decode(encoding, errors='replace')
    parser = VisibleText()
    parser.feed(html)
    text = re.sub(r'[ \t]+', ' ', ''.join(parser.parts))
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    if len(text) < 80 or text.count('\ufffd') > max(3, len(text) // 100):
        raise RuntimeError('page has no usable public text; no candidate receipt created')
    observed = datetime.now(timezone.utc).isoformat(timespec='seconds')
    key = hashlib.sha256((url + observed).encode()).hexdigest()[:20]
    out = EVIDENCE_ROOT / label / key
    out.mkdir(parents=True, exist_ok=False)
    raw_path = out / 'response.html'
    text_path = out / 'extracted.txt'
    raw_path.write_bytes(raw)
    text_path.write_text(text, encoding='utf-8')
    receipt = {
        'requested_url': url,
        'final_url': final_url,
        'observed_at': observed,
        'http_status': status,
        'raw_file_path': str(raw_path),
        'raw_file_sha256': hashlib.sha256(raw).hexdigest(),
        'extracted_text_path': str(text_path),
        'extracted_text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'extractor_version': EXTRACTOR,
        'source_strategy_id': 'social-self-report-v1',
        'source_id': source_id,
        'request_events': budget.events,
    }
    receipt_path = out / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'receipt': receipt, 'receipt_path': str(receipt_path), 'text_path': str(text_path), 'text': text}


def attach_to_candidate(db_path, doc_id, result):
    import sqlite3
    from tools.ingestion_contract import begin_batch, finish_batch, abort_batch
    db_path = Path(db_path).resolve()
    con = sqlite3.connect('file:' + db_path.as_posix() + '?mode=rw', uri=True)
    con.row_factory = sqlite3.Row
    batch = None
    try:
        row = con.execute("SELECT * FROM discovery_documents WHERE id=? AND review_status='pending'", (doc_id,)).fetchone()
        if row is None:
            raise ValueError('pending candidate not found')
        receipt = result['receipt']
        if row['url'] != receipt['requested_url']:
            raise ValueError('fetched URL does not match the pending candidate')
        evidence = json.loads(row['evidence_json'])
        evidence.update(source_verified=False, fetch_received=True,
                        source_observation={k: receipt[k] for k in (
                            'requested_url', 'final_url', 'observed_at', 'http_status',
                            'raw_file_path', 'raw_file_sha256', 'extracted_text_sha256',
                            'extractor_version', 'source_strategy_id')})
        digest = hashlib.sha256(result['text'].encode('utf-8')).hexdigest()
        batch = begin_batch(con, 'social_source_fetch', digest)
        batch['document_ids'] = [doc_id]
        con.execute('''UPDATE discovery_documents SET content=?,content_sha256=?,evidence_json=?,last_seen=?,liveness='uncertain'
                       WHERE id=? AND review_status='pending' ''',
                    (result['text'], digest, json.dumps(evidence, ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat(timespec='seconds'), doc_id))
        summary = finish_batch(con, batch)
        con.commit()
        return summary
    except BaseException as error:
        con.rollback()
        if batch:
            abort_batch(con, batch, error)
        raise
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--state-path')
    parser.add_argument('--document-id')
    parser.add_argument('--db', default=str(ROOT / 'ai_jobs.db'))
    args = parser.parse_args()
    result = fetch(args.url, state_path=args.state_path)
    if args.document_id:
        result['batch'] = attach_to_candidate(args.db, args.document_id, result)
    print(json.dumps({k: v for k, v in result.items() if k != 'text'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
