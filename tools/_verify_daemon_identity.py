"""仅离线验证托管守护身份；使用本轮自建进程，保留证据目录。"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.dont_write_bytecode=True
os.environ['AI_JOBS_TESTING']='1'
from tools import ai_server as server
from tools import daemon_runtime as runtime


def run_checks(out: Path, limit: int):
    out.mkdir(parents=True,exist_ok=True)
    results=[]
    saved=(server.DAEMON_RUN_FILE,server.PIPELINE_LOG_FILE,server._daemon_proc,server._pipeline_proc)
    server.DAEMON_RUN_FILE=out/'managed.json'
    server.PIPELINE_LOG_FILE=out/'managed.log'
    server._daemon_proc=None; server._pipeline_proc=None
    try:
        for number in range(1,limit+1):
            try:
                if number==1:
                    assert runtime.execution_lock(ROOT,out/'a.json',False) == runtime.execution_lock(ROOT,out/'b.json',False)
                    with patch.dict(os.environ,{'AI_JOBS_DAEMON_IDENTITY_ONLY':'1'}):
                        with runtime.file_lock(server.DAEMON_RUN_FILE.with_suffix('.control.lock')):
                            started,code=server._start_managed_daemon(10,False,True)
                    assert code==200,(started,code)
                    child=server._daemon_proc
                    try:
                        server._daemon_proc=None  # 模拟服务重启；只从磁盘与OS身份恢复。
                        state=server._sync_daemon_state()
                        assert state['running'] and state['resumed'] and state['pid']==child.pid,state
                        # 同一控制锁从另一自建Python进程竞争，必须被拒绝。
                        check="from pathlib import Path; from tools.daemon_runtime import file_lock; "
                        check+="\nwith file_lock(Path("+repr(str(server.DAEMON_RUN_FILE.with_suffix('.control.lock')))+")): pass"
                        with runtime.file_lock(server.DAEMON_RUN_FILE.with_suffix('.control.lock')):
                            competitor=subprocess.run([sys.executable,'-B','-c',check],cwd=ROOT,capture_output=True,encoding='utf-8')
                        assert competitor.returncode!=0,competitor.stdout
                        stopped,code=server._stop_managed_daemon()
                        assert code==200,(stopped,code)
                        child.wait(timeout=5)
                        assert not server.DAEMON_RUN_FILE.exists()
                        with runtime.file_lock(runtime.execution_lock(ROOT,server.DAEMON_RUN_FILE,True)):
                            assert server._start_managed_daemon(10,False,True)[1]==409
                            assert server._trigger_managed_pipeline(False,True)[1]==409
                    finally:
                        if child.poll() is None:
                            child.terminate(); child.wait(timeout=5)
                    label='正确身份恢复、跨进程控制锁、命名Job停止'
                elif number==2:
                    # 非守护的自建进程必须被拒绝；不得向其发送停止命令。
                    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
                    try:
                        fake={'schema':2,'workspace':str(ROOT.resolve()),'script':str((ROOT/'tools/pipeline_daemon.py').resolve()),
                              'run_id':'11111111-1111-4111-8111-111111111111','interval_min':10,'offline':True,'dry_run':False,
                              'pid':child.pid,'created':'wrong','exe':sys.executable,'command':'wrong','started_at':'test'}
                        fake['command']=subprocess.list2cmdline(runtime.daemon_command(ROOT.resolve(),server.DAEMON_RUN_FILE.resolve(),fake['run_id'],10,True,False)) if os.name=='nt' else runtime.daemon_command(ROOT.resolve(),server.DAEMON_RUN_FILE.resolve(),fake['run_id'],10,True,False)
                        runtime.atomic_json(server.DAEMON_RUN_FILE,fake)
                        before=server.DAEMON_RUN_FILE.read_bytes()
                        with patch.object(server,'terminate_owned',side_effect=AssertionError('must not terminate unrelated process')):
                            assert server._sync_daemon_state()['blocked']
                            assert server._stop_managed_daemon()[1]==409
                            assert server._start_managed_daemon(10,False,True)[1]==409
                        assert child.poll() is None
                        assert server.DAEMON_RUN_FILE.read_bytes()==before
                    finally:
                        child.terminate(); child.wait(timeout=5)
                    label='无关PID拒绝恢复、停止与新启动'
                else:
                    damaged=out/'damaged.json'; damaged.write_text('{broken',encoding='utf-8')
                    server.DAEMON_RUN_FILE=damaged
                    assert server._start_managed_daemon(10,False,True)[1]==409
                    assert damaged.read_text(encoding='utf-8')=='{broken'
                    with patch.object(runtime,'atomic_json',side_effect=OSError('write denied')):
                        try:
                            runtime.publish_record(out/'write-failed.json',ROOT,'22222222-2222-4222-8222-222222222222',10,True,False,'test')
                        except OSError:
                            pass
                        else:
                            raise AssertionError('publication failure must propagate')
                    assert not (out/'write-failed.json').exists()
                    with patch.object(runtime,'process_identity',return_value={'state':'unknown'}):
                        assert runtime.inspect_record(out/'managed.json',ROOT)['blocked']
                    label='损坏账本、写失败与身份查询未知均阻断'
                results.append({'case':number,'label':label,'status':'PASS'})
            except Exception as exc:
                results.append({'case':number,'status':'FAIL','error':repr(exc)})
                raise
    finally:
        server.DAEMON_RUN_FILE,server.PIPELINE_LOG_FILE,server._daemon_proc,server._pipeline_proc=saved
        (out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(results,ensure_ascii=False,indent=2))
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--limit',type=int,default=3); p.add_argument('--out',type=Path,required=True); a=p.parse_args()
    if not 1<=a.limit<=3: p.error('--limit must be 1..3')
    run_checks(a.out,a.limit)
