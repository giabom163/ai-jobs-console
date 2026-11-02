#!/usr/bin/env python
"""采集腾讯招聘公开岗位（免登录官方 REST 接口）。

口径与安全约束：
  1) 前置严格限中国大陆境内：通过 countryId=1 + area=cn 与白名单城市参数，并在本地双重校验 is_domestic_only。
  2) 受 RequestBudget 与 SharedScheduler 严格节流（单次请求间隔 >= 20 秒，轮次预算上限）。
  3) 写入 ai_jobs.db 时接入 ingestion_contract 批量事务与 source_runs 账本。

示例：
  python tools/collect_tencent.py --keywords "大模型" --limit 3 --dry-run
  python tools/collect_tencent.py --keywords "大模型" --limit 3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
sys.path.insert(0, str(ROOT))

from tools.ai_lexicon import classify  # noqa: E402
from tools.job_geography import is_domestic_only  # noqa: E402
from tools.job_fields import ensure_columns as ensure_source_columns, save_source_fields  # noqa: E402
from tools.request_policy import RequestBudget, DEFAULT_BUDGET, TENCENT_DOMESTIC_CITY_IDS  # noqa: E402
from tools.ingestion_contract import standard_fields, validate_record, begin_batch, finish_batch, abort_batch, record_external_failure  # noqa: E402
from tools.source_runs import start_run, finish_run  # noqa: E402

API = "https://careers.tencent.com/tencentcareer/api/post/Query"
DETAIL_URL_TMPL = "https://careers.tencent.com/jobdesc.html?postId={postId}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
HARD_FLOOR = "2026-06-01"
BEIJING_TZ = timezone(timedelta(hours=8))
TENCENT_COMPANY_ID = "company-53874092e000069bfeda101b"


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_tencent_date(val: str | None) -> str | None:
    if not val:
        return None
    val = val.strip()
    m = re.match(r'(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})', val)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T00:00:00+08:00"
    return None


def beijing_date(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(BEIJING_TZ).date().isoformat()
    except (TypeError, ValueError):
        return None


def fetch(page=1, size=10, keyword="大模型", city_id=None, budget=None):
    budget = budget or DEFAULT_BUDGET
    ts = int(time.time() * 1000)
    city_param = city_id if city_id else "1,2,3,9"
    params = [
        ("timestamp", str(ts)),
        ("countryId", "1"),
        ("cityId", city_param),
        ("bgIds", ""),
        ("productId", ""),
        ("categoryId", ""),
        ("parentCategoryId", ""),
        ("attrId", ""),
        ("keyword", keyword),
        ("pageIndex", str(page)),
        ("pageSize", str(size)),
        ("language", "zh-cn"),
        ("area", "cn"),
    ]
    query_str = urllib.parse.urlencode(params)
    url = f"{API}?{query_str}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": "https://careers.tencent.com/search.html",
            "Accept": "application/json, text/plain, */*",
        },
    )
    with budget.open(req, timeout=30, source_id="official:tencent", operation="list") as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("Code") != 200:
        budget.stop(url, "api_refused")
        raise RuntimeError(f"Tencent API refused request: Code={payload.get('Code')}")
    return payload.get("Data") or {}


def upsert(con, item, cid, since: str | None = None, observed_at=None):
    observed_at = observed_at or now()
    ext = str(item.get("PostId") or "")
    title = (item.get("RecruitPostName") or "").strip()
    if not ext or not title:
        return None
    location = (item.get("LocationName") or "").strip()
    if not is_domestic_only(location):
        return {"skipped_non_domestic": True}
    updated_at = parse_tencent_date(item.get("LastUpdateTime"))
    posted_at = updated_at
    published_day = beijing_date(posted_at)
    if since and not published_day:
        return {"skipped_source_date_unknown": True}
    if since and published_day < since:
        return {"skipped_old": True, "posted_at": posted_at}
    source_update_day = beijing_date(updated_at)
    source_date_qualifies = bool(source_update_day and source_update_day >= HARD_FLOOR)

    desc = (item.get("Responsibility") or "").strip()
    bg_name = item.get("BGName") or ""
    cat_name = item.get("CategoryName") or ""
    dept = f"{bg_name}/{cat_name}".strip("/")

    url = item.get("PostURL") or DETAIL_URL_TMPL.format(postId=ext)
    content_hash = hashlib.sha256(f"{title}\n{location}\n{desc}".encode()).hexdigest()
    derived = standard_fields(title, desc, dept)
    tier, conf, reason = derived["ai_relevance"], derived["ai_confidence"], derived["ai_reason"]

    validate_record(dict(
        source="official:tencent",
        external_id=ext,
        title=title,
        location=location,
        first_seen=observed_at,
        last_seen=observed_at,
        url=url,
    ))

    existing = con.execute(
        "SELECT id, content_hash, status FROM jobs WHERE source='official:tencent' AND external_id=?",
        (ext,),
    ).fetchone()

    source_date_demoted = False
    if existing:
        con.execute(
            "UPDATE jobs SET title=?,location=?,description=?,ai_relevance=?,ai_confidence=?,"
            "ai_reason=?,last_seen=?,url=?,content_hash=?,posted_at=COALESCE(?,posted_at),"
            "updated_at=COALESCE(?,updated_at) WHERE id=?",
            (title, location, desc[:20000], tier, conf, reason, observed_at, url, content_hash,
             posted_at, updated_at, existing["id"]),
        )
        jid = existing["id"]
        added = False
        if tier in ("build", "adjacent") and not source_date_qualifies and existing["status"] == "open":
            source_date_demoted = con.execute(
                "UPDATE jobs SET status='verification_pending' WHERE id=? AND status='open'",
                (jid,),
            ).rowcount == 1
        if tier in ("build", "adjacent"):
            con.execute("UPDATE jobs SET status='verification_pending' WHERE id=? AND status='excluded'", (jid,))
        if existing["content_hash"] != content_hash:
            con.execute(
                "INSERT INTO job_changes(job_id,change_type,changed_at,old_hash,new_hash,note) VALUES(?,?,?,?,?,?)",
                (jid, "changed", observed_at, existing["content_hash"], content_hash, reason),
            )
    else:
        jid = "job-" + hashlib.sha256(f"tencent|{ext}".encode()).hexdigest()[:24]
        con.execute(
            "INSERT INTO jobs(id,source,external_id,company_id,title,location,description,"
            "ai_relevance,ai_confidence,ai_reason,first_seen,last_seen,status,url,content_hash,"
            "posted_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'verification_pending',?,?,?,?)",
            (jid, "official:tencent", ext, cid, title, location, desc[:20000], tier, conf,
             reason, observed_at, observed_at, url, content_hash, posted_at, updated_at),
        )
        con.execute(
            "INSERT INTO job_changes(job_id,change_type,changed_at,new_hash,note) VALUES(?,?,?,?,?)",
            (jid, "new", observed_at, content_hash, reason),
        )
        added = True

    con.execute(
        "UPDATE jobs SET normalized_title=?,status=CASE WHEN ai_relevance='none' AND status NOT IN ('merged','closed') THEN 'excluded' ELSE status END WHERE id=?",
        (derived["normalized_title"], jid),
    )
    con.execute(
        "INSERT INTO job_evidence(job_id,source,url,seen_at) VALUES(?,?,?,?) "
        "ON CONFLICT(job_id,source) DO UPDATE SET url=excluded.url,seen_at=excluded.seen_at",
        (jid, "official:tencent", url, observed_at),
    )
    save_source_fields(
        con, jid,
        published=posted_at,
        updated=updated_at,
        department=dept or None,
        source_status="listed",
        checked_at=observed_at,
    )

    item_json = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    date_evidence = {
        "source": "official:tencent",
        "external_id": ext,
        "observed_at": observed_at,
        "published_at": {"field": "LastUpdateTime", "raw": item.get("LastUpdateTime"), "parsed": posted_at},
        "source_updated_at": {"field": "LastUpdateTime", "raw": item.get("LastUpdateTime"), "parsed": updated_at},
        "item_sha256": hashlib.sha256(item_json.encode("utf-8")).hexdigest(),
        "freshness_cutoff": HARD_FLOOR,
        "freshness_result": "pass" if source_date_qualifies else "review",
    }
    con.execute(
        "UPDATE jobs SET date_evidence_json=? WHERE id=?",
        (json.dumps(date_evidence, ensure_ascii=False, sort_keys=True), jid),
    )
    return {"added": added, "jid": jid, "source_date_demoted": source_date_demoted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--keywords", default="大模型")
    parser.add_argument("--cities", default="1,2,3,9", help="Comma-separated Tencent city IDs (e.g. 1,2,3,9)")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--since", default=None)
    parser.add_argument("--ai-only", action="store_true", default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    budget = RequestBudget(limit=args.limit)
    stats = {
        "source": "official:tencent",
        "limit": args.limit,
        "seen": 0,
        "added": 0,
        "updated": 0,
        "source_date_demoted": 0,
        "errors": [],
    }

    # Verify cities parameter against whitelist
    for cid in args.cities.split(","):
        cid = cid.strip()
        if cid and cid not in TENCENT_DOMESTIC_CITY_IDS:
            raise ValueError(f"City ID {cid} is not in approved domestic city whitelist")

    run_id = None
    if not args.dry_run and Path(args.db).exists():
        run_id = start_run(args.db, "official:tencent", "腾讯招聘官方接口", "official", run_kind="collection")

    con = None
    batch = None
    request_start = budget.requests
    seen_ids = set()
    staged = []

    try:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        for kw in keywords:
            if stats["seen"] >= args.limit:
                break
            fetch_size = min(args.page_size, args.limit - stats["seen"])
            data = fetch(page=1, size=fetch_size, keyword=kw, city_id=args.cities, budget=budget)
            posts = data.get("Posts") or []
            for item in posts[:args.limit - stats["seen"]]:
                ext = str(item.get("PostId") or "")
                if not ext or ext in seen_ids:
                    continue
                seen_ids.add(ext)
                stats["seen"] += 1
                if args.ai_only:
                    desc = item.get("Responsibility") or ""
                    bg = item.get("BGName") or ""
                    cat = item.get("CategoryName") or ""
                    dept = f"{bg}/{cat}".strip("/")
                    sf = standard_fields(item.get("RecruitPostName") or "", desc, dept)
                    if sf["ai_relevance"] == "none":
                        stats["skipped_non_ai"] = stats.get("skipped_non_ai", 0) + 1
                        continue
                staged.append((item, now()))

        if not args.dry_run:
            con = sqlite3.connect("file:" + Path(args.db).resolve().as_posix() + "?mode=rw", uri=True)
            con.row_factory = sqlite3.Row
            ensure_source_columns(con)
            batch = begin_batch(con, "tencent_bounded_list")
            cid = TENCENT_COMPANY_ID
            con.execute(
                "INSERT OR IGNORE INTO companies(id,name,ats_type,created_at) VALUES(?,?,?,?)",
                (cid, "腾讯", "official", now()),
            )
            for item, observed_at in staged:
                result = upsert(con, item, cid, args.since or None, observed_at=observed_at)
                if result and result.get("skipped_source_date_unknown"):
                    stats["skipped_source_date_unknown"] = stats.get("skipped_source_date_unknown", 0) + 1
                if result and "added" in result:
                    stats["added" if result["added"] else "updated"] += 1
                    stats["source_date_demoted"] += int(result.get("source_date_demoted", False))

        if con:
            batch["request_events"] = budget.events
            stats["batch"] = finish_batch(con, batch)
            con.commit()

        if run_id:
            finish_run(
                args.db, run_id, "succeeded",
                request_count=budget.requests - request_start,
                records_seen=stats["seen"],
                records_added=stats["added"],
                records_updated=stats["updated"],
                details={"operation": "tencent_list"},
            )
    except BaseException as exc:
        if con and batch:
            batch["request_events"] = budget.events
            abort_batch(con, batch, exc)
        if batch is None:
            stats["failure_id"] = record_external_failure("tencent_bounded_list", exc, budget.events)
        if run_id:
            try:
                finish_run(
                    args.db, run_id, "failed",
                    request_count=budget.requests - request_start,
                    records_seen=stats["seen"],
                    error=exc,
                    details={"operation": "tencent_list", "ingestion_rolled_back": bool(batch)},
                )
            except ValueError:
                pass
        stats["errors"].append(type(exc).__name__)
        stats["error_message"] = str(exc)
        stats["rolled_back"] = bool(con)
    finally:
        if con:
            con.close()

    stats["requests"] = budget.requests
    stats["request_events"] = budget.events
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
