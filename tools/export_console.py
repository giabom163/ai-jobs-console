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
    from .db_conn import connect_ro, read_snapshot
    from .employment_signals import count_signals, list_signals, storage_status
    from .source_health import source_health
    from .job_geography import is_domestic_only, is_mixed_location, is_unknown_location
    from .job_fields import register_read_view
    from .job_details import source_details, duplicate_records
    from .employment_events import build_daily_digest, event_summary, load_events
    from .employment_trends import programming_job_trend
    from .job_taxonomy import enrich_job, ROLES, EMPLOYMENT, CITIES, VERSION
else:
    from db_conn import connect_ro, read_snapshot
    from employment_signals import count_signals, list_signals, storage_status
    from source_health import source_health
    from job_geography import is_domestic_only, is_mixed_location, is_unknown_location
    from job_fields import register_read_view
    from job_details import source_details, duplicate_records
    from employment_events import build_daily_digest, event_summary, load_events
    from employment_trends import programming_job_trend
    from job_taxonomy import enrich_job, ROLES, EMPLOYMENT, CITIES, VERSION

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
AI_TIERS = "('build','adjacent')"
DATE_FLOOR = "2026-06-01"
TZ = timezone(timedelta(hours=8))


def connect() -> sqlite3.Connection:
    return connect_ro(DB, timeout=5.0, row_factory=True, with_udf=True, with_read_view=True, query_only=True)


def one(con, sql, args=()):
    return con.execute(sql, args).fetchone()[0]


