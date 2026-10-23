#!/usr/bin/env python
"""AI 岗位词典回归用例（stdlib assert，无 pytest）。

运行：python tools/test_lexicon.py
每次改 ai_lexicon.py 必须跑一遍；任何一条 FAIL 说明类型判定退化了。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.ai_lexicon import classify  # noqa: E402

CASES = [
    # (标题, 描述, 期望 tier)
    ("税务BP", "", "none"),
    ("算法工程师", "", "build"),
    ("大模型算法专家", "", "build"),
    ("AI Agent 开发工程师", "", "build"),
    ("Agent 开发工程师（全栈）", "", "build"),
    ("AI产品经理", "", "adjacent"),
    ("公司AI治理内控", "", "adjacent"),
    ("AI人才发展专家", "", "none"),
    ("机器人电子电气架构师", "负责具身智能机器人电子电气架构，熟悉多模态传感器", "none"),
    ("功能安全工程师", "具身智能机器人功能安全，控制算法", "none"),
    ("Asset Manager (Energy Storage) - Memphis", "", "none"),
    ("Civil Engineer, SpaceXAI Battery Storage", "", "none"),
    ("Storage Engineer", "", "none"),
    ("HR Business Partner", "", "none"),
    ("Chrome Extension Developer", "", "none"),
    ("Legal Counsel, AI Policy", "", "none"),
    ("AI Tutor - Croatian", "", "adjacent"),
    ("Applied AI Engineer", "", "build"),
    ("Machine Learning Engineer", "", "build"),
    ("Research Scientist, Alignment", "", "build"),
    ("RAG Engineer", "", "build"),
    ("AI Support Engineer - Tokyo", "", "adjacent"),
    ("无人车渠道销售负责人", "", "none"),
    ("销售数据分析（AI&奖金激励方向）", "", "none"),
]


def main() -> int:
    failed = 0
    for title, desc, expect in CASES:
        tier, conf, reason = classify(title, desc)
        ok = tier == expect
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {title[:46]:46s} -> {tier:8s} (expect {expect:8s}) [{reason}]")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
