"""Bounded read-only duplicate review. Never merge based on similarity alone."""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.job_geography import classify_location


def city_set(value):
    value=unicodedata.normalize('NFKC',value or '')
    named=re.findall(r'[\u4e00-\u9fff]{2,8}市',value)
    if named:return tuple(sorted(set(x.replace('浙江·','').removesuffix('市') for x in named)))
    return tuple(sorted(set(x.strip().removesuffix('市') for x in re.split(r'[/、,，|\s]+',value) if x.strip())))


def canonical_title(value):
    return re.sub(r'\s+','',re.sub(r'[（(]J\d+[）)]$','',unicodedata.normalize('NFKC',value or ''))).lower()


def tokens(value):
    text=re.sub(r'\s+','',unicodedata.normalize('NFKC',value or '').lower())
    return {text[i:i+3] for i in range(max(0,len(text)-2))}


def review_pair(a,b):
    ta,tb=tokens(a.get('description')),tokens(b.get('description'))
    score=len(ta&tb)/len(ta|tb) if ta|tb else 0
    gates=[]
    if a['company_id']!=b['company_id']:gates.append('公司不同')
    left_geo, right_geo = classify_location(a.get('location')), classify_location(b.get('location'))
    if left_geo != 'domestic' or right_geo != 'domestic':gates.append('两侧均须为明确中国大陆地点')
    if not city_set(a.get('location')) or city_set(a.get('location'))!=city_set(b.get('location')):gates.append('完整城市集合不同或未知')
    if bool(re.search(r'实习|\bintern\b',a['title'],re.I))!=bool(re.search(r'实习|\bintern\b',b['title'],re.I)):gates.append('实习类型冲突或另一侧未知')
    da,db=a.get('department'),b.get('department')
    if da and db and da!=db:gates.append('部门不同')
    ranks=lambda t:set(re.findall(r'专家|高级|资深|负责人|总监|实习',t))
    if ranks(a['title'])!=ranks(b['title']):gates.append('职级或岗位性质不同')
    years=lambda t:set(re.findall(r'\d+\s*年以上|\d+[-~至]\d+\s*年',t or ''))
    if years(a.get('description'))!=years(b.get('description')):gates.append('经验条件不同，须复核')
    exact=bool(a.get('description')) and re.sub(r'\s+','',a['description'])==re.sub(r'\s+','',b.get('description') or '')
    return {'left_id':a['id'],'right_id':b['id'],'left_title':a['title'],'right_title':b['title'],'company_id':a['company_id'],'left_geography':left_geo,'right_geography':right_geo,'full_cities':list(city_set(a.get('location'))),'similarity':round(score,4),'exact_body':exact,'same_title':canonical_title(a['title'])==canonical_title(b['title']),'holds':gates,'decision':'hold' if gates else 'manual_review','auto_merge':False,'left_hash':hashlib.sha256((a.get('description') or '').encode()).hexdigest(),'right_hash':hashlib.sha256((b.get('description') or '').encode()).hexdigest()}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--db',type=Path,default=ROOT/'ai_jobs.db');ap.add_argument('--limit',type=int,default=3);ap.add_argument('--dry-run',action='store_true');ap.add_argument('--json-out',type=Path);args=ap.parse_args()
    if not 1<=args.limit<=50:ap.error('limit must be 1..50; start with 3')
    con=sqlite3.connect('file:'+args.db.resolve().as_posix()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    rows=[dict(r) for r in con.execute("SELECT * FROM jobs WHERE status IN ('open','review') AND ai_relevance IN ('build','adjacent') ORDER BY company_id,id")];con.close()
    groups=defaultdict(list);result=[];checked=0
    for r in rows:groups[r['company_id']].append(r)
    for group in groups.values():
        for i,a in enumerate(group):
            for b in group[i+1:]:
                if city_set(a.get('location'))!=city_set(b.get('location')):continue
                pair=review_pair(a,b);checked+=1
                if pair['similarity']>=0.8 or (pair['same_title'] and pair['similarity']>=0.5):result.append(pair)
                if len(result)>=args.limit:break
            if len(result)>=args.limit:break
        if len(result)>=args.limit:break
    payload={'mode':'read_only_candidates','pairs_compared':checked,'candidates':result,'merged':0,'evidence_deleted':0,'note':'相似度仅用于召回复核候选；合并必须使用明确复核清单、备份与原文哈希校验。'}
    if args.json_out:args.json_out.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(payload,ensure_ascii=False))


if __name__=='__main__':main()
