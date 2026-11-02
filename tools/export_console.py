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
    from .employment_signals import list_signals, storage_status
    from .source_health import source_health
    from .job_geography import is_domestic_only, is_mixed_location, is_unknown_location
    from .job_fields import register_read_view
    from .job_details import source_details, duplicate_records
    from .employment_events import load_events
    from .job_taxonomy import enrich_job, ROLES, EMPLOYMENT, CITIES, VERSION
else:
    from employment_signals import list_signals, storage_status
    from source_health import source_health
    from job_geography import is_domestic_only, is_mixed_location, is_unknown_location
    from job_fields import register_read_view
    from job_details import source_details, duplicate_records
    from employment_events import load_events
    from job_taxonomy import enrich_job, ROLES, EMPLOYMENT, CITIES, VERSION

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
AI_TIERS = "('build','adjacent')"
DATE_FLOOR = "2026-06-01"
TZ = timezone(timedelta(hours=8))


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.create_function("is_domestic_only", 1, is_domestic_only, deterministic=True)
    con.create_function("is_mixed_location", 1, is_mixed_location, deterministic=True)
    con.create_function("is_unknown_location", 1, is_unknown_location, deterministic=True)
    register_read_view(con)
    return con


def one(con, sql, args=()):
    return con.execute(sql, args).fetchone()[0]


