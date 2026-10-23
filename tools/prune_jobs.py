#!/usr/bin/env python
"""清理：把「已过期/已关闭」和「长期没再见过」的岗位下线（不物理删除）。

判定顺序（只做只读页面检查）：
  1. 页面 liveness = expired（404/410、关闭横幅、截止日期已过）→ status='closed'
  2. 连续 stale-days 天没在采集里出现，且页面也读不到投递入口 → status='stale'
  3. uncertain 一律不动，只写 liveness 字段，留给人工/下次复检

用法：
  python tools/prune_jobs.py --limit 60 --dry-run
  python tools/prune_jobs.py --limit 200 --workers 4 --json-out out\\prune.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.liveness import check_job  # noqa: E402

COLS = {
    "liveness": "TEXT",
    "liveness_code": "TEXT",
    "liveness_reason": "TEXT",
    "liveness_checked_at": "TEXT",
}


def ensure_columns(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    for name, typ in COLS.items():
        if name not in have:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {typ}")
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "ai_jobs.db"))
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--stale-days", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    if not args.dry_run:
        ensure_columns(con)

    today = datetime.now(timezone.utc).date().isoformat()
    rows = con.execute(
        "SELECT id,title,url,last_seen,source,external_id FROM jobs WHERE status='open' "
        "AND ai_relevance IN ('build','adjacent') "
        "ORDER BY COALESCE(liveness_checked_at,'') ASC, last_seen DESC LIMIT ?",
        (args.limit,)).fetchall()

    def work(row):
        res = check_job(row["source"], row["external_id"], row["url"], today)
        res["id"] = row["id"]
        res["title"] = row["title"]
        res["last_seen"] = row["last_seen"]
        return res

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        results = list(pool.map(work, rows))

    stats = {"checked": len(results), "active": 0, "expired": 0, "uncertain": 0,
             "closed": 0, "stale": 0, "by_code": {}, "samples": {"expired": [], "uncertain": []}}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.stale_days)).isoformat()
    with con:
        for r in results:
            stats[r["verdict"]] = stats.get(r["verdict"], 0) + 1
            stats["by_code"][r["code"]] = stats["by_code"].get(r["code"], 0) + 1
            if r["verdict"] == "expired" and len(stats["samples"]["expired"]) < 10:
                stats["samples"]["expired"].append({"title": r["title"], "code": r["code"],
                                                    "reason": r["reason"]})
            if r["verdict"] == "uncertain" and len(stats["samples"]["uncertain"]) < 5:
                stats["samples"]["uncertain"].append({"title": r["title"], "code": r["code"],
                                                      "reason": r["reason"]})
            if args.dry_run:
                continue
            con.execute(
                "UPDATE jobs SET liveness=?, liveness_code=?, liveness_reason=?, "
                "liveness_checked_at=?, http_status=COALESCE(?, http_status), "
                "final_url=COALESCE(?, final_url) WHERE id=?",
                (r["verdict"], r["code"], r["reason"], datetime.now(timezone.utc).isoformat(),
                 r["http_status"], r["final_url"], r["id"]))
            if r["verdict"] == "expired":
                con.execute("UPDATE jobs SET status='closed' WHERE id=? AND status='open'", (r["id"],))
                con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,note) "
                            "VALUES(?,?,?,?)",
                            (r["id"], "closed", datetime.now(timezone.utc).isoformat(),
                             f"liveness:{r['code']} {r['reason']}"))
                stats["closed"] += 1
            elif r["verdict"] == "uncertain" and (r["last_seen"] or "") < cutoff:
                con.execute("UPDATE jobs SET status='stale' WHERE id=? AND status='open'", (r["id"],))
                stats["stale"] += 1

    text = json.dumps(stats, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    con.close()


if __name__ == "__main__":
    main()

