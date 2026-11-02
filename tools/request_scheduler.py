"""Persistent project-wide reservations. New rounds require an explicit command."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4


class SchedulingBlocked(RuntimeError):
    pass


class SharedScheduler:
    def __init__(self,path,lock):
        self.path=Path(path)
        self.lock=lock

    def _read(self):
        if not self.path.exists():
            return dict(version=1,round_id='initial-'+uuid4().hex,used=0,limit=15,hosts={},leases={},blocked={})
        try:
            state=json.loads(self.path.read_text(encoding='utf8'))
            assert state['version']==1 and 0<=state['used']<=state['limit']<=15
            assert all(isinstance(state[k],dict) for k in ('hosts','leases','blocked'))
            return state
        except (OSError,ValueError,KeyError,TypeError,AssertionError) as error:
            raise SchedulingBlocked('shared request state invalid; no request dispatched') from error

    def _write(self,state):
        temporary=self.path.with_name(self.path.name+'.'+uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(state,sort_keys=True),encoding='utf8')
        os.replace(temporary,self.path)

    def reserve(self,host,interval,lease=None):
        while True:
            with self.lock(self.path):
                state=self._read()
                if host in state['blocked']:
                    raise SchedulingBlocked('source requires supported recovery: '+state['blocked'][host])
                if state['used']>=state['limit']:
                    raise SchedulingBlocked('shared round exhausted; explicit new round required')
                active=state['leases']
                if lease is None and len(active)>=5:
                    raise SchedulingBlocked('global concurrency limit 5 reached')
                if any(k!=lease and v['host']==host for k,v in active.items()):
                    raise SchedulingBlocked('same-host request already in progress')
                current=time.time()
                previous=state['hosts'].get(host,0)
                delay=max(0,previous+interval-current)
                if delay>60:
                    raise SchedulingBlocked('clock moved backwards; refusing request')
                if not delay:
                    token=lease or uuid4().hex
                    state['used']+=1
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
        if not round_id.strip() or not 1<=limit<=15:raise ValueError('round ID and limit 1..15 required')
        with self.lock(self.path):
            state=self._read()
            if state['leases']:raise SchedulingBlocked('active or interrupted leases require investigation before a new round')
            if state['round_id']==round_id:raise SchedulingBlocked('cannot reset an existing round')
            state.update(round_id=round_id,used=0,limit=limit)
            self._write(state)
            return state
