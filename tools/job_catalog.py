"""Filtering and counts shared by read-only API routes."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

if __package__:
    from .job_taxonomy import ROLES, EMPLOYMENT, CITIES
else:
    from job_taxonomy import ROLES, EMPLOYMENT, CITIES

TZ = timezone(timedelta(hours=8))
FLOOR = '2026-06-01'


def normalized_filters(query):
    def get(key):
        value = query.get(key, [''])
        return (value[0] if isinstance(value, list) else value).strip()
    values = {key: get(key) for key in ('q', 'company', 'ai', 'level', 'status', 'role', 'employment', 'cities', 'city_text', 'education_text', 'experience_text', 'since', 'until')}
    values['status'] = values['status'] or 'listed'
    for key, allowed in [('role', ROLES), ('employment', EMPLOYMENT), ('ai', {'ai', 'build', 'adjacent'}), ('status', {'listed', 'open', 'review'})]:
        if values[key] and values[key] not in allowed:
            raise ValueError('unsupported ' + key)
    codes = list(dict.fromkeys(filter(None, values['cities'].split(','))))
    if any(code not in CITIES for code in codes):
        raise ValueError('unsupported city')
    values['cities'] = codes
    since = values['since']
    values['since'] = FLOOR if since == 'all' else max(FLOOR, since or (datetime.now(TZ) - timedelta(days=30)).date().isoformat())
    for key in ('since', 'until'):
        if values[key]:
            if datetime.strptime(values[key], '%Y-%m-%d').strftime('%Y-%m-%d') != values[key]:
                raise ValueError('invalid date')
    if values['until'] and values['until'] < values['since']:
        raise ValueError('invalid date bounds')
    return values


def select_jobs(jobs, filters):
    result = []
    for job in jobs:
        if filters['status'] == 'listed' and job.get('status') != 'open':
            continue
        if filters['status'] != 'listed' and job.get('status') != filters['status']:
            continue
        first = datetime.fromisoformat(job['first_seen'].replace('Z', '+00:00')).astimezone(TZ).date().isoformat()
        if first < filters['since'] or (filters['until'] and first > filters['until']):
            continue
        if filters['q'] and filters['q'].lower() not in job.get('search_text', '').lower():
            continue
        if any(filters[k] and job.get(field) != filters[k] for k, field in [('company', 'company'), ('level', 'authenticity_level'), ('role', 'role_family'), ('employment', 'employment_type')]):
            continue
        if filters['ai'] in ('build', 'adjacent') and job.get('ai_relevance') != filters['ai']:
            continue
        if filters['cities'] and not set(filters['cities']).intersection(job.get('city_codes', [])):
            continue
        if filters['city_text'] and filters['city_text'].casefold() not in (job.get('location') or '').casefold():
            continue
        details = job.get('job_details') or {}
        if filters['education_text'] and filters['education_text'].casefold() not in str((details.get('education') or {}).get('value') or '').casefold():
            continue
        if filters['experience_text'] and filters['experience_text'].casefold() not in str((details.get('experience') or {}).get('value') or '').casefold():
            continue
        result.append(job)
    return sorted(result, key=lambda j: (datetime.fromisoformat(j['first_seen'].replace('Z', '+00:00')).timestamp(), j['id']), reverse=True)


def summarize_jobs(jobs, base, filters, review_jobs=None):
    def groups(field):
        return [{'label': k, 'value': v} for k, v in sorted(Counter(j.get(field) or '未知' for j in jobs).items(), key=lambda x: (-x[1], x[0]))]
    now = datetime.now(TZ)
    counts = Counter(j.get('employment_type', 'unknown') for j in jobs)
    day = now.date().isoformat()
    week = (now - timedelta(days=6)).date().isoformat()
    trend = {}
    for j in jobs:
        date = datetime.fromisoformat(j['first_seen'].replace('Z', '+00:00')).astimezone(TZ).date().isoformat()
        if date >= (now - timedelta(days=13)).date().isoformat():
            trend[date] = trend.get(date, 0) + 1
    return dict(base, ai_total=len(jobs), total=len(jobs), open=sum(j.get('status') == 'open' for j in jobs),
                review_count=len(review_jobs) if review_jobs is not None else sum(j.get('status') == 'review' for j in jobs),
                build=sum(j['ai_relevance'] == 'build' for j in jobs), adjacent=sum(j['ai_relevance'] == 'adjacent' for j in jobs),
                verified=sum(j.get('http_status') is not None and 200 <= j.get('http_status') < 300 for j in jobs),
                liveness_checked=sum(bool(j.get('liveness')) for j in jobs),
                companies=len({j['company_id'] for j in jobs}), employment_counts=dict(counts),
                intern=counts['intern'], regular=counts['regular'], unknown=counts['unknown'],
                other_employment=counts['contract'] + counts['part_time'],
                today_new=sum(j['first_seen'] and datetime.fromisoformat(j['first_seen'].replace('Z', '+00:00')).astimezone(TZ).date().isoformat() == day for j in jobs),
                week_new=sum(datetime.fromisoformat(j['first_seen'].replace('Z', '+00:00')).astimezone(TZ).date().isoformat() >= week for j in jobs),
                levels=groups('authenticity_level'), top_companies=groups('company')[:8], roles=groups('role_label'),
                window_since=filters['since'], date_basis='first_seen',
                trend=[{'day': d, 'new_jobs': n, 'closed_jobs': 0} for d, n in sorted(trend.items())])
