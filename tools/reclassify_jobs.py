"""Bounded AI reclassification; preview by default, explicit --apply to write."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tools.ingestion_contract import standard_fields, begin_batch, finish_batch, abort_batch
from tools.job_states import STATUSES, classification_state


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db',type=Path,default=ROOT/'ai_jobs.db')
    ap.add_argument('--limit',type=int,default=3)
    group=ap.add_mutually_exclusive_group()
    group.add_argument('--apply',action='store_true')
    group.add_argument('--dry-run',action='store_true')
    ap.add_argument('--exclude-none',action='store_true')
    ap.add_argument('--status',action='append',choices=sorted(STATUSES))
    ap.add_argument('--json-out',type=Path)
    args=ap.parse_args()
    if not 1<=args.limit<=15:ap.error('limit must be 1..15')
    states=args.status or ['open','review','verification_pending']
    con=sqlite3.connect('file:'+args.db.resolve().as_posix()+('?mode=rw' if args.apply else '?mode=ro'),uri=True)
    con.row_factory=sqlite3.Row
    batch=None;result={'mode':'apply' if args.apply else 'preview','records':[]}
    try:
        if args.apply:batch=begin_batch(con,'reclassification')
        rows=con.execute('SELECT * FROM jobs WHERE status IN ('+','.join('?' for _ in states)+') ORDER BY id LIMIT ?',[*states,args.limit]).fetchall()
        for row in rows:
            derived=standard_fields(row['title'],row['description'] or '',row['department'] or '')
            state=classification_state(row['status'],derived['ai_relevance']) if args.exclude_none else row['status']
            result['records'].append(dict(id=row['id'],before_tier=row['ai_relevance'],after_tier=derived['ai_relevance'],before_status=row['status'],after_status=state,reason=derived['ai_reason']))
            if args.apply:
                con.execute('UPDATE jobs SET normalized_title=?,ai_relevance=?,ai_confidence=?,ai_reason=?,status=? WHERE id=?',(derived['normalized_title'],derived['ai_relevance'],derived['ai_confidence'],derived['ai_reason'],state,row['id']))
                con.execute('INSERT INTO job_changes(job_id,change_type,changed_at,note) VALUES(?,?,?,?)',(row['id'],'reclassified',datetime.now(timezone.utc).isoformat(),json.dumps(result['records'][-1],ensure_ascii=False)))
        if batch:
            result['batch']=finish_batch(con,batch);con.commit()
    except BaseException as error:
        if batch:abort_batch(con,batch,error)
        raise
    finally:con.close()
    if args.json_out:args.json_out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=True))

if __name__=='__main__':main()
