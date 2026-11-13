"""Shared read-only projection for reviewed events and provisional discoveries."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
try:
    from .evidence_contract import is_reviewed_observation
except ImportError:
    from evidence_contract import is_reviewed_observation

TZ = timezone(timedelta(hours=8))
EVENT_TYPES = {'hiring', 'layoff', 'shrink', 'freeze', 'reorg', 'other'}
AI_SCOPES = {'ai_role', 'ai_team', 'ai_impact', 'ai_company_other'}
TAGS = {'hiring': '招聘线索', 'layoff': '裁员线索', 'shrink': '缩招线索', 'freeze': '停招线索', 'reorg': '组织调整', 'other': '其他就业线索'}


def storage_status(con):
    tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    legacy=con.execute('SELECT count(*) FROM signals').fetchone()[0] if 'signals' in tables else 0
    if 'discovery_documents' not in tables:
        return dict(storage_status='not_initialized',approved_count=0,pending_count=0,last_ingested_at=None,legacy_archived_count=legacy,note='就业线索存储尚未初始化；历史条目未自动转为已核实信息。')
    counts=dict(con.execute('SELECT review_status,count(*) FROM discovery_documents GROUP BY review_status'))
    last=con.execute('SELECT max(last_seen) FROM discovery_documents').fetchone()[0]
    return dict(storage_status='ready' if last else 'empty',approved_count=counts.get('approved',0),pending_count=counts.get('pending',0),last_ingested_at=last,legacy_archived_count=legacy,note='已复核内容与待核验线索分开；待核验原文只供本地复核。' if last else '存储已初始化，尚无新线索；不代表没有就业事件。')


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            # Calendar dates are interpreted as Beijing dates; bare clock times are unknown.
            if len(str(value)) != 10:
                return None
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def list_signals(con, *, review='approved', since=None, until=None, category=None, limit=50):
    """No schema creation, legacy keyword-only RSS entries, or implicit fact approval."""
    if review not in {'approved', 'pending'}:
        raise ValueError('unsupported review status')
    if category and category not in EVENT_TYPES:
        raise ValueError('unsupported event category')
    lower, upper = parse_time(since), parse_time(until)
    if since and lower is None or until and upper is None:
        raise ValueError('invalid date bound')
    if lower and upper and lower >= upper:
        raise ValueError('since must precede until (exclusive)')
    limit = max(1, min(200, int(limit)))
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_documents'").fetchone():
        return []
    sql = 'SELECT * FROM discovery_documents WHERE review_status=?'
    args = [review]
    if category:
        sql += ' AND category=?'
        args.append(category)
    if lower or upper:
        # Missing dates do not enter a date-filtered result.
        sql += ' AND published_at IS NOT NULL'
    if lower:
        sql += ' AND julianday(published_at)>=julianday(?)'
        args.append(lower.isoformat())
    if upper:
        sql += ' AND julianday(published_at)<julianday(?)'
        args.append(upper.isoformat())
    if review == 'approved':
        sql += " AND ai_scope IN ('ai_role','ai_team','ai_impact','ai_company_other') AND date_basis='source_verified'"
    sql += ' ORDER BY last_seen DESC, id LIMIT ?'
    args.append(limit)
    result = []
    for row in con.execute(sql, args):
        d = dict(row)
        if d['category'] not in EVENT_TYPES:
            continue
        evidence = json.loads(d['evidence_json'])
        if review=='approved' and not is_reviewed_observation(evidence,d['url'],d['published_at'],d['content_sha256']):
            continue
        public_evidence = dict(evidence)
        observation = public_evidence.get('source_observation')
        if isinstance(observation, dict):
            observation = dict(observation)
            observation.pop('raw_file_path', None)
            public_evidence['source_observation'] = observation
        result.append({
            'id': d['id'], 'title': d['title'], 'url': d['url'],
            'source': d['source'], 'region': 'domestic' if evidence.get('geography', {}).get('region') == 'CN-mainland' else None,
            'published_at': d['published_at'],
            'event_date': evidence.get('event_date'), 'date_basis': d['date_basis'], 'first_seen': d['first_seen'], 'last_seen': d['last_seen'],
            'summary': evidence.get('employment', {}).get('text', ''),
            'analysis_note': evidence.get('analysis_note') or ('作者自述内容仅按其原文呈现；AI 影响只按原文表述，不由单条自述推导因果。' if d['claim_status']=='self_reported' else ''),
            'ai_relation': evidence.get('ai_relation', 'unknown'),
            'ai_relation_evidence': evidence.get('ai_relation_evidence'),
            'tag': TAGS[d['category']], 'category': d['category'],
            'review_status': d['review_status'], 'claim_status': d['claim_status'],
            'ai_scope': d['ai_scope'], 'liveness': d['liveness'],
            'last_verified_at': d['last_verified_at'], 'company_id': d['company_id'],
            'evidence': public_evidence,
        })
    return result


def count_signals(con, *, review='approved', since=None, until=None, category=None):
    """Count eligible observations without applying the public list page limit."""
    if review not in {'approved', 'pending'}:
        raise ValueError('unsupported review status')
    if category and category not in EVENT_TYPES:
        raise ValueError('unsupported event category')
    lower, upper = parse_time(since), parse_time(until)
    if since and lower is None or until and upper is None:
        raise ValueError('invalid date bound')
    if lower and upper and lower >= upper:
        raise ValueError('since must precede until (exclusive)')
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_documents'").fetchone():
        return 0
    sql = 'SELECT category,evidence_json,url,published_at,content_sha256 FROM discovery_documents WHERE review_status=?'
    args = [review]
    if category:
        sql += ' AND category=?'
        args.append(category)
    if lower or upper:
        sql += ' AND published_at IS NOT NULL'
    if lower:
        sql += ' AND julianday(published_at)>=julianday(?)'
        args.append(lower.isoformat())
    if upper:
        sql += ' AND julianday(published_at)<julianday(?)'
        args.append(upper.isoformat())
    if review == 'approved':
        sql += " AND ai_scope IN ('ai_role','ai_team','ai_impact','ai_company_other') AND date_basis='source_verified'"
    total = 0
    for row in con.execute(sql, args):
        if row['category'] not in EVENT_TYPES:
            continue
        evidence = json.loads(row['evidence_json'])
        if review == 'approved' and not is_reviewed_observation(
                evidence, row['url'], row['published_at'], row['content_sha256']):
            continue
        total += 1
    return total
