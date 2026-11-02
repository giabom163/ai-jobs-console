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
from tools.ingestion_contract import begin_batch,finish_batch,abort_batch

CLAIMS={'reported','official_announcement','implemented','denied','rumor','prediction'}


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
                receipt=evidence.get('fetch_receipt') or {}
                if evidence.get('source_field')!='content' or evidence.get('fetch_received') is not True:
                    raise ValueError('source Fetch evidence required; search summaries cannot be approved')
                if receipt.get('action')!='fetch' or receipt.get('http_status')!=200 or receipt.get('input',{}).get('url')!=row['url'] or receipt.get('dispatched') is not True:
                    raise ValueError('successful original-URL Fetch receipt required')
                receipt_file=Path(receipt.get('response_file') or '').resolve()
                evidence_root=Path.home()/'.codex'/'documents'
                if not receipt_file.is_relative_to(evidence_root.resolve()) or not receipt_file.is_file():
                    raise ValueError('Fetch response artifact must exist in Codex documents')
                raw=receipt_file.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=receipt.get('response_sha256'):
                    raise ValueError('Fetch response artifact hash differs')
                source=json.loads(raw)
                if str(source.get('content') or '').strip()!=row['content']:
                    raise ValueError('Fetch response content differs from reviewed document')
                if d.get('ai_scope') not in AI_SCOPES or d.get('claim_status') not in CLAIMS:
                    raise ValueError('AI scope and evidence claim type required')
                if not parse_time(d.get('published_at')):raise ValueError('source publication date required')
                span=d.get('date_evidence') or {};start,end=span.get('start'),span.get('end')
                if not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(row['content']) or row['content'][start:end]!=span.get('text'):
                    raise ValueError('source publication date excerpt required')
                if d.get('evidence_url')!=row['url']:raise ValueError('review must refer to the stored original URL')
                date_parts=re.findall(r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?',span['text'])
                dates={date(*map(int,parts)).isoformat() for parts in date_parts}
                if dates!={parse_time(d['published_at']).date().isoformat()}:
                    raise ValueError('source date excerpt must match the approved publication date')
                evidence.update(source_verified=True,date_evidence=span)
                if apply:
                    con.execute("UPDATE discovery_documents SET review_status='approved',ai_scope=?,claim_status=?,published_at=?,date_basis='source_verified',evidence_json=? WHERE id=?",(d['ai_scope'],d['claim_status'],d['published_at'],json.dumps(evidence,ensure_ascii=False),d['id']))
            elif apply:
                con.execute('UPDATE discovery_documents SET review_status=? WHERE id=?',(d['review_status'],d['id']))
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
