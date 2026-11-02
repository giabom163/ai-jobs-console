#!/usr/bin/env python
"""AI 岗位核查台 - 多源全自动化数据管道与守护调度系统。

核心职责：
  1) 自动化全源管道：串联美团官方、腾讯招聘官方、DeepSeek官方直招、Boss直聘平滑采集产物。
  2) 严格安全与节流门禁：每个网络端点严格遵循 SharedScheduler >= 20 秒间隔，前置中国境内校验。
  3) 事务闭环与自动建站：每次入库前创建物理备份，入库后自动执行 build_site.py 修剪废弃分片，刷新 8792 大盘。

用法：
  python tools/pipeline_daemon.py --once       # 运行一轮完整的自动化多源管道
  python tools/pipeline_daemon.py --daemon     # 启动常驻定时巡检守护进程
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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
PYTHON = sys.executable
VAL_DIR = Path.home() / ".codex" / "documents" / "ai-jobs-validation-20261102"


def now():
    return datetime.now(timezone.utc).isoformat()


def ensure_db_backup(tag="pipeline"):
    if not DB.exists():
        return None
    backup_name = f"_backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}.db"
    backup_path = ROOT / backup_name
    shutil.copyfile(DB, backup_path)
    return str(backup_path)


def run_cmd(args, timeout=120):
    cmd_str = " ".join(args)
    print(f"[pipeline] 执行: {cmd_str}")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        print(f"[pipeline] 报错 (退出码 {proc.returncode}):\n{proc.stderr}")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def step_tencent():
    print("\n=== [1/4] 启动腾讯招聘官方接口增量采集 ===")
    ensure_db_backup("pre-tencent")
    args = [PYTHON, "-B", str(ROOT / "tools" / "collect_tencent.py"), "--keywords", "大模型,算法", "--limit", "5"]
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] 腾讯采集结果: {stdout}")
    return code == 0


def step_meituan():
    print("\n=== [2/4] 启动美团官方接口增量采集 ===")
    ensure_db_backup("pre-meituan")
    args = [PYTHON, "-B", str(ROOT / "tools" / "collect_meituan.py"), "--keywords", "大模型,算法", "--limit", "5"]
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] 美团采集结果: {stdout}")
    return code == 0


def step_deepseek():
    print("\n=== [3/4] 同步 DeepSeek 官方直招岗位 ===")
    ensure_db_backup("pre-deepseek")
    args = [PYTHON, "-B", str(ROOT / "tools" / "collect_deepseek.py"), "--limit", "5"]
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] DeepSeek 同步结果: {stdout}")
    return code == 0


def step_boss():
    print("\n=== [4/4] 检查并导入 Boss直聘平滑采集批次 ===")
    boss_batch_file = VAL_DIR / "boss-continuous-batch.json"
    if not boss_batch_file.exists():
        print(f"[pipeline] 提示: 尚未检测到 {boss_batch_file}，跳过本轮 Boss 直聘导入")
        return True

    ensure_db_backup("pre-boss")
    args = [PYTHON, "-B", str(ROOT / "tools" / "ingest_browser_jobs.py"), "--input", str(boss_batch_file)]
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] Boss 导入结果: {stdout}")
    return code == 0


def step_rebuild_site():
    print("\n=== [系统同步] 重构静态分片与修剪废弃索引 ===")
    args = [PYTHON, "-B", str(ROOT / "tools" / "build_site.py"), "--prune-obsolete"]
    code, stdout, _ = run_cmd(args)
    print(f"[pipeline] 静态站重构结果: 退出码 {code}")
    return code == 0


def run_full_pipeline():
    start_time = time.time()
    print(f"\n==========================================")
    print(f"[pipeline] 自动化系统调度轮次开始: {now()}")
    print(f"==========================================")

    res_tencent = step_tencent()
    time.sleep(5)

    res_meituan = step_meituan()
    time.sleep(5)

    res_deepseek = step_deepseek()
    time.sleep(5)

    res_boss = step_boss()

    res_build = step_rebuild_site()

    elapsed = round(time.time() - start_time, 2)
    print(f"\n==========================================")
    print(f"[pipeline] 本轮调度完成! 耗时 {elapsed} 秒")
    print(f"  腾讯采集: {'成功' if res_tencent else '失败'}")
    print(f"  美团采集: {'成功' if res_meituan else '失败'}")
    print(f"  DeepSeek同步: {'成功' if res_deepseek else '失败'}")
    print(f"  Boss直聘检查: {'成功' if res_boss else '失败'}")
    print(f"  控制台静态分片重构: {'成功' if res_build else '失败'}")
    print(f"==========================================\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", default=True, help="运行单次全量自动化流水线")
    parser.add_argument("--daemon", action="store_true", help="常驻循环调度模式")
    parser.add_argument("--interval-min", type=int, default=30, help="常驻调度轮询间隔（分钟）")
    args = parser.parse_args()

    VAL_DIR.mkdir(parents=True, exist_ok=True)

    if args.daemon:
        print(f"[daemon] 自动化守护进程已启动，轮询间隔: {args.interval_min} 分钟")
        while True:
            try:
                run_full_pipeline()
            except Exception as e:
                print(f"[daemon] 管道运行异常: {e}")
            print(f"[daemon] 休眠 {args.interval_min} 分钟进入下一轮巡检...")
            time.sleep(args.interval_min * 60)
    else:
        run_full_pipeline()


if __name__ == "__main__":
    main()
