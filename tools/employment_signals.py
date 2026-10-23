"""Shared read-only projection for reviewed events and provisional discoveries."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
EVENT_TYPES = {'hiring', 'layoff', 'shrink', 'freeze'}
AI_SCOPES = {'ai_role', 'ai_team', 'ai_impact', 'ai_company_other'}
TAGS = {'hiring': '招聘线索', 'layoff': '裁员线索', 'shrink': '缩招线索', 'freeze': '停招线索'}


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
        result.append({
            'id': d['id'], 'title': d['title'], 'url': d['url'],
            'source': d['source'], 'published_at': d['published_at'],
            'date_basis': d['date_basis'], 'first_seen': d['first_seen'], 'last_seen': d['last_seen'],
            'summary': evidence.get('employment', {}).get('text', ''),
            'tag': TAGS[d['category']], 'category': d['category'],
            'review_status': d['review_status'], 'claim_status': d['claim_status'],
            'ai_scope': d['ai_scope'], 'liveness': d['liveness'],
            'last_verified_at': d['last_verified_at'], 'company_id': d['company_id'],
            'evidence': evidence,
        })
    return result
