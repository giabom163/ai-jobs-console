#!/usr/bin/env python
"""AI 岗位核查台 - 多源全自动化数据管道与守护调度系统。

核心职责：
  1) 自动化多源管道：串联腾讯招聘官方、美团官方、DeepSeek官方直招。
  2) 严格安全与节流门禁：每个网络端点严格遵循 SharedScheduler >= 20 秒间隔，前置中国境内校验。
  3) 事务闭环与自动建站：每次入库前创建物理备份，入库后自动执行 build_site.py。
  4) 平台许可合规门禁：未取得明确平台许可前，默认禁用 Boss 直聘等商业平台的自动化抓取。

用法：
  python tools/pipeline_daemon.py --once       # 运行一轮完整的自动化多源管道
  python tools/pipeline_daemon.py --daemon     # 启动常驻定时巡检守护进程
  python tools/pipeline_daemon.py --dry-run    # 只读演练模式
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
PYTHON = sys.executable
VAL_DIR = Path.home() / ".codex" / "documents" / "ai-jobs-validation-20261102"


def now():
    return datetime.now(timezone.utc).isoformat()


if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from tools.backup_manager import ensure_backup as mgr_ensure_backup
except ModuleNotFoundError:
    from backup_manager import ensure_backup as mgr_ensure_backup


def ensure_db_backup(tag="pipeline"):
    if not DB.exists():
        return None
    return str(mgr_ensure_backup(tag))


def run_cmd(args, timeout=360):
    cmd_str = " ".join(args)
    print(f"[pipeline] 启动任务: {cmd_str}", flush=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if args and args[0] == "ego-browser" and os.name == "nt":
        cmd_args = ["cmd.exe", "/c"] + args
    else:
        cmd_args = args

    try:
        proc = subprocess.Popen(
            cmd_args,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=env,
        )
        stdout_text, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
        if stdout_text:
            for line in stdout_text.splitlines():
                print(f"  > {line}", flush=True)
        return code, (stdout_text or "").strip(), ""
    except subprocess.TimeoutExpired as exc:
        partial_stdout = exc.stdout or ""
        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode("utf-8", errors="replace")
        try:
            # 使用Popen原生句柄，不把数字PID重新交给taskkill。
            proc.kill()
            # 进程终止后冲刷读取管道中已输出的残留内容，彻底杜绝输出丢失 (R7)
            rem_stdout, _ = proc.communicate(timeout=2)
            if rem_stdout:
                # communicate重试返回累计缓冲，不能把超时前缀重复拼接。
                partial_stdout = rem_stdout
        except Exception:
            pass
        cleanup = '将退出托管Job清理后代' if os.environ.get('AI_JOBS_MANAGED_RUN_ID') else '后代退出情况未确认'
        print(f"[pipeline] 任务超时 ({timeout}秒)，已尝试终止直接子进程；{cleanup} (保留已有输出 {len(partial_stdout)} 字符)", flush=True)
        if os.environ.get('AI_JOBS_MANAGED_RUN_ID'):
            # 退出本轮托管进程，关闭Job使其全部后代退出；不得继续下一采集步骤。
            raise SystemExit(124)
        return 124, partial_stdout.strip(), f"Timeout ({timeout}s)"
    except Exception as exc:
        print(f"[pipeline] 任务执行异常: {exc}", flush=True)
        return 1, "", str(exc)


def step_tencent(dry_run=False, offline=False):
    print("\n=== [1/4] 启动腾讯招聘官方接口采集 ===")
    if offline:
        print("[pipeline] [offline] 离线演练模式：跳过腾讯外网API请求，外网派发次数严格为 0", flush=True)
        return True
    if not dry_run:
        ensure_db_backup("pre-tencent")
    args = [PYTHON, "-B", str(ROOT / "tools" / "collect_tencent.py"), "--keywords", "大模型,算法", "--limit", "5"]
    if dry_run:
        args.append("--dry-run")
        print("[pipeline] [dry-run] 采集器运行于非写入模式（注意：仍会调用真实接口）", flush=True)
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] 腾讯采集结果: {stdout}")
    return code == 0


def step_meituan(dry_run=False, offline=False):
    print("\n=== [2/4] 启动美团官方接口采集 ===")
    if offline:
        print("[pipeline] [offline] 离线演练模式：跳过美团外网API请求，外网派发次数严格为 0", flush=True)
        return True
    if not dry_run:
        ensure_db_backup("pre-meituan")
    args = [PYTHON, "-B", str(ROOT / "tools" / "collect_meituan.py"), "--keywords", "大模型,算法", "--limit", "5"]
    if dry_run:
        args.append("--dry-run")
        print("[pipeline] [dry-run] 采集器运行于非写入模式（注意：仍会调用真实接口）", flush=True)
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] 美团采集结果: {stdout}")
    return code == 0


def step_deepseek(dry_run=False, offline=False):
    print("\n=== [3/4] 同步 DeepSeek 官方直招岗位 ===")
    if offline:
        print("[pipeline] [offline] 离线演练模式：跳过DeepSeek外网API请求，外网派发次数严格为 0", flush=True)
        return True
    if not dry_run:
        ensure_db_backup("pre-deepseek")
    args = [PYTHON, "-B", str(ROOT / "tools" / "collect_deepseek.py"), "--limit", "5"]
    if dry_run:
        args.append("--dry-run")
        print("[pipeline] [dry-run] 采集器运行于非写入模式（注意：仍会调用真实接口）", flush=True)
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] DeepSeek 同步结果: {stdout}")
    return code == 0


def step_boss(dry_run=False, enable_boss=False, offline=False):
    print("\n=== [4/4] 检查 Boss 直聘采集引擎状态 ===")
    if offline:
        print("[pipeline] [offline] 离线演练模式：跳过Boss直聘抓取，外网派发次数严格为 0", flush=True)
        return True
    is_enabled = enable_boss or (os.environ.get("ENABLE_BOSS_COLLECTOR", "0") == "1")
    if not is_enabled:
        print("[pipeline] 提示: Boss 直聘自动化采集已默认禁用（依据平台许可门禁规则，未取得平台明确许可前严禁自动化抓取）。跳过此步骤。", flush=True)
        return True

    if dry_run:
        print("[pipeline] dry-run 模式跳过浏览器真实操作与入库", flush=True)
        return True

    boss_batch_file = VAL_DIR / "boss-continuous-batch.json"
    if boss_batch_file.exists():
        try:
            with open(boss_batch_file, "r", encoding="utf-8") as f:
                batch_data = json.load(f)
            aborted_reason = batch_data.get("aborted_reason")
            if aborted_reason == "platform_verification_detected":
                print(f"[pipeline] 安全门禁拦截: 上次抓取记录中包含平台风控拦截信号 ({aborted_reason})，暂停自动调度。", flush=True)
                return True
        except Exception as e:
            print(f"[pipeline] 警告: 读取批次阻断标志失败: {e}", flush=True)

    mjs_file = ROOT / "tools" / "boss_continuous_collector.mjs"
    eval_arg = f"await import('{mjs_file.as_uri()}')"
    print("[pipeline] 正在调用 Ego Lite 执行受控平滑采集...")
    try:
        code_browser, stdout_browser, stderr_browser = run_cmd(["ego-browser", "nodejs", "-e", eval_arg], timeout=300)
        print(f"[pipeline] 浏览器执行完成: 退出码 {code_browser}")
        if stdout_browser:
            print(f"[pipeline] 输出概要: {stdout_browser}")
        if stderr_browser:
            print(f"[pipeline] 异常警报: {stderr_browser}")
    except Exception as exc:
        print(f"[pipeline] 浏览器调用异常: {exc}")

    if boss_batch_file.exists():
        print(f"[pipeline] 正在处理最新批次落盘数据: {boss_batch_file}")
        ensure_db_backup("pre-boss")
        args = [PYTHON, "-B", str(ROOT / "tools" / "ingest_browser_jobs.py"), "--input", str(boss_batch_file)]
        code_ingest, stdout_ingest, _ = run_cmd(args)
        print(f"[pipeline] Boss 批量入库结果: {stdout_ingest}")
        return code_ingest == 0
    else:
        print(f"[pipeline] 提示: 本轮未生成新的批次文件，平稳跳过")
        return True


def step_rebuild_site(dry_run=False, offline=False):
    print("\n=== [系统同步] 重构静态分片与修剪废弃索引 ===")
    if offline or dry_run:
        mode_str = "offline" if offline else "dry-run"
        print(f"[pipeline] [{mode_str}] 模式跳过静态分片重构与废弃文件清理，保持本地文件哈希不变", flush=True)
        return True
    args = [PYTHON, "-B", str(ROOT / "tools" / "build_site.py"), "--prune-obsolete"]
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] 静态站重构结果: 退出码 {code}")
    return code == 0


def run_planned_tasks(plan_file, dry_run=False):
    from tools.request_policy import DEFAULT_BUDGET
    from tools.task_plan import load_plan, claim_plan
    scheduler = DEFAULT_BUDGET.scheduler
    runs_file = ROOT / '.task-plan-runs.json'
    try:
        plan = load_plan(plan_file, scheduler)
        claim_plan(plan, runs_file)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {'success': False, 'status': 'blocked', 'reason': str(exc), 'requests': 0}
    results = []
    plan_started_at = datetime.now(timezone.utc).isoformat()
    for task_index, task in enumerate(plan['tasks']):
        source = task['source_id']
        task_id = task.get('task_id', f"{source}:{task.get('mode', 'discover')}")
        task_start = datetime.now(timezone.utc).isoformat()
        if datetime.now(timezone.utc) >= datetime.fromisoformat(plan['deadline'].replace('Z', '+00:00')):
            results.append({'task_id': task_id, 'source_id': source, 'status': 'blocked', 'reason': 'plan_deadline_reached',
                            'started_at': task_start, 'ended_at': datetime.now(timezone.utc).isoformat()})            for skipped in plan['tasks'][task_index + 1:]:                skipped_id = skipped.get('task_id') or '{}:{}'.format(skipped['source_id'], skipped.get('mode', 'discover'))                results.append({'task_id': skipped_id, 'source_id': skipped['source_id'], 'status': 'skipped',                                'reason': 'plan_deadline_reached', 'started_at': None, 'ended_at': None})
            break
        if scheduler.snapshot()['remaining'] <= 0:
            results.append({'task_id': task_id, 'source_id': source, 'status': 'blocked', 'reason': 'shared_round_exhausted',
                            'started_at': task_start, 'ended_at': datetime.now(timezone.utc).isoformat()})            for skipped in plan['tasks'][task_index + 1:]:                skipped_id = skipped.get('task_id') or '{}:{}'.format(skipped['source_id'], skipped.get('mode', 'discover'))                results.append({'task_id': skipped_id, 'source_id': skipped['source_id'], 'status': 'skipped',                                'reason': 'shared_round_exhausted', 'started_at': None, 'ended_at': None})
            break
        script = 'collect_tencent.py' if source == 'official:tencent' else 'collect_meituan.py'
        args = [PYTHON, '-B', str(ROOT / 'tools' / script), '--keywords', task['keywords'],
                '--limit', str(task['records_limit']), '--max-requests', str(task['max_requests']),
                '--authorization-limit', str(task['authorization_limit']), '--source-limit', str(task['source_limit'])]
        if task.get('mode') == 'refresh':
            args.append('--refresh')
        if dry_run:
            args.append('--dry-run')
        code, stdout, stderr = run_cmd(args)
        task_end = datetime.now(timezone.utc).isoformat()
        payload = None
        for line in reversed(stdout.splitlines()):
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict):
                    payload = candidate
                    break
            except ValueError:
                continue
        result = {'task_id': task_id, 'source_id': source, 'exit_code': code, 'status': 'failed',
                  'started_at': task_start, 'ended_at': task_end, 'result': payload}
        if code == 0 and payload is not None:
            added = payload.get('added', 0)
            updated = payload.get('updated', 0)
            seen = payload.get('seen', 0)
            result.update(seen=seen, added=added, updated=updated,
                          requests=payload.get('requests', 0),
                          cursor=payload.get('cursor'))
            result['status'] = 'no_change' if not (added or updated) else 'succeeded'
        else:
            reported_status = (payload or {}).get('status')            if reported_status in ('blocked', 'failed'):                result['status'] = reported_status            result['reason'] = (payload or {}).get('error_message') or stderr or 'child_failed_or_missing_result'
        if (payload or {}).get('cursor_error'):
            result.update(status='blocked', reason=payload['cursor_error'])
        results.append(result)

    has_success = any(x['status'] in ('succeeded', 'no_change') for x in results)
    has_failed = any(x['status'] == 'failed' for x in results)
    has_blocked = any(x['status'] == 'blocked' for x in results)
    has_skipped = any(x['status'] == 'skipped' for x in results)

    if not results:
        overall_status = 'blocked'
        success = False
    elif all(x['status'] in ('succeeded', 'no_change') for x in results):
        overall_status = 'succeeded'
        success = True
    elif has_success and (has_failed or has_blocked or has_skipped):
        overall_status = 'partial'
        success = False
    elif has_failed:
        overall_status = 'failed'
        success = False
    elif has_blocked:
        overall_status = 'blocked'
        success = False
    else:
        overall_status = 'skipped'
        success = False

    outcome = {
        'success': success,
        'status': overall_status,
        'plan_id': plan['plan_id'],
        'started_at': plan_started_at,
        'ended_at': datetime.now(timezone.utc).isoformat(),
        'tasks': results,
        'scheduler': scheduler.snapshot(),
        'build': 'not_requested',
        'deployment': 'not_requested'
    }
    try:
        from tools.request_policy import _journal_lock
        with _journal_lock(runs_file):
            run_data = json.loads(runs_file.read_text(encoding='utf-8')) if runs_file.exists() else {}
            if plan['plan_id'] in run_data:
                run_data[plan['plan_id']].update(outcome=overall_status, finished_at=outcome['ended_at'],
                                                 tasks=results)
            run_data['last_plan_outcome'] = outcome
            runs_file.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass
    return outcome


def run_full_pipeline(dry_run=False, enable_boss=False, offline=False, plan_file=None):
    if not offline:
        if not plan_file:
            return {'success': False, 'status': 'blocked', 'reason': 'explicit finite --plan required', 'requests': 0}
        return run_planned_tasks(plan_file, dry_run)
    start_time = time.time()
    print(f"\n==========================================")
    print(f"[pipeline] 自动化系统调度轮次开始: {now()} (dry_run={dry_run}, offline={offline})")
    print(f"==========================================")

    res_tencent = step_tencent(dry_run=dry_run, offline=offline)
    time.sleep(0.1 if offline else (1 if dry_run else 5))

    res_meituan = step_meituan(dry_run=dry_run, offline=offline)
    time.sleep(0.1 if offline else (1 if dry_run else 5))

    res_deepseek = step_deepseek(dry_run=dry_run, offline=offline)
    time.sleep(0.1 if offline else (1 if dry_run else 5))

    res_boss = step_boss(dry_run=dry_run, enable_boss=enable_boss, offline=offline)

    res_build = step_rebuild_site(dry_run=dry_run, offline=offline)

    elapsed = round(time.time() - start_time, 2)
    all_success = all([res_tencent, res_meituan, res_deepseek, res_boss, res_build])

    print(f"\n==========================================")
    print(f"[pipeline] 本轮调度完成! 耗时 {elapsed} 秒")
    print(f"  腾讯采集: {'成功' if res_tencent else '失败'}")
    print(f"  美团采集: {'成功' if res_meituan else '失败'}")
    print(f"  DeepSeek同步: {'成功' if res_deepseek else '失败'}")
    print(f"  Boss直聘检查: {'成功/已按规则安全跳过' if res_boss else '失败'}")
    print(f"  控制台静态分片重构: {'成功' if res_build else '失败'}")
    print(f"  全流程状态: {'全部成功' if all_success else '存在失败项'}")
    print(f"==========================================\n")

    return {
        "success": all_success,
        "tencent": res_tencent,
        "meituan": res_meituan,
        "deepseek": res_deepseek,
        "boss": res_boss,
        "build": res_build,
        "elapsed": elapsed
    }


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", default=True, help="运行单次全量自动化流水线")
    parser.add_argument("--daemon", action="store_true", help="常驻循环调度模式")
    parser.add_argument("--interval-min", type=int, default=30, help="常驻调度轮询间隔（分钟）")
    parser.add_argument("--dry-run", action="store_true", help="只读演练模式，调用接口但不写主库")
    parser.add_argument("--offline", action="store_true", help="纯离线演练模式，零网络请求、不写库、不改文件")
    parser.add_argument("--enable-boss", action="store_true", help="显式开启 Boss 直聘采集（需具备平台授权）")
    parser.add_argument("--plan", type=Path, help="Explicit one-shot authorized plan; no plan means no source requests")
    parser.add_argument("--preflight", action="store_true", help="Read-only shared quota and source capability preview")
    parser.add_argument("--run-id", help="托管守护的运行UUID")
    parser.add_argument("--run-file", type=Path, help="托管守护身份账本")
    args = parser.parse_args()

    if args.preflight:
        from tools.request_policy import DEFAULT_BUDGET
        from tools.task_plan import SOURCE_CAPABILITIES
        print(json.dumps({'scheduler': DEFAULT_BUDGET.scheduler.snapshot(), 'sources': SOURCE_CAPABILITIES, 'requires_plan': True}, ensure_ascii=False))
        return
    if args.enable_boss:
        parser.error('BOSS automatic collection remains unapproved')
    if bool(args.run_id) != bool(args.run_file) or (args.run_id and not args.daemon):
        parser.error('--run-id 和 --run-file 必须一同用于 --daemon')
    if not args.offline:
        VAL_DIR.mkdir(parents=True, exist_ok=True)

    if args.daemon:
        try:
            from tools.daemon_runtime import file_lock, inspect_record, publish_record, OwnedJob, daemon_command, execution_lock
        except ModuleNotFoundError:
            from daemon_runtime import file_lock, inspect_record, publish_record, OwnedJob, daemon_command, execution_lock
        if args.enable_boss:
            parser.error('托管守护不支持开启未获平台许可的BOSS来源')
        if not 10 <= args.interval_min <= 1440:
            parser.error('守护间隔必须为10至1440分钟')
        if not args.run_id:
            cmd = daemon_command(ROOT, ROOT / '.daemon-run.json', str(uuid.uuid4()), args.interval_min, args.offline, args.dry_run)
            os.execv(sys.executable, cmd)
        args.run_file = args.run_file.resolve()
        if not args.offline and args.run_file != (ROOT / '.daemon-run.json').resolve():
            parser.error('非离线守护必须使用仓库固定账本')
        str(uuid.UUID(args.run_id))
        with ExitStack() as stack:
            stack.enter_context(file_lock(execution_lock(ROOT, args.run_file, args.offline)))
            stack.enter_context(file_lock(args.run_file.with_suffix('.lease.lock')))
            previous = inspect_record(args.run_file, ROOT)
            if previous['running'] or previous['blocked']:
                raise RuntimeError('既有守护账本未解除，拒绝覆盖或启动')
            stack.enter_context(OwnedJob(args.run_id))
            os.environ['AI_JOBS_MANAGED_RUN_ID'] = args.run_id
            publish_record(args.run_file, ROOT, args.run_id, args.interval_min, args.offline, args.dry_run, now())
            print(f"[daemon] 身份已确认并写入账本 (PID: {os.getpid()})", flush=True)
            identity_only = os.environ.get('AI_JOBS_TESTING') == '1' and os.environ.get('AI_JOBS_DAEMON_IDENTITY_ONLY') == '1' and args.offline
            while True:
                try:
                    if not identity_only:
                        result = run_full_pipeline(dry_run=args.dry_run, enable_boss=args.enable_boss, offline=args.offline, plan_file=args.plan)
                        if result.get('status') == 'blocked' or args.plan:
                            print(json.dumps(result, ensure_ascii=False), flush=True)
                            break
                except Exception as e:
                    print(f"[daemon] 管道运行异常: {e}", flush=True)
                time.sleep(args.interval_min * 60)
    else:
        try:
            from tools.daemon_runtime import file_lock, execution_lock, OwnedJob
        except ModuleNotFoundError:
            from daemon_runtime import file_lock, execution_lock, OwnedJob
        run_id = str(uuid.uuid4())
        with file_lock(execution_lock(ROOT, ROOT / '.daemon-run.json', args.offline)), (OwnedJob(run_id) if os.name == 'nt' else nullcontext()):
            if os.name == 'nt':
                os.environ['AI_JOBS_MANAGED_RUN_ID'] = run_id
            res = run_full_pipeline(dry_run=args.dry_run, enable_boss=args.enable_boss, offline=args.offline, plan_file=args.plan)
            print(json.dumps(res, ensure_ascii=False), flush=True)
            if not res["success"]:
                sys.exit(1)


if __name__ == "__main__":
    main()
