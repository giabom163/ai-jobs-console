"""Reviewed event ledger. Documents are sources; distinct events are the count unit."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent/'configs'/'employment_events.json'
TZ = timezone(timedelta(hours=8))
TYPES={'layoff':'裁员','shrink':'缩招','freeze':'停招','reorg':'组织调整','hiring':'招聘岗位变化'}
STATUSES={'announced':'官方已公告','implemented':'实施有证据','reported':'媒体报道待交叉核实','response_reported':'公司回应（媒体引述）','pending':'待核实线索','denied':'已否认/更正'}
AI_RELATIONS={'explicit_ai':'来源明确归因 AI','related_unconfirmed':'AI 相关，因果未证实','unknown':'未确认 AI 因果'}


def verify_event_documents(event, con):
    """Verify document_id + expected_sha256 + approved discovery_reviews links."""
    doc_refs = [s for s in event.get('sources', []) if isinstance(s, dict) and s.get('document_id')]
    if not doc_refs:
        return True, []
    if con is None:
        return False, ['缺少数据库连接，无法核验关联文档']
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'discovery_documents' not in tables or 'discovery_reviews' not in tables:
        return False, ['缺少 discovery_documents 或 discovery_reviews 审计表']
    reasons = []
    for ref in doc_refs:
        doc_id = str(ref['document_id'])
        expected = str(ref.get('expected_sha256') or '').strip()
        if not expected:
            reasons.append(f'关联文档 {doc_id} 缺少固定 expected_sha256')
            continue
        row = con.execute(
            "SELECT id, review_status, content_sha256, url FROM discovery_documents WHERE id=?",
            (doc_id,),
        ).fetchone()
        if row is None:
            reasons.append(f'关联文档 {doc_id} 不存在')
            continue
        review_status, sha, doc_url = row[1], row[2], row[3]
        if review_status != 'approved':
            reasons.append(f'关联文档 {doc_id} 当前状态为 {review_status}（已非 approved）')
            continue
        if expected != sha:
            reasons.append(f'关联文档 {doc_id} 原文哈希已变更，需重新评估')
            continue
        if ref.get('url') and doc_url and ref['url'] != doc_url:
            reasons.append(f'关联文档 {doc_id} 来源 URL 不一致')
            continue
        rev_rows = con.execute(
            "SELECT decision_json FROM discovery_reviews WHERE document_id=? AND content_sha256=?",
            (doc_id, sha),
        ).fetchall()
        has_approved_decision = False
        for rev in rev_rows:
            try:
                dec = json.loads(rev[0] or '{}')
            except (ValueError, TypeError):
                dec = {}
            if dec.get('review_status') == 'approved':
                has_approved_decision = True
                break
        if not has_approved_decision:
            reasons.append(f'关联文档 {doc_id} 缺少 approved 审核决定记录')
    return len(reasons) == 0, reasons


def load_events(con=None):
    data=json.loads(PATH.read_text(encoding='utf-8'))
    seen=set()
    for event in data['events']:
        if event.get('region')!='domestic':raise ValueError('mainland employment events only')
        if event['id'] in seen:raise ValueError('duplicate event ID; merge sources into the same event')
        seen.add(event['id'])
        date.fromisoformat(event['event_date'])
        if event['event_type'] not in TYPES or event['status'] not in STATUSES:raise ValueError('unsupported event type/status')
        if event.get('ai_relation','unknown') not in AI_RELATIONS:raise ValueError('unsupported AI causality status')
        if event.get('ai_relation')=='explicit_ai' and not str(event.get('ai_relation_evidence') or '').strip():
            raise ValueError('explicit AI causality requires an exact source excerpt')
        if not event.get('sources') or not event.get('unresolved'):raise ValueError('sources and unresolved boundary required')
        if event['status']=='announced' and not any(s['grade']=='official' for s in event['sources']):raise ValueError('announcement requires official evidence')
        if event['status']=='implemented' and not event.get('implementation_evidence'):raise ValueError('implementation evidence required')
        doc_refs = [s for s in event.get('sources', []) if isinstance(s, dict) and s.get('document_id')]
        ok, reasons = verify_event_documents(event, con)
        event['evidence_bound'] = bool(doc_refs)
        event['evidence_verified'] = ok
        event['evidence_stale'] = not ok
        if not ok:
            event['evidence_stale_reasons'] = reasons
    return data


def select_events(events, query, include_stale=False):
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
        if not include_stale and e.get('evidence_stale'):continue
        if any(filters[k] and e.get(k)!=filters[k] for k in ['event_type','region']):continue
        if filters['event_status'] and e['status']!=filters['event_status']:continue
        if filters['since'] and e['event_date']<filters['since']:continue
        if filters['until'] and e['event_date']>filters['until']:continue
        if filters['q'] and filters['q'].lower() not in (e['company']+' '+e['title']+' '+e.get('department','')).lower():continue
        result.append(e)
    return sorted(result,key=lambda e:(e['event_date'],e['id']),reverse=True)


def event_summary(events):
    valid = [e for e in events if not e.get('evidence_stale')]
    stale_count = sum(1 for e in events if e.get('evidence_stale'))
    return {'total':len(valid),'announced':sum(e['event_type']=='layoff' and e['status']=='announced' for e in valid),'implemented':sum(e['event_type']=='layoff' and e['status']=='implemented' for e in valid),'responses':sum(e['status'] in {'response_reported','denied'} for e in valid),'pending':sum(e['status'] in {'pending','reported'} for e in valid),'ai_explicit':sum(e.get('ai_relation')=='explicit_ai' for e in valid),'ai_unconfirmed':sum(e.get('ai_relation')=='related_unconfirmed' for e in valid),'stale':stale_count,'headcount_total':None,'note':'按独立事件计数；不同地区、岗位/员工口径和重叠轮次不合并成人数总计。AI因果仅在原文明确归因且保存原文摘录时标记；证据失效事件不计入公开总数。'}


def build_daily_digest(*, report_date=None, stats, events_summary, events_items, approved_signals_count, pending_signals_count, coverage_note):
    """Unified structured daily digest shared by /api/report and static console export."""
    day = report_date or datetime.now(TZ).date().isoformat()
    open_cnt = int(stats.get('open', stats.get('ai_total', 0)) or 0)
    today_new = int(stats.get('today_new', 0) or 0)
    valid_events = [e for e in events_items if not e.get('evidence_stale')]
    ev_total = int(events_summary.get('total', len(valid_events)) or 0)
    items = [
        {
            'title': f'公开岗位存量 {open_cnt} 条（当日新收录 {today_new} 条）',
            'point': f"build {stats.get('build', 0)} 条，adjacent {stats.get('adjacent', 0)} 条；链接核验 {stats.get('verified', 0)} 条。",
            'source': 'ai_jobs.db',
        },
        {
            'title': f'已复核就业事件 {ev_total} 起，已复核线索 {approved_signals_count} 条',
            'point': f'待人工复核候选 {pending_signals_count} 条；空事件不代表市场没有发生调整。',
            'source': 'employment_events + discovery_documents',
        },
    ]
    for ev in valid_events[:3]:
        items.append({
            'title': f"{ev.get('company', '')} · {ev.get('title', '')}",
            'point': f"事件日期 {ev.get('event_date')} · 状态 {STATUSES.get(ev.get('status'), ev.get('status'))}",
            'source': (ev.get('sources') or [{}])[0].get('name', '已复核来源'),
        })
    return {
        'date': day,
        'title': f'AI 就业与舆情日报（{day}）',
        'intro': f'截至 {day}（北京时间），公开岗位 {open_cnt} 条（当日新收录 {today_new} 条），已复核就业事件 {ev_total} 起，已复核线索 {approved_signals_count} 条，待核候选 {pending_signals_count} 条。',
        'items': items,
        'outro': coverage_note,
    }


def event_payload(query, con=None):
    data=load_events(con=con)
    all_matched = select_events(data['events'], query, include_stale=True)
    valid_items = [e for e in all_matched if not e.get('evidence_stale')]
    return {'items':valid_items,'summary':event_summary(all_matched),'coverage_note':data['coverage_note'],'ai_relation_labels':AI_RELATIONS}
