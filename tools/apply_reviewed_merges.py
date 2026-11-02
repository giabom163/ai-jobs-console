"""Apply a bounded, explicitly reviewed duplicate manifest. Default is preview.

Keeps both records and all evidence; only the duplicate's publication status changes.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tools.ingestion_contract import begin_batch, finish_batch, abort_batch
from tools.job_geography import is_domestic_only

def fingerprint(row):
    keys = ('id','source','status','company_id','title','location','description','first_seen','last_seen','duplicate_of')
    return hashlib.sha256(json.dumps({k:row[k] for k in keys},sort_keys=True).encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', type=Path, default=ROOT/'ai_jobs.db')
    ap.add_argument('--input', type=Path, required=True)
    ap.add_argument('--limit', type=int, default=3)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--json-out', type=Path, required=True)
    args = ap.parse_args()
    if not 1 <= args.limit <= 15:
        ap.error('limit must be 1..15; start with --limit 3')
    con = sqlite3.connect('file:' + args.db.resolve().as_posix() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    records = []
    seen = set()
    try:
        for row in json.loads(args.input.read_text(encoding='utf-8'))[:args.limit]:
            a = con.execute('SELECT * FROM jobs WHERE id=?', (row['keep_id'],)).fetchone()
            b = con.execute('SELECT * FROM jobs WHERE id=?', (row['duplicate_id'],)).fetchone()
            if not a or not b or a['id'] == b['id'] or b['id'] in seen:
                raise ValueError('missing, identical or repeated record IDs')
            if row.get('decision') != 'merge_content_duplicate' or not row.get('reason') or not row.get('reviewer'):
                raise ValueError('explicit review decision and evidence required')
            source, status = row.get('source', 'official:meituan'), row.get('status', 'open')
            if (source, status) not in {('official:meituan', 'open'), ('official:baidu', 'review')}:
                raise ValueError('unreviewed source/status pair')
            if any(x['source'] != source or x['status'] != status for x in [a, b]):
                raise ValueError('source or status changed since review')
            if not is_domestic_only(a['location']) or not is_domestic_only(b['location']):
                raise ValueError('both merge records must have Mainland-only locations')
            cities = lambda value: sorted(re.split(r'\s*/\s*', value or ''))
            if a['company_id'] != b['company_id'] or cities(a['location']) != cities(b['location']):
                raise ValueError('employer or complete city set differs')
            if source == 'official:meituan' and not all('实习' in x['title'] for x in [a, b]):
                raise ValueError('this manifest only reviewed internship content duplicates')
            if source == 'official:baidu':
                titles = [re.sub(r'（J\d+）$', '', x['title']).strip() for x in [a, b]]
                bodies = [re.sub(r'\s+', '', x['description'] or '') for x in [a, b]]
                if titles[0] != titles[1] or bodies[0] != bodies[1]:
                    raise ValueError('Baidu merge requires identical role title and complete source description apart from whitespace')
            for prefix, item in [('keep', a), ('duplicate', b)]:
                if hashlib.sha256((item['description'] or '').encode()).hexdigest() != row[prefix+'_description_sha256']:
                    raise ValueError('description changed since semantic review')
            seen.add(b['id'])
            records.append(dict(row, keep_version=fingerprint(a), duplicate_version=fingerprint(b), earliest_first_seen=min([a['first_seen'], b['first_seen']], key=lambda x: datetime.fromisoformat(x.replace('Z','+00:00'))),
                                latest_last_seen=max([a['last_seen'], b['last_seen']], key=lambda x: datetime.fromisoformat(x.replace('Z','+00:00')))))
    finally:
        con.close()
    result = {'mode':'apply' if args.apply else 'preview', 'records':records, 'physical_deletions':0}
    if {x['keep_id'] for x in records} & {x['duplicate_id'] for x in records}:
        raise ValueError('merge chains must be reviewed as a single resolved group')
    if args.apply and records:
        con = sqlite3.connect('file:'+args.db.resolve().as_posix()+'?mode=rw',uri=True)
        con.row_factory = sqlite3.Row
        batch = None
        try:
            batch = begin_batch(con, 'reviewed_merge', hashlib.sha256(args.input.read_bytes()).hexdigest())
            for row in records:
                for prefix, key in [('keep','keep_id'),('duplicate','duplicate_id')]:
                    current = con.execute('SELECT * FROM jobs WHERE id=?',(row[key],)).fetchone()
                    if not current or fingerprint(current) != row[prefix+'_version']:
                        raise ValueError('record changed since preview')
                changed = con.execute("UPDATE jobs SET status='merged',duplicate_of=?,merged_at=? WHERE id=? AND status=?",
                                      (row['keep_id'],datetime.now(timezone.utc).isoformat(),row['duplicate_id'],row.get('status','open'))).rowcount
                if changed != 1:
                    raise ValueError('concurrent record change')
                note = dict(reason=row['reason'], reviewer=row['reviewer'], keep_id=row['keep_id'],
                            group_earliest_seen=row['earliest_first_seen'], group_latest_seen=row['latest_last_seen'])
                con.execute('INSERT INTO job_changes(job_id,change_type,changed_at,note) VALUES(?,?,?,?)',
                            (row['duplicate_id'],'merged',datetime.now(timezone.utc).isoformat(),json.dumps(note,ensure_ascii=False)))
            result['batch'] = finish_batch(con,batch)
            result['backup'] = batch['backup_path']
            con.commit()
        except BaseException as error:
            if batch:
                abort_batch(con,batch,error)
            raise
        finally:
            con.close()
    args.json_out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'mode':result['mode'],'reviewed':len(records),'physical_deletions':0,'backup':result.get('backup')},ensure_ascii=False))


if __name__ == '__main__':
    main()
