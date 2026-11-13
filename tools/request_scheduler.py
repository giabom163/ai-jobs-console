"""Persistent project-wide reservations. New rounds require an explicit command."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4
try:
    from .request_limits import MAX_ROUND_REQUESTS, DEFAULT_TASK_REQUESTS, MAX_INFLIGHT, request_count
except ImportError:
    from request_limits import MAX_ROUND_REQUESTS, DEFAULT_TASK_REQUESTS, MAX_INFLIGHT, request_count


class SchedulingBlocked(RuntimeError):
    pass


class SharedScheduler:
    def __init__(self,path,lock):
        self.path=Path(path)
        self.lock=lock

    def _read(self):
        if not self.path.exists():
            return dict(version=1,round_id='initial-'+uuid4().hex,used=0,limit=DEFAULT_TASK_REQUESTS,hosts={},leases={},blocked={})
        try:
            state=json.loads(self.path.read_text(encoding='utf8'))
            if (state['version'] != 1 or type(state['used']) is not int or
                    not 0 <= state['used'] <= request_count(state['limit']) or
                    not all(isinstance(state[k],dict) for k in ('hosts','leases','blocked'))):
                raise ValueError('invalid shared state')
            for key in ('source_used', 'source_caps'):
                if key in state and not isinstance(state[key], dict):
                    raise ValueError('invalid source quota state')
            for source, used in state.get('source_used', {}).items():
                if type(used) is not int or used < 0 or used > state['used']:
                    raise ValueError('invalid source count')
            for cap in state.get('source_caps', {}).values():
                request_count(cap, 'source cap')
            return state
        except (OSError,ValueError,KeyError,TypeError,AssertionError) as error:
            raise SchedulingBlocked('shared request state invalid; no request dispatched') from error

    def _write(self,state):
        temporary=self.path.with_name(self.path.name+'.'+uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(state,sort_keys=True),encoding='utf8')
        os.replace(temporary,self.path)

    def reserve(self,host,interval,lease=None,*,source_id=None,source_limit=None):
        while True:
            with self.lock(self.path):
                state=self._read()
                if host in state['blocked']:
                    raise SchedulingBlocked('source requires supported recovery: '+state['blocked'][host])
                if state['used']>=state['limit']:
                    raise SchedulingBlocked('shared round exhausted; explicit new round required')
                active=state['leases']
                if lease is None and len(active)>=MAX_INFLIGHT:
                    raise SchedulingBlocked('global concurrency limit 5 reached')
                if any(k!=lease and v['host']==host for k,v in active.items()):
                    raise SchedulingBlocked('same-host request already in progress')
                if source_id:
                    cap = request_count(MAX_ROUND_REQUESTS if source_limit is None else source_limit, 'source cap')
                    caps = state.setdefault('source_caps', {})
                    cap = min(cap, caps.get(source_id, cap))
                    if state.setdefault('source_used', {}).get(source_id, 0) >= cap:
                        raise SchedulingBlocked('shared source quota exhausted')
                current=time.time()
                previous=state['hosts'].get(host,0)
                delay=max(0,previous+interval-current)
                if delay>60:
                    raise SchedulingBlocked('clock moved backwards; refusing request')
                if not delay:
                    token=lease or uuid4().hex
                    state['used']+=1
                    if source_id:
                        caps[source_id] = cap
                        state['source_used'][source_id] = state['source_used'].get(source_id, 0) + 1
                    state['hosts'][host]=current
                    active[token]=dict(host=host,pid=os.getpid(),started_at=current)
                    self._write(state)
                    return token,dict(round_id=state['round_id'],global_request_number=state['used'],started_at=current)
            time.sleep(delay)

    def release(self,lease):
        if lease is None:return
        with self.lock(self.path):
            state=self._read();state['leases'].pop(lease,None);self._write(state)

    def block(self,host,reason):
        # 429 is timed by the existing cooldown journal. Explicit refusals persist
        # across rounds; restarting a process or a round cannot recover a source.
        if reason=='http_429':return
        with self.lock(self.path):
            state=self._read();state['blocked'][host]=reason;self._write(state)

    def start_round(self,round_id,limit):
        request_count(limit)
        if not round_id.strip():raise ValueError('round ID required')
        with self.lock(self.path):
            state=self._read()
            if state['leases']:raise SchedulingBlocked('active or interrupted leases require investigation before a new round')
            if state['round_id']==round_id:raise SchedulingBlocked('cannot reset an existing round')
            state.update(round_id=round_id,used=0,limit=limit,source_used={},source_caps={},legacy_unattributed_used=0)
            self._write(state)
            return state

    def snapshot(self):
        """Read only: does not create state or reserve a request."""
        state = self._read()  # Writers publish by atomic replace; no lock file write on preview.
        return {**state, 'remaining': state['limit'] - state['used'],
                'max_round_requests': MAX_ROUND_REQUESTS}

    def expand_round(self, round_id, expected_used, expected_limit, limit):
        """Explicit compare-and-set expansion, preserving all consumption/refusals."""
        request_count(limit)
        request_count(expected_limit, 'expected limit')
        if type(expected_used) is not int or expected_used < 0:
            raise ValueError('expected used must be a nonnegative integer')
        with self.lock(self.path):
            state = self._read()
            if state['leases']:
                raise SchedulingBlocked('cannot expand while requests are in flight')
            if (state['round_id'], state['used'], state['limit']) != (round_id, expected_used, expected_limit):
                raise SchedulingBlocked('round changed; inspect before expansion')
            if limit < state['limit']:
                raise ValueError('expansion cannot lower the limit')
            state.setdefault('legacy_unattributed_used', state['used'])
            state['limit'] = limit
            self._write(state)
            return state
