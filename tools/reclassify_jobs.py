#!/usr/bin/env python
"""按 AI 岗位词典重算相关度；可选把非 AI 岗位标记为 excluded（不物理删除）。

用法：
  python tools/reclassify_jobs.py --dry-run          # 只看分布，不改库
  python tools/reclassify_jobs.py                    # 只重写 ai_relevance/ai_confidence/ai_reason
  python tools/reclassify_jobs.py --exclude-none     # 非 AI 岗 status='excluded'（前端默认不展示）
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.ai_lexicon import classify  # noqa: E402


def ensure_column(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    if "ai_reason" not in cols:
        con.execute("ALTER TABLE jobs ADD COLUMN ai_reason TEXT")
        con.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "ai_jobs.db"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--exclude-none", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    if not args.dry_run:
        ensure_column(con)

    rows = con.execute(
        "SELECT id,title,description,location FROM jobs WHERE status='open'").fetchall()
    buckets = collections.Counter()
    samples = {"build": [], "adjacent": [], "none": []}
    updates = []
    for r in rows:
        tier, conf, reason = classify(r["title"], r["description"] or "")
        buckets[tier] += 1
        if len(samples[tier]) < 12:
            samples[tier].append({"title": r["title"], "reason": reason, "conf": round(conf, 2)})
        updates.append((tier, conf, reason, r["id"]))

    result = {"open": len(rows), "counts": dict(buckets),
              "ai_total": buckets["build"] + buckets["adjacent"],
              "samples": samples}
    if not args.dry_run:
        with con:
            con.executemany("UPDATE jobs SET ai_relevance=?,ai_confidence=?,ai_reason=? WHERE id=?",
                            updates)
            if args.exclude_none:
                cur = con.execute(
                    "UPDATE jobs SET status='excluded' WHERE status='open' AND ai_relevance='none'")
                result["excluded"] = cur.rowcount
        result["written"] = len(updates)

    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text if not args.json_out else text[:1200])


if __name__ == "__main__":
    main()