def build(con, include_review: bool = True) -> dict:
    generated_at = datetime.now(TZ).isoformat(timespec="seconds")
    job_columns = {row[1] for row in con.execute("PRAGMA table_info(job_records)")}
    salary_evidence_column = "j.salary_evidence_json" if "salary_evidence_json" in job_columns else "NULL AS salary_evidence_json"
    source_date = "COALESCE(j.source_updated_at,j.published_at,j.posted_at)"
    source_date_ok = (
        f"COALESCE(((j.source='official:meituan' AND "
        f"date(j.source_updated_at,'+8 hours') >= '{DATE_FLOOR}') OR "
        f"(j.source!='official:meituan' AND "
        f"({source_date} IS NULL OR date({source_date},'+8 hours') >= '{DATE_FLOOR}'))),0)=1"
    )
    base = (
        f"is_domestic_only(j.location)=1 AND j.ai_relevance IN {AI_TIERS} "
        f"AND date(j.first_seen,'+8 hours') >= '{DATE_FLOOR}'"
    )
    win = (f"j.status IN ('open','review') AND {base} AND "
           "(j.application_deadline IS NULL OR date(j.application_deadline,'+8 hours') >= date('now','+8 hours'))")
    public_win = f"{win} AND {source_date_ok}"
    rows = con.execute(
        "SELECT j.id,j.title,c.name company,c.id company_id,j.location,j.ai_relevance,"
        "j.ai_reason,j.published_at,j.source_updated_at,j.updated_at AS meituan_legacy_updated_at,j.application_start,j.application_deadline,j.source_status,j.source_status_raw,j.department,j.requisition_id,j.external_id,j.last_checked_at,j.posted_at,j.first_seen,j.last_seen,j.last_verified_at,j.http_status,j.presence_status,j.presence_checked_at,j.source_response_sha256,"
        f"j.source,j.url,j.salary_min,j.salary_max,j.currency,{salary_evidence_column},"
        f"CASE WHEN {source_date_ok} THEN j.status ELSE 'review' END AS status,j.description,"
        "j.authenticity_score,j.authenticity_level,j.liveness,j.liveness_reason "
        "FROM job_records j JOIN companies c ON c.id=j.company_id "
        f"WHERE {win} ORDER BY j.first_seen DESC"
    ).fetchall()
    details, duplicates = source_details(con), duplicate_records(con)
    jobs = [enrich_job(dict(r), details.get(r['id'])) for r in rows]
    for job in jobs:
        job['duplicate_records'] = duplicates.get(job['id'], [])
        job.pop('description', None)
    if not include_review:
        jobs = [j for j in jobs if j.get('status') == 'open']

    def group(sql, args=()):
        return [dict(r) for r in con.execute(sql, args).fetchall()]

    levels = group(
        f"SELECT j.authenticity_level label,count(*) value FROM job_records j WHERE {public_win} "
        "GROUP BY j.authenticity_level ORDER BY value DESC"
    )
    top_companies = group(
        f"SELECT c.name label,count(*) value FROM job_records j JOIN companies c ON c.id=j.company_id "
        f"WHERE {public_win} GROUP BY c.name ORDER BY value DESC LIMIT 8"
    )
    trend = group(
        "SELECT date(c.changed_at,'+8 hours') day,count(DISTINCT CASE WHEN c.change_type='new' THEN c.job_id END) new_jobs,"
        "count(DISTINCT CASE WHEN c.change_type='closed' THEN c.job_id END) closed_jobs FROM job_changes c "
        f"JOIN job_records j ON j.id=c.job_id WHERE {public_win} "
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
            f"SELECT count(*) FROM job_records j WHERE {win} AND NOT {source_date_ok}",
        ),
        "source_date_verified_count": one(
            con,
            f"SELECT count(*) FROM job_records j WHERE {public_win}",
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
            f"AND {public_win}",
        ),
        "levels": levels,
        "top_companies": top_companies,
        "trend": trend,
    }
    tech_keywords = [
        "大模型", "Agent", "多模态", "PyTorch", "RAG", "具身智能", "RLHF",
        "Transformer", "DPO", "Megatron", "vLLM", "CUDA", "SGLang", "Triton", "DeepSpeed", "LangChain"
    ]
    tech_counts = {kw: 0 for kw in tech_keywords}
    for j in jobs:
        txt = f"{j.get('title', '')} {j.get('ai_reason', '')} {j.get('skills_json', '')}".lower()
        for kw in tech_keywords:
            if kw.lower() in txt:
                tech_counts[kw] += 1

    tech_ranking = [
        {"name": kw, "count": tech_counts[kw], "percentage": round(tech_counts[kw] / max(1, len(jobs)) * 100, 1)}
        for kw in sorted(tech_counts, key=lambda k: tech_counts[k], reverse=True)
        if tech_counts[kw] > 0
    ]

    city_counts = {}
    for j in jobs:
        loc = (j.get("location") or "其他").split("·")[0].split("/")[0].strip()
        for main_city in ("北京", "上海", "深圳", "杭州", "广州", "成都", "武汉", "南京"):
            if main_city in loc:
                loc = main_city
                break
        city_counts[loc] = city_counts.get(loc, 0) + 1
    top_cities = [
        {"city": c, "count": cnt, "percentage": round(cnt / max(1, len(jobs)) * 100, 1)}
        for c, cnt in sorted(city_counts.items(), key=lambda x: x[1], reverse=True)[:6]
    ]

    stats["tech_stack_ranking"] = tech_ranking
    stats["top_cities"] = top_cities
    signals = list_signals(con, review="approved", limit=200)
    signal_state=storage_status(con)
    approved_signal_count = count_signals(con, review="approved")
    pending_signal_count = count_signals(con, review="pending")
    events_data = load_events(con)
    valid_events = [e for e in events_data['events'] if not e.get('evidence_stale')]
    events_summary = event_summary(events_data['events'])
    public_events_data = dict(events_data, events=valid_events)
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
        "signals": approved_signal_count,
        "signal_candidates": pending_signal_count,
        "events": events_summary["total"],
        "stale_events": events_summary["stale"],
        "top_companies": top_companies[:5],
        "report_windows": {
            "jobs": f"{DATE_FLOOR} 至今（静态全量公开 open，按 first_seen）",
            "events": "全量已复核有效独立事件",
            "programming_trend": "固定近 30 天（北京时间）",
        },
        "daily_digest": build_daily_digest(
            report_date=generated_at[:10],
            stats={**stats, "open": sum(1 for j in jobs if j.get("status") == "open")},
            events_summary=events_summary,
            events_items=valid_events,
            approved_signals_count=approved_signal_count,
            pending_signals_count=pending_signal_count,
            coverage_note=events_data["coverage_note"],
        ),
        "quality_notes": [
            "2026-10-22 记录的 3,020 条新增岗位变化属于存量数据初始化批量导入，来源尚未逐条核实前标记为归因未知，不代表市场招聘峰值。",
            "job_daily 历史数据仅供参考，不作为市场长期真值；每日岗位新增按首次入库时间统计，与雇主实际发布时间分开。"
        ],
        "programming_trend": programming_job_trend(con),
        "note": "首次入库时间范围从 2026-06-01 起。美团旧记录可能使用历史兼容时间作收录筛查；只有官方 source_updated_at 才作为来源更新时间证据，缺失即显示未知。未映射招聘状态保持未知，历史 active 只表示当次链接观察。",
    }
    events = group("SELECT c.job_id,c.change_type,date(c.changed_at,'+8 hours') day,j.first_seen "
                    f"FROM job_changes c JOIN job_records j ON j.id=c.job_id WHERE {public_win} "
                   "AND date(c.changed_at,'+8 hours') >= date('now','+8 hours','-13 day')")
    return {"employment_events": public_events_data, "job_events": events, "meta": {"schema_version": 3, "taxonomy_version": VERSION,
                     "roles": ROLES, "employment_types": EMPLOYMENT, "cities": CITIES,
                      "date_basis": "first_seen",
                      "source_date_basis": "public listings require source date >= 2026-06-01 when present; Meituan requires official source_updated_at; older dates remain review-only",
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
        with read_snapshot(con):
            payload = build(con)
    finally:
        con.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {out} :: {len(payload['jobs'])} jobs, {out.stat().st_size/1024:.1f} KB")


if __name__ == "__main__":
    main()
