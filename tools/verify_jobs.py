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
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.ingestion_contract import begin_batch, finish_batch, abort_batch
from tools.liveness import check_job
from tools.verification_records import record_observation, assert_target_unchanged
from tools.request_policy import RequestBudget
from tools.job_geography import is_domestic_only
DB = ROOT / "ai_jobs.db"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

SOURCE_BASE = {
    "ats:greenhouse": 45, "ats:lever": 45, "ats:ashby": 45,
    "ats:workable": 45, "ats:smartrecruiters": 45, "ats:recruitee": 45,
    "official": 45, "aggregator": 20, "platform": 10,
}
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
             "smartrecruiters.com", "recruitee.com")


def today_local():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def score(job, http_status=None, final_url="", company_domain="", cross_source=0):
    source = job["source"] or ""
    base = SOURCE_BASE.get(source, 0)
    if not base and ":" in source:
        base = SOURCE_BASE.get(source.split(":", 1)[0], 0)
    score = base
    host = urllib.parse.urlsplit(final_url or job["url"] or "").hostname or ""
    domain_match = bool(host and (any(host == h or host.endswith('.' + h) for h in ATS_HOSTS) or
                                  (company_domain and (host == company_domain or host.endswith("." + company_domain)))))
    if domain_match:
        score += 20
    if cross_source:
        score += 10
    if (job["last_seen"] or "")[:10] == today_local():
        score += 15
    if job["title"] and job["location"] and (job["description"] or ""):
        score += 10
    if http_status is None:
        score -= 20  # 未真实核验 URL，不能算已验证
    elif http_status >= 400:
        score -= 25
    if (job["last_seen"] or "")[:10] < today_local():
        # not seen today; conservative stale penalty only if older than 30 days
        try:
            seen = datetime.fromisoformat(job["last_seen"]).date()
            if (datetime.now(timezone.utc).date() - seen).days > 30:
                score -= 10
        except Exception:
            pass
    score = max(0, min(100, score))
    level = '链接可达（非岗位核实）' if http_status is not None and 200 <= http_status < 300 else '待核验'
    return score, level, int(domain_match), int(cross_source)


def main():
    parser = argparse.ArgumentParser(description='Bounded scoring and explicit source observation')
    parser.add_argument('--db', type=Path, default=DB)
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--score-only', action='store_true')
    parser.add_argument('--json-out', type=Path)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--ids', default=None, help='Comma-separated exact job row IDs; only mainland official:meituan records are accepted')
    parser.add_argument('--no-score', action='store_true', help='Record source observations without recalculating authenticity scores')
    args = parser.parse_args()
    if not 1 <= args.limit <= 15 or args.workers != 1:
        parser.error('limit must be 1..15; requests are sequential')
    if args.score_only and args.no_score:
        parser.error('--score-only and --no-score cannot be combined')
    ids = None
    if args.ids is not None:
        ids = list(dict.fromkeys(x.strip() for x in args.ids.split(',') if x.strip()))
        if not ids:
            parser.error('--ids must contain at least one exact job row ID')
        if len(ids) > args.limit:
            parser.error('--ids count must not exceed --limit')
    con = sqlite3.connect('file:'+args.db.resolve().as_posix()+'?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    batch = None
    budget = RequestBudget()
    stats = dict(checked=0, scored=0, source_accessible=0)
    try:
        if ids is not None:
            placeholders = ','.join('?' for _ in ids)
            rows = con.execute("SELECT j.*,c.domain company_domain,(SELECT count(DISTINCT source) FROM job_evidence e WHERE e.job_id=j.id) evidence_sources FROM jobs j LEFT JOIN companies c ON c.id=j.company_id WHERE j.id IN ("+placeholders+") AND j.status IN ('open','review','verification_pending')", ids).fetchall()
            if len(rows) != len(ids) or any(r['source'] != 'official:meituan' or not is_domestic_only(r['location']) for r in rows):
                parser.error('--ids must resolve only to existing official:meituan records with mainland-only locations')
        else:
            candidates = con.execute("SELECT j.*,c.domain company_domain,(SELECT count(DISTINCT source) FROM job_evidence e WHERE e.job_id=j.id) evidence_sources FROM jobs j LEFT JOIN companies c ON c.id=j.company_id WHERE j.status IN ('open','review','verification_pending') ORDER BY j.first_seen DESC,j.id").fetchall()
            if args.score_only:
                rows = candidates[:args.limit]
            else:
                rows = [r for r in candidates if r['source'] == 'official:meituan' and is_domestic_only(r['location'])][:args.limit]
        observations=[]
        for job in rows:
            observation=None if args.score_only else check_job(job['source'],job['external_id'],job['url'],today_local(),budget=budget,location=job['location'])
            observations.append((dict(job),observation))
        con.close()
        con=sqlite3.connect('file:'+args.db.resolve().as_posix()+'?mode=rw',uri=True)
        con.row_factory=sqlite3.Row
        batch=begin_batch(con,'score_only' if args.score_only else 'verify_source')
        for job,observation in observations:
            assert_target_unchanged(con,job)
            status,final=job['http_status'],job['final_url'] or ''
            if observation is not None and record_observation(con,job,observation):
                status,final=observation['http_status'],observation['final_url']
                stats['checked']+=1
                stats['source_accessible']+=int(status is not None and 200<=status<300)
            if args.no_score:
                continue
            sc, level, domain_match, cross = score(job, status, final, job['company_domain'] or '', int(job['evidence_sources'] > 1))
            con.execute('UPDATE jobs SET authenticity_score=?,authenticity_level=?,scored_at=?,domain_match=?,cross_source=? WHERE id=?',
                        (sc, level, datetime.now(timezone.utc).isoformat(), domain_match, cross, job['id']))
            stats['scored'] += 1
        batch['request_events']=budget.events
        batch['requests']=budget.requests
        stats['batch'] = finish_batch(con, batch)
        con.commit()
    except BaseException as error:
        if batch:
            batch['request_events']=budget.events
            batch['requests']=budget.requests
            abort_batch(con, batch, error)
        raise
    finally:
        con.close()
    stats.update(requests=budget.requests, request_events=budget.events)
    if args.json_out:
        args.json_out.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=True))

if __name__ == '__main__':
    main()
