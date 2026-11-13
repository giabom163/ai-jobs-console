"""One bounded sequential request budget, including redirects. No automatic retry."""
import time
import json
import os
import re
from uuid import uuid4
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
if __package__:
    from .request_scheduler import SharedScheduler, SchedulingBlocked
    from .request_limits import MAX_ROUND_REQUESTS, DEFAULT_TASK_REQUESTS, DEFAULT_AUTHORIZATION_REQUESTS, MIN_HOST_INTERVAL, request_count
    from .job_geography import is_domestic_only
else:
    from request_scheduler import SharedScheduler, SchedulingBlocked
    from request_limits import MAX_ROUND_REQUESTS, DEFAULT_TASK_REQUESTS, DEFAULT_AUTHORIZATION_REQUESTS, MIN_HOST_INTERVAL, request_count
    from job_geography import is_domestic_only


# Only this ID-specific official detail endpoint is currently eligible.
# List endpoints whose Mainland China request scope is unproven remain absent.
APPROVED_ENDPOINTS = {
    ('social:v2ex', 'fetch'): ('GET', 'www.v2ex.com', 'regex:^/t/[0-9]+/?$'),
    ('social:bilibili', 'fetch'): ('GET', 'www.bilibili.com', 'regex:^/video/BV[0-9A-Za-z]+/?$'),
    ('social:douyin', 'fetch'): ('GET', 'www.douyin.com', 'regex:^/(?:video|shipin)/[0-9]+/?$'),
    ('social:youtube', 'fetch'): ('GET', 'www.youtube.com', '/watch'),
    ('official:meituan', 'detail'): ('POST', 'zhaopin.meituan.com', '/api/official/job/getJobDetail'),
    ('official:meituan', 'list'): ('POST', 'zhaopin.meituan.com', '/api/official/job/getJobList'),
    ('official:tencent', 'detail'): ('GET', 'careers.tencent.com', '/tencentcareer/api/post/ByPostId'),
    ('official:tencent', 'list'): ('GET', 'careers.tencent.com', '/tencentcareer/api/post/Query'),
    # Baidu detail pages are eligible only after the user-facing Ego list flow
    # has been filtered to Mainland locations before submitting the query.
    ('official:baidu', 'detail'): ('GET', 'talent.baidu.com', 'regex:^/jobs/detail/(INTERN|GRADUATE|SOCIAL)/[0-9a-fA-F-]{36}$'),
    ('official:moonshot', 'career_discovery'): ('GET', 'www.moonshot.cn', '/'),
    ('official:moonshot', 'careers_portal'): ('GET', 'careers.kimi.com', '/'),
    ('official:moonshot', 'list'): ('GET', 'app.mokahr.com', '/apply/moonshot/148506'),
    ('official:moonshot', 'detail'): ('GET', 'app.mokahr.com', '/apply/moonshot/148506'),
    ('official:stepfun', 'list'): ('GET', 'app.mokahr.com', '/social-recruitment/step/94904'),
    ('official:stepfun', 'detail'): ('GET', 'app.mokahr.com', '/social-recruitment/step/94904'),
    ('official:stepfun', 'campus_list'): ('GET', 'app.mokahr.com', '/campus-recruitment/step/94905'),
    ('official:stepfun', 'campus_detail'): ('GET', 'app.mokahr.com', '/campus-recruitment/step/94905'),
    ('official:zhipu', 'career_discovery'): ('GET', 'www.zhipuai.cn', '/'),
    ('official:minimax', 'list'): ('GET', 'vrfi1sk8a0.jobs.feishu.cn', '/379481/'),
    ('official:minimax', 'social_list'): ('GET', 'vrfi1sk8a0.jobs.feishu.cn', '/index/'),
    ('official:minimax', 'detail'): ('GET', 'vrfi1sk8a0.jobs.feishu.cn', 'regex:^/379481/position/[0-9]+/detail$'),
    ('official:baichuan', 'list'): ('GET', 'cq6qe6bvfr6.jobs.feishu.cn', '/baichuanzhaopin/'),
    ('official:baichuan', 'campus_list'): ('GET', 'cq6qe6bvfr6.jobs.feishu.cn', '/646926/'),
    ('official:baichuan', 'detail'): ('GET', 'cq6qe6bvfr6.jobs.feishu.cn', 'regex:^/baichuanzhaopin/position/[0-9]+/detail$'),
    ('official:modelbest', 'list'): ('GET', 'modelbest.jobs.feishu.cn', '/career/position/list'),
    ('official:modelbest', 'campus_list'): ('GET', 'modelbest.jobs.feishu.cn', '/campus/position/list'),
}

