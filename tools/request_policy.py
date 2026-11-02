"""One bounded sequential request budget, including redirects. No automatic retry."""
import time
import json
import os
from uuid import uuid4
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
if __package__:
    from .request_scheduler import SharedScheduler, SchedulingBlocked
    from .job_geography import is_domestic_only
else:
    from request_scheduler import SharedScheduler, SchedulingBlocked
    from job_geography import is_domestic_only


# Only this ID-specific official detail endpoint is currently eligible.
# List endpoints whose Mainland China request scope is unproven remain absent.
APPROVED_ENDPOINTS = {
    ('official:meituan', 'detail'): ('POST', 'zhaopin.meituan.com', '/api/official/job/getJobDetail'),
    ('official:meituan', 'list'): ('POST', 'zhaopin.meituan.com', '/api/official/job/getJobList'),
    ('official:tencent', 'detail'): ('GET', 'careers.tencent.com', '/tencentcareer/api/post/ByPostId'),
    ('official:tencent', 'list'): ('GET', 'careers.tencent.com', '/tencentcareer/api/post/Query'),
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
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            parsed.hostname != expected[1] or parsed.port not in (None, 443) or
            parsed.path != expected[2] or method != expected[0]):
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
        if area and area != ['cn']:
            raise SourceBlocked('official:tencent list request area must be cn')
        city_id = qs.get('cityId')
        if city_id and city_id[0]:
            for cid in city_id[0].split(','):
                cid_strip = cid.strip()
                if cid_strip and cid_strip not in TENCENT_DOMESTIC_CITY_IDS:
                    raise SourceBlocked('cityId ' + cid_strip + ' is not in proven Mainland city whitelist')
    if source_id == 'official:tencent' and operation == 'detail':
        qs = urllib.parse.parse_qs(parsed.query)
        post_id = qs.get('postId')
        if not post_id or not post_id[0] or not post_id[0].isdigit():
            raise SourceBlocked('official:tencent detail requires valid numeric postId')
        if external_id and str(post_id[0]) != str(external_id):
            raise SourceBlocked('official:tencent detail postId does not match external_id')
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
    def __init__(self, limit=3, interval=20, *, authorization_limit=9,
                 round_limit=15, source_limit=15):
        if (not 1 <= limit <= 15 or not 1 <= round_limit <= 15 or
                not 1 <= source_limit <= 15 or not 1 <= authorization_limit <= 9 or interval < 20):
            raise ValueError('request policy cannot exceed project limits')
        self.limit = min(limit, authorization_limit, round_limit, source_limit)
        self.interval = interval
        self.requests, self.last, self.blocked, self.events = 0, {}, set(), []
        self.scheduler=SharedScheduler(Path(__file__).resolve().parent.parent / '.request-scheduler.json',_journal_lock)

    def stop(self, url, reason, retry_after=None):
        host = urllib.parse.urlsplit(url).hostname or ''
        self.blocked.add(host)
        self.scheduler.block(host,reason)
        if reason == 'http_429':
            path = Path(__file__).resolve().parent.parent / '.request-cooldown.json'
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
        path = Path(__file__).resolve().parent.parent / '.request-cooldown.json'
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
        try:
            token,shared=self.scheduler.reserve(host,self.interval,lease)
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
    parser.add_argument('--start-round',action='store_true',required=True)
    parser.add_argument('--round-id',required=True)
    parser.add_argument('--limit',type=int,default=3)
    args=parser.parse_args()
    print(json.dumps(DEFAULT_BUDGET.scheduler.start_round(args.round_id,args.limit)))
