#!/usr/bin/env python
"""GUI Launcher for AI Jobs Console (Forefront)."""
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
        print("[*] 正在启动本地 8792 核心服务 (ai_server.py)...")
        server_py = ROOT / "tools" / "ai_server.py"
        creation_flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        subprocess.Popen([PY, "-B", str(server_py)], cwd=str(ROOT), creationflags=creation_flags)
        for _ in range(30):
            time.sleep(0.2)
            if is_port_in_use(PORT):
                print("[OK] 本地 8792 服务已成功就绪！")
                return True
        print("[!] 警告: 8792 服务启动超时，尝试继续打开浏览器...")
    else:
        print("[OK] 本地 8792 服务已在常驻运行中。")
    return True


def main():
    ensure_server()
    print("[*] 正在打开 AI 岗位核查台大盘...")
    webbrowser.open(f"http://127.0.0.1:{PORT}/")


if __name__ == "__main__":
    main()
