#!/usr/bin/env python
"""将 Ego Lite 浏览器只读提取的岗位 JSON 安全导入 ai_jobs.db。

特性与安全契约：
  1) 前置严格校验中国境内地域（is_domestic_only）。
  2) 使用 standard_fields 与 classify 计算 AI 级别与置信度。
  3) 接入 ingestion_contract 批量事务，自动创建物理备份与审计记录。
  4) 新源记录默认归入 status='review'。
  5) 来源发布/更新时间未知时严格保持 None，杜绝将抓取时间伪造成发布时间。
  6) 批次状态与消费锁校验：拒绝入库带阻断标记或已消费的历史批次。

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


def parse_salary_evidence(text):
    raw = str(text or '').strip()
    result = {'raw': raw, 'currency': 'CNY', 'period': 'unknown', 'unit': 'unknown',
              'monthly_min_k': None, 'monthly_max_k': None, 'basis': 'source_text_only'}
    # Do not invent working days or convert annual compensation into monthly pay.
    if not raw or not re.search(r'/月|每月|月薪|元/月', raw):
        return result
    amount = r'(?<![\d.])(\d+(?:\.\d+)?)\s*([kK万Ww]|元)?\s*(?:[-~至到]\s*(\d+(?:\.\d+)?)\s*([kK万Ww]|元)?)?'
    match = re.search(r'月薪\s*[:：]?\s*' + amount, raw) or re.search(amount + r'\s*(?:/月|每月)', raw)
    if not match:
        return result
    lo_val, lo_unit, hi_val, hi_unit = match.group(1), match.group(2), match.group(3), match.group(4)
    unit = hi_unit or lo_unit
    if not unit:
        return result
    lo = float(lo_val)
    hi = float(hi_val) if hi_val else lo
    if not 0 < lo <= hi:
        return result
    lo_factor = 10 if (lo_unit or unit) in ('万', 'W', 'w') else (0.001 if (lo_unit or unit) == '元' else 1)
    hi_factor = 10 if unit in ('万', 'W', 'w') else (0.001 if unit == '元' else 1)
    result.update(period='month', unit='kCNY', monthly_min_k=round(lo * lo_factor, 4),
                  monthly_max_k=round(hi * hi_factor, 4), basis='explicit_monthly_source_text')
    return result


def parse_salary_range(text: str) -> tuple[float | None, float | None, str]:
    evidence = parse_salary_evidence(text)
    return evidence['monthly_min_k'], evidence['monthly_max_k'], evidence['currency']


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
    sal_str = str(item.get("salary") or "").strip()
    s_min, s_max, curr = parse_salary_range(sal_str)

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
            "posted_at,updated_at,salary_min,salary_max,currency) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'review',?,?,?,?,?,?,?)",
            (jid, source, ext, cid, title, location, desc[:20000], tier, conf,
             reason, observed_at, observed_at, url, content_hash, None, None, s_min, s_max, curr),
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
    if sal_str:
        evidence = parse_salary_evidence(sal_str)
        con.execute('UPDATE jobs SET salary_min=?,salary_max=?,currency=?,salary_evidence_json=? WHERE id=?',
                    (s_min,s_max,curr,json.dumps(evidence,ensure_ascii=False),jid))
    # 官方发布与更新时间未知时严格保持 None，不得用观察时间伪造
    save_source_fields(
        con, jid,
        published=None,
        updated=None,
        source_status="listed",
        checked_at=observed_at,
    )
    return {"added": added, "jid": jid}


def ensure_batches_table(con: sqlite3.Connection):
    con.execute("""
        CREATE TABLE IF NOT EXISTS ingested_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            file_name TEXT NOT NULL,
            content_sha256 TEXT NOT NULL UNIQUE,
            items_count INTEGER NOT NULL,
            added_count INTEGER NOT NULL,
            updated_count INTEGER NOT NULL,
            skipped_count INTEGER NOT NULL,
            ingested_at TEXT NOT NULL
        )
    """)


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

    # 保持输入文件严格只读，计算原始内容 SHA-256 (R4)
    raw_bytes = input_path.read_bytes()
    content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    data = json.loads(raw_bytes.decode("utf-8"))

    source = data.get("source") or "platform:browser_extracted"
    items = data.get("items") or []
    captured_at = data.get("captured_at") or now()

    stats = {
        "source": source,
        "input": str(input_path),
        "content_sha256": content_sha256,
        "total": len(items),
        "added": 0,
        "updated": 0,
        "skipped_non_domestic": 0,
        "errors": [],
    }

    # 批次异常与平台阻断校验
    aborted = data.get("aborted_reason") or data.get("error")
    if aborted:
        err_msg = f"批次记录平台阻断或异常 ({aborted})，拒绝入库"
        stats["errors"].append(err_msg)
        print(json.dumps(stats, ensure_ascii=False), file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    con = sqlite3.connect("file:" + Path(args.db).resolve().as_posix() + "?mode=rw", uri=True)
    con.row_factory = sqlite3.Row

    # 数据库原子消费凭据校验 (R4): 同一内容哈希在同事务内保证唯一消费与幂等防重放
    cur = con.cursor()
    table_exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingested_batches'").fetchone()
    existing_batch = None
    if table_exists:
        cur.execute("SELECT id, added_count, updated_count, skipped_count, ingested_at FROM ingested_batches WHERE content_sha256 = ?", (content_sha256,))
        existing_batch = cur.fetchone()
    if existing_batch:
        stats["replayed"] = True
        stats["batch_id"] = existing_batch["id"]
        stats["added"] = existing_batch["added_count"]
        stats["updated"] = existing_batch["updated_count"]
        stats["skipped_non_domestic"] = existing_batch["skipped_count"]
        stats["ingested_at"] = existing_batch["ingested_at"]
        print(f"[ingest] 该批次此前已在数据库成功提交 (Batch #{existing_batch['id']}, ingested_at: {existing_batch['ingested_at']})，幂等返回已有记录，杜绝重复写入", flush=True)
        con.close()
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    def migrate_browser_input(connection):
        ensure_batches_table(connection)
        if 'salary_evidence_json' not in {row[1] for row in connection.execute('PRAGMA table_info(jobs)')}:
            connection.execute('ALTER TABLE jobs ADD COLUMN salary_evidence_json TEXT')
        connection.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?,?)', ('browser-input-v2-salary-evidence',now()))
    batch = begin_batch(con, f"{source}_ingestion", extra_migration=migrate_browser_input)

    try:
        for it in items:
            res = upsert_browser_job(con, it, source, observed_at=captured_at)
            if res and res.get("skipped_non_domestic"):
                stats["skipped_non_domestic"] += 1
            elif res and "added" in res:
                stats["added" if res["added"] else "updated"] += 1

        stats["batch"] = finish_batch(con, batch)

        # 在同一数据库事务内写入消费凭据记录，利用 UNIQUE(content_sha256) 物理杜绝并发竞争 (R4)
        cur.execute(
            """INSERT INTO ingested_batches(source, file_name, content_sha256, items_count, added_count, updated_count, skipped_count, ingested_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, input_path.name, content_sha256, len(items), stats["added"], stats["updated"], stats["skipped_non_domestic"], now())
        )
        con.commit()

    except sqlite3.IntegrityError as ie:
        # 并发冲突：另一进程已抢先提交相同哈希批次
        con.rollback()
        abort_batch(con, batch, ie)
        stats["errors"].append(f"并发消费冲突: 该批次已由另一进程提交 ({ie})")
    except BaseException as exc:
        con.rollback()
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