# Verified Tencent domestic city IDs (China countryId=1)
TENCENT_DOMESTIC_CITY_IDS = {
    '1',   # Shenzhen
    '2',   # Beijing
    '3',   # Shanghai
    '4',   # Guangzhou
    '5',   # Chengdu
    '9',   # Hangzhou
    '11',  # Wuhan
    '12',  # Xi'an
    '17',  # Nanjing
    '25',  # Xiamen
}


def _validate_endpoint(request, source_id, operation, location, external_id):
    url = request.full_url if hasattr(request, 'full_url') else str(request)
    parsed = urllib.parse.urlsplit(url)
    method = request.get_method() if hasattr(request, 'get_method') else 'GET'
    expected = APPROVED_ENDPOINTS.get((source_id, operation))
    if not expected:
        raise SourceBlocked('source operation is not approved for Mainland job requests')
    path_ok = (bool(re.fullmatch(expected[2][6:], parsed.path)) if expected[2].startswith('regex:')
               else parsed.path == expected[2])
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            parsed.hostname != expected[1] or parsed.port not in (None, 443) or
            not path_ok or method != expected[0]):
        raise SourceBlocked('request does not match the approved source endpoint')
    override_host = request.get_header('Host') if hasattr(request, 'get_header') else None
    if override_host and override_host.casefold() != expected[1]:
        raise SourceBlocked('custom Host header does not match the approved source')
    if source_id == 'official:meituan' and operation == 'detail':
        if not external_id or not is_domestic_only(location):
            raise SourceBlocked('official detail requires an existing Mainland-only job')
        try:
            payload = json.loads(request.data.decode('utf-8'))
        except (AttributeError, UnicodeDecodeError, ValueError, TypeError) as error:
            raise SourceBlocked('official detail request has no valid job identity') from error
        if not isinstance(payload, dict) or str(payload.get('jobUnionId') or '') != str(external_id):
            raise SourceBlocked('official detail request identity does not match the Mainland job')
    if source_id == 'official:moonshot' and operation == 'list':
        # The official Moonshot careers page links this exact tenant and the two AI hiring categories.
        fragment_query = parsed.fragment.split('?', 1)[1] if '?' in parsed.fragment else ''
        qs = urllib.parse.parse_qs(fragment_query)
        if qs.get('zhineng[0]') != ['190965'] or qs.get('zhineng[1]') != ['195114']:
            raise SourceBlocked('official:moonshot list requires the company-provided AI category filters')
        locations = [value for key, values in qs.items() if re.fullmatch(r'location\[\d+\]', key) for value in values]
        allowed_cities = {'北京市', '上海市', '深圳市', '成都市'}
        if not locations or any(value not in allowed_cities for value in locations):
            raise SourceBlocked('official:moonshot list requires explicit Mainland-only city filters')
    if source_id == 'official:moonshot' and operation == 'detail':
        match = re.fullmatch(r'/job/([0-9a-fA-F-]{36})', parsed.fragment)
        if not match or not external_id or match[1].casefold() != str(external_id).casefold():
            raise SourceBlocked('official:moonshot detail requires matching job UUID')
        if not is_domestic_only(location):
            raise SourceBlocked('official:moonshot detail requires a Mainland-only listing observation')
    if source_id == 'official:stepfun' and operation in {'list', 'campus_list'}:
        fragment_query = parsed.fragment.split('?', 1)[1] if '?' in parsed.fragment else ''
        qs = urllib.parse.parse_qs(fragment_query)
        locations = [value for key, values in qs.items() if re.fullmatch(r'location\[\d+\]', key) for value in values]
        allowed_cities = {'北京市', '上海市', '深圳市'}
        if not locations or any(value not in allowed_cities for value in locations):
            raise SourceBlocked('official:stepfun list requires explicit Mainland-only city filters')
    if source_id == 'official:stepfun' and operation in {'detail', 'campus_detail'}:
        match = re.fullmatch(r'/job/([0-9a-fA-F-]{36})', parsed.fragment)
        if not match or not external_id or match[1].casefold() != str(external_id).casefold():
            raise SourceBlocked('official:stepfun detail requires matching job UUID')
        if not is_domestic_only(location):
            raise SourceBlocked('official:stepfun detail requires a Mainland-only listing observation')
    if source_id == 'official:meituan' and operation == 'list':
        try:
            payload = json.loads(request.data.decode('utf-8'))
        except (AttributeError, UnicodeDecodeError, ValueError, TypeError) as error:
            raise SourceBlocked('official list request has no valid JSON payload') from error
        if not isinstance(payload, dict):
            raise SourceBlocked('official list payload must be a JSON object')
        cities = payload.get('cityList')
        if not cities or not isinstance(cities, list) or len(cities) == 0:
            raise SourceBlocked('official list request requires explicit Mainland-only cityList')
        for item in cities:
            if not isinstance(item, dict):
                raise SourceBlocked('invalid city item format')
            name = item.get('name') or ''
            code = str(item.get('code') or '')
            if not is_domestic_only(name) or not code.startswith('001'):
                raise SourceBlocked('city ' + name + ' (' + code + ') is not proven Mainland-only')
    if source_id == 'official:tencent' and operation == 'list':
        qs = urllib.parse.parse_qs(parsed.query)
        country_id = qs.get('countryId')
        if not country_id or country_id != ['1']:
            raise SourceBlocked('official:tencent list request requires explicit countryId=1 (Mainland China)')
        area = qs.get('area')
        if area != ['cn']:
            raise SourceBlocked('official:tencent list request area must be cn')
        city_id = qs.get('cityId')
        if not city_id or len(city_id) != 1 or not city_id[0]:
            raise SourceBlocked('official:tencent list request requires explicit Mainland cityId')
        if city_id and city_id[0]:
            for cid in city_id[0].split(','):
                cid_strip = cid.strip()
                if cid_strip and cid_strip not in TENCENT_DOMESTIC_CITY_IDS:
                    raise SourceBlocked('cityId ' + cid_strip + ' is not in proven Mainland city whitelist')
    if source_id == 'official:tencent' and operation == 'detail':
        if not external_id or not is_domestic_only(location):
            raise SourceBlocked('official:tencent detail requires an existing Mainland-only job')
        qs = urllib.parse.parse_qs(parsed.query)
        post_id = qs.get('postId')
        if not post_id or not post_id[0] or not post_id[0].isdigit():
            raise SourceBlocked('official:tencent detail requires valid numeric postId')
        if external_id and str(post_id[0]) != str(external_id):
            raise SourceBlocked('official:tencent detail postId does not match external_id')
    if source_id == 'official:baidu' and operation == 'detail':
        match = re.fullmatch(r'/jobs/detail/(?:INTERN|GRADUATE|SOCIAL)/([0-9a-fA-F-]{36})', parsed.path)
        if not match or not external_id or match[1].casefold() != str(external_id).casefold():
            raise SourceBlocked('official:baidu detail requires matching stable job identity')
        if not is_domestic_only(location):
            raise SourceBlocked('official:baidu detail requires an existing Mainland-only listing observation')
    if source_id == 'official:minimax' and operation == 'list':
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get('location') != ['CT_11']:
            raise SourceBlocked('official:minimax list requires the preselected Mainland Beijing filter (CT_11)')
        projects = (qs.get('project') or [''])[0].split(',')
        allowed_projects = {'7495675705720965415', '7352753013591755047'}
        if not projects or any(project not in allowed_projects for project in projects):
            raise SourceBlocked('official:minimax list project must be a supported campus or internship filter')
        keywords = (qs.get('keywords') or [''])[0].strip().casefold()
        if not any(term in keywords for term in ('agent', 'ai', '大模型', 'llm', '算法')):
            raise SourceBlocked('official:minimax list requires an AI-role keyword')
    if source_id == 'official:minimax' and operation == 'social_list':
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get('location') != ['CT_11']:
            raise SourceBlocked('official:minimax social list requires the preselected Mainland Beijing filter (CT_11)')
        keywords = (qs.get('keywords') or [''])[0].strip().casefold()
        if not any(term in keywords for term in ('agent', 'ai', '大模型', 'llm', '算法')):
            raise SourceBlocked('official:minimax social list requires an AI-role keyword')
    if source_id == 'official:minimax' and operation == 'detail':
        match = re.fullmatch(r'/379481/position/([0-9]+)/detail', parsed.path)
        if not match or not external_id or match[1] != str(external_id):
            raise SourceBlocked('official:minimax detail requires matching stable job identity')
        if not is_domestic_only(location):
            raise SourceBlocked('official:minimax detail requires an existing Mainland-only listing observation')
    if source_id == 'official:baichuan' and operation == 'list':
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get('location') != ['CT_11']:
            raise SourceBlocked('official:baichuan list requires the preselected Mainland Beijing filter (CT_11)')
        keywords = (qs.get('keywords') or [''])[0].strip().casefold()
        if not any(term in keywords for term in ('agent', 'ai', '大模型', 'llm', '算法')):
            raise SourceBlocked('official:baichuan list requires an AI-role keyword')
    if source_id == 'official:baichuan' and operation == 'campus_list':
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get('location') != ['CT_11']:
            raise SourceBlocked('official:baichuan campus list requires the preselected Mainland Beijing filter (CT_11)')
        keywords = (qs.get('keywords') or [''])[0].strip().casefold()
        if not any(term in keywords for term in ('agent', 'ai', '大模型', 'llm', '算法')):
            raise SourceBlocked('official:baichuan campus list requires an AI-role keyword')
    if source_id == 'official:baichuan' and operation == 'detail':
        match = re.fullmatch(r'/baichuanzhaopin/position/([0-9]+)/detail', parsed.path)
        if not match or not external_id or match[1] != str(external_id):
            raise SourceBlocked('official:baichuan detail requires matching stable job identity')
        if not is_domestic_only(location):
            raise SourceBlocked('official:baichuan detail requires an existing Mainland-only listing observation')
    if source_id == 'official:modelbest' and operation == 'list':
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get('location') != ['CT_11']:
            raise SourceBlocked('official:modelbest list requires the preselected Mainland Beijing filter (CT_11)')
        keywords = (qs.get('keywords') or [''])[0].strip().casefold()
        if not any(term in keywords for term in ('agent', 'ai', '大模型', 'llm', '算法')):
            raise SourceBlocked('official:modelbest list requires an AI-role keyword')
    if source_id == 'official:modelbest' and operation == 'campus_list':
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get('location') != ['CT_11']:
            raise SourceBlocked('official:modelbest campus list requires the Mainland Beijing filter (CT_11)')
        keywords = (qs.get('keywords') or [''])[0].strip().casefold()
        if not any(term in keywords for term in ('agent', 'ai', '大模型', 'llm', '算法')):
            raise SourceBlocked('official:modelbest campus list requires an AI-role keyword')
    return parsed.hostname


