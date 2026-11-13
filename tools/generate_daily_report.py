#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 AI 岗位核查台的日报与周报深度总结。
支持直接输出到控制台或写入文件，支持 --days (默认 7) 和 --limit 3 最小验证。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "ai_jobs.db"
EVENTS_PATH = ROOT / "configs" / "employment_events.json"
TZ = timezone(timedelta(hours=8))


def generate_report(con: sqlite3.Connection, days: int = 7, sample_limit: int = 5) -> dict:
    raise RuntimeError("独立日报生成器已停用：时间窗、地域与事件证据口径尚未接入统一服务。")


def format_markdown(data: dict) -> str:
    raise RuntimeError("独立日报 Markdown 输出已停用：尚未接入统一数据口径。")


def main():
    parser = argparse.ArgumentParser(description="已停用的独立日报入口；等待统一口径接入")
    parser.parse_args()
    parser.error("该生成器已停用；请使用控制台当前统计接口，完整日报后端尚未接入。")


if __name__ == "__main__":
    main()
