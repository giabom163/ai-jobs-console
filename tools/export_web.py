#!/usr/bin/env python
"""Export the public AI-jobs snapshot used by the blog page /jobs/.

Read-only against ai_jobs.db. Writes a single JSON file consumed at build
time by the Astro site (src/data/ai-jobs.json).

Usage:
    python tools/export_web.py --out "C:/path/to/blog/src/data/ai-jobs.json"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
AI_TIERS = "('build','adjacent')"
TZ = timezone(timedelta(hours=8))


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def one(con: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    return con.execute(sql, args).fetchone()[0]


DATE_FLOOR = "2026-06-01"  # 用户口径：只要今年 6 月以后发布的岗位


def build_payload(con: sqlite3.Connection) -> dict:
    where = (
        f"j.status='open' AND j.ai_relevance IN {AI_TIERS} "
        f"AND substr(j.posted_at,1,10) >= '{DATE_FLOOR}'"
    )
    items = [
        {
            "id": r["id"],
            "title": r["title"],
            "company": r["company"],
            "location": r["location"] or "",
            "type": r["ai_relevance"],
            "reason": r["ai_reason"] or "",
            "posted_at": (r["posted_at"] or "")[:10],
            "first_seen": (r["first_seen"] or "")[:10],
            "url": r["url"] or "",
            "liveness": r["liveness"] or "",
            "level": r["authenticity_level"] or "",
            "score": round(r["authenticity_score"] or 0),
        }
        for r in con.execute(
            "SELECT j.id,j.title,c.name company,j.location,j.ai_relevance,j.ai_reason,"
            "j.posted_at,j.first_seen,j.url,j.liveness,j.authenticity_level,j.authenticity_score "
            "FROM jobs j JOIN companies c ON c.id=j.company_id "
            f"WHERE {where} "
            "ORDER BY COALESCE(j.posted_at,'') DESC, j.first_seen DESC",
        )
    ]

    by_source = [
        {"name": r["name"], "count": r["n"]}
        for r in con.execute(
            "SELECT c.name, count(*) n FROM jobs j JOIN companies c ON c.id=j.company_id "
            f"WHERE {where} GROUP BY c.name ORDER BY n DESC",
        )
    ]
    by_city = [
        {"name": r["location"], "count": r["n"]}
        for r in con.execute(
            f"SELECT j.location, count(*) n FROM jobs j WHERE {where} AND j.location <> '' "
            "GROUP BY j.location ORDER BY n DESC LIMIT 10",
        )
    ]

    checked = con.execute(
        f"SELECT max(j.liveness_checked_at) v FROM jobs j WHERE {where}"
    ).fetchone()[0]
    collected = con.execute(
        f"SELECT max(j.last_seen) v FROM jobs j WHERE {where}"
    ).fetchone()[0]

    return {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "scope": "在招 · AI 研发与 AI 支撑",
        "note": "数据来自公开招聘页面；已验证仅代表链接当日可访问，不代表岗位真实或仍在招聘。",
        "updated_liveness": (checked or "")[:19].replace("T", " "),
        "collected_at": (collected or "")[:19].replace("T", " "),
        "totals": {
            "ai_open": len(items),
            "build": sum(1 for i in items if i["type"] == "build"),
            "adjacent": sum(1 for i in items if i["type"] == "adjacent"),
            "sources": len(by_source),
            "companies": len({i["company"] for i in items}),
            "liveness_ok": sum(1 for i in items if i["liveness"] == "active"),
            "liveness_checked": sum(1 for i in items if i["liveness"]),
            "new_7d": sum(
                1
                for i in items
                if i["first_seen"] >= (datetime.now(TZ) - timedelta(days=7)).date().isoformat()
            ),
        },
        "by_source": by_source,
        "by_city": by_city,
        "items": items,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="target JSON path")
    args = ap.parse_args()
    payload = build_payload(connect())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out} :: {len(payload['items'])} items, {out.stat().st_size/1024:.1f} KB")


if __name__ == "__main__":
    main()
