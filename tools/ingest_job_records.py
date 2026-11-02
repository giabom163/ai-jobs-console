"""Offline source-record ingestion. Default preview; --apply writes review records only."""
import argparse, hashlib, json, re, sqlite3, sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tools.ingestion_contract import standard_fields, validate_record, begin_batch, finish_batch, abort_batch
from tools.job_geography import is_domestic_only

SOURCE_KEYS={
 'zhaopin.jd.com':('official:jd','requementId'),
 'job.10086.cn':('official:chinamobile','id'),
 'jobs.bytedance.com':('official:bytedance',None),
 'talent.baidu.com':('official:baidu',None),
 'app.mokahr.com':('official:deepseek',None),
 'careers.tencent.com':('official:tencent','postId'),
 'career.huawei.com':('official:huawei','advertisementId'),
 'jobs.bilibili.com':('official:bilibili',None),
 'talent.alibaba.com':('official:alibaba','positionId'),
 'www.liepin.com':('platform:liepin',None),'m.liepin.com':('platform:liepin',None),
 'www.zhaopin.com':('platform:zhaopin',None),
 'bj.58.com':('platform:58',None),
 'www.zhipin.com':('platform:boss',None),
}


def identity(url):
 u=urlsplit(url); item=SOURCE_KEYS.get(u.hostname)
 # Tencent's HTTPS public API supplies HTTP PostURL links. Preserve that exact
 # source URL; do not invent an unobserved HTTPS detail address.
 schemes={'https','http'} if u.hostname=='careers.tencent.com' else {'https'}
 if not item or u.scheme not in schemes or u.username or u.password: raise ValueError('unregistered source URL')
 source,key=item; params=parse_qs(u.query)
 if u.hostname == 'bj.58.com':
  match=re.fullmatch(r'/tech/(\d+)x\.shtml',u.path)
  if not match: raise ValueError('unregistered 58 detail path')
  return source,match[1]
 if u.hostname == 'www.zhipin.com':
  match=re.fullmatch(r'/job_detail/([A-Za-z0-9_-]+)\.html',u.path)
  if not match: raise ValueError('unregistered Boss detail path')
  return source,match[1]
 if u.hostname=='app.mokahr.com':
  # This exact tenant is linked by talent.deepseek.com; other Moka tenants
  # must never inherit DeepSeek's identity.
  match=re.fullmatch(r'/job/([0-9a-f-]{36})',u.fragment)
  if u.path!='/social-recruitment/high-flyer/140576' or not match:
   raise ValueError('unregistered Moka tenant or missing concrete job ID')
  return source,match[1]
 ext=(params.get(key) or params.get('jobId') or [None])[0] if key else None
 if not ext:
  patterns=[r'/position/(\d+)/detail',r'/jobs/detail/[^/]+/([^/]+)',r'/positions/(\d+)',r'/job/(\d+)\.shtml',r'/jobdetail/([^/]+)\.htm']
  ext=next((m[1] for pattern in patterns if (m:=re.search(pattern,u.path))),None)
 if not ext: raise ValueError('missing stable detail ID; aggregate pages are not jobs')
 return source,ext


def source_body(row):
 p=Path(row.get('raw_file') or '').resolve()
 allowed=Path.home()/'.codex'/'documents'
 if not p.is_relative_to(allowed) or p.suffix!='.json' or not p.is_file(): return ''
 data=json.loads(p.read_text(encoding='utf8'))
 if isinstance(data.get('content'),str): return data['content']
 target=identity(row['url'])
 for entry in data.get('results',[]):
  try: same=identity(entry.get('url',''))==target
  except ValueError: same=False
  if same: return entry.get('content') or entry.get('summary') or ''
 return ''


def dated_fact(body,labels):
 m=re.search(r'(?:'+labels+r')[：:\s]*(\d{4})[-年](\d{1,2})[-月](\d{1,2})',body)
 if not m: return None,None
 value=f'{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}'
 datetime.fromisoformat(value)
 return value,{'text':m.group(),'start':m.start(),'end':m.end()}


def observed_time(value):
 if not value: return None
 value=str(value); dt=datetime.fromisoformat(value.replace('Z','+00:00'))
 if dt.tzinfo is None: raise ValueError('observation timestamp needs an explicit timezone')
 return value


def normalize(row):
 source,ext=identity(row.get('url') or '')
 if row.get('source_type')=='official_direction_detail': raise ValueError('recruiting direction is not a concrete job')
 title=str(row.get('title') or '').strip(); company=str(row.get('company') or '').strip(); location=str(row.get('location') or '').strip()
 if not title or not company or re.fullmatch(r'[\d\s\-Kk·薪万千]+',title): raise ValueError('missing company or complete job title')
 if not is_domestic_only(location): raise ValueError('explicit Mainland-only location required')
 first=observed_time(row.get('first_seen') or row.get('retrieved_at'))
 last=observed_time(row.get('last_seen') or row.get('retrieved_at'))
 if not first or not last: raise ValueError('missing original observation time; never fill import time')
 body=source_body(row); facts={}; values={}
 for field,labels in [('published_at','发布时间|发布日期|发布于'),('source_updated_at','更新于|更新时间'),('application_start','网申开始日期|开始时间'),('application_deadline','网申截止日期|截止时间|报名截止时间|申请截止日期')]:
  values[field],evidence=dated_fact(body,labels)
  if evidence: facts[field]={**evidence,'raw_file':row.get('raw_file')}
 department=str(row.get('department') or '')
 if department and department not in body: department=''
 evidence=row.get('ai_evidence') or []
 description='\n'.join(e.get('text','') for e in evidence if e.get('text') and e['text'] in body)
 if not description: raise ValueError('missing source-aligned AI responsibility evidence')
 derived=standard_fields(title,description,department)
 tier,confidence,reason=derived['ai_relevance'],derived['ai_confidence'],derived['ai_reason']
 if tier not in {'build','adjacent'}: raise ValueError('AI rule rejected: '+reason)
 stored_description=body if source=='official:baidu' else description
 raw=row.get('native_status')
 # Only a named structured status supplied by a collector; ignore page readability/hiring_status.
 status_map={'OPEN':'open','CLOSED':'closed','PUBLISHED':'open','UNPUBLISHED':'closed'}
 status=status_map.get(raw,'unknown') if isinstance(raw,str) else 'unknown'
 return validate_record(dict(values,source=source,external_id=ext,requisition_id=row.get('source_job_id'),title=title,
             company=company,normalized_title=derived['normalized_title'],location=location,department=department or None,first_seen=first,last_seen=last,
             last_checked_at=observed_time(row.get('last_page_checked_at')),source_status=status,
             source_status_raw=json.dumps(raw,ensure_ascii=False) if raw is not None else None,
             date_evidence_json=json.dumps(facts,ensure_ascii=False),url=row['url'],description=stored_description,
             ai_relevance=tier,ai_confidence=confidence,ai_reason=reason))


