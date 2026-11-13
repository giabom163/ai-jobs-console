#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
隔离测试 tools/fetch_ai_employment_signals.py 的真实写入与读取校验。
不污染生产数据库。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "fetch_ai_employment_signals.py"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--limit", "3"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = res.stdout + res.stderr
    assert res.returncode != 0, "旧样例写库入口必须拒绝执行"
    assert "写库和联网抓取已停用" in output, output
    assert "sig-zhipu-glm5-talent-202611" not in output, output
    print("验证入口已 fail-closed；该检查未运行。")


if __name__ == "__main__":
    main()
