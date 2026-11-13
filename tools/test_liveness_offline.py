#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tools/test_liveness_offline.py
三态存活判定的合成样例离线检查，仅覆盖脚本所列输入。
纯离线运行，零网络请求，零额度消耗。
"""
from __future__ import annotations

import sys
import os
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.liveness import classify

def test_liveness_model():
    print("--- 1. 测试三态存活判定模型 (Active / Expired / Uncertain) ---")
    today = date.today().isoformat()
    
    # 1.1 Active: 页面正常且可见投递按钮
    html_active = '<html><body><h1>大模型算法实习生</h1><button>立即投递</button></body></html>'
    v1, c1, r1 = classify(200, 'https://careers.example.com/job/101', 'https://careers.example.com/job/101', html_active, today)
    assert v1 == 'active', f"Expected active, got {v1}"
    print(f"  [PASS] 活跃岗位判定: verdict={v1}, code={c1}")
    
    # 1.2 Expired: 页面显示停止招聘/已下线
    html_closed = '<html><body><h1>该职位已停止招聘</h1><p>已下线</p></body></html>'
    v2, c2, r2 = classify(200, 'https://careers.example.com/job/102', 'https://careers.example.com/job/102', html_closed, today)
    assert v2 == 'expired', f"Expected expired, got {v2}"
    print(f"  [PASS] 下线文案判定: verdict={v2}, code={c2}")
    
    # 1.3 Expired: HTTP 404/410 状态码
    v3, c3, r3 = classify(404, 'https://careers.example.com/job/103', 'https://careers.example.com/job/103', '', today)
    assert v3 == 'expired', f"Expected expired, got {v3}"
    print(f"  [PASS] HTTP 404 判定: verdict={v3}, code={c3}")
    
    # 1.4 Uncertain: 验证码拦截 / 502 网关错误（绝不误杀）
    html_bot = '<html><body><h1>请完成安全验证</h1><div id="captcha"></div></body></html>'
    v4, c4, r4 = classify(200, 'https://careers.example.com/job/104', 'https://careers.example.com/job/104', html_bot, today)
    assert v4 == 'uncertain', f"Expected uncertain, got {v4}"
    print(f"  [PASS] 人机拦截判定: verdict={v4}, code={c4} (安全证据链保护)")






def main():
    print("==================================================")
    print(" AI 岗位核查台：三态存活判定离线样例检查 ")
    print("==================================================")
    test_liveness_model()
    print("==================================================")
    print(" 离线样例断言通过（仅覆盖所列存活场景） ")
    print("==================================================")

if __name__ == '__main__':
    main()
