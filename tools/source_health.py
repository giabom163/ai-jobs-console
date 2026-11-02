"""Read-only configured/source coverage backed by recorded crawler runs."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
try:
    from .job_geography import is_domestic_only
except ImportError:
    from job_geography import is_domestic_only


def _source_id(key):
    return 'source-' + hashlib.sha256(str(key).encode('utf-8')).hexdigest()[:20]


def _state(latest, enabled):
    if enabled is False:
        return 'not_enabled'
    if not latest:
        return 'no_history'
    if latest['status'] == 'running':
        try:
            started = datetime.fromisoformat(latest['started_at'].replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - started).total_seconds() > 3600:
                return 'interrupted'
        except (ValueError, TypeError):
            return 'interrupted'
        return 'running'
    return latest['status']


def source_health(con):
    registry = {}
    con.create_function("is_domestic_only",1,is_domestic_only,deterministic=True)
    config = Path(__file__).resolve().parent.parent / 'configs' / 'ats_companies.json'
    for company in json.loads(config.read_text(encoding='utf-8'))['companies']:
        key = 'configured:' + str(company['type']) + ':' + str(company.get('token') or company['name'])
        registry[key] = dict(id=_source_id(key), source_key=key, name=company['name'],
                             kind='招聘来源', enabled=bool(company.get('enabled')),
                             state='no_history' if company.get('enabled') else 'not_enabled',
                             latest_attempt_at=None, last_success_at=None, last_status=None,
                             request_count=0, records_seen=0, records_added=0, records_updated=0,
                             error_summary=None)
    for row in con.execute('SELECT c.name,j.company_id,j.source,count(*) records FROM jobs j LEFT JOIN companies c ON c.id=j.company_id WHERE is_domestic_only(j.location)=1 GROUP BY j.company_id,j.source'):
        data = dict(row)
        if data['source'].startswith('ats:') and any(item['name'] == data['name'] for item in registry.values()):
            continue
        key = str(data['company_id']) + ':' + str(data['source'])
        registry[key] = dict(id=_source_id(key), source_key=key, name=data['name'] or '公司待确认',
                             kind='已收录来源', enabled=None, state='no_history',
                             latest_attempt_at=None, last_success_at=None, last_status=None,
                             request_count=0, records_seen=0, records_added=0, records_updated=0,
                             error_summary=None)

    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'source_registry' not in tables or 'crawler_runs' not in tables:
        return dict(storage_status='run_ledger_missing', items=sorted(registry.values(), key=lambda x: (x['name'], x['id'])),
                    note='来源运行账本尚未初始化；最近成功时间与成功率未知。岗位数量和链接可访问状态不代表采集成功。')

    persisted = {row['source_key']: dict(row) for row in con.execute('SELECT * FROM source_registry')}
    for key, stored in persisted.items():
        if key.startswith('configured:') and key not in registry:
            continue
        registry.setdefault(key, dict(id=stored['source_id'], source_key=key, name=stored['name'],
                                      kind='招聘来源', enabled=bool(stored['enabled']) if stored['enabled'] is not None else None,
                                      state='no_history', latest_attempt_at=None, last_success_at=None,
                                      last_status=None, request_count=0, records_seen=0,
                                      records_added=0, records_updated=0, error_summary=None))
    latest_runs = {}
    last_success = {}
    for row in con.execute(
            'SELECT r.*,s.source_key FROM crawler_runs r JOIN source_registry s ON s.source_id=r.source_id ORDER BY r.started_at DESC,r.run_id DESC'):
        run = dict(row)
        latest_runs.setdefault(run['source_key'], run)
        if run['status'] == 'succeeded' and run['completed_at']:
            last_success.setdefault(run['source_key'], run['completed_at'])

    for key, entry in registry.items():
        stored = persisted.get(key)
        if stored:
            entry['id'] = stored['source_id']
            if entry['enabled'] is None:
                entry['enabled'] = bool(stored['enabled']) if stored['enabled'] is not None else None
        latest = latest_runs.get(key)
        if latest:
            entry.update(state=_state(latest, entry['enabled']),
                         latest_attempt_at=latest['completed_at'] or latest['started_at'],
                         last_status=latest['status'], request_count=latest['request_count'],
                         records_seen=latest['records_seen'], records_added=latest['records_added'],
                         records_updated=latest['records_updated'],
                         error_summary=latest['error_summary'])
        entry['last_success_at'] = last_success.get(key)

    return dict(storage_status='ready', items=sorted(registry.values(), key=lambda x: (x['name'], x['id'])),
                note='运行状态来自持久采集账本；“最近成功”只统计明确成功的采集，缺少记录仍显示未知。')
