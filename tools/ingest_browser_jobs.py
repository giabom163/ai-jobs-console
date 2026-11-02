#!/usr/bin/env python
"""将 Ego Lite 浏览器只读提取的岗位 JSON 安全导入 ai_jobs.db。

特性与安全契约：
  1) 前置严格校验中国境内地域（is_domestic_only）。
  2) 使用 standard_fields 与 classify 计算 AI 级别与置信度。
  3) 接入 ingestion_contract 批量事务，自动创建物理备份与审计记录。
  4) 新源记录默认归入 status='review'。

示例：
  python tools/ingest_browser_jobs.py --input C:/Users/Lenovo/.codex/documents/ai-jobs-validation-20261102/baidu-browser-extracted.json
  python tools/ingest_browser_jobs.py --input C:/Users/Lenovo/.codex/documents/ai-jobs-validation-20261102/boss-browser-extracted.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
sys.path.insert(0, str(ROOT))

from tools.ai_lexicon import classify  # noqa: E402
from tools.job_geography import is_domestic_only  # noqa: E402
from tools.job_fields import ensure_columns as ensure_source_columns, save_source_fields  # noqa: E402
from tools.ingestion_contract import standard_fields, validate_record, begin_batch, finish_batch, abort_batch  # noqa: E402

BAIDU_COMPANY_ID = "company-455ece81de5605d85e54ec41"


def now():
    return datetime.now(timezone.utc).isoformat()


def upsert_browser_job(con, item, source, observed_at=None):
    observed_at = observed_at or now()
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    url = item.get("url") or ""
    location = str(item.get("location") or "全国").strip()
    if not is_domestic_only(location):
        return {"skipped_non_domestic": True}

    company_name = str(item.get("company") or ("百度" if "baidu" in source else "")).strip()
    card_text = str(item.get("card_text") or "").strip()
    desc = card_text[:5000]

    # Resolve company_id
    cid = None
    if "baidu" in source:
        cid = BAIDU_COMPANY_ID
    elif company_name:
        row = con.execute("SELECT id FROM companies WHERE name=?", (company_name,)).fetchone()
        if row:
            cid = row[0]
        else:
            cid = "company-" + hashlib.sha256(company_name.encode("utf-8")).hexdigest()[:24]
            con.execute("INSERT OR IGNORE INTO companies(id, name, ats_type, created_at) VALUES(?,?,?,?)",
                        (cid, company_name, "platform", observed_at))

    # Resolve external_id
    ext = None
    if "job_detail/" in url:
        m = re.search(r"job_detail/([A-Za-z0-9_-]+)\.html", url)
        if m:
            ext = m.group(1)
    if not ext:
        ext = hashlib.sha256(f"{source}|{url}|{title}".encode("utf-8")).hexdigest()[:20]

    content_hash = hashlib.sha256(f"{title}\n{location}\n{desc}".encode()).hexdigest()
    derived = standard_fields(title, desc, "")
    tier, conf, reason = derived["ai_relevance"], derived["ai_confidence"], derived["ai_reason"]

    validate_record(dict(
        source=source,
        external_id=ext,
        title=title,
        location=location,
        first_seen=observed_at,
        last_seen=observed_at,
        url=url,
    ))

    existing = con.execute(
        "SELECT id, content_hash, status FROM jobs WHERE source=? AND external_id=?",
        (source, ext),
    ).fetchone()

    if existing:
        con.execute(
            "UPDATE jobs SET title=?,location=?,description=?,ai_relevance=?,ai_confidence=?,"
            "ai_reason=?,last_seen=?,url=?,content_hash=? WHERE id=?",
            (title, location, desc[:20000], tier, conf, reason, observed_at, url, content_hash, existing["id"]),
        )
        jid = existing["id"]
        added = False
        if tier in ("build", "adjacent"):
            con.execute("UPDATE jobs SET status='review' WHERE id=? AND status='excluded'", (jid,))
    else:
        jid = "job-" + hashlib.sha256(f"{source}|{ext}".encode()).hexdigest()[:24]
        con.execute(
            "INSERT INTO jobs(id,source,external_id,company_id,title,location,description,"
            "ai_relevance,ai_confidence,ai_reason,first_seen,last_seen,status,url,content_hash,"
            "posted_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'review',?,?,?,?)",
            (jid, source, ext, cid, title, location, desc[:20000], tier, conf,
             reason, observed_at, observed_at, url, content_hash, observed_at, observed_at),
        )
        con.execute(
            "INSERT INTO job_changes(job_id,change_type,changed_at,new_hash,note) VALUES(?,?,?,?,?)",
            (jid, "new", observed_at, content_hash, reason),
        )
        added = True

    con.execute(
        "UPDATE jobs SET normalized_title=?,status=CASE WHEN ai_relevance='none' AND status NOT IN ('merged','closed') THEN 'excluded' ELSE status END WHERE id=?",
        (derived["normalized_title"], jid),
    )
    con.execute(
        "INSERT INTO job_evidence(job_id,source,url,seen_at) VALUES(?,?,?,?) "
        "ON CONFLICT(job_id,source) DO UPDATE SET url=excluded.url,seen_at=excluded.seen_at",
        (jid, source, url, observed_at),
    )
    save_source_fields(
        con, jid,
        published=observed_at,
        updated=observed_at,
        source_status="listed",
        checked_at=observed_at,
    )
    return {"added": added, "jid": jid}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--input", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    source = data.get("source") or "platform:browser_extracted"
    items = data.get("items") or []
    captured_at = data.get("captured_at") or now()

    stats = {
        "source": source,
        "input": str(input_path),
        "total": len(items),
        "added": 0,
        "updated": 0,
        "skipped_non_domestic": 0,
        "errors": [],
    }

    if args.dry_run:
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    con = sqlite3.connect("file:" + Path(args.db).resolve().as_posix() + "?mode=rw", uri=True)
    con.row_factory = sqlite3.Row
    ensure_source_columns(con)
    batch = begin_batch(con, f"{source}_ingestion")

    try:
        for it in items:
            res = upsert_browser_job(con, it, source, observed_at=captured_at)
            if res and res.get("skipped_non_domestic"):
                stats["skipped_non_domestic"] += 1
            elif res and "added" in res:
                stats["added" if res["added"] else "updated"] += 1

        stats["batch"] = finish_batch(con, batch)
        con.commit()
    except BaseException as exc:
        abort_batch(con, batch, exc)
        stats["errors"].append(str(exc))
    finally:
        con.close()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
