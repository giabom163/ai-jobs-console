#!/usr/bin/env python
"""First-pass dedup: same company + normalized title + city, keep newest.

Also merges exact same apply URL regardless of title.
Duplicates are kept with status='merged' and duplicate_of for audit;
they are excluded from the open-job UI and API by default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
STOP = set("岗位职责 任职要求 工作职责 职位描述 我们 负责 要求 优先 熟悉 具备 能力 经验 以及 相关 工作 进行 能够 良好 团队 业务".split())


def norm_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[\s\-_/|（）()【】\[\]·,，。:：;；!！?？]+", "", value)
    return value.strip()


def norm_city(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.split(r"[/、,，\-—]", value)[0]
    return value.replace("市", "").strip().lower()


def norm_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[（(][^）)]*[）)]", "", value)
    value = re.sub(r"[\s\-_/|·,，。:：;；!！?？]+", "", value)
    return value.strip()


def tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text or "").lower()
    out = set(re.findall(r"[a-z0-9+#.]{2,}", text))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        out.update(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
    return {t for t in out if t not in STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 1.0 if not ta and not tb else 0.0
    return len(ta & tb) / len(ta | tb)


def priority(source: str) -> int:
    if source.startswith("ats:") or source.startswith("official:"):
        return 3
    if source.startswith("aggregator"):
        return 2
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--threshold", type=float, default=0.62)
    args = parser.parse_args()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT j.*,c.name company FROM jobs j JOIN companies c ON c.id=j.company_id "
                       "WHERE j.status='open' AND j.duplicate_of IS NULL").fetchall()
    groups = defaultdict(list)
    for j in rows:
        key = f"{j['company_id']}|{norm_title(j['title'])}|{norm_city(j['location'])}"
        groups[key].append(j)

    merged = 0
    with con:
        for key, items in groups.items():
            if not items:
                continue
            # exact same URL always merges
            by_url = defaultdict(list)
            for j in items:
                if j["url"]:
                    by_url[j["url"]].append(j)
            remaining = []
            for url, same in by_url.items():
                if len(same) > 1:
                    same.sort(key=lambda x: (x["last_seen"] or "", priority(x["source"])), reverse=True)
                    keep = same[0]
                    for dup in same[1:]:
                        merge(con, keep, dup)
                        merged += 1
                    remaining.append(keep)
                else:
                    remaining.append(same[0])
            # similar descriptions within same title/city
            remaining.sort(key=lambda x: (x["last_seen"] or "", priority(x["source"])), reverse=True)
            kept = []
            for j in remaining:
                target = None
                for k in kept:
                    if similarity(j["description"] or "", k["description"] or "") >= args.threshold:
                        target = k
                        break
                if target:
                    merge(con, target, j)
                    merged += 1
                else:
                    kept.append(j)
            for j in kept:
                con.execute("UPDATE jobs SET dedup_key=? WHERE id=?",
                            (hashlib.sha1(key.encode()).hexdigest()[:16], j["id"]))
    print(json.dumps({"merged": merged, "open": con.execute("SELECT count(*) FROM jobs WHERE status='open'").fetchone()[0]}, ensure_ascii=False))


def merge(con, keep, dup):
    con.execute("INSERT OR IGNORE INTO job_evidence(job_id,source,url,seen_at) "
                "SELECT ?,source,url,seen_at FROM job_evidence WHERE job_id=?",
                (keep["id"], dup["id"]))
    con.execute("DELETE FROM job_evidence WHERE job_id=?", (dup["id"],))
    con.execute("UPDATE jobs SET status='merged',duplicate_of=?,merged_at=? WHERE id=?",
                (keep["id"], datetime.now(timezone.utc).isoformat(), dup["id"]))
    con.execute("UPDATE jobs SET last_seen=MAX(last_seen,?) WHERE id=?", (dup["last_seen"], keep["id"]))


if __name__ == "__main__":
    main()
