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
    from tools.ai_lexicon import classify
    tier, conf, reason = classify(title, description, department)
    return tier, conf, [reason]


def extract_skills(title, description):
    text = f"{title or ''} {description or ''}".lower()
    return [s for s in SKILL_TERMS if s in text]


def fetch_json(url, timeout=25, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url}: {last}")


def load_jobs(company):
    kind, token = company["type"], company["token"]
    if kind == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        payload = fetch_json(url)
        for item in payload.get("jobs", []):
            yield {
                "external_id": str(item.get("id")),
                "title": item.get("title") or "",
                "location": (item.get("location") or {}).get("name") or "",
                "published_at": item.get("first_published") or item.get("updated_at"),
                "url": item.get("absolute_url"),
                "description": strip_html(item.get("content") or ""),
                "department": ", ".join(d.get("name", "") for d in item.get("departments", [])),
                "remote": None,
                "raw": item,
            }
    elif kind == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
        payload = fetch_json(url)
        for item in payload.get("jobs", []):
            yield {
                "external_id": item.get("id"),
                "title": item.get("title") or "",
                "location": item.get("location") or "",
                "published_at": item.get("publishedAt"),
                "url": item.get("jobUrl") or item.get("applyUrl"),
                "description": strip_html(item.get("descriptionPlain") or item.get("descriptionHtml") or ""),
                "department": item.get("department") or item.get("team") or "",
                "remote": item.get("isRemote"),
                "raw": item,
            }
    else:
        raise RuntimeError(f"unsupported company type: {kind}")


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
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
    parser.add_argument("--config", default=str(ROOT / "configs" / "ats_companies.json"))
    parser.add_argument("--db", default=str(ROOT / "ai_jobs.db"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", default="", help="comma-separated company names")
    parser.add_argument("--reclassify", action="store_true", help="只重算已有岗位的 AI 相关度")
    parser.add_argument("--json-out", default="", help="把统计写到 UTF-8 JSON 文件，避免控制台编码问题")
    parser.add_argument("--since", default="", help="只收该日期之后发布的岗位；默认近 90 天且不早于 2026-06-01")
    args = parser.parse_args()

    if args.reclassify:
        con = connect(args.db)
        rows = con.execute("SELECT id,title FROM jobs").fetchall()
        with con:
            for r in rows:
                tier, conf, _ = classify_ai(r["title"])
                con.execute("UPDATE jobs SET ai_relevance=?,ai_confidence=? WHERE id=?",
                            (tier, conf, r["id"]))
        print(json.dumps({"reclassified": len(rows)}, ensure_ascii=False))
        return

    since = args.since or max("2026-06-01", (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=90)).date().isoformat())
    companies = json.loads(Path(args.config).read_text(encoding="utf-8"))["companies"]
    if args.only:
        wanted = {x.strip().lower() for x in args.only.split(",")}
        companies = [c for c in companies if c["name"].lower() in wanted]
    companies = [c for c in companies if c.get("enabled")]

    con = None if args.dry_run else connect(args.db)
    stats = {"companies": 0, "fetched": 0, "new": 0, "changed": 0, "closed": 0, "skipped_old": 0, "failed": []}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for company in companies:
        try:
            jobs = list(load_jobs(company))
        except Exception as exc:  # noqa: BLE001
            stats["failed"].append({"company": company["name"], "error": str(exc)})
            continue
        stats["companies"] += 1
        stats["fetched"] += len(jobs)
        if args.dry_run:
            print(json.dumps({"company": company["name"], "jobs": len(jobs)}, ensure_ascii=False))
            continue

        cid = upsert_company(con, company)
        new_c = changed_c = closed_c = ai_c = 0
        seen = set()
        for job in jobs:
            ext = str(job["external_id"])
            seen.add(ext)
            title = job["title"].strip()
            desc = job["description"][:20000]
            posted_at = (job.get("published_at") or "")[:10] or None
            if since and posted_at and posted_at < since:
                stats["skipped_old"] += 1
                continue
            content_hash = hashlib.sha256(
                f"{title}\n{job['location']}\n{desc}".encode("utf-8")).hexdigest()
            existing = con.execute(
                "SELECT id,content_hash,status FROM jobs WHERE source=? AND external_id=?",
                (f"ats:{company['type']}", ext)).fetchone()
            if existing and existing["content_hash"] == content_hash:
                con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (now(), existing["id"]))
                con.execute("INSERT INTO job_snapshots(job_id,seen_at,status,content_hash,location,raw_json) "
                            "VALUES(?,?,?,?,?,?)",
                            (existing["id"], now(), "open", content_hash, job["location"],
                             json.dumps(job["raw"], ensure_ascii=False)[:20000]))
                continue

            tier, confidence, evidence = classify_ai(title, desc)
            skills = extract_skills(title, desc)
            job_id = existing["id"] if existing else "job-" + hashlib.sha256(
                f"{company['name']}|{ext}".encode()).hexdigest()[:24]
            if existing:
                con.execute(
                    "UPDATE jobs SET title=?,location=?,description=?,skills_json=?,ai_relevance=?,"
                    "ai_confidence=?,last_seen=?,status='open',url=?,content_hash=?,"
                    "posted_at=COALESCE(?,posted_at),updated_at=? WHERE id=?",
                    (title, job["location"], desc, json.dumps(skills, ensure_ascii=False),
                     tier, confidence, now(), job["url"], content_hash,
                     posted_at, now(), job_id))
                con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,old_hash,new_hash,note) "
                            "VALUES(?,?,?,?,?,?)",
                            (job_id, "changed", now(), existing["content_hash"], content_hash,
                             ",".join(evidence[:3])))
                stats["changed"] += 1
                changed_c += 1
            else:
                con.execute(
                    "INSERT INTO jobs(id,source,external_id,company_id,title,location,remote,description,"
                    "skills_json,ai_relevance,ai_confidence,first_seen,last_seen,status,url,content_hash,"
                    "posted_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
                    (job_id, f"ats:{company['type']}", ext, cid, title, job["location"],
                     job["remote"], desc, json.dumps(skills, ensure_ascii=False), tier, confidence,
                     now(), now(), job["url"], content_hash, posted_at, now()))
                con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,new_hash,note) "
                            "VALUES(?,?,?,?,?)",
                            (job_id, "new", now(), content_hash, ",".join(evidence[:3])))
                stats["new"] += 1
                new_c += 1
                if tier in ("build", "adjacent"):
                    ai_c += 1
            con.execute("INSERT INTO job_snapshots(job_id,seen_at,status,content_hash,location,raw_json) "
                        "VALUES(?,?,?,?,?,?)",
                        (job_id, now(), "open", content_hash, job["location"],
                         json.dumps(job["raw"], ensure_ascii=False)[:20000]))
            con.execute("INSERT INTO job_evidence(job_id,source,url,seen_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(job_id,source) DO UPDATE SET url=excluded.url,seen_at=excluded.seen_at",
                        (job_id, f"ats:{company['type']}", job["url"], now()))

        stale = con.execute(
            "SELECT id,external_id,content_hash FROM jobs WHERE company_id=? AND status='open'",
            (cid,)).fetchall()
        for row in stale:
            if str(row["external_id"]) in seen:
                continue
            con.execute("UPDATE jobs SET status='closed',last_seen=? WHERE id=?", (now(), row["id"]))
            con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,old_hash) VALUES(?,?,?,?)",
                        (row["id"], "closed", now(), row["content_hash"]))
            stats["closed"] += 1
            closed_c += 1

        con.execute(
            "INSERT INTO job_daily(day,company_id,new_jobs,closed_jobs,changed_jobs,ai_jobs) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(day,company_id) DO UPDATE SET "
            "new_jobs=new_jobs+excluded.new_jobs,closed_jobs=closed_jobs+excluded.closed_jobs,"
            "changed_jobs=changed_jobs+excluded.changed_jobs,ai_jobs=ai_jobs+excluded.ai_jobs",
            (today, cid, new_c, closed_c, changed_c, ai_c))

    if con:
        con.commit()
        con.close()
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "companies": stats["companies"], "fetched": stats["fetched"],
                          "new": stats["new"], "closed": stats["closed"],
                          "failed": len(stats["failed"])}, ensure_ascii=False))
    else:
        print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

