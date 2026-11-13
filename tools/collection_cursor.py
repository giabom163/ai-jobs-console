"""Finite, resumable list scanning. Checkpoints advance only after ingestion commits."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
try:
    from .request_policy import _journal_lock
    from .request_scheduler import SharedScheduler
except ImportError:
    from request_policy import _journal_lock
    from request_scheduler import SharedScheduler


def prepare_page(path, source, keywords, scope, page_size, record_limit, fetcher, *, refresh=False):
    """Return at most record_limit records, retaining unprocessed list items locally.

    fetcher(keyword, page, page_size) must already use the approved RequestBudget.
    A cached observation retains its original timestamp and is not a new source check.
    """
    path = Path(path)
    raw = path.read_bytes() if path.exists() else None
    revision = hashlib.sha256(raw).hexdigest() if raw is not None else None
    state = json.loads(raw) if raw is not None else {'version': 1, 'queries': {}, 'rotation': {}}
    if state.get('version') != 1 or not isinstance(state.get('queries'), dict) or not isinstance(state.get('rotation'), dict):
        raise ValueError('invalid collection cursor; refusing restart from page one')
    group = hashlib.sha256(json.dumps([source, keywords, scope, page_size], sort_keys=True).encode()).hexdigest()
    rotation = state['rotation'].get(group, 0)
    selected = None
    for offset in range(len(keywords)):
        idx = (rotation + offset) % len(keywords)
        kw = keywords[idx]
        key = hashlib.sha256(json.dumps([source, kw, scope, page_size], sort_keys=True).encode()).hexdigest()
        row = state['queries'].get(key, {'page': 1, 'offset': 0, 'cached': [], 'exhausted': False})
        if refresh or not row.get('exhausted'):
            selected = (idx, kw, key, dict(row))
            break
    if selected is None:
        return [], None, {'status': 'no_change', 'reason': 'scan_exhausted; use an explicit refresh task'}
    idx, kw, key, row = selected
    if refresh:
        row = {'page': 1, 'offset': 0, 'cached': [], 'exhausted': False}
    cached = bool(row.get('cached'))
    if not cached:
        row['cached'] = fetcher(kw, row['page'], page_size)
        if not isinstance(row['cached'], list) or len(row['cached']) > page_size:
            raise ValueError('source list does not match the bounded page contract')
        row['observed_at'] = datetime.now(timezone.utc).isoformat()
        row['offset'] = 0
    start = row['offset']
    items = row['cached'][start:start + record_limit]
    row['offset'] += len(items)
    if row['offset'] >= len(row['cached']):
        row['exhausted'] = len(row['cached']) < page_size
        row.update(page=row['page'] + 1, offset=0, cached=[])
    row['source'] = source
    row['scope_hash'] = key
    row['keyword'] = kw
    row['updated_at'] = datetime.now(timezone.utc).isoformat()
    state['queries'][key] = row
    state['rotation'][group] = (idx + 1) % len(keywords)
    checkpoint = None if refresh else (path, revision, state)
    return [(item, row['observed_at']) for item in items], checkpoint, {
        'status': 'cached_observation' if cached else 'source_observation', 'keyword': kw,
        'next_page': row['page'], 'next_offset': row['offset'], 'exhausted': row['exhausted']}


def commit_checkpoint(checkpoint):
    if checkpoint is None:
        return
    path, expected, state = checkpoint
    with _journal_lock(path):
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if actual != expected:
            raise ValueError('cursor changed concurrently; database committed, rescan remains idempotent')
        SharedScheduler(path, _journal_lock)._write(state)
