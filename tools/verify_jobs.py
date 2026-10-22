#!/usr/bin/env python
"""Job authenticity scoring + optional apply-URL verification.

Usage:
  python tools/verify_jobs.py --score-only          # score all without network
  python tools/verify_jobs.py --limit 50            # verify 50 URLs + score
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

SOURCE_BASE = {
    "ats:greenhouse": 45, "ats:lever": 45, "ats:ashby": 45,
    "ats:workable": 45, "ats:smartrecruiters": 45, "ats:recruitee": 45,
    "official": 35, "aggregator": 20, "platform": 10,
}
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
             "smartrecruiters.com", "recruitee.com")


def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def score(job, http_status=None, final_url="", company_domain="", cross_source=0):
    base = SOURCE_BASE.get(job["source"].split(":")[0] + ":" + job["source"].split(":")[1]
                           if ":" in job["source"] else job["source"], 0)
    if job["source"].startswith("ats:"):
        base = 45
    score = base
    host = urllib.parse.urlsplit(final_url or job["url"] or "").hostname or ""
    domain_match = bool(host and (any(h in host for h in ATS_HOSTS) or
                                  (company_domain and (host == company_domain or host.endswith("." + company_domain)))))
    if domain_match:
        score += 20
    if cross_source:
        score += 10
    if (job["last_seen"] or "")[:10] == today_utc():
        score += 15
    if job["title"] and job["location"] and (job["description"] or ""):
        score += 10
    if http_status is None:
        score -= 20  # 未真实核验 URL，不能算已验证
    elif http_status >= 400:
        score -= 25
    if (job["last_seen"] or "")[:10] < today_utc():
        # not seen today; conservative stale penalty only if older than 30 days
        try:
            seen = datetime.fromisoformat(job["last_seen"]).date()
            if (datetime.now(timezone.utc).date() - seen).days > 30:
                score -= 10
        except Exception:
            pass
    score = max(0, min(100, score))
    level = "已验证" if score >= 90 else "可信" if score >= 70 else "待核验" if score >= 50 else "低可信"
    if http_status is None:
        level = "待核验"
    return score, level, int(domain_match), int(cross_source)


def check(url):
    if not url:
        return None, ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status, resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, url
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return resp.status, resp.geturl()
        except urllib.error.HTTPError as exc:
            return exc.code, url
        except Exception:
            return None, url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT j.*,c.domain company_domain,"
                       "(SELECT count(DISTINCT source) FROM job_evidence e WHERE e.job_id=j.id) evidence_sources "
                       "FROM jobs j LEFT JOIN companies c ON c.id=j.company_id "
                       "WHERE j.status='open' ORDER BY j.first_seen DESC").fetchall()
    if not args.score_only:
        rows = rows[:max(1, args.limit)]
    stats = {"checked": 0, "verified": 0, "errors": 0, "scored": 0}
    for i, job in enumerate(rows, 1):
        status = job["http_status"] if args.score_only else None
        final = (job["final_url"] or "") if args.score_only else ""
        if not args.score_only:
            status, final = check(job["url"])
            stats["checked"] += 1
            if status and status < 400:
                stats["verified"] += 1
            else:
                stats["errors"] += 1
        sc, level, domain_match, cross = score(
            job, status, final, job["company_domain"] or "",
            1 if (job["evidence_sources"] or 0) > 1 else 0)
        con.execute("UPDATE jobs SET authenticity_score=?,authenticity_level=?,"
                    "last_verified_at=?,http_status=?,final_url=?,domain_match=?,cross_source=? WHERE id=?",
                    (sc, level, datetime.now(timezone.utc).isoformat(), status, final,
                     domain_match, cross, job["id"]))
        if i % 10 == 0:
            con.commit()
        stats["scored"] += 1
        if not args.score_only:
            time.sleep(0.08)
    con.commit()
    out = json.dumps(stats, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(out, encoding="utf-8")
        print(json.dumps({"ok": True, **stats}, ensure_ascii=False))
    else:
        print(out)


if __name__ == "__main__":
    main()
