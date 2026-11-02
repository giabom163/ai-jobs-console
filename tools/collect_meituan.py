#!/usr/bin/env python
"""采集美团公开岗位（免登录接口）。

三种模式：
  1) 关键词增量（默认推荐）：--keywords "算法,大模型,AI Agent"，只收 AI 相关岗位，
     并且只收「最近 N 天 / since 之后」发布或刷新的岗位（旧岗位不爬）。
  2) 全量快照：--snapshot，翻完所有页，用于「缺席即关闭」+ 回填发布时间。
  3) 全量入库：不带 keywords 也不带 snapshot（调试用）。

时间口径：美团 list 接口的 firstPostTime 常为 null，refreshTime 必有（epoch ms）。
首次发布时间只取 firstPostTime；刷新时间只取 refreshTime，未知不互相兜底。

示例：
  python tools/collect_meituan.py --keywords "算法,大模型,机器学习,AI Agent" --limit 120
  python tools/collect_meituan.py --snapshot
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
SCHEMA = ROOT / "schema" / "ai_employment.sql"
sys.path.insert(0, str(ROOT))
from tools.ai_lexicon import classify  # noqa: E402
from tools.job_geography import is_domestic_only  # noqa: E402
from tools.job_fields import ensure_columns as ensure_source_columns, save_source_fields
from tools.job_details import department_name
from tools.request_policy import RequestBudget, DEFAULT_BUDGET
from tools.ingestion_contract import standard_fields, validate_record, begin_batch, finish_batch, abort_batch, record_external_failure
from tools.source_runs import start_run, finish_run

API = "https://zhaopin.meituan.com/api/official/job/getJobList"
DETAIL = "https://zhaopin.meituan.com/web/position/detail?jobUnionId={id}&jobShareType=1&highlightType={hl}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
HARD_FLOOR = "2026-06-01"   # 用户口径：最晚也要 6 月之后
BEIJING_TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(timezone.utc).isoformat()


def default_since(days: int = 90) -> str:
    """默认最近 3 个月，但不早于 2026-06-01。"""
    floor = datetime.fromisoformat(HARD_FLOOR).date()
    recent = (datetime.now(BEIJING_TZ) - timedelta(days=days)).date()
    return max(floor, recent).isoformat()


def epoch_ms(ms):
    try:
        if int(ms) <= 0:
            return None
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def job_times(item):
    first = epoch_ms(item.get("firstPostTime"))
    refresh = epoch_ms(item.get("refreshTime"))
    return first, refresh


def beijing_date(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(BEIJING_TZ).date().isoformat()
    except (TypeError, ValueError):
        return None


# Verified Mainland-only tech recruitment hubs with 001 codes
DEFAULT_MAINLAND_CITIES = [
    {"code": "001001", "name": "北京市"},
    {"code": "001009", "name": "上海市"},
    {"code": "001019002", "name": "深圳市"},
    {"code": "001011001", "name": "杭州市"},
    {"code": "001023001", "name": "成都市"},
]


def fetch(page, size=15, keywords="", city_list=None, budget=None):
    budget = budget or DEFAULT_BUDGET
    cities = city_list if city_list is not None else DEFAULT_MAINLAND_CITIES
    body = {"page": {"pageNo": page, "pageSize": size}, "jobShareType": "1",
            "keywords": keywords, "cityList": cities, "department": [],
            "jobType": [{"code": "1", "subCode": []}, {"code": "2", "subCode": []},
                        {"code": "3", "subCode": []}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Referer": "https://zhaopin.meituan.com/web/social"})
    with budget.open(req, timeout=30, source_id='official:meituan', operation='list') as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != 1:
        budget.stop(API, 'api_refused')
        raise RuntimeError('meituan API refused this request')
    return payload.get("data") or {}


def ensure_columns(con):
    ensure_source_columns(con)
    have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    for col in ("ai_reason", "posted_at", "updated_at"):
        if col not in have:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
    con.commit()


def upsert(con, item, cid, since: str | None = None, observed_at=None):
    observed_at=observed_at or now()
    ext = str(item.get("jobUnionId") or "")
    title = (item.get("name") or "").strip()
    if not ext or not title:
        return None
    posted_at, updated_at = job_times(item)
    published_day = beijing_date(posted_at)
    if since and not published_day:
        return {'skipped_source_date_unknown': True}
    if since and published_day < since:
        return {"skipped_old": True, "posted_at": posted_at}
    source_update_day = beijing_date(updated_at)
    source_date_qualifies = bool(source_update_day and source_update_day >= HARD_FLOOR)
    cities = " / ".join(c.get("name", "") for c in (item.get("cityList") or []) if c.get("name"))
    if not is_domestic_only(cities):
        return {"skipped_non_domestic": True}
    desc = "\n".join(x for x in (item.get("jobDuty"), item.get("jobRequirement")) if x)
    jtype = str(item.get("jobType") or "")
    hl = "social" if jtype == "3" else "campus"
    url = DETAIL.format(id=ext, hl=hl)
    content_hash = hashlib.sha256(f"{title}\n{cities}\n{desc}".encode()).hexdigest()
    derived = standard_fields(title, desc, department_name(item.get('department')) or '')
    tier, conf, reason = derived['ai_relevance'], derived['ai_confidence'], derived['ai_reason']
    validate_record(dict(source='official:meituan', external_id=ext, title=title, location=cities, first_seen=observed_at, last_seen=observed_at, url=url))
    existing = con.execute(
        "SELECT id,content_hash,status FROM jobs WHERE source='official:meituan' AND external_id=?", (ext,)).fetchone()
    source_date_demoted = False
    if existing:
        con.execute("UPDATE jobs SET title=?,location=?,description=?,ai_relevance=?,ai_confidence=?,"
                    "ai_reason=?,last_seen=?,url=?,content_hash=?,posted_at=COALESCE(?,posted_at),"
                    "updated_at=COALESCE(?,updated_at) WHERE id=?",
                    (title, cities, desc[:20000], tier, conf, reason, observed_at, url, content_hash,
                     posted_at, updated_at, existing["id"]))
        jid = existing["id"]
        added = False
        if tier in ('build', 'adjacent') and not source_date_qualifies and existing['status'] == 'open':
            source_date_demoted = con.execute(
                "UPDATE jobs SET status='verification_pending' WHERE id=? AND status='open'", (jid,)
            ).rowcount == 1
        if tier in ("build", "adjacent"):
            con.execute("UPDATE jobs SET status='verification_pending' WHERE id=? AND status='excluded'", (jid,))
        if existing["content_hash"] != content_hash:
            con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,old_hash,new_hash,note) "
                        "VALUES(?,?,?,?,?,?)",
                        (jid, "changed", observed_at, existing["content_hash"], content_hash, reason))
    else:
        jid = "job-" + hashlib.sha256(f"meituan|{ext}".encode()).hexdigest()[:24]
        con.execute("INSERT INTO jobs(id,source,external_id,company_id,title,location,description,"
                    "ai_relevance,ai_confidence,ai_reason,first_seen,last_seen,status,url,content_hash,"
                    "posted_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'verification_pending',?,?,?,?)",
                    (jid, "official:meituan", ext, cid, title, cities, desc[:20000], tier, conf,
                     reason, observed_at, observed_at, url, content_hash, posted_at, updated_at))
        con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,new_hash,note) "
                    "VALUES(?,?,?,?,?)", (jid, "new", observed_at, content_hash, reason))
        added = True
    con.execute("UPDATE jobs SET normalized_title=?,status=CASE WHEN ai_relevance='none' AND status NOT IN ('merged','closed') THEN 'excluded' ELSE status END WHERE id=?",
                (derived['normalized_title'], jid))
    con.execute("INSERT INTO job_evidence(job_id,source,url,seen_at) VALUES(?,?,?,?) "
                "ON CONFLICT(job_id,source) DO UPDATE SET url=excluded.url,seen_at=excluded.seen_at",
                (jid, "official:meituan", url, observed_at))
    save_source_fields(con, jid, published=posted_at, updated=updated_at,
                       department=department_name(item.get('department')) or None,
                       source_status='listed', checked_at=observed_at)
    item_json = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    date_evidence = {
        'source': 'official:meituan',
        'external_id': ext,
        'observed_at': observed_at,
        'published_at': {'field': 'firstPostTime', 'raw': item.get('firstPostTime'), 'parsed': posted_at},
        'source_updated_at': {'field': 'refreshTime', 'raw': item.get('refreshTime'), 'parsed': updated_at},
        'item_sha256': hashlib.sha256(item_json.encode('utf-8')).hexdigest(),
        'freshness_cutoff': HARD_FLOOR,
        'freshness_result': 'pass' if source_date_qualifies else 'review',
    }
    con.execute('UPDATE jobs SET date_evidence_json=? WHERE id=?',
                (json.dumps(date_evidence, ensure_ascii=False, sort_keys=True), jid))
    if added or existing['content_hash'] != content_hash:
        con.execute('INSERT INTO job_snapshots(job_id,seen_at,status,content_hash,location,raw_json) VALUES(?,?,?,?,?,?)',
                    (jid,observed_at,con.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],content_hash,cities,json.dumps({'kind':'official_job_list_record','payload':item},ensure_ascii=False)))
    return {"added": added, "tier": tier, "title": title, "posted_at": posted_at,
            "source_date_demoted": source_date_demoted,
            "source_date_result": 'pass' if source_date_qualifies else 'review'}


def main():
    parser = argparse.ArgumentParser(description='Bounded Meituan list capture; no automatic verification promotion')
    parser.add_argument('--db', default=str(DB))
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--page-size', type=int, default=15)
    parser.add_argument('--keywords', default='')
    parser.add_argument('--since', '--published-since', dest='since', default='', help='Explicit first-publication lower bound; unknown dates are held out')
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--ai-only', action='store_true')
    parser.add_argument('--json-out', default='')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.limit <= 15 or not 1 <= args.page_size <= 15:
        parser.error('limit and page-size must be 1..15; start with --limit 3')
    if args.snapshot:
        parser.error('Unbounded snapshots disabled; partial captures cannot establish absence')
    keywords = [x.strip() for x in args.keywords.split(',') if x.strip()] or ['']
    con = None
    batch = None
    budget = RequestBudget()
    run_id = None
    request_start = budget.requests
    if not args.dry_run:
        run_id = start_run(args.db, 'company-meituan:official:meituan', '美团', 'official')
    stats = dict(mode='preview' if args.dry_run else 'bounded_list', seen=0, added=0, updated=0,
                 source_date_demoted=0, errors=[], requests=0)
    try:
        staged=[]
        seen_ids = set()
        for keyword in keywords[:15]:
            if stats['seen'] >= args.limit:
                break
            data = fetch(1, min(args.page_size, args.limit - stats['seen']), keyword, budget=budget)
            for item in (data.get('list') or [])[:args.limit - stats['seen']]:
                ext = str(item.get('jobUnionId') or '')
                if ext in seen_ids:
                    continue
                seen_ids.add(ext)
                stats['seen'] += 1
                if args.ai_only:
                    desc = '\n'.join(x for x in (item.get('jobDuty'), item.get('jobRequirement')) if x)
                    if standard_fields(item.get('name') or '', desc, department_name(item.get('department')) or '')['ai_relevance'] == 'none':
                        stats['skipped_non_ai'] = stats.get('skipped_non_ai', 0) + 1
                        continue
                staged.append((item,now()))
        if not args.dry_run:
            con=sqlite3.connect('file:'+Path(args.db).resolve().as_posix()+'?mode=rw',uri=True)
            con.row_factory=sqlite3.Row
            batch=begin_batch(con,'meituan_bounded_list')
            cid='company-meituan'
            con.execute("INSERT OR IGNORE INTO companies(id,name,ats_type,created_at) VALUES(?,?,?,?)",(cid,'美团','official',now()))
            for item,observed_at in staged:
                result=upsert(con,item,cid,args.since or None,observed_at=observed_at)
                if result and result.get('skipped_source_date_unknown'):
                    stats['skipped_source_date_unknown']=stats.get('skipped_source_date_unknown',0)+1
                if result and 'added' in result:
                    stats['added' if result['added'] else 'updated']+=1
                    stats['source_date_demoted']+=int(result.get('source_date_demoted',False))
        if con:
            batch['request_events'] = budget.events
            stats['batch'] = finish_batch(con, batch)
            con.commit()
        if run_id:
            finish_run(args.db, run_id, 'succeeded', request_count=budget.requests-request_start,
                       records_seen=stats['seen'], records_added=stats['added'],
                       records_updated=stats['updated'], details={'operation':'meituan_list'})
    except BaseException as exc:
        if con and batch:
            batch['request_events'] = budget.events
            abort_batch(con, batch, exc)
        if batch is None:
            stats['failure_id']=record_external_failure('meituan_bounded_list',exc,budget.events)
        if run_id:
            try:
                finish_run(args.db, run_id, 'failed', request_count=budget.requests-request_start,
                           records_seen=stats['seen'], error=exc,
                           details={'operation':'meituan_list','ingestion_rolled_back':bool(batch)})
            except ValueError:
                pass
        stats['errors'].append(type(exc).__name__)
        stats['rolled_back'] = bool(con)
    finally:
        if con:
            con.close()
    stats['requests'] = budget.requests
    stats['request_events'] = budget.events
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(stats,ensure_ascii=True))
    return 1 if stats['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
