#!/usr/bin/env python
"""CLI tool for ingesting domestic AI employment signals into database.

Features:
- Mainland China location gate: requires verified domestic location, rejects unknown/overseas.
- Strict ISO date validation via datetime.fromisoformat.
- Truthful review status: default is 'pending' (unaudited), NEVER artificially 'approved'.
- Unified summary and content fields (eliminates KeyError).
- Mandatory physical backup before write (via backup_manager).
- Supports --dry-run and --target-db for safe offline/isolated testing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "ai_jobs.db"
TZ = timezone(timedelta(hours=8))

if __package__:
    from . import backup_manager
    from .job_geography import classify_location
else:
    import backup_manager
    from job_geography import classify_location


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", help="Signal ID (auto-generated if omitted)")
    parser.add_argument("--title", required=True, help="Signal title")
    parser.add_argument("--url", required=True, help="Source URL (must be valid http/https)")
    parser.add_argument("--source", required=True, help="Source name (e.g., 哔哩哔哩招聘, 字节跳动招聘)")
    parser.add_argument("--published-at", required=True, help="Publication date/time (ISO format or YYYY-MM-DD)")
    parser.add_argument("--summary", required=True, help="Employment summary text")
    parser.add_argument("--location", required=True, help="Work location (must be verified mainland China location)")
    parser.add_argument("--category", default="hiring", choices=["hiring", "layoff", "shrink", "freeze"], help="Event category (default: hiring)")
    parser.add_argument("--ai-scope", default="ai_role", choices=["ai_role", "ai_team", "ai_impact", "ai_company_other"], help="AI scope tag")
    parser.add_argument("--company-id", default="unknown", help="Company ID identifier")
    parser.add_argument("--target-db", help="Path to target SQLite DB (defaults to ai_jobs.db)")
    parser.add_argument("--dry-run", action="store_true", help="Perform validation and dry-run without modifying DB")
    return parser.parse_args(argv)


def validate_input(args) -> dict:
    # 1. URL 校验
    if not args.url.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")

    # 2. 地点门禁（复用 job_geography.classify_location 唯一入口，严格限定 domestic）
    loc = args.location.strip()
    if not loc:
        raise ValueError("Location must not be empty; mainland China location evidence required")
    geo = classify_location(loc)
    if geo != 'domestic':
        raise ValueError(f"Only verified mainland China location allowed (job_geography must be 'domestic', got '{geo}'): {loc}")

    # 3. 严格日期校验（拒绝 2026-99-99）
    pub = args.published_at.strip()
    try:
        if len(pub) == 10 and pub.count("-") == 2:
            d = date.fromisoformat(pub)
            dt = datetime(d.year, d.month, d.day, 9, 0, 0, tzinfo=TZ)
        else:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
        pub_iso = dt.astimezone(TZ).isoformat()
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid calendar date or ISO timestamp: {pub}") from e

    # 4. 生成规范 ID 与字段
    sig_id = args.id
    if not sig_id:
        slug = hashlib.sha256(f"{args.url}|{pub_iso}|{args.title}".encode("utf-8")).hexdigest()[:10]
        sig_id = f"sig-{args.category}-{slug}"

    now_iso = datetime.now(TZ).isoformat()
    summary_text = args.summary.strip()
    evidence = {
        "source_verified": False,
        "fetch_received": False,
        "location": loc,
        "source_field": "content",
        "employment": {
            "text": summary_text
        },
        "audit_note": "CLI 原始录入待核验线索，未经过真实 HTTP 抓取回执与一手证据审计"
    }

    return {
        "id": sig_id,
        "url": args.url.strip(),
        "title": args.title.strip(),
        "source": args.source.strip(),
        "published_at": pub_iso,
        "date_basis": "unverified",
        "first_seen": now_iso,
        "last_seen": now_iso,
        "content": summary_text,
        "summary": summary_text,  # 统一顶层 summary 字段
        "query": f"{args.source} {args.category}",
        "category": args.category,
        "ai_scope": args.ai_scope,
        "claim_status": "unverified",
        "company_id": args.company_id.strip(),
        "review_status": "pending",  # 默认必须为 pending
        "liveness": "uncertain",
        "last_verified_at": None,
        "location": loc,
        "evidence": evidence
    }


def ingest_signal(record: dict, target_db: Path | str | None = None, dry_run: bool = False):
    db_path = Path(target_db).resolve() if target_db else DEFAULT_DB_PATH
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    print(f"[*] Target DB: {db_path}")
    print(f"[*] Signal ID: {record['id']}")
    print(f"[*] Title: {record['title']}")
    print(f"[*] Location: {record['location']} (Mainland China verified)")
    print(f"[*] Source: {record['source']} ({record['url']})")
    print(f"[*] Category: {record['category']}, AI Scope: {record['ai_scope']}")
    print(f"[*] Review Status: {record['review_status']} (truthful unverified boundary)")
    print(f"[*] Published: {record['published_at']}")

    if dry_run:
        print("[DRY-RUN] Validation passed. Database write skipped (--dry-run).")
        return

    # 若操作主库，执行强制前置物理备份；若为隔离临时库，可直接安全操作
    if db_path.name == "ai_jobs.db":
        bk = backup_manager.ensure_backup(tag="signal-cli-ingest")
        print(f"[+] Physical backup created: {bk.name}")

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        ev_str = json.dumps(record["evidence"], ensure_ascii=False)
        sha256 = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()

        cur.execute("""
            INSERT INTO discovery_documents (
                id, url, title, source, published_at, date_basis,
                first_seen, last_seen, content, content_sha256,
                query, category, ai_scope, claim_status, evidence_json,
                company_id, review_status, liveness, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                url=excluded.url, title=excluded.title, source=excluded.source,
                published_at=excluded.published_at, date_basis=excluded.date_basis,
                last_seen=excluded.last_seen, content=excluded.content,
                content_sha256=excluded.content_sha256, query=excluded.query,
                category=excluded.category, ai_scope=excluded.ai_scope,
                claim_status=CASE WHEN
                    excluded.content_sha256=discovery_documents.content_sha256 AND
                    excluded.url=discovery_documents.url AND excluded.title=discovery_documents.title AND
                    excluded.source=discovery_documents.source AND excluded.published_at IS discovery_documents.published_at AND
                    excluded.date_basis=discovery_documents.date_basis AND excluded.category=discovery_documents.category AND
                    excluded.ai_scope=discovery_documents.ai_scope AND excluded.claim_status=discovery_documents.claim_status
                    THEN discovery_documents.claim_status ELSE 'unverified' END,
                evidence_json=excluded.evidence_json, company_id=excluded.company_id,
                review_status=CASE WHEN
                    excluded.content_sha256=discovery_documents.content_sha256 AND
                    excluded.url=discovery_documents.url AND excluded.title=discovery_documents.title AND
                    excluded.source=discovery_documents.source AND excluded.published_at IS discovery_documents.published_at AND
                    excluded.date_basis=discovery_documents.date_basis AND excluded.category=discovery_documents.category AND
                    excluded.ai_scope=discovery_documents.ai_scope AND excluded.claim_status=discovery_documents.claim_status
                    THEN discovery_documents.review_status ELSE 'pending' END,
                liveness=CASE WHEN
                    excluded.content_sha256=discovery_documents.content_sha256 AND
                    excluded.url=discovery_documents.url AND excluded.title=discovery_documents.title AND
                    excluded.source=discovery_documents.source AND excluded.published_at IS discovery_documents.published_at AND
                    excluded.date_basis=discovery_documents.date_basis AND excluded.category=discovery_documents.category AND
                    excluded.ai_scope=discovery_documents.ai_scope AND excluded.claim_status=discovery_documents.claim_status
                    THEN discovery_documents.liveness ELSE 'uncertain' END,
                last_verified_at=CASE WHEN
                    excluded.content_sha256=discovery_documents.content_sha256 AND
                    excluded.url=discovery_documents.url AND excluded.title=discovery_documents.title AND
                    excluded.source=discovery_documents.source AND excluded.published_at IS discovery_documents.published_at AND
                    excluded.date_basis=discovery_documents.date_basis AND excluded.category=discovery_documents.category AND
                    excluded.ai_scope=discovery_documents.ai_scope AND excluded.claim_status=discovery_documents.claim_status
                    THEN discovery_documents.last_verified_at ELSE NULL END
        """, (
            record["id"], record["url"], record["title"], record["source"], record["published_at"], record["date_basis"],
            record["first_seen"], record["last_seen"], record["content"], sha256,
            record["query"], record["category"], record["ai_scope"], record["claim_status"], ev_str,
            record["company_id"], record["review_status"], record["liveness"], record["last_verified_at"]
        ))

        # Same-content reimports preserve review; changed material returns to pending.
        final_review = cur.execute("SELECT review_status FROM discovery_documents WHERE id=?", (record["id"],)).fetchone()[0]
        if final_review == "approved":
            tag_label = "招聘线索" if record["category"] == "hiring" else "行业线索"
            cur.execute("""
                INSERT INTO signals (
                    id, title, url, source, published_at, summary, tag
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, url=excluded.url,
                    source=excluded.source, published_at=excluded.published_at,
                    summary=excluded.summary, tag=excluded.tag
            """, (
                record["id"], record["title"], record["url"], record["source"], record["published_at"],
                record["summary"], tag_label
            ))
        else:
            cur.execute("DELETE FROM signals WHERE id=?", (record["id"],))

        con.commit()
        print("[+] Signal successfully committed to database.")
    except Exception as e:
        con.rollback()
        print(f"[-] Database transaction failed and rolled back: {e}", file=sys.stderr)
        raise e
    finally:
        con.close()

    # 验证完整性
    con = sqlite3.connect(db_path)
    check = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if check != "ok":
        raise RuntimeError(f"Database integrity check failed: {check}")
    print("[+] DB integrity check: ok")


def main(argv=None):
    args = parse_args(argv)
    try:
        record = validate_input(args)
        ingest_signal(record, target_db=args.target_db, dry_run=args.dry_run)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
