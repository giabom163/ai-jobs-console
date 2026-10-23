#!/usr/bin/env python
"""采集美团公开岗位（免登录接口）。

三种模式：
  1) 关键词增量（默认推荐）：--keywords "算法,大模型,AI Agent"，只收 AI 相关岗位，
     并且只收「最近 N 天 / since 之后」发布或刷新的岗位（旧岗位不爬）。
  2) 全量快照：--snapshot，翻完所有页，用于「缺席即关闭」+ 回填发布时间。
  3) 全量入库：不带 keywords 也不带 snapshot（调试用）。

时间口径：美团 list 接口的 firstPostTime 常为 null，refreshTime 必有（epoch ms）。
岗位时间取 max(firstPostTime, refreshTime)，同时写入 posted_at / updated_at。

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

API = "https://zhaopin.meituan.com/api/official/job/getJobList"
DETAIL = "https://zhaopin.meituan.com/web/position/detail?jobUnionId={id}&jobShareType=1&highlightType={hl}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
HARD_FLOOR = "2026-06-01"   # 用户口径：最晚也要 6 月之后


def now():
    return datetime.now(timezone.utc).isoformat()


def default_since(days: int = 90) -> str:
    """默认最近 3 个月，但不早于 2026-06-01。"""
    floor = datetime.fromisoformat(HARD_FLOOR).date()
    recent = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    return max(floor, recent).isoformat()


def epoch_ms(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def job_times(item):
    first = epoch_ms(item.get("firstPostTime"))
    refresh = epoch_ms(item.get("refreshTime"))
    posted = first or refresh
    return posted, refresh or posted


def fetch(page, size=100, keywords=""):
    body = {"page": {"pageNo": page, "pageSize": size}, "jobShareType": "1",
            "keywords": keywords, "cityList": [], "department": [],
            "jobType": [{"code": "1", "subCode": []}, {"code": "2", "subCode": []},
                        {"code": "3", "subCode": []}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Referer": "https://zhaopin.meituan.com/web/social"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != 1:
        raise RuntimeError(payload.get("message") or "meituan api error")
    return payload.get("data") or {}


def ensure_columns(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    for col in ("ai_reason", "posted_at", "updated_at"):
        if col not in have:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
    con.commit()


def upsert(con, item, cid, since: str | None = None):
    ext = str(item.get("jobUnionId") or "")
    title = (item.get("name") or "").strip()
    if not ext or not title:
        return None
    posted_at, updated_at = job_times(item)
    if since and posted_at and posted_at[:10] < since:
        return {"skipped_old": True, "posted_at": posted_at}
    cities = " / ".join(c.get("name", "") for c in (item.get("cityList") or []) if c.get("name"))
    desc = "\n".join(x for x in (item.get("jobDuty"), item.get("jobRequirement")) if x)
    jtype = str(item.get("jobType") or "")
    hl = "social" if jtype == "3" else "campus"
    url = DETAIL.format(id=ext, hl=hl)
    content_hash = hashlib.sha256(f"{title}\n{cities}\n{desc}".encode()).hexdigest()
    tier, conf, reason = classify(title, desc, item.get("department") or "")
    existing = con.execute(
        "SELECT id,content_hash FROM jobs WHERE source='official:meituan' AND external_id=?", (ext,)).fetchone()
    if existing:
        con.execute("UPDATE jobs SET title=?,location=?,description=?,ai_relevance=?,ai_confidence=?,"
                    "ai_reason=?,last_seen=?,url=?,content_hash=?,posted_at=COALESCE(?,posted_at),"
                    "updated_at=COALESCE(?,updated_at) WHERE id=?",
                    (title, cities, desc[:20000], tier, conf, reason, now(), url, content_hash,
                     posted_at, updated_at, existing["id"]))
        jid = existing["id"]
        added = False
        if tier in ("build", "adjacent"):
            con.execute("UPDATE jobs SET status='open' WHERE id=? AND status='excluded'", (jid,))
        if existing["content_hash"] != content_hash:
            con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,old_hash,new_hash,note) "
                        "VALUES(?,?,?,?,?,?)",
                        (jid, "changed", now(), existing["content_hash"], content_hash, reason))
    else:
        jid = "job-" + hashlib.sha256(f"meituan|{ext}".encode()).hexdigest()[:24]
        con.execute("INSERT INTO jobs(id,source,external_id,company_id,title,location,description,"
                    "ai_relevance,ai_confidence,ai_reason,first_seen,last_seen,status,url,content_hash,"
                    "posted_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
                    (jid, "official:meituan", ext, cid, title, cities, desc[:20000], tier, conf,
                     reason, now(), now(), url, content_hash, posted_at, updated_at))
        con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,new_hash,note) "
                    "VALUES(?,?,?,?,?)", (jid, "new", now(), content_hash, reason))
        added = True
    con.execute("INSERT INTO job_evidence(job_id,source,url,seen_at) VALUES(?,?,?,?) "
                "ON CONFLICT(job_id,source) DO UPDATE SET url=excluded.url,seen_at=excluded.seen_at",
                (jid, "official:meituan", url, now()))
    return {"added": added, "tier": tier, "title": title, "posted_at": posted_at}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--keywords", default="", help="逗号分隔；给出则按关键词搜索，只收 AI 岗")
    parser.add_argument("--since", default="", help="只收该日期之后发布/刷新的岗位；默认近 90 天且不早于 2026-06-01")
    parser.add_argument("--snapshot", action="store_true", help="全量翻页，用于缺席即关闭")
    parser.add_argument("--ai-only", action="store_true")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    since = args.since or default_since()
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    ai_only = args.ai_only or bool(keywords)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    ensure_columns(con)
    cid = "company-meituan"
    con.execute("INSERT INTO companies(id,name,ats_type,board_token,watchlist,domain,created_at) "
                "VALUES(?,?,?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET domain=excluded.domain",
                (cid, "美团", "official", "zhaopin.meituan.com", "meituan.com", now()))

    stats = {"mode": "snapshot" if args.snapshot else ("keyword" if keywords else "full"),
             "since": None if args.snapshot else since, "keywords": keywords,
             "seen": 0, "added": 0, "updated": 0, "skipped_old": 0, "skipped_non_ai": 0,
             "closed": 0, "by_tier": {"build": 0, "adjacent": 0, "none": 0}, "errors": []}
    seen_ids = set()

    def handle(item):
        ext = str(item.get("jobUnionId") or "")
        if ext and ext in seen_ids:
            return
        if ext:
            seen_ids.add(ext)
        stats["seen"] += 1
        if args.dry_run:
            title = (item.get("name") or "").strip()
            desc = "\n".join(x for x in (item.get("jobDuty"), item.get("jobRequirement")) if x)
            tier, _, _ = classify(title, desc, item.get("department") or "")
            posted, _ = job_times(item)
            stats["by_tier"][tier] += 1
            if since and posted and posted[:10] < since:
                stats["skipped_old"] += 1
            elif ai_only and tier == "none":
                stats["skipped_non_ai"] += 1
            return
        probe = upsert(con, item, cid, None if args.snapshot else since)
        if not probe:
            return
        if probe.get("skipped_old"):
            stats["skipped_old"] += 1
            return
        stats["by_tier"][probe["tier"]] += 1
        if ai_only and probe["tier"] == "none":
            con.execute("UPDATE jobs SET status='excluded' WHERE source='official:meituan' "
                        "AND external_id=?", (ext,))
            stats["skipped_non_ai"] += 1
        elif probe["added"]:
            stats["added"] += 1
        else:
            stats["updated"] += 1

    try:
        if args.snapshot:
            page = 1
            while True:
                data = fetch(page, args.page_size, "")
                rows = data.get("list") or []
                if not rows:
                    break
                for item in rows:
                    handle(item)
                con.commit()
                if len(rows) < args.page_size:
                    break
                page += 1
                time.sleep(0.6)
            # 缺席即关闭（只针对美团）
            if not args.dry_run:
                stale = con.execute(
                    "SELECT id FROM jobs WHERE source='official:meituan' AND status='open'").fetchall()
                for row in stale:
                    ext = con.execute("SELECT external_id FROM jobs WHERE id=?", (row["id"],)).fetchone()[0]
                    if ext not in seen_ids:
                        con.execute("UPDATE jobs SET status='closed' WHERE id=?", (row["id"],))
                        con.execute("INSERT INTO job_changes(job_id,change_type,changed_at,note) "
                                    "VALUES(?,?,?,?)", (row["id"], "closed", now(), "snapshot:absent"))
                        stats["closed"] += 1
                con.commit()
        elif keywords:
            per_kw = max(20, args.limit // max(1, len(keywords)))
            for kw in keywords:
                got, page = 0, 1
                while got < per_kw and stats["seen"] < args.limit * 2:
                    data = fetch(page, min(args.page_size, per_kw - got), kw)
                    rows = data.get("list") or []
                    if not rows:
                        break
                    for item in rows:
                        handle(item)
                    got += len(rows)
                    if len(rows) < args.page_size:
                        break
                    page += 1
                    time.sleep(0.6)
                con.commit()
                time.sleep(0.4)
        else:
            page = 1
            while stats["seen"] < args.limit:
                data = fetch(page, args.page_size, "")
                rows = data.get("list") or []
                if not rows:
                    break
                for item in rows:
                    handle(item)
                con.commit()
                if len(rows) < args.page_size:
                    break
                page += 1
                time.sleep(0.5)
    except Exception as exc:  # noqa: BLE001
        stats["errors"].append(str(exc))
        con.commit()

    stats["open_total"] = con.execute(
        "SELECT count(*) FROM jobs WHERE status='open'").fetchone()[0]
    stats["ai_open"] = con.execute(
        "SELECT count(*) FROM jobs WHERE status='open' AND ai_relevance IN ('build','adjacent')"
    ).fetchone()[0]
    text = json.dumps(stats, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    con.close()


if __name__ == "__main__":
    main()
