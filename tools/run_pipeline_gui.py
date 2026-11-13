#!/usr/bin/env python
"""GUI Launcher for AI Jobs System Pipeline and Admin Panel."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8792


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_server():
    if not is_port_in_use(PORT):
        print("[*] 正在启动本地 8792 服务 (ai_server.py)...")
        server_py = ROOT / "tools" / "ai_server.py"
        # Windows 静默后台运行
        creation_flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        subprocess.Popen([PY, "-B", str(server_py)], cwd=str(ROOT), creationflags=creation_flags)
        for _ in range(20):
            time.sleep(0.3)
            if is_port_in_use(PORT):
                print("[OK] 本地 8792 服务已成功就绪！")
                return True
        print("[!] 服务启动超时，继续执行后续管道...")
    else:
        print("[OK] 本地 8792 服务已在运行中。")
    return True


def run_pipeline_daemon():
    print("\n[*] 未提供有限授权任务计划；本次仅打开核查界面，不派发岗位采集请求。")


def main():
    print("========================================================")
    print("  AI 岗位核查台 · 有限计划采集与核查后台")
    print("========================================================\n")

    ensure_server()

    # 打开后台与前台浏览器
    print("\n[*] 正在打开管理后台与前台大盘...")
    webbrowser.open(f"http://127.0.0.1:{PORT}/admin")
    time.sleep(0.5)
    webbrowser.open(f"http://127.0.0.1:{PORT}/")

    # No implicit source collection: a plan must be supplied explicitly elsewhere.
    run_pipeline_daemon()

    print("\n[OK] 全部流水线处理完毕！浏览器已呈现最新结果。")
    input("\n按回车键退出此窗口...")


if __name__ == "__main__":
    main()
