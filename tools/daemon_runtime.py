"""守护身份、原子账本与进程间锁；不请求网络，不启用特殊权限。"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCHEMA = 2


@contextlib.contextmanager
def file_lock(path: Path):
    """锁文件保留，锁由操作系统随句柄释放；不删除别的进程的锁文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, value: dict):
    # 同目录替换，异常文件保留，供诊断；不覆写半份JSON。
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def process_identity(pid: int) -> dict:
    if type(pid) is not int or pid <= 0:
        return {'state': 'unknown', 'error': 'invalid pid'}
    try:
        if os.name == 'nt':
            # 数字PID由类型检查限定。通过CIM读取，不打印命令行或读取凭据。
            script = (
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
                "$ErrorActionPreference='Stop';"
                f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}';"
                "if($null -eq $p){'null'}else{"
                "[pscustomobject]@{pid=$p.ProcessId;exe=$p.ExecutablePath;"
                "created=$p.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString();"
                "command=$p.CommandLine}|ConvertTo-Json -Compress}"
            )
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                    capture_output=True, encoding='utf-8', errors='replace', timeout=8)
            if result.returncode:
                return {'state': 'unknown', 'error': 'process query failed'}
            data = json.loads(result.stdout.lstrip('\ufeff'))
            if data is None:
                return {'state': 'dead'}
            if not all(data.get(k) for k in ('exe', 'created', 'command')):
                return {'state': 'unknown', 'error': 'process identity unavailable'}
            return {'state': 'alive', **data}
        proc = Path('/proc') / str(pid)
        if not proc.exists():
            return {'state': 'dead'}
        stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        command = (proc / 'cmdline').read_bytes().split(b'\0')
        return {'state': 'alive', 'pid': pid, 'exe': str((proc / 'exe').resolve(strict=True)),
                'created': stat[19], 'command': [x.decode() for x in command if x]}
    except Exception as exc:
        return {'state': 'unknown', 'error': type(exc).__name__}


def execution_lock(root: Path, run_file: Path, offline: bool) -> Path:
    # 非离线任务始终共用仓库级锁；离线夹具允许与真实服务分别验证。
    return (run_file.parent if offline else root) / '.pipeline-execution.lock'


def daemon_command(root: Path, run_file: Path, run_id: str, interval: int, offline: bool, dry_run: bool, executable=None):
    if not executable:
        wb_py = Path(r"C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe")
        executable = str(wb_py) if wb_py.exists() else sys.executable
    cmd = [executable, '-u', '-B', str(root / 'tools' / 'pipeline_daemon.py'),
           '--daemon', '--interval-min', str(interval), '--run-id', run_id, '--run-file', str(run_file)]
    if offline:
        cmd.append('--offline')
    elif dry_run:
        cmd.append('--dry-run')
    return cmd


def inspect_record(path: Path, root: Path) -> dict:
    idle = {'running': False, 'blocked': False, 'pid': None, 'resumed': False, 'interval_min': 30, 'started_at': None}
    if not path.exists():
        return idle
    blocked = {**idle, 'blocked': True, 'status': 'identity_unconfirmed'}
    try:
        info = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(info, dict) or info.get('schema') != SCHEMA:
            return {**blocked, 'error': 'legacy or invalid daemon ledger'}
        if info.get('workspace') != str(root.resolve()) or info.get('script') != str((root/'tools/pipeline_daemon.py').resolve()):
            return {**blocked, 'error': 'workspace mismatch'}
        if str(uuid.UUID(info['run_id'])) != info['run_id']:
            return {**blocked, 'error': 'invalid run id'}
        if type(info['interval_min']) is not int or not 10 <= info['interval_min'] <= 1440:
            return {**blocked, 'error': 'invalid interval'}
        if type(info['offline']) is not bool or type(info['dry_run']) is not bool:
            return {**blocked, 'error': 'invalid mode'}
        if not isinstance(info.get('exe'), str) or not Path(info['exe']).is_absolute():
            return {**blocked, 'error': 'invalid executable path'}
        expected = daemon_command(root.resolve(), path.resolve(), info['run_id'], info['interval_min'], info['offline'], info['dry_run'], info['exe'])
        expected_command = subprocess.list2cmdline(expected) if os.name == 'nt' else expected
        if info.get('command') != expected_command:
            return {**blocked, 'error': 'unexpected daemon command'}
        observed = process_identity(info['pid'])
        if observed['state'] == 'dead':
            return {**idle, 'stale': True, 'record': info}
        if observed['state'] != 'alive':
            return {**blocked, 'error': 'process identity query unconfirmed'}
        if any(observed.get(k) != info.get(k) for k in ('pid', 'created', 'exe', 'command')):
            return {**blocked, 'error': 'process identity mismatch'}
        return {**idle, 'running': True, 'pid': info['pid'], 'resumed': True,
                'interval_min': info['interval_min'], 'started_at': info['started_at'], 'record': info}
    except Exception as exc:
        return {**blocked, 'error': 'ledger validation failed: ' + type(exc).__name__}


