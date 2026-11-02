#!/usr/bin/env python
"""Daily ATS collector: pull public job boards, detect new/changed/closed.

Usage:
  python tools/collect_ats.py --db ai_jobs.db --config configs/ats_companies.json
  python tools/collect_ats.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "ai_employment.sql"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.request_policy import DEFAULT_BUDGET
from tools.job_geography import is_domestic_only
from tools.ingestion_contract import standard_fields, validate_record, begin_batch, finish_batch, abort_batch, record_external_failure
from tools.source_runs import start_run, finish_run, ats_source_key
from tools.job_fields import ensure_columns, save_source_fields

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

SKILL_TERMS = ("python", "pytorch", "tensorflow", "jax", "cuda", "kubernetes", "aws",
               "gcp", "azure", "sql", "spark", "ray", "transformers", "llm", "rag",
               "reinforcement learning", "distributed systems", "typescript", "go")


def now():
    return datetime.now(timezone.utc).isoformat()


def strip_html(value):
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def classify_ai(title, description="", department=""):
    """统一走 tools/ai_lexicon.classify（单一事实源），返回 (tier, conf, evidence)。"""
    fields = standard_fields(title, description, department)
    tier, conf, reason = fields['ai_relevance'], fields['ai_confidence'], fields['ai_reason']
    return tier, conf, [reason]


def extract_skills(title, description):
    text = f"{title or ''} {description or ''}".lower()
    return [s for s in SKILL_TERMS if s in text]


def fetch_json(url, source_id, timeout=25, retries=1):
    from tools.liveness import BOT_WALL,_first
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with DEFAULT_BUDGET.open(req, timeout=timeout, source_id=source_id, operation='list') as resp:
        raw=resp.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:raise ValueError('oversized ATS response')
        headers=getattr(resp,'headers',None)
        charset=headers.get_content_charset() if headers is not None and hasattr(headers,'get_content_charset') else None
        for encoding in ([charset] if charset else [])+['utf-8','gb18030']:
            try:
                text=raw.decode(encoding)
                break
            except (LookupError,UnicodeDecodeError):continue
        else:
            DEFAULT_BUDGET.stop(url,'undecodable_response')
            raise ValueError('ATS response encoding cannot be safely decoded')
        if '\x00' in text:
            DEFAULT_BUDGET.stop(url,'unreadable_encoding')
            raise ValueError('ATS response encoding is not usable')
        try:
            payload=json.loads(text)
        except ValueError:
            if _first(BOT_WALL,text):
                DEFAULT_BUDGET.stop(url,'bot_wall')
                raise RuntimeError('ATS source requires security verification')
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list):
            if _first(BOT_WALL,text):
                DEFAULT_BUDGET.stop(url,'bot_wall')
                raise RuntimeError('ATS source requires security verification')
            raise ValueError('invalid job list response')
        return payload


def load_jobs(company, completeness=None):
    kind, token = company["type"], company["token"]
    if kind == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        payload = fetch_json(url, f"ats:{kind}")
        meta = payload.get('meta') or payload.get('metadata') or {}
        if completeness is not None:
            total = meta.get('total')
            completeness['complete'] = isinstance(total, int) and total == len(payload['jobs']) and not payload.get('next') and not payload.get('next_page')
        for item in payload.get("jobs", []):
            yield {
                "external_id": str(item.get("id")),
                "title": item.get("title") or "",
                "location": (item.get("location") or {}).get("name") or "",
                "published_at": item.get("first_published"),
                "source_updated_at": item.get("updated_at"),
                "url": item.get("absolute_url"),
                "description": strip_html(item.get("content") or ""),
                "department": ", ".join(d.get("name", "") for d in item.get("departments", [])),
                "remote": None,
                "raw": item,
            }
    elif kind == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
        payload = fetch_json(url, f"ats:{kind}")
        meta = payload.get('meta') or payload.get('metadata') or {}
        if completeness is not None:
            total = meta.get('total')
            completeness['complete'] = isinstance(total, int) and total == len(payload['jobs']) and not payload.get('next') and not payload.get('next_page')
        for item in payload.get("jobs", []):
            yield {
                "external_id": item.get("id"),
                "title": item.get("title") or "",
                "location": item.get("location") or "",
                "published_at": item.get("publishedAt"),
                "source_updated_at": None,
                "url": item.get("jobUrl") or item.get("applyUrl"),
                "description": strip_html(item.get("descriptionPlain") or item.get("descriptionHtml") or ""),
                "department": item.get("department") or item.get("team") or "",
                "remote": item.get("isRemote"),
                "raw": item,
            }
    else:
        raise RuntimeError(f"unsupported company type: {kind}")


def connect(db_path):
    con = sqlite3.connect('file:'+Path(db_path).resolve().as_posix()+'?mode=rw', uri=True)
    con.row_factory = sqlite3.Row
    return con


def upsert_company(con, company):
    cid = "company-" + re.sub(r"[^a-z0-9]+", "-", company["name"].lower()).strip("-")
    con.execute(
        "INSERT INTO companies(id,name,ats_type,board_token,watchlist,domain,created_at) VALUES(?,?,?,?,1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name,ats_type=excluded.ats_type,"
        "board_token=excluded.board_token,domain=excluded.domain",
        (cid, company["name"], company["type"], company["token"], company.get("domain"), now()))
    return cid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3, help="Maximum records per run, 1..15; start with 3")
    parser.add_argument("--config", default=str(ROOT / "configs" / "ats_companies.json"))
    parser.add_argument("--db", default=str(ROOT / "ai_jobs.db"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", default="", help="comma-separated company names")
    parser.add_argument("--reclassify", action="store_true", help="只重算已有岗位的 AI 相关度")
    parser.add_argument("--json-out", default="", help="把统计写到 UTF-8 JSON 文件，避免控制台编码问题")
    parser.add_argument("--since", "--published-since", dest="since", default="", help="Explicit first-publication lower bound; unknown dates withheld")
    args = parser.parse_args()
    if not 1 <= args.limit <= 15:
        parser.error("limit must be 1..15")

    if args.reclassify:
        parser.error("Use the dedicated evidence-preserving reclassification workflow")

    since = args.since or None
    companies = json.loads(Path(args.config).read_text(encoding="utf-8"))["companies"]
    if args.only:
        wanted = {x.strip().lower() for x in args.only.split(",")}
        companies = [c for c in companies if c["name"].lower() in wanted]
    companies = [c for c in companies if c.get("enabled")]

    con = None
    batch = None
    run_entries = []
    stats = dict(companies=0, fetched=0, new=0, changed=0, closed=0, skipped_old=0,
                 skipped_non_domestic=0, skipped_source_date_unknown=0, failed=[])
    try:
        staged=[]
        processed=0
        today=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        for company in companies:
            if processed >= args.limit or stats['companies'] >= args.limit:
                break
            run_id = None
            request_start = DEFAULT_BUDGET.requests
            if not args.dry_run:
                run_id = start_run(args.db, ats_source_key(company), company['name'], company['type'])
            try:
                completeness={}
                jobs=list(load_jobs(company,completeness))
                observed_at=now()
            except Exception as error:
                if run_id:
                    finish_run(args.db, run_id, 'failed', request_count=DEFAULT_BUDGET.requests-request_start,
                               error=error, details={'operation':'ats_list'})
                failed_id=record_external_failure('ats_collect',error,DEFAULT_BUDGET.events)
                stats['failed'].append(dict(company=company['name'],error_type=type(error).__name__,failure_id=failed_id))
                continue
            stats['companies']+=1
            stats['fetched']+=len(jobs)
            remaining=args.limit-processed
            completeness['complete']=completeness.get('complete',False) and len(jobs)<=remaining
            selected=jobs[:remaining]
            for job in selected:job['_observed_at']=observed_at
            staged.append((company,selected,completeness,observed_at,run_id,request_start))
            if run_id:
                run_entries.append(dict(run_id=run_id, company=company['name'], request_start=request_start,
                                        records_seen=len(jobs), records_added=0, records_updated=0,
                                        complete=bool(completeness.get('complete'))))
            processed+=len(selected)
        if not args.dry_run:
            con=connect(args.db)
            batch=begin_batch(con,'ats_collect',hashlib.sha256(Path(args.config).read_bytes()).hexdigest())
        for company,jobs,completeness,capture_time,run_id,request_start in staged:
            if args.dry_run:
                continue
            cid = upsert_company(con, company)
            new_c = changed_c = closed_c = ai_c = 0
            seen = set()
            complete_snapshot = completeness.get('complete', False)
            for job in jobs:
                observed_at=job['_observed_at']
                ext = str(job["external_id"])
                seen.add(ext)
                if not is_domestic_only(job.get("location")):
                    stats["skipped_non_domestic"] += 1
                    continue
                validate_record(dict(source=f"ats:{company['type']}", external_id=ext,
                                     title=job['title'], location=job['location'],
                                     first_seen=observed_at, last_seen=observed_at, url=job['url'], company=company['name']))
                title = job["title"].strip()
                desc = job["description"][:20000]
                posted_at = (job.get("published_at") or "")[:10] or None
                if since and not posted_at:
                    stats['skipped_source_date_unknown']+=1
                    continue
                if since and posted_at and posted_at < since:
                    stats["skipped_old"] += 1
                    continue
                content_hash = hashlib.sha256(
                    f"{title}\n{job['location']}\n{desc}".encode("utf-8")).hexdigest()
                existing = con.execute(
                    "SELECT id,content_hash,status FROM jobs WHERE source=? AND external_id=?",
                    (f"ats:{company['type']}", ext)).fetchone()
                if existing and existing["content_hash"] == content_hash:
                    tier, confidence, evidence = classify_ai(title, desc, job.get('department') or '')
                    con.execute('UPDATE jobs SET ai_relevance=?,ai_confidence=?,ai_reason=? WHERE id=?',
                                (tier, confidence, ','.join(evidence), existing['id']))
                    save_source_fields(con, existing['id'], published=job.get('published_at'),
                                       updated=job.get('source_updated_at'), department=job.get('department'),
                                       source_status='listed', checked_at=observed_at)
                    con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (observed_at, existing["id"]))
                    con.execute("INSERT INTO job_snapshots(job_id,seen_at,status,content_hash,location,raw_json) "
                                "VALUES(?,?,?,?,?,?)",
                                (existing["id"], observed_at, con.execute("SELECT status FROM jobs WHERE id=?", (existing["id"],)).fetchone()[0], content_hash, job["location"],
                                 json.dumps(job["raw"], ensure_ascii=False)[:20000]))
                    con.execute("UPDATE jobs SET normalized_title=?,status=CASE WHEN ai_relevance='none' AND status NOT IN ('merged','closed') THEN 'excluded' ELSE status END WHERE id=?",
                                (standard_fields(title, desc)['normalized_title'], existing['id']))
                    continue

                tier, confidence, evidence = classify_ai(title, desc, job.get('department') or '')
                skills = extract_skills(title, desc)
                job_id = existing["id"] if existing else "job-" + hashlib.sha256(
                    f"{company['name']}|{ext}".encode()).hexdigest()[:24]
                if existing:
                    con.execute(
                        "UPDATE jobs SET title=?,location=?,description=?,skills_json=?,ai_relevance=?,"
                        "ai_confidence=?,last_seen=?,url=?,content_hash=?,"
                        "posted_at=COALESCE(?,posted_at),updated_at=? WHERE id=?",
                        (title, job["location"], desc, json.dumps(skills, ensure_ascii=False),
                         tier, confidence, observed_at, job["url"], content_hash,
                         posted_at, observed_at, job_id))
                    con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,old_hash,new_hash,note) "
                                "VALUES(?,?,?,?,?,?)",
                                (job_id, "changed", observed_at, existing["content_hash"], content_hash,
                                 ",".join(evidence[:3])))
                    stats["changed"] += 1
                    changed_c += 1
                else:
                    con.execute(
                        "INSERT INTO jobs(id,source,external_id,company_id,title,location,remote,description,"
                        "skills_json,ai_relevance,ai_confidence,first_seen,last_seen,status,url,content_hash,"
                        "posted_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'review',?,?,?,?)",
                        (job_id, f"ats:{company['type']}", ext, cid, title, job["location"],
                         job["remote"], desc, json.dumps(skills, ensure_ascii=False), tier, confidence,
                         observed_at, observed_at, job["url"], content_hash, posted_at, observed_at))
                    con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,new_hash,note) "
                                "VALUES(?,?,?,?,?)",
                                (job_id, "new", observed_at, content_hash, ",".join(evidence[:3])))
                    stats["new"] += 1
                    new_c += 1
                    if tier in ("build", "adjacent"):
                        ai_c += 1
                con.execute("UPDATE jobs SET normalized_title=?,status=CASE WHEN ai_relevance='none' AND status NOT IN ('merged','closed') THEN 'excluded' ELSE status END WHERE id=?",
                            (standard_fields(title, desc)['normalized_title'], job_id))
                save_source_fields(con, job_id, published=job.get('published_at'),
                                   updated=job.get('source_updated_at'), department=job.get('department'),
                                   source_status='listed', checked_at=observed_at)
                con.execute("UPDATE jobs SET ai_reason=? WHERE id=?", (','.join(evidence), job_id))
                con.execute("INSERT INTO job_snapshots(job_id,seen_at,status,content_hash,location,raw_json) "
                            "VALUES(?,?,?,?,?,?)",
                            (job_id, observed_at, con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0], content_hash, job["location"],
                             json.dumps(job["raw"], ensure_ascii=False)[:20000]))
                con.execute("INSERT INTO job_evidence(job_id,source,url,seen_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(job_id,source) DO UPDATE SET url=excluded.url,seen_at=excluded.seen_at",
                            (job_id, f"ats:{company['type']}", job["url"], observed_at))

            stale = con.execute(
                "SELECT id,external_id,content_hash FROM jobs WHERE company_id=? AND source=? AND status IN ('open','review')",
                (cid, f"ats:{company['type']}" )).fetchall()
            for row in stale if complete_snapshot else []:
                if str(row["external_id"]) in seen:
                    continue
                con.execute("UPDATE jobs SET liveness='uncertain',liveness_code='source_absent',"
                            "liveness_reason='完整来源列表本次缺席，未确认关闭',last_checked_at=? WHERE id=?", (capture_time, row["id"]))
                con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,old_hash) VALUES(?,?,?,?)",
                            (row["id"], "unseen", capture_time, row["content_hash"]))

            con.execute(
                "INSERT INTO job_daily(day,company_id,new_jobs,closed_jobs,changed_jobs,ai_jobs) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(day,company_id) DO UPDATE SET "
                "new_jobs=new_jobs+excluded.new_jobs,closed_jobs=closed_jobs+excluded.closed_jobs,"
                "changed_jobs=changed_jobs+excluded.changed_jobs,ai_jobs=ai_jobs+excluded.ai_jobs",
                (today, cid, new_c, closed_c, changed_c, ai_c))
            for entry in run_entries:
                if entry['run_id'] == run_id:
                    entry['records_added'] = new_c
                    entry['records_updated'] = changed_c
                    break

        if con:
            batch['source_failures'] = stats['failed']
            batch['request_events'] = DEFAULT_BUDGET.events
            stats['batch'] = finish_batch(con, batch)
            con.commit()
        for entry in run_entries:
            finish_run(args.db, entry['run_id'], 'succeeded',
                       request_count=DEFAULT_BUDGET.requests-entry['request_start'],
                       records_seen=entry['records_seen'], records_added=entry['records_added'],
                       records_updated=entry['records_updated'],
                       details={'operation':'ats_list','complete_snapshot':entry['complete']})
    except BaseException as error:
        if con and batch:
            batch['request_events'] = DEFAULT_BUDGET.events
            abort_batch(con, batch, error)
        for entry in run_entries:
            try:
                finish_run(args.db, entry['run_id'], 'failed',
                           request_count=DEFAULT_BUDGET.requests-entry['request_start'],
                           records_seen=entry['records_seen'], error=error,
                           details={'operation':'ats_list','ingestion_rolled_back':bool(batch)})
            except ValueError:
                pass
        raise
    finally:
        if con:
            con.close()
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": not stats["failed"], "companies": stats["companies"], "fetched": stats["fetched"],
                          "new": stats["new"], "closed": stats["closed"],
                          "failed": len(stats["failed"])}, ensure_ascii=False))
    else:
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