@contextmanager
def _journal_lock(path):
    # The OS releases this lock if a writer exits; do not use stale lockfile ownership.
    with path.with_suffix('.lock').open('a+b') as handle:
        handle.seek(0,2)
        if handle.tell()==0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name=='nt':
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


class SourceBlocked(RuntimeError):
    pass


class RequestBudget:
    def __init__(self, limit=DEFAULT_TASK_REQUESTS, interval=MIN_HOST_INTERVAL, *, authorization_limit=DEFAULT_AUTHORIZATION_REQUESTS,
                 round_limit=MAX_ROUND_REQUESTS, source_limit=MAX_ROUND_REQUESTS, state_path=None):
        for value in (limit, authorization_limit, round_limit, source_limit):
            request_count(value)
        if interval <= 10:
            raise ValueError('host interval must be greater than 10 seconds')
        self.limit = min(limit, authorization_limit, round_limit, source_limit)
        self.source_limit = source_limit
        self.limits = dict(requested=limit, authorization=authorization_limit,
                           round=round_limit, source=source_limit, effective=self.limit)
        self.interval = interval
        self.requests, self.last, self.blocked, self.events = 0, {}, set(), []
        self.scheduler=SharedScheduler(Path(state_path) if state_path else Path(__file__).resolve().parent.parent / '.request-scheduler.json',_journal_lock)
        self.cooldown_path = self.scheduler.path.with_name(self.scheduler.path.stem.replace('scheduler','cooldown') + '.json')

    def stop(self, url, reason, retry_after=None):
        host = urllib.parse.urlsplit(url).hostname or ''
        self.blocked.add(host)
        self.scheduler.block(host,reason)
        if reason == 'http_429':
            path = self.cooldown_path
            with _journal_lock(path):
                try:
                    cooldown = json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
                except (ValueError, OSError) as error:
                    raise SourceBlocked('cooldown journal unavailable; source stays blocked') from error
                until = time.time() + 300
                if retry_after:
                    try:
                        delay = int(retry_after)
                        until = max(until, time.time() + max(0, delay))
                    except (ValueError, TypeError):
                        try:
                            until = max(until, parsedate_to_datetime(str(retry_after)).timestamp())
                        except (ValueError, TypeError, OverflowError):
                            pass
                cooldown[host] = max(cooldown.get(host, 0), until)
                temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
                temporary.write_text(json.dumps(cooldown), encoding='utf8')
                os.replace(temporary, path)
        self.events.append({'host': host, 'outcome': reason})

    def before(self, request, source_id, operation, location=None, external_id=None, lease=None):
        host = _validate_endpoint(request, source_id, operation, location, external_id)
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        for denied in ('zhipin.com', 'zhaopin.com', '58.com'):
            if host == denied or host.endswith('.' + denied):
                raise SourceBlocked('platform permission required')
        path = self.cooldown_path
        if path.exists():
            try:
                cooldown = json.loads(path.read_text(encoding='utf8'))
            except (ValueError, OSError):
                raise SourceBlocked('cooldown journal unreadable; refusing request')
            if cooldown.get(host, 0) > time.time():
                raise SourceBlocked('source remains in 429 cooldown')
        if host in self.blocked:
            raise SourceBlocked('source stopped for this round')
        if self.requests >= self.limit:
            raise SourceBlocked('round request budget exhausted')
        if self.scheduler.snapshot()['remaining'] <= 0:
            raise SourceBlocked('shared round budget exhausted; explicit new round required')
        try:
            token,shared=self.scheduler.reserve(host,self.interval,lease,source_id=source_id,source_limit=self.source_limit)
        except SchedulingBlocked as error:
            raise SourceBlocked(str(error)) from error
        self.requests += 1
        self.last[host] = time.monotonic()
        self.events.append(dict(host=host,request_number=self.requests,outcome='attempted',**shared))
        return token

    @staticmethod
    def authorize(request, *, source_id=None, operation=None, location=None, external_id=None):
        """Pure preflight: validates scope without reserving quota or sending a request."""
        return _validate_endpoint(request, source_id, operation, location, external_id)

    def open(self, request, timeout=20, allow_redirects=True, *, source_id=None,
             operation=None, location=None, external_id=None):
        budget = self
        lease = None

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                nonlocal lease
                if not allow_redirects:return None
                redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
                if redirected is not None:
                    lease=budget.before(redirected,source_id,operation,location,external_id,lease)
                return redirected

        url = request.full_url if hasattr(request, 'full_url') else request
        lease=self.before(request,source_id,operation,location,external_id)
        try:
            response=urllib.request.build_opener(Redirect()).open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                if exc.code in (401, 403, 429):
                    self.stop(exc.url or url, 'http_' + str(exc.code), exc.headers.get('Retry-After') if exc.headers else None)
            finally:
                self.scheduler.release(lease)
            raise
        except BaseException:
            self.scheduler.release(lease)
            raise
        return _ReservedResponse(response,self.scheduler,lease)