def publish_record(path: Path, root: Path, run_id: str, interval: int, offline: bool, dry_run: bool, started_at: str):
    observed = process_identity(os.getpid())
    if observed['state'] != 'alive':
        raise RuntimeError('cannot establish own OS process identity')
    record = {**{k: observed[k] for k in ('pid', 'exe', 'created', 'command')},
              'schema': SCHEMA, 'workspace': str(root.resolve()), 'script': str((root/'tools/pipeline_daemon.py').resolve()),
              'run_id': run_id, 'interval_min': interval, 'offline': offline, 'dry_run': dry_run, 'started_at': started_at}
    atomic_json(path, record)
    state = inspect_record(path, root)
    if not state['running']:
        raise RuntimeError('published daemon identity did not match')
    return record


class OwnedJob:
    """Windows守护自己持有命名Job，退出时清理该Job内子进程；不接管既有Job。"""
    def __init__(self, run_id: str):
        self.run_id = str(uuid.UUID(run_id))
        self.handle = None

    def __enter__(self):
        if os.name != 'nt':
            if os.getpgrp() != os.getpid():
                raise RuntimeError('managed daemon must lead a new process group')
            return self
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', w.DWORD),
                        ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', w.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]; k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE,w.INT,ctypes.c_void_p,w.DWORD]; k.SetInformationJobObject.restype = w.BOOL
        k.GetCurrentProcess.restype = w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]; k.AssignProcessToJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE,w.UINT]; k.TerminateJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]; k.CloseHandle.restype = w.BOOL
        handle = k.CreateJobObjectW(None, 'Local\\AIJobs-' + self.run_id)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            k.CloseHandle(handle)
            raise RuntimeError('job name already exists')
        limits = Extended(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not k.AssignProcessToJobObject(handle, k.GetCurrentProcess()):
            code = ctypes.get_last_error(); k.CloseHandle(handle); raise ctypes.WinError(code)
        self.kernel = k; self.handle = handle
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle:
            if exc is not None:
                code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
                if not self.kernel.TerminateJobObject(self.handle, code):
                    raise ctypes.WinError(ctypes.get_last_error())
            # KILL_ON_JOB_CLOSE也会终止自身；仅用于托管守护独立进程。
            self.kernel.CloseHandle(self.handle)


def terminate_owned(record: dict):
    """调用方须先核对完整身份；Windows按本次GUID打开Job，不按PID taskkill。"""
    if os.name != 'nt':
        import signal
        if os.getpgid(record['pid']) != record['pid']:
            raise RuntimeError('daemon is not its own process group leader')
        os.killpg(record['pid'], signal.SIGTERM)
        return
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenJobObjectW.argtypes=[w.DWORD,w.BOOL,w.LPCWSTR]; k.OpenJobObjectW.restype=w.HANDLE
    k.TerminateJobObject.argtypes=[w.HANDLE,w.UINT]; k.TerminateJobObject.restype=w.BOOL
    k.QueryInformationJobObject.argtypes=[w.HANDLE,w.INT,ctypes.c_void_p,w.DWORD,ctypes.c_void_p]
    k.QueryInformationJobObject.restype=w.BOOL
    k.CloseHandle.argtypes=[w.HANDLE]; k.CloseHandle.restype=w.BOOL
    handle=k.OpenJobObjectW(0x0008 | 0x0004, False, 'Local\\AIJobs-' + str(uuid.UUID(record['run_id'])))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not k.TerminateJobObject(handle, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        class Accounting(ctypes.Structure):
            _fields_ = [(n, ctypes.c_int64) for n in ('user','kernel','period_user','period_kernel')] + [
                (n, w.DWORD) for n in ('faults','total','active','terminated')]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = Accounting()
            if not k.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.active == 0:
                return
            time.sleep(0.05)
        raise TimeoutError('job still has active processes after termination')
    finally:
        k.CloseHandle(handle)
