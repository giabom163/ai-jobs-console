"""Reviewed event ledger. Documents are sources; distinct events are the count unit."""
import json
from datetime import date
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent/'configs'/'employment_events.json'
TYPES={'layoff':'裁员','shrink':'缩招','freeze':'停招','reorg':'组织调整'}
STATUSES={'announced':'官方已公告','implemented':'实施有证据','reported':'媒体报道待交叉核实','response_reported':'公司回应（媒体引述）','pending':'待核实线索','denied':'已否认/更正'}


def load_events():
    data=json.loads(PATH.read_text(encoding='utf-8'))
    seen=set()
    for event in data['events']:
        if event.get('region')!='domestic':raise ValueError('mainland employment events only')
        if event['id'] in seen:raise ValueError('duplicate event ID; merge sources into the same event')
        seen.add(event['id'])
        date.fromisoformat(event['event_date'])
        if event['event_type'] not in TYPES or event['status'] not in STATUSES:raise ValueError('unsupported event type/status')
        if not event.get('sources') or not event.get('unresolved'):raise ValueError('sources and unresolved boundary required')
        if event['status']=='announced' and not any(s['grade']=='official' for s in event['sources']):raise ValueError('announcement requires official evidence')
        if event['status']=='implemented' and not event.get('implementation_evidence'):raise ValueError('implementation evidence required')
    return data


def select_events(events,query):
    def get(key):
        value=query.get(key,'');return (value[0] if isinstance(value,list) else value).strip()
    filters={k:get(k) for k in ['event_type','region','event_status','q','since','until']}
    for key,allowed in [('event_type',TYPES),('event_status',STATUSES),('region',{'domestic'})]:
        if filters[key] and filters[key] not in allowed:raise ValueError('unsupported '+key)
    for key in ['since','until']:
        if filters[key] and date.fromisoformat(filters[key]).isoformat()!=filters[key]:raise ValueError('invalid event date')
    if filters['since'] and filters['until'] and filters['since']>filters['until']:raise ValueError('invalid event date range')
    result=[]
    for e in events:
        if any(filters[k] and e.get(k)!=filters[k] for k in ['event_type','region']):continue
        if filters['event_status'] and e['status']!=filters['event_status']:continue
        if filters['since'] and e['event_date']<filters['since']:continue
        if filters['until'] and e['event_date']>filters['until']:continue
        if filters['q'] and filters['q'].lower() not in (e['company']+' '+e['title']+' '+e.get('department','')).lower():continue
        result.append(e)
    return sorted(result,key=lambda e:(e['event_date'],e['id']),reverse=True)


def event_summary(events):
    return {'total':len(events),'announced':sum(e['event_type']=='layoff' and e['status']=='announced' for e in events),'implemented':sum(e['event_type']=='layoff' and e['status']=='implemented' for e in events),'responses':sum(e['status'] in {'response_reported','denied'} for e in events),'pending':sum(e['status'] in {'pending','reported'} for e in events),'headcount_total':None,'note':'按独立事件计数；不同地区、岗位/员工口径和重叠轮次不合并成人数总计。'}


def event_payload(query):
    data=load_events();items=select_events(data['events'],query)
    return {'items':items,'summary':event_summary(items),'coverage_note':data['coverage_note']}
