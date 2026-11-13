#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 就业与舆情线索定向抓取与待审入库工具。
落实“定向实体 + 时间窗 + 原始出处溯源”分步流：
1. 实体与动词分离（AI公司/团队 + 调整/招聘/变动 + 2026）；
2. 两类时间严格切分（抓取时间 != 发布时间 != 事件时间）；
3. 刚性门禁：强制 review_status='pending' 入库，严禁直接 approved，杜绝假闭环；
4. 节流与安全门禁：支持 --limit N（默认 3）、--dry-run、同 host 间隔保护。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "ai_jobs.db"
TZ = timezone(timedelta(hours=8))

# 定向关注的境内核心 AI 实体与团队
TARGET_ENTITIES = [
    {"name": "月之暗面", "company_id": "moonshot", "keywords": ["月之暗面", "Kimi", "大模型"]},
    {"name": "智谱AI", "company_id": "zhipu", "keywords": ["智谱AI", "GLM", "大模型"]},
    {"name": "MiniMax", "company_id": "minimax", "keywords": ["MiniMax", "名之梦", "大模型"]},
    {"name": "百川智能", "company_id": "baichuan", "keywords": ["百川智能", "Baichuan"]},
    {"name": "字节跳动Seed团队", "company_id": "bytedance", "keywords": ["字节跳动", "Seed", "大模型"]},
    {"name": "腾讯AI与智能体", "company_id": "tencent", "keywords": ["腾讯", "混元", "智能体"]},
    {"name": "百度大模型", "company_id": "baidu", "keywords": ["百度", "文心大模型", "AI Infra"]},
    {"name": "阿里巴巴通义", "company_id": "alibaba", "keywords": ["阿里", "通义千问", "千问"]},
]

ACTION_KEYWORDS = ["招聘", "校招", "扩招", "调整", "裁员", "变动", "缩招"]


def build_candidate_queries() -> list[dict]:
    """生成定向实体 + 动作 + 2026 年份的精准搜索计划"""
    queries = []
    for ent in TARGET_ENTITIES:
        for act in ["招聘", "组织调整", "业务变动"]:
            queries.append({
                "entity": ent["name"],
                "company_id": ent["company_id"],
                "action": act,
                "query": f"{ent['name']} {act} 2026",
                "category": "hiring" if act in ["招聘", "校招", "扩招"] else "layoff" if act == "裁员" else "shrink"
            })
    return queries


def ingest_pending_signal(con: sqlite3.Connection, item: dict, dry_run: bool = False) -> bool:
    """旧样例写库入口已停用，等待真实搜索和证据契约实现。"""
    raise RuntimeError("舆情样例写库入口已停用；当前脚本没有真实搜索或来源回执。")


def main():
    parser = argparse.ArgumentParser(description="已停用的舆情样例写入入口；仅可离线列出查询词")
    parser.add_argument("--limit", type=int, default=3, help="兼容旧调用；不会触发抓取或写库")
    parser.add_argument("--dry-run", action="store_true", help="兼容旧调用；不会触发抓取或写库")
    parser.add_argument("--list-queries", action="store_true", help="只打印离线候选查询词，不联网、不写库")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    queries = build_candidate_queries()
    if args.list_queries:
        print(f"规划的定向检索矩阵共 {len(queries)} 条：")
        for q in queries[:10]:
            print(f"- 查询词: [{q['query']}] (实体: {q['entity']}, 类型: {q['category']})")
        print("...更多略")
        return
    parser.error("写库和联网抓取已停用；此脚本目前只支持 --list-queries。")


if __name__ == "__main__":
    main()