def main():
 ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('--input',type=Path,required=True)
 ap.add_argument('--db',type=Path,default=ROOT/'ai_jobs.db');ap.add_argument('--limit',type=int,default=3)
 ap.add_argument('--apply',action='store_true');ap.add_argument('--json-out',type=Path,required=True)
 args=ap.parse_args()
 if not 1<=args.limit<=200: ap.error('limit must be 1..200')
 result={'mode':'apply_review_only' if args.apply else 'preview','accepted':[],'rejected':[]}
 con=None;batch=None
 try:
  if args.apply:
   con=sqlite3.connect('file:'+args.db.resolve().as_posix()+'?mode=rw',uri=True)
   batch=begin_batch(con,'offline_import',hashlib.sha256(args.input.read_bytes()).hexdigest())
  for row in json.loads(args.input.read_text(encoding='utf8'))[:args.limit]:
   try: record=normalize(row)
   except (ValueError,TypeError,KeyError) as error:
    result['rejected'].append({'url':row.get('url'),'reason':str(error)}); continue
   result['accepted'].append(record)
   if not con: continue
   company=con.execute('SELECT id FROM companies WHERE name=?',(record['company'],)).fetchone()
   cid=company[0] if company else 'company-'+hashlib.sha256(record['company'].encode()).hexdigest()[:24]
   owner=con.execute('SELECT name FROM companies WHERE id=?',(cid,)).fetchone()
   if owner and owner[0]!=record['company']:
    raise ValueError('company ID collision; refusing to associate a different employer')
   con.execute('INSERT OR IGNORE INTO companies(id,name,ats_type,domain,created_at) VALUES(?,?,?,?,?)',
               (cid,record['company'],'official' if record['source'].startswith('official:') else 'platform',urlsplit(record['url']).hostname,record['first_seen']))
   existing=con.execute('SELECT id,last_seen,last_checked_at,source_status,published_at,source_updated_at,date_evidence_json FROM jobs WHERE source=? AND external_id=?',(record['source'],record['external_id'])).fetchone()
   jid=existing[0] if existing else 'job-'+hashlib.sha256((record['source']+'|'+record['external_id']).encode()).hexdigest()[:24]
   fields={k:v for k,v in record.items() if k!='company'}
   fields.update(id=jid,company_id=cid,content_hash=hashlib.sha256(record['description'].encode()).hexdigest())
   if existing:
    if datetime.fromisoformat(record['last_seen'].replace('Z','+00:00')) < datetime.fromisoformat(existing[1].replace('Z','+00:00')):
     result.setdefault('skipped_stale',[]).append(jid)
     continue
    # Source facts may be enriched, but this importer cannot promote/close a reviewed job.
    updates={k:v for k,v in fields.items() if k not in {'id','company_id','first_seen'} and v is not None}
    if record['source_status']=='unknown' and existing[3]:
     updates.pop('source_status',None);updates.pop('source_status_raw',None)
    if existing[4]: updates.pop('published_at',None)
    if existing[5] and record.get('source_updated_at'):
     newer=con.execute('SELECT julianday(?)>=julianday(?)',(record['source_updated_at'],existing[5])).fetchone()[0]
     if not newer: updates.pop('source_updated_at',None)
    if existing[2] and record.get('last_checked_at') and datetime.fromisoformat(record['last_checked_at'].replace('Z','+00:00')) < datetime.fromisoformat(existing[2].replace('Z','+00:00')):
     updates.pop('last_checked_at',None)
    facts=json.loads(existing[6] or '{}')
    for key,value in json.loads(record['date_evidence_json']).items():
     if key in updates: facts[key]=value
    updates['date_evidence_json']=json.dumps(facts,ensure_ascii=False)
    con.execute('UPDATE jobs SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',[*updates.values(),jid])
   else:
    fields.update(status='review',liveness='uncertain')
    con.execute('INSERT INTO jobs('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',list(fields.values()))
  if con:
   result['batch']=finish_batch(con,batch)
   con.commit()
 except BaseException as error:
  if con and batch: abort_batch(con,batch,error)
  raise
 finally:
  if con: con.close()
 args.json_out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
 print(json.dumps({'mode':result['mode'],'accepted':len(result['accepted']),'rejected':len(result['rejected'])},ensure_ascii=False))


if __name__=='__main__': main()
