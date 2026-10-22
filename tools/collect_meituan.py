#!/usr/bin/env python
"""Collect Meituan public jobs (unauthenticated API), up to N jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
SCHEMA = ROOT / "schema" / "ai_employment.sql"
sys.path.insert(0, str(ROOT))
from tools.collect_ats import classify_ai  # noqa: E402

API = "https://zhaopin.meituan.com/api/official/job/getJobList"
DETAIL = "https://zhaopin.meituan.com/web/position/detail?jobUnionId={id}&jobShareType=1&highlightType={hl}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def now():
    return datetime.now(timezone.utc).isoformat()


def fetch(page, size=100):
    body = {"page": {"pageNo": page, "pageSize": size}, "jobShareType": "1",
            "keywords": "", "cityList": [], "department": [],
            "jobType": [{"code": "1", "subCode": []}, {"code": "2", "subCode": []},
                        {"code": "3", "subCode": []}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Referer": "https://zhaopin.meituan.com/web/social"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != 1:
        raise RuntimeError(payload.get("message") or "meituan api error")
    return payload.get("data") or {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    cid = "company-meituan"
    con.execute("INSERT INTO companies(id,name,ats_type,board_token,watchlist,domain,created_at) "
                "VALUES(?,?,?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET domain=excluded.domain",
                (cid, "美团", "official", "zhaopin.meituan.com", "meituan.com", now()))
    added = 0
    page = 1
    while added < args.limit:
        data = fetch(page, args.page_size)
        rows = data.get("list") or []
        if not rows:
            break
        for item in rows:
            if added >= args.limit:
                break
            ext = str(item.get("jobUnionId") or "")
            title = (item.get("name") or "").strip()
            if not ext or not title:
                continue
            cities = " / ".join(c.get("name", "") for c in (item.get("cityList") or []) if c.get("name"))
            desc = "\n".join(x for x in (item.get("jobDuty"), item.get("jobRequirement")) if x)
            jtype = str(item.get("jobType") or "")
            hl = "social" if jtype == "3" else "campus"
            url = DETAIL.format(id=ext, hl=hl)
            content_hash = hashlib.sha256(f"{title}\n{cities}\n{desc}".encode()).hexdigest()
            tier, conf, _ = classify_ai(title)
            existing = con.execute("SELECT id FROM jobs WHERE source='official:meituan' AND external_id=?", (ext,)).fetchone()
            if existing:
                con.execute("UPDATE jobs SET title=?,location=?,description=?,ai_relevance=?,ai_confidence=?,"
                            "last_seen=?,url=?,content_hash=? WHERE id=?",
                            (title, cities, desc[:20000], tier, conf, now(), url, content_hash, existing["id"]))
                jid = existing["id"]
            else:
                jid = "job-" + hashlib.sha256(f"meituan|{ext}".encode()).hexdigest()[:24]
                con.execute("INSERT INTO jobs(id,source,external_id,company_id,title,location,description,"
                            "ai_relevance,ai_confidence,first_seen,last_seen,status,url,content_hash) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                            (jid, "official:meituan", ext, cid, title, cities, desc[:20000], tier, conf,
                             now(), now(), url, content_hash))
                added += 1
            con.execute("INSERT INTO job_evidence(job_id,source,url,seen_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(job_id,source) DO UPDATE SET url=excluded.url,seen_at=excluded.seen_at",
                        (jid, "official:meituan", url, now()))
        con.commit()
        if len(rows) < args.page_size:
            break
        page += 1
        time.sleep(0.5)
    print(json.dumps({"added": added, "total": con.execute("SELECT count(*) FROM jobs").fetchone()[0]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
