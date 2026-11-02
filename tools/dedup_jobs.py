#!/usr/bin/env python
"""First-pass dedup: same company + normalized title + city, keep newest.

Also merges exact same apply URL regardless of title.
Duplicates are kept with status='merged' and duplicate_of for audit;
they are excluded from the open-job UI and API by default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
STOP = set("岗位职责 任职要求 工作职责 职位描述 我们 负责 要求 优先 熟悉 具备 能力 经验 以及 相关 工作 进行 能够 良好 团队 业务".split())


def norm_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[\s\-_/|（）()【】\[\]·,，。:：;；!！?？]+", "", value)
    return value.strip()


def norm_city(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.split(r"[/、,，\-—]", value)[0]
    return value.replace("市", "").strip().lower()


def norm_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[（(][^）)]*[）)]", "", value)
    value = re.sub(r"[\s\-_/|·,，。:：;；!！?？]+", "", value)
    return value.strip()


def tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text or "").lower()
    out = set(re.findall(r"[a-z0-9+#.]{2,}", text))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        out.update(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
    return {t for t in out if t not in STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 1.0 if not ta and not tb else 0.0
    return len(ta & tb) / len(ta | tb)


def priority(source: str) -> int:
    if source.startswith("ats:") or source.startswith("official:"):
        return 3
    if source.startswith("aggregator"):
        return 2
    return 1


def main():
    try:
        from .dedup_candidates import main as preview
    except ImportError:
        from dedup_candidates import main as preview
    return preview()


def merge(*args, **kwargs):
    raise RuntimeError('Automatic merge disabled; use apply_reviewed_merges with a reviewed manifest')


if __name__ == "__main__":
    main()
