"""Bounded source verification. Unknown observations never hide or close jobs."""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.liveness import check_job
from tools.job_geography import is_domestic_only
from tools.request_policy import RequestBudget
from tools.ingestion_contract import begin_batch, finish_batch, abort_batch
from tools.verification_records import record_observation, assert_target_unchanged


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', type=Path, default=ROOT/'ai_jobs.db')
    ap.add_argument('--limit', type=int, default=3)
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--stale-days', type=int, default=14, help='Compatibility only; age never closes uncertain jobs')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json-out', type=Path)
    args = ap.parse_args()
    if not 1 <= args.limit <= 15 or args.workers != 1:
        ap.error('limit must be 1..15; source verification is sequential')
    mode = 'ro'
    con = sqlite3.connect('file:'+args.db.resolve().as_posix()+'?mode='+mode, uri=True)
    con.row_factory = sqlite3.Row
    batch = None
    budget = RequestBudget()
    stats = dict(checked=0, active=0, expired=0, uncertain=0, closed=0, stale=0)
    try:
        candidates = con.execute("SELECT * FROM jobs WHERE status IN ('open','review','verification_pending') AND ai_relevance IN ('build','adjacent') ORDER BY COALESCE(liveness_checked_at,''),id").fetchall()
        rows = [row for row in candidates if row['source'] == 'official:meituan' and is_domestic_only(row['location'])][:args.limit]
        observations=[]
        for row in rows:
            result = check_job(row['source'], row['external_id'], row['url'], datetime.now(timezone(timedelta(hours=8))).date().isoformat(), budget=budget, location=row['location'])
            observations.append((dict(row),result))
            stats['checked'] += int(result['attempted'])
            stats[result['verdict']] += 1
        con.close()
        if not args.dry_run:
            con = sqlite3.connect('file:'+args.db.resolve().as_posix()+'?mode=rw',uri=True)
            con.row_factory=sqlite3.Row
            batch=begin_batch(con,'source_verification')
            for job,result in observations:
                assert_target_unchanged(con,job)
                changed=record_observation(con,job,result)
                stats['closed'] += int(changed and result['verdict']=='expired')
        if batch:
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
