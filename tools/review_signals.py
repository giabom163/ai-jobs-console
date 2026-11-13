"""Apply explicit human review decisions to exact document hashes, offline only."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import sys
from datetime import datetime,timezone,date
from uuid import uuid4

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tools.employment_signals import AI_SCOPES,parse_time
from tools.evidence_contract import normalize_observation
from tools.ingestion_contract import begin_batch,finish_batch,abort_batch

CLAIMS={'self_reported','reported','official_announcement','implemented','denied','rumor','prediction'}
AI_RELATIONS={'explicit_ai','related_unconfirmed','unknown'}


def apply_decisions(db,manifest,apply=False):
    reviewer=str(manifest.get('reviewer') or '').strip()
    decisions=manifest.get('decisions')
    if not reviewer or not isinstance(decisions,list) or not decisions or len(decisions)>15:
        raise ValueError('reviewer and 1..15 explicit decisions required')
    if len({d['id'] for d in decisions})!=len(decisions):raise ValueError('duplicate document decision')
    con=sqlite3.connect('file:'+Path(db).resolve().as_posix()+('?mode=rw' if apply else '?mode=ro'),uri=True)
    con.row_factory=sqlite3.Row;batch=None
    try:
        if apply:
            batch=begin_batch(con,'discovery_review',hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
            batch['document_ids']=[d['id'] for d in decisions]
        for d in decisions:
            row=con.execute('SELECT * FROM discovery_documents WHERE id=?',(d['id'],)).fetchone()
            if row is None:raise ValueError('document not found')
            if d['expected_sha256']!=row['content_sha256'] or hashlib.sha256(row['content'].encode()).hexdigest()!=row['content_sha256']:
                raise ValueError('document content changed; new review required')
            if d.get('review_status') not in {'approved','pending','excluded'} or not str(d.get('reason') or '').strip():
                raise ValueError('explicit status and review reason required')
            if d['review_status']=='approved':
                evidence=json.loads(row['evidence_json'])
                evidence_root=Path.home()/'.codex'/'documents'
                observation=normalize_observation(evidence,row['url'],row['content'],evidence_root)
                if not observation.get('final_url'):
                    raise ValueError('fetch receipt does not prove the final source URL; cannot publish')
                if d.get('ai_scope') not in AI_SCOPES or d.get('claim_status') not in CLAIMS:
                    raise ValueError('AI scope and evidence claim type required')
                if d.get('ai_relation') not in AI_RELATIONS:
                    raise ValueError('AI causality status required')
                ai_span=d.get('ai_relation_evidence') or {}
                ai_start,ai_end=ai_span.get('start'),ai_span.get('end')
                if d['ai_relation']=='explicit_ai' and (
                    not isinstance(ai_start,int) or not isinstance(ai_end,int) or
                    not 0<=ai_start<ai_end<=len(row['content']) or
                    row['content'][ai_start:ai_end]!=ai_span.get('text')
                ):
                    raise ValueError('explicit AI causality requires an exact source excerpt')
                if not parse_time(d.get('published_at')):raise ValueError('source publication date required')
                span=d.get('date_evidence') or {};start,end=span.get('start'),span.get('end')
                if not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(row['content']) or row['content'][start:end]!=span.get('text'):
                    raise ValueError('source publication date excerpt required')
                if d.get('evidence_url')!=row['url']:raise ValueError('review must refer to the stored original URL')
                geography=d.get('geography_evidence') or {};gstart,gend=geography.get('start'),geography.get('end')
                if not isinstance(gstart,int) or not isinstance(gend,int) or not 0<=gstart<gend<=len(row['content']) or row['content'][gstart:gend]!=geography.get('text'):
                    raise ValueError('Mainland geography evidence excerpt required')
                date_parts=re.findall(r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?',span['text'])
                dates={date(*map(int,parts)).isoformat() for parts in date_parts}
                if dates!={parse_time(d['published_at']).date().isoformat()}:
                    raise ValueError('source date excerpt must match the approved publication date')
                if observation.get('published_at') not in (None,d['published_at']):
                    raise ValueError('review publication date differs from source observation')
                if observation.get('date_span') not in (None,span):
                    raise ValueError('review date span differs from source observation')
                observation.update(published_at=d['published_at'],date_span=span)
                event_date=d.get('event_date')
                if event_date and not parse_time(event_date):raise ValueError('event_date must be a valid ISO date/time or empty')
                evidence.update(source_verified=True,source_observation=observation,date_evidence=span,event_date=event_date,geography={'region':'CN-mainland','evidence':geography},ai_relation=d['ai_relation'],ai_relation_evidence=ai_span if d['ai_relation']=='explicit_ai' else None)
                if apply:
                    con.execute("UPDATE discovery_documents SET review_status='approved',ai_scope=?,claim_status=?,published_at=?,date_basis='source_verified',evidence_json=?,last_verified_at=? WHERE id=?",(d['ai_scope'],d['claim_status'],d['published_at'],json.dumps(evidence,ensure_ascii=False),datetime.now(timezone.utc).isoformat(),d['id']))
            elif apply:
                evidence=json.loads(row['evidence_json']);event_date=d.get('event_date')
                if event_date and not parse_time(event_date):raise ValueError('event_date must be a valid ISO date/time or empty')
                evidence['event_date']=event_date
                con.execute('UPDATE discovery_documents SET review_status=?,evidence_json=? WHERE id=?',(d['review_status'],json.dumps(evidence,ensure_ascii=False),d['id']))
            if apply:
                con.execute('INSERT INTO discovery_reviews VALUES(?,?,?,?,?,?,?,?)',('review-'+uuid4().hex,d['id'],batch['batch_id'],reviewer,datetime.now(timezone.utc).isoformat(),row['content_sha256'],json.dumps(dict(row),ensure_ascii=False),json.dumps(d,ensure_ascii=False)))
        result=dict(mode='preview',decisions=len(decisions),requests=0)
        if apply:
            result.update(mode='applied',batch=finish_batch(con,batch));con.commit()
        return result
    except BaseException as error:
        if batch:abort_batch(con,batch,error)
        raise
    finally:con.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--db',type=Path,default=ROOT/'ai_jobs.db');p.add_argument('--limit',type=int,default=3);p.add_argument('--apply',action='store_true');args=p.parse_args()
    if not 1<=args.limit<=15:p.error('limit must be 1..15')
    manifest=json.loads(args.manifest.read_text('utf8'))
    if len(manifest.get('decisions',[]))>args.limit:p.error('decision limit exceeded')
    print(json.dumps(apply_decisions(args.db,manifest,args.apply),ensure_ascii=True))


if __name__=='__main__':main()
