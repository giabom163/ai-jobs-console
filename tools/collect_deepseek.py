#!/usr/bin/env python
"""DeepSeek 官方直招岗位标准化适配器。

口径与安全契约：
  1) 严格限定官方 Moka 专属租户：app.mokahr.com/social-recruitment/high-flyer/140576#/job/{uuid}
  2) 前置限定中国大陆城市（北京、杭州等），通过 is_domestic_only 强制核验。
  3) 接入 ingestion_contract 批量事务与 source_runs 账本。

示例：
  python tools/collect_deepseek.py --dry-run
  python tools/collect_deepseek.py --limit 3
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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
sys.path.insert(0, str(ROOT))

from tools.ai_lexicon import classify  # noqa: E402
from tools.job_geography import is_domestic_only  # noqa: E402
from tools.job_fields import ensure_columns as ensure_source_columns, save_source_fields  # noqa: E402
from tools.ingestion_contract import standard_fields, validate_record, begin_batch, finish_batch, abort_batch  # noqa: E402
from tools.source_runs import start_run, finish_run  # noqa: E402

DEEPSEEK_COMPANY_ID = "company-0648d2111ec0ca3d37d68ea0"
HARD_FLOOR = "2026-06-01"
DEFAULT_INPUT = Path.home() / ".codex" / "documents" / "ai-jobs-update-20261028" / "deepseek-ai-core-list.json"


def now():
    return datetime.now(timezone.utc).isoformat()


def extract_deepseek_identity(url: str) -> str:
    u = urlsplit(url)
    if u.hostname != "app.mokahr.com":
        raise ValueError("unregistered host; DeepSeek jobs must originate from app.mokahr.com")
    if u.path != "/social-recruitment/high-flyer/140576":
        raise ValueError("unregistered Moka tenant; must be high-flyer/140576")
    m = re.fullmatch(r"/job/([0-9a-f-]{36})", u.fragment)
    if not m:
        raise ValueError("missing or invalid DeepSeek job uuid in url fragment")
    return m.group(1)


def parse_deepseek_text(raw_text: str):
    """解析 Moka 页面抓取的纯文本，提取职位、城市、部门和职责。"""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return {}

    title = lines[0]
    location = "杭州市"  # 默认总部
    dept = "AI核心研发"
    desc_lines = []

    for line in lines[1:]:
        if any(c in line for c in ["杭州", "北京", "上海", "深圳"]):
            for loc in ["杭州市", "北京市", "上海市", "深圳市"]:
                if loc[:2] in line:
                    location = loc
                    break
        elif any(kw in line for kw in ["职责", "要求", "加分项", "团队使命", "1.", "2.", "3."]):
            desc_lines.append(line)

    description = "\n".join(desc_lines) if desc_lines else raw_text[:2000]
    return {
        "title": title,
        "location": location,
        "department": dept,
        "description": description,
    }


def upsert(con, item_data, cid, observed_at=None):
    observed_at = observed_at or now()
    url = item_data["url"]
    ext = extract_deepseek_identity(url)
    title = item_data["title"].strip()
    location = item_data["location"].strip()

    if not is_domestic_only(location):
        return {"skipped_non_domestic": True}

    desc = item_data["description"].strip()
    dept = item_data.get("department", "AI研发")

    content_hash = hashlib.sha256(f"{title}\n{location}\n{desc}".encode()).hexdigest()
    derived = standard_fields(title, desc, dept)
    tier, conf, reason = derived["ai_relevance"], derived["ai_confidence"], derived["ai_reason"]

    validate_record(dict(
        source="official:deepseek",
        external_id=ext,
        title=title,
        location=location,
        first_seen=observed_at,
        last_seen=observed_at,
        url=url,
    ))

    existing = con.execute(
        "SELECT id, content_hash, status FROM jobs WHERE source='official:deepseek' AND external_id=?",
        (ext,),
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
            con.execute("UPDATE jobs SET status='verification_pending' WHERE id=? AND status='excluded'", (jid,))
        if existing["content_hash"] != content_hash:
            con.execute(
                "INSERT INTO job_changes(job_id,change_type,changed_at,old_hash,new_hash,note) VALUES(?,?,?,?,?,?)",
                (jid, "changed", observed_at, existing["content_hash"], content_hash, reason),
            )
    else:
        jid = "job-" + hashlib.sha256(f"deepseek|{ext}".encode()).hexdigest()[:24]
        con.execute(
            "INSERT INTO jobs(id,source,external_id,company_id,title,location,description,"
            "ai_relevance,ai_confidence,ai_reason,first_seen,last_seen,status,url,content_hash,"
            "posted_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'verification_pending',?,?,?,?)",
            (jid, "official:deepseek", ext, cid, title, location, desc[:20000], tier, conf,
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
        (jid, "official:deepseek", url, observed_at),
    )
    save_source_fields(
        con, jid,
        published=observed_at,
        updated=observed_at,
        department=dept,
        source_status="listed",
        checked_at=observed_at,
    )
    return {"added": added, "jid": jid}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    stats = {
        "source": "official:deepseek",
        "input": str(input_path),
        "limit": args.limit,
        "seen": 0,
        "added": 0,
        "updated": 0,
        "errors": [],
    }

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items") or []
    captured_at = data.get("captured_at") or now()

    con = None
    batch = None
    staged = []

    try:
        for it in items[:args.limit]:
            url = it.get("url") or ""
            if not url:
                continue
            raw_text = it.get("title") or it.get("card") or ""
            parsed = parse_deepseek_text(raw_text)
            parsed["url"] = url
            stats["seen"] += 1
            staged.append(parsed)

        if not args.dry_run:
            con = sqlite3.connect("file:" + Path(args.db).resolve().as_posix() + "?mode=rw", uri=True)
            con.row_factory = sqlite3.Row
            ensure_source_columns(con)
            batch = begin_batch(con, "deepseek_official_ingestion")
            cid = DEEPSEEK_COMPANY_ID
            con.execute(
                "INSERT OR IGNORE INTO companies(id,name,ats_type,created_at) VALUES(?,?,?,?)",
                (cid, "DeepSeek", "official", now()),
            )
            for parsed in staged:
                res = upsert(con, parsed, cid, observed_at=captured_at)
                if res and "added" in res:
                    stats["added" if res["added"] else "updated"] += 1

        if con:
            stats["batch"] = finish_batch(con, batch)
            con.commit()
    except BaseException as exc:
        if con and batch:
            abort_batch(con, batch, exc)
        stats["errors"].append(type(exc).__name__)
        stats["error_message"] = str(exc)
        stats["rolled_back"] = bool(con)
    finally:
        if con:
            con.close()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
