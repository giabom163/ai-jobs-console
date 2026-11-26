"""Descriptive, source-dated trend counts for mainland programming-job records."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from .job_geography import is_domestic_only
except ImportError:
    from job_geography import is_domestic_only

try:
    TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    TZ = timezone(timedelta(hours=8))
DATE_FLOOR = date(2026, 6, 1)
PROGRAMMING_TITLE = re.compile(
    r"软件|程序|编程|开发工程师|前端|后端|客户端|移动端|全栈|测试开发|"
    r"software|developer|programmer|frontend|backend|full[ -]?stack|mobile engineer",
    re.I,
)
ENTRY_LEVEL = re.compile(r"校招|应届|毕业生|实习|初级|junior|new[ -]?grad|graduate|entry[ -]?level|0\s*[-至到]\s*1\s*年", re.I)
EXPERIENCE_REQUIRED = re.compile(r"\d+\s*[-至到]\s*\d+\s*年经验|\d+\s*年以上经验|工作经验\s*[:：]?\s*\d+|senior|高级工程师", re.I)


def _beijing_day(value):
    if not value:
        return None
    raw = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(TZ).date()
    except (ValueError, TypeError):
        return None


def _period_counts(rows, start, end, today):
    totals = {"programming_records": 0, "entry_level_records": 0,
              "experience_required_records": 0, "experience_unknown_records": 0,
              "ai_related_records": 0, "companies": set(), "sources": set()}
    for row in rows:
        if not is_domestic_only(row["location"] or ""):
            continue
        source = str(row["source"] or "")
        if not source.startswith("official:"):
            continue
        if row["status"] not in {"open", "review", "excluded"}:
            continue
        deadline = _beijing_day(row["application_deadline"])
        if deadline and deadline < today:
            continue
        if source == "official:meituan":
            posted = _beijing_day(row["source_updated_at"])
        else:
            posted = next((_beijing_day(row[key]) for key in ("source_updated_at", "published_at", "posted_at")
                           if _beijing_day(row[key]) is not None), None)
        if posted is None or posted < DATE_FLOOR or not (start <= posted < end):
            continue
        title = str(row["title"] or "")
        if not PROGRAMMING_TITLE.search(title):
            continue
        totals["programming_records"] += 1
        totals["companies"].add(row["company_id"])
        totals["sources"].add(source)
        if row["ai_relevance"] in {"build", "adjacent"}:
            totals["ai_related_records"] += 1
        evidence = " ".join((title, str(row["seniority"] or ""), str(row["description"] or "")[:1600]))
        if ENTRY_LEVEL.search(evidence):
            totals["entry_level_records"] += 1
        elif EXPERIENCE_REQUIRED.search(evidence):
            totals["experience_required_records"] += 1
        else:
            totals["experience_unknown_records"] += 1
    totals["companies"] = len(totals["companies"])
    totals["sources"] = len(totals["sources"])
    totals["entry_level_share_percent"] = round(
        totals["entry_level_records"] / totals["programming_records"] * 100, 1
    ) if totals["programming_records"] else None
    return totals


def programming_job_trend(con, *, today=None, days=30):
    """Compare source-dated, currently retained public programming-role records.

    This is a coverage-bound posting proxy, not a count of hires, applicants,
    vacancies across the whole market, or proof that AI caused a change.
    """
    if type(days) is not int or days < 1 or days > 90:
        raise ValueError("trend window must be between 1 and 90 days")
    today = today or datetime.now(TZ).date()
    recent_start = today - timedelta(days=days - 1)
    previous_start = today - timedelta(days=days * 2 - 1)
    recent_end = today + timedelta(days=1)
    rows = con.execute(
        "SELECT j.title,j.seniority,j.description,j.source,j.source_updated_at,j.published_at,"
        "j.posted_at,j.location,j.status,j.application_deadline,j.ai_relevance,j.company_id "
        "FROM jobs j WHERE date(j.first_seen,'+8 hours') >= ?",
        (DATE_FLOOR.isoformat(),),
    ).fetchall()
    previous = _period_counts(rows, previous_start, recent_start, today)
    recent = _period_counts(rows, recent_start, recent_end, today)
    delta = recent["programming_records"] - previous["programming_records"]
    percent = round(delta / previous["programming_records"] * 100, 1) if previous["programming_records"] else None
    has_baseline = previous["programming_records"] > 0 and recent["programming_records"] > 0
    has_coverage = min(previous["sources"], recent["sources"]) >= 2 and min(previous["companies"], recent["companies"]) >= 2
    return {
        "status": "descriptive" if has_baseline and has_coverage else "limited_coverage" if has_baseline else "insufficient_baseline",
        "window_days": days,
        "date_basis": "source_updated_at/published_at/posted_at, Beijing time",
        "recent": {"since": recent_start.isoformat(), "until_exclusive": recent_end.isoformat(), **recent},
        "previous": {"since": previous_start.isoformat(), "until_exclusive": recent_start.isoformat(), **previous},
        "change": {"programming_records": delta, "percent": percent},
        "note": "仅统计数据库内有来源日期的中国大陆官方招聘页编程类岗位记录；应届/初级按标题与岗位原文关键词识别，未识别经验要求单列未知。每个对比窗口至少覆盖两家单位和两个官方来源，才标为可描述趋势；不满足时显示覆盖有限。来源覆盖、招聘状态和历史记录完整性会影响比较。岗位记录变化不等于录用人数或全市场需求变化，也不能单独证明由 AI 导致。",
    }