class _ReservedResponse:
    def __init__(self,response,scheduler,lease):
        self.response,self.scheduler,self.lease=response,scheduler,lease

    def __getattr__(self,name):return getattr(self.response,name)

    def close(self):
        try:self.response.close()
        finally:
            if self.lease is not None:
                self.scheduler.release(self.lease)
                self.lease=None

    def __enter__(self):return self

    def __exit__(self,*args):self.close()


DEFAULT_BUDGET = RequestBudget()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Explicit shared round management; no source requests')
    actions=parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--start-round',action='store_true')
    actions.add_argument('--expand-round',action='store_true')
    actions.add_argument('--preflight',action='store_true')
    parser.add_argument('--round-id')
    parser.add_argument('--limit',type=int,default=3)
    parser.add_argument('--expected-used',type=int)
    parser.add_argument('--expected-limit',type=int)
    args=parser.parse_args()
    if args.preflight:
        result=DEFAULT_BUDGET.scheduler.snapshot()
    elif args.expand_round:
        result=DEFAULT_BUDGET.scheduler.expand_round(args.round_id,args.expected_used,args.expected_limit,args.limit)
    else:
        if not args.round_id:parser.error('--round-id required')
        result=DEFAULT_BUDGET.scheduler.start_round(args.round_id,args.limit)
    print(json.dumps(result))