def build(con) -> dict:
    generated_at = datetime.now(TZ).isoformat(timespec="seconds")
    base = (
        f"is_domestic_only(j.location)=1 AND j.ai_relevance IN {AI_TIERS} "
        f"AND date(j.first_seen,'+8 hours') >= '{DATE_FLOOR}' "
        f"AND (j.source!='official:meituan' OR "
        f"date(COALESCE(j.source_updated_at,j.updated_at),'+8 hours') >= '{DATE_FLOOR}')"
    )
    win = (f"j.status IN ('open','review') AND {base} AND "
           "(j.application_deadline IS NULL OR date(j.application_deadline,'+8 hours') >= date('now','+8 hours'))")
    rows = con.execute(
        "SELECT j.id,j.title,c.name company,c.id company_id,j.location,j.ai_relevance,"
        "j.ai_reason,j.published_at,j.source_updated_at,j.updated_at AS meituan_legacy_updated_at,j.application_start,j.application_deadline,j.source_status,j.source_status_raw,j.department,j.requisition_id,j.external_id,j.last_checked_at,j.posted_at,j.first_seen,j.last_seen,j.last_verified_at,j.http_status,j.presence_status,j.presence_checked_at,j.source_response_sha256,"
        "j.source,j.url,j.salary_min,j.salary_max,j.currency,j.status,j.description,"
        "j.authenticity_score,j.authenticity_level,j.liveness,j.liveness_reason "
        "FROM job_records j JOIN companies c ON c.id=j.company_id "
        f"WHERE {win} ORDER BY j.first_seen DESC"
    ).fetchall()
    details, duplicates = source_details(con), duplicate_records(con)
    jobs = [enrich_job(dict(r), details.get(r['id'])) for r in rows]
    for job in jobs:
        job['duplicate_records'] = duplicates.get(job['id'], [])
        job.pop('description', None)

    def group(sql, args=()):
        return [dict(r) for r in con.execute(sql, args).fetchall()]

    levels = group(
        f"SELECT j.authenticity_level label,count(*) value FROM job_records j WHERE {win} "
        "GROUP BY j.authenticity_level ORDER BY value DESC"
    )
    top_companies = group(
        f"SELECT c.name label,count(*) value FROM job_records j JOIN companies c ON c.id=j.company_id "
        f"WHERE {win} GROUP BY c.name ORDER BY value DESC LIMIT 8"
    )
    trend = group(
        "SELECT date(c.changed_at,'+8 hours') day,count(DISTINCT CASE WHEN c.change_type='new' THEN c.job_id END) new_jobs,"
        "count(DISTINCT CASE WHEN c.change_type='closed' THEN c.job_id END) closed_jobs FROM job_changes c "
        f"JOIN job_records j ON j.id=c.job_id WHERE {base} "
        "AND date(c.changed_at,'+8 hours') BETWEEN date('now','+8 hours','-13 day') AND date('now','+8 hours') GROUP BY day ORDER BY day"
    )
    sources = group(
        "SELECT c.id company_id,c.name,c.ats_type,count(j.id) jobs,"
        "sum(CASE WHEN j.authenticity_level='已验证' THEN 1 ELSE 0 END) verified,"
        "max(j.last_seen) last_seen FROM companies c "
        f"LEFT JOIN job_records j ON j.company_id=c.id AND {win} "
        "WHERE EXISTS (SELECT 1 FROM jobs local_job WHERE local_job.company_id=c.id AND is_domestic_only(local_job.location)=1) GROUP BY c.id ORDER BY jobs DESC, c.name"
    )

    ai_total = len(jobs)
    build_n = sum(1 for j in jobs if j["ai_relevance"] == "build")
    adjacent_n = ai_total - build_n
    verified = sum(1 for j in jobs if j["authenticity_level"] == "已验证")
    checked = sum(1 for j in jobs if j["liveness"])
    stats = {
        "window_since": DATE_FLOOR,
        "global_window_since": DATE_FLOOR,
        "generated_at": generated_at,
        "source_date_review_count": one(
            con,
            "SELECT count(*) FROM job_records WHERE source='official:meituan' "
            "AND ai_relevance IN ('build','adjacent') AND status IN ('open','review','verification_pending') "
            "AND (date(source_updated_at,'+8 hours') IS NULL "
            f"OR date(source_updated_at,'+8 hours') < '{DATE_FLOOR}')",
        ),
        "verification_pending_count": one(
            con,
            "SELECT count(*) FROM job_records WHERE status='verification_pending' "
            "AND ai_relevance IN ('build','adjacent') AND is_domestic_only(location)=1 "
            f"AND date(first_seen,'+8 hours') >= '{DATE_FLOOR}'",
        ),
        "meituan_mixed_location_count": one(
            con,
            "SELECT count(*) FROM job_records WHERE source='official:meituan' "
            "AND ai_relevance IN ('build','adjacent') AND status IN ('open','review','verification_pending') "
            f"AND date(first_seen,'+8 hours') >= '{DATE_FLOOR}' AND is_mixed_location(location)=1",
        ),
        "meituan_unknown_location_count": one(
            con,
            "SELECT count(*) FROM job_records WHERE source='official:meituan' "
            "AND ai_relevance IN ('build','adjacent') AND status IN ('open','review','verification_pending') "
            f"AND date(first_seen,'+8 hours') >= '{DATE_FLOOR}' AND is_unknown_location(location)=1",
        ),
        "meituan_pending_old_date_count": one(
            con,
            "SELECT count(*) FROM job_records WHERE source='official:meituan' "
            "AND ai_relevance IN ('build','adjacent') AND status='verification_pending' "
            "AND (date(source_updated_at,'+8 hours') IS NULL "
            f"OR date(source_updated_at,'+8 hours') < '{DATE_FLOOR}')",
        ),
        "meituan_pending_identity_count": one(
            con,
            "SELECT count(*) FROM job_records WHERE source='official:meituan' "
            "AND ai_relevance IN ('build','adjacent') AND status='verification_pending' "
            f"AND date(source_updated_at,'+8 hours') >= '{DATE_FLOOR}' "
            "AND (presence_status IS NULL OR presence_status!='confirmed' OR source_response_sha256 IS NULL)",
        ),
        "ai_total": ai_total,
        "build": build_n,
        "adjacent": adjacent_n,
        "verified": verified,
        "liveness_checked": checked,
        "official_identity_confirmed_count": sum(1 for j in jobs if j.get("status") == "open" and j.get("identity_evidence_status") == "confirmed"),
        "source_update_unknown_count": sum(1 for j in jobs if j.get("status") == "open" and not j.get("source_updated_at")),
        "hiring_status_unknown_count": sum(1 for j in jobs if j.get("status") == "open" and j.get("hiring_status") == "unknown"),
        "closed": one(con, "SELECT count(*) FROM job_records WHERE status='closed'"),
        "stale": one(con, "SELECT count(*) FROM job_records WHERE status='stale'"),
        "excluded": one(con, "SELECT count(*) FROM job_records WHERE status='excluded'"),
        "merged": one(con, "SELECT count(*) FROM job_records WHERE status='merged'"),
        "today_new": one(
            con,
            "SELECT count(DISTINCT c.job_id) FROM job_changes c JOIN job_records j ON j.id=c.job_id "
            "WHERE c.change_type='new' AND date(c.changed_at,'+8 hours')=date('now','+8 hours') "
            f"AND {base}",
        ),
        "levels": levels,
        "top_companies": top_companies,
        "trend": trend,
    }
    signals = list_signals(con, review="approved", limit=200)
    signal_state=storage_status(con)
    report = {
        "generated_at": generated_at,
        "total": ai_total,
        "source_date_review_count": stats["source_date_review_count"],
        "build": build_n,
        "adjacent": adjacent_n,
        "excluded": stats["excluded"],
        "verified": verified,
        "pending": ai_total - verified,
        "today_new": stats["today_new"],
        "merged": stats["merged"],
        "companies": sum(1 for s in sources if s["jobs"]),
        "signals": len(signals),
        "signal_candidates": signal_state['pending_count'],
        "top_companies": top_companies[:5],
        "note": "首次入库时间范围从 2026-06-01 起。美团旧记录可能使用历史兼容时间作收录筛查；只有官方 source_updated_at 才作为来源更新时间证据，缺失即显示未知。未映射招聘状态保持未知，历史 active 只表示当次链接观察。",
    }
    events = group("SELECT c.job_id,c.change_type,date(c.changed_at,'+8 hours') day,j.first_seen "
                   f"FROM job_changes c JOIN job_records j ON j.id=c.job_id WHERE {base} "
                   "AND date(c.changed_at,'+8 hours') >= date('now','+8 hours','-13 day')")
    return {"employment_events": load_events(), "job_events": events, "meta": {"schema_version": 3, "taxonomy_version": VERSION,
                     "roles": ROLES, "employment_types": EMPLOYMENT, "cities": CITIES,
                     "date_basis": "first_seen",
                     "source_date_basis": "first_seen defines the collection window; only official source_updated_at is displayed as source update evidence; legacy updated_at is historical screening data only",
                     "default_window_days": 30, "display_timezone": "Asia/Shanghai",
                     "supported_statuses": ["listed", "open", "review"],
                     "status_semantics": {"listed": "默认公开结果仅含 open；review 单独筛选", "open": "历史已收录，不等于来源当前确认在招", "review": "待复核来源记录，不计入默认公开总数", "verification_pending": "保留待核验，不计入默认公开总数"},
                     "source_status_semantics": "官方招聘状态只有存在来源定义的枚举映射时才给出；当前美团状态码未映射，显示未知。历史active只表示当次链接观察，不证明当前在招。",
                      "location_semantics": "默认公开仅纳入明确可判定为境内的地点；美团混合地点和无法判断地点保留在库并单列复核，不自动修改业务状态",
                     "meituan_source_date_policy": "source_updated_at is the official source update date; legacy updated_at may be used only for historical inclusion screening and is never presented as official freshness evidence; unknown source date remains unknown",
                     "count_semantics": "独立来源记录；跨来源岗位身份须复核，不等于招聘人数",
                     "window_since": DATE_FLOOR,
                     "history_available": False, "signal_export_limit_per_review": 200}, "stats": stats, "jobs": jobs, "sources": sources, "report": report,
            "signals": signals, "signal_candidates": [],
            "signal_storage": signal_state, "source_health": source_health(con),
            "generated_at": report["generated_at"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / 'site' / 'console-data.json')
    ap.add_argument("--limit", type=int, default=3, help="Maximum output artifacts (one JSON); no collection is performed")
    args = ap.parse_args()
    if args.limit < 1:
        ap.error('limit must be positive')
    con = connect()
    try:
        payload = build(con)
    finally:
        con.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {out} :: {len(payload['jobs'])} jobs, {out.stat().st_size/1024:.1f} KB")


if __name__ == "__main__":
    main()
