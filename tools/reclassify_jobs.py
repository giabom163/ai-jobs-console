#!/usr/bin/env python
"""Recompute AI relevance for open jobs after dedup."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.collect_ats import classify_ai  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "ai_jobs.db"))
    args = parser.parse_args()
    con = sqlite3.connect(args.db)
    rows = con.execute("SELECT id,title FROM jobs WHERE status='open'").fetchall()
    with con:
        for jid, title in rows:
            tier, conf, _ = classify_ai(title)
            con.execute("UPDATE jobs SET ai_relevance=?,ai_confidence=? WHERE id=?", (tier, conf, jid))
    print({"updated": len(rows)})


if __name__ == "__main__":
    main()
