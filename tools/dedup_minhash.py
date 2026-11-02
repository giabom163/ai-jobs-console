#!/usr/bin/env python
"""Second-pass near-duplicate merge for open jobs (bottom-k MinHash + exact Jaccard).

为什么不用 datasketch：本项目运行时是 Python 标准库（无 numpy），装 datasketch 会带
numpy 原生 wheel。这里按 datasketch 的 MinHash 思路（MIT）实现 bottom-k 签名，无外部
依赖；算法来源记在 docs/复用清单.md。

两段：
  A. 同公司 + 归一化标题 + 同城市：同 URL 或描述相似度 >= --same-title-threshold 合并
  B. 同公司内跨标题（同城市/同 URL）：签名估计过候选线后算精确 Jaccard，
     标题不同时要求 >= --cross-title-threshold（默认 0.90，防模板 JD 误合并）

用法：
  python tools/dedup_minhash.py --dry-run
  python tools/dedup_minhash.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.dedup_jobs import norm_city, norm_title, priority  # noqa: E402

DB = ROOT / "ai_jobs.db"
K = 64  # bottom-k 签名大小


def shingles(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return set()
    grams = {text[i:i + 4] for i in range(max(0, len(text) - 3))}
    words = set(re.findall(r"[a-z0-9+#.]{2,}", text))
    return grams | words


def fingerprint(text: str) -> set[int]:
    hs = {int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")
          for s in shingles(text)}
    return set(sorted(hs)[:K])


def estimate(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    union = sorted(a | b)[:K]
    if not union:
        return 0.0
    return sum(1 for h in union if h in a and h in b) / len(union)


def exact_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def parens(value: str) -> set[str]:
    parts = re.findall(r"[（(]([^）)]*)[）)]", unicodedata.normalize("NFKC", value or ""))
    return {re.sub(r"\s+", "", p).lower() for p in parts}

def tight_title(value: str) -> str:
    """只折叠空白与全半角，保留括号里的方向/团队，避免把不同子岗位当成同标题。"""
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[\s]+", "", value)
    return value.strip()

def cities(value: str) -> set[str]:
    parts = re.split(r"[/、,，\-—|]", unicodedata.normalize("NFKC", value or ""))
    return {norm_city(p) for p in parts if norm_city(p)}


def merge(*args, **kwargs):
    raise RuntimeError('Automatic merge disabled; use apply_reviewed_merges with a reviewed manifest')


def main():
    try:
        from .dedup_candidates import main as preview
    except ImportError:
        from dedup_candidates import main as preview
    return preview()


if __name__ == "__main__":
    main()




