#!/usr/bin/env python
"""Export a single static dataset for the standalone console UI.

The console originally called /api/* on tools/ai_server.py. For a static
host (Cloudflare Pages) we precompute the same shapes and let a small
fetch shim serve them client-side.

Usage:
    python tools/export_console.py --out <path>/console-data.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__:
    from .employment_signals import list_signals
else:
    from employment_signals import list_signals

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
AI_TIERS = "('build','adjacent')"
DATE_FLOOR = "2026-06-01"
TZ = timezone(timedelta(hours=8))


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def one(con, sql, args=()):
    return con.execute(sql, args).fetchone()[0]


def build(con) -> dict:
    generated_at = datetime.now(TZ).isoformat(timespec="seconds")
    base = (
        f"j.ai_relevance IN {AI_TIERS} "
        f"AND substr(j.posted_at,1,10) >= '{DATE_FLOOR}'"
    )
    win = f"j.status='open' AND {base}"
    rows = con.execute(
        "SELECT j.id,j.title,c.name company,c.id company_id,j.location,j.ai_relevance,"
        "j.ai_reason,j.posted_at,j.first_seen,j.last_seen,j.last_verified_at,j.http_status,"
        "j.source,j.url,j.salary_min,j.salary_max,j.currency,j.status,"
        "j.authenticity_score,j.authenticity_level,j.liveness,j.liveness_reason "
        "FROM jobs j JOIN companies c ON c.id=j.company_id "
        f"WHERE {win} ORDER BY COALESCE(j.posted_at,'') DESC, j.first_seen DESC"
    ).fetchall()
    jobs = [dict(r) for r in rows]

    def group(sql, args=()):
        return [dict(r) for r in con.execute(sql, args).fetchall()]

    levels = group(
        f"SELECT j.authenticity_level label,count(*) value FROM jobs j WHERE {win} "
        "GROUP BY j.authenticity_level ORDER BY value DESC"
    )
    top_companies = group(
        f"SELECT c.name label,count(*) value FROM jobs j JOIN companies c ON c.id=j.company_id "
        f"WHERE {win} GROUP BY c.name ORDER BY value DESC LIMIT 8"
    )
    trend = group(
        "SELECT date(c.changed_at,'+8 hours') day,count(DISTINCT CASE WHEN c.change_type='new' THEN c.job_id END) new_jobs,"
        "count(DISTINCT CASE WHEN c.change_type='closed' THEN c.job_id END) closed_jobs FROM job_changes c "
        f"JOIN jobs j ON j.id=c.job_id WHERE {base} "
        "AND date(c.changed_at,'+8 hours') BETWEEN date('now','+8 hours','-13 day') AND date('now','+8 hours') GROUP BY day ORDER BY day"
    )
    sources = group(
        "SELECT c.name,c.ats_type,count(j.id) jobs,"
        "sum(CASE WHEN j.authenticity_level='已验证' THEN 1 ELSE 0 END) verified,"
        "max(j.last_seen) last_seen FROM companies c "
        f"LEFT JOIN jobs j ON j.company_id=c.id AND {win} "
        "GROUP BY c.id ORDER BY jobs DESC, c.name"
    )

    ai_total = len(jobs)
    build_n = sum(1 for j in jobs if j["ai_relevance"] == "build")
    adjacent_n = ai_total - build_n
    verified = sum(1 for j in jobs if j["authenticity_level"] == "已验证")
    checked = sum(1 for j in jobs if j["liveness"])
    stats = {
        "window_since": DATE_FLOOR,
        "generated_at": generated_at,
        "ai_total": ai_total,
        "build": build_n,
        "adjacent": adjacent_n,
        "verified": verified,
        "liveness_checked": checked,
        "closed": one(con, "SELECT count(*) FROM jobs WHERE status='closed'"),
        "stale": one(con, "SELECT count(*) FROM jobs WHERE status='stale'"),
        "excluded": one(con, "SELECT count(*) FROM jobs WHERE status='excluded'"),
        "merged": one(con, "SELECT count(*) FROM jobs WHERE status='merged'"),
        "today_new": one(
            con,
            "SELECT count(DISTINCT c.job_id) FROM job_changes c JOIN jobs j ON j.id=c.job_id "
            "WHERE c.change_type='new' AND date(c.changed_at,'+8 hours')=date('now','+8 hours') "
            f"AND {base}",
        ),
        "levels": levels,
        "top_companies": top_companies,
        "trend": trend,
    }
    signals = list_signals(con, review="approved", limit=200)
    signal_candidates = list_signals(con, review="pending", limit=200)
    report = {
        "generated_at": generated_at,
        "total": ai_total,
        "build": build_n,
        "adjacent": adjacent_n,
        "excluded": stats["excluded"],
        "verified": verified,
        "pending": ai_total - verified,
        "today_new": stats["today_new"],
        "merged": stats["merged"],
        "companies": sum(1 for s in sources if s["jobs"]),
        "signals": len(signals),
        "signal_candidates": len(signal_candidates),
        "top_companies": top_companies[:5],
        "note": "在招岗位统计范围：2026-06-01 起发布的 AI 岗位；今日新增按北京时间统计窗口内入库事件。链接核验通过仅代表最近一次 URL 核验通过，不保证当前仍可投递；剔除、合并、关闭及长期未见为库内累计。",
    }
    return {"meta": {"schema_version": 1, "display_timezone": "Asia/Shanghai",
                     "supported_statuses": ["open"], "window_since": DATE_FLOOR,
                     "history_available": False, "signal_export_limit_per_review": 200}, "stats": stats, "jobs": jobs, "sources": sources, "report": report,
            "signals": signals, "signal_candidates": signal_candidates,
            "generated_at": report["generated_at"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    payload = build(connect())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {out} :: {len(payload['jobs'])} jobs, {out.stat().st_size/1024:.1f} KB")


if __name__ == "__main__":
    main()
