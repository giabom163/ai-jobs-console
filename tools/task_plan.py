"""Validate finite job plans without dispatching requests or changing quota."""
import json
from datetime import datetime, timezone
from pathlib import Path
try:
    from .request_limits import request_count, DEFAULT_TASK_REQUESTS, DEFAULT_AUTHORIZATION_REQUESTS, MAX_ROUND_REQUESTS
except ImportError:
    from request_limits import request_count, DEFAULT_TASK_REQUESTS, DEFAULT_AUTHORIZATION_REQUESTS, MAX_ROUND_REQUESTS

SOURCE_CAPABILITIES = {
    'official:tencent': {
        'source_id': 'official:tencent',
        'business_kind': 'jobs',
        'network_enabled': True,
        'operations': {
            'list': {'method': 'GET', 'host': 'careers.tencent.com', 'path': '/tencentcareer/api/post/Query'},
            'detail': {'method': 'GET', 'host': 'careers.tencent.com', 'path': '/tencentcareer/api/post/ByPostId'},
        },
        'pagination_type': 'page_index_and_size',
        'cursor_fields': ['pageIndex', 'pageSize'],
        'date_filter_capability': False,
        'date_fields': ['LastUpdateTime'],
        'request_mainland_params': {'countryId': '1', 'area': 'cn', 'cityId': '1,2,3,9'},
        'city_whitelist_version': 'tencent_domestic_v1',
        'max_page_size': 15,
        'max_record_limit': 15,
        'identity_fields': ['PostId'],
        'response_status_field': 'Code',
    },
    'official:meituan': {
        'source_id': 'official:meituan',
        'business_kind': 'jobs',
        'network_enabled': True,
        'operations': {
            'list': {'method': 'POST', 'host': 'zhaopin.meituan.com', 'path': '/api/official/job/getJobList'},
            'detail': {'method': 'POST', 'host': 'zhaopin.meituan.com', 'path': '/api/official/job/getJobDetail'},
        },
        'pagination_type': 'page_no_and_size',
        'cursor_fields': ['page.pageNo', 'page.pageSize'],
        'date_filter_capability': False,
        'date_fields': ['refreshTime', 'firstPostTime'],
        'request_mainland_params': {'cityList': [{'code': '001001', 'name': '北京市'}]},
        'city_whitelist_version': 'meituan_mainland_001_v1',
        'max_page_size': 15,
        'max_record_limit': 15,
        'identity_fields': ['jobUnionId'],
        'response_status_field': 'status',
    },
    'official:deepseek': {
        'source_id': 'official:deepseek',
        'business_kind': 'jobs',
        'network_enabled': False,
        'operations': {
            'offline_import': {'method': 'FILE', 'host': 'local', 'path': 'static_import'},
        },
        'pagination_type': 'none',
        'cursor_fields': [],
        'date_filter_capability': False,
        'date_fields': [],
        'request_mainland_params': {},
        'city_whitelist_version': 'domestic_only',
        'max_page_size': 15,
        'max_record_limit': 15,
        'identity_fields': ['id'],
        'response_status_field': None,
    },
    'employment_events:local': {
        'source_id': 'employment_events:local',
        'business_kind': 'employment_events',
        'network_enabled': False,
        'operations': {
            'local_review': {'method': 'FILE', 'host': 'local', 'path': 'configs/employment_events.json'},
        },
        'pagination_type': 'none',
        'cursor_fields': [],
        'date_filter_capability': True,
        'date_fields': ['event_date', 'published_at'],
        'request_mainland_params': {'region': 'domestic'},
        'city_whitelist_version': 'domestic_only',
        'max_page_size': 50,
        'max_record_limit': 50,
        'identity_fields': ['id'],
        'response_status_field': None,
    },
}


def load_plan(path, scheduler):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(plan, dict):
        raise ValueError('plan must be a JSON object')
    state = scheduler.snapshot()
    if (type(plan.get('version')) is not int or plan['version'] != 1 or
            plan.get('round_id') != state['round_id'] or
            not isinstance(plan.get('plan_id'), str) or not plan['plan_id'].strip()):
        raise ValueError('plan version, ID or shared round mismatch')
    deadline = datetime.fromisoformat(plan.get('deadline', '').replace('Z', '+00:00'))
    if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
        raise ValueError('plan must have a future timezone-aware deadline')
    tasks = plan.get('tasks')
    if not isinstance(tasks, list) or not tasks or len(tasks) > 200:
        raise ValueError('plan must contain a finite nonempty task list')
    total = 0
    source_totals = {}
    source_caps = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError('each task must be a JSON object')
        source = task.get('source_id')
        if source not in ('official:tencent', 'official:meituan') or task.get('operation') != 'list':
            raise ValueError('plan operation has no approved network adapter')
        if task.get('mode', 'discover') not in ('discover', 'refresh'):
            raise ValueError('unsupported scan mode')
        if not isinstance(task.get('keywords'), str) or not task['keywords'].strip():
            raise ValueError('explicit keywords required')
        for key in ('max_requests', 'authorization_limit', 'source_limit', 'records_limit'):
            request_count(task.get(key), key)
        if task['records_limit'] > 15:
            raise ValueError('records_limit remains bounded to 15; start with 3')
        if task['authorization_limit'] > DEFAULT_AUTHORIZATION_REQUESTS:
            raise ValueError(f'authorization_limit cannot exceed {DEFAULT_AUTHORIZATION_REQUESTS}')
        if task['max_requests'] > min(task['authorization_limit'], task['source_limit']):
            raise ValueError('task request allowance exceeds its explicit allocation')
        total += task['max_requests']
        source_totals[source] = source_totals.get(source, 0) + task['max_requests']
        source_caps[source] = min(source_caps.get(source, task['source_limit']), task['source_limit'])
    for source, allocated in source_totals.items():
        if allocated > source_caps[source]:
            raise ValueError(f'combined allocation for {source} exceeds its source cap')
    if total > state['remaining']:
        raise ValueError('plan allocation exceeds the shared remaining budget')
    if state['leases']:
        raise ValueError('existing request leases require investigation')
    return plan


def claim_plan(plan, path):
    """One-shot claim before dispatch. Crashes do not silently reauthorize the plan."""
    try:
        from .request_policy import _journal_lock
        from .request_scheduler import SharedScheduler
    except ImportError:
        from request_policy import _journal_lock
        from request_scheduler import SharedScheduler
    path = Path(path)
    with _journal_lock(path):
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if plan['plan_id'] in state:
            raise ValueError('plan already claimed; inspect its result before creating a new authorized plan')
        state[plan['plan_id']] = {'claimed_at': datetime.now(timezone.utc).isoformat(), 'round_id': plan['round_id']}
        SharedScheduler(path, _journal_lock)._write(state)
