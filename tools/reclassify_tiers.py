"""重算已入库记录的 relevance_tier / conflict_score。"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import relevance  # noqa: E402
import storage  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(ROOT, "sentiment.db"))
    args = parser.parse_args()
    db = storage.connect(args.db)
    rows = db.execute("SELECT id,title,summary,coalesce(views,0) views FROM records").fetchall()
    counts = {"conflict": 0, "issue": 0, "unrelated": 0}
    with db:
        for r in rows:
            tier = relevance.classify_tier(r["title"], r["summary"] or "")
            score = relevance.conflict_score(r["title"], r["summary"] or "", r["views"])
            db.execute("UPDATE records SET relevance_tier=?,conflict_score=? WHERE id=?",
                       (tier, score, r["id"]))
            counts[tier] += 1
    print({"total": len(rows), **counts})


if __name__ == "__main__":
    main()
