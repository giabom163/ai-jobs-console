#!/usr/bin/env python
"""Second-pass near-duplicate merge for open jobs (bottom-k MinHash + exact Jaccard).

为什么不用 datasketch：本项目运行时是 Python 标准库（无 numpy），装 datasketch 会带
numpy 原生 wheel。这里按 datasketch 的 MinHash 思路（MIT）实现 bottom-k 签名，无外部
依赖；算法来源记在 docs/复用清单.md。

两段：
  A. 同公司 + 归一化标题 + 同城市：同 URL 或描述相似度 >= --same-title-threshold 合并
  B. 同公司内跨标题（同城市/同 URL）：签名估计过候选线后算精确 Jaccard，
     标题不同时要求 >= --cross-title-threshold（默认 0.90，防模板 JD 误合并）

用法：
  python tools/dedup_minhash.py --dry-run
  python tools/dedup_minhash.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.dedup_jobs import norm_city, norm_title, priority  # noqa: E402

DB = ROOT / "ai_jobs.db"
K = 64  # bottom-k 签名大小


def shingles(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return set()
    grams = {text[i:i + 4] for i in range(max(0, len(text) - 3))}
    words = set(re.findall(r"[a-z0-9+#.]{2,}", text))
    return grams | words


def fingerprint(text: str) -> set[int]:
    hs = {int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")
          for s in shingles(text)}
    return set(sorted(hs)[:K])


def estimate(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    union = sorted(a | b)[:K]
    if not union:
        return 0.0
    return sum(1 for h in union if h in a and h in b) / len(union)


def exact_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def parens(value: str) -> set[str]:
    parts = re.findall(r"[（(]([^）)]*)[）)]", unicodedata.normalize("NFKC", value or ""))
    return {re.sub(r"\s+", "", p).lower() for p in parts}

def tight_title(value: str) -> str:
    """只折叠空白与全半角，保留括号里的方向/团队，避免把不同子岗位当成同标题。"""
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[\s]+", "", value)
    return value.strip()

def cities(value: str) -> set[str]:
    parts = re.split(r"[/、,，\-—|]", unicodedata.normalize("NFKC", value or ""))
    return {norm_city(p) for p in parts if norm_city(p)}


def merge(con, keep, dup, reason: str):
    con.execute("INSERT OR IGNORE INTO job_evidence(job_id,source,url,seen_at) "
                "SELECT ?,source,url,seen_at FROM job_evidence WHERE job_id=?",
                (keep["id"], dup["id"]))
    con.execute("DELETE FROM job_evidence WHERE job_id=?", (dup["id"],))
    con.execute("UPDATE jobs SET status='merged',duplicate_of=?,merged_at=?,ai_reason=? WHERE id=?",
                (keep["id"], datetime.now(timezone.utc).isoformat(),
                 f"merged:{reason}", dup["id"]))
    con.execute("UPDATE jobs SET last_seen=MAX(last_seen,?) WHERE id=?", (dup["last_seen"], keep["id"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--same-title-threshold", type=float, default=0.62)
    ap.add_argument("--cross-title-threshold", type=float, default=0.95)
    ap.add_argument("--candidate", type=float, default=0.55)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT j.*,c.name company FROM jobs j JOIN companies c ON c.id=j.company_id "
        "WHERE j.status='open' AND j.duplicate_of IS NULL").fetchall()

    cache: dict[str, set[str]] = {}

    def desc_set(jid, text):
        if jid not in cache:
            cache[jid] = shingles(text or "")
        return cache[jid]

    stats = {"open_before": len(rows), "merged_same_title": 0, "merged_cross_title": 0,
             "pairs_checked": 0, "samples": [], "dry_run": args.dry_run}

    # ---------- A. 同公司 + 同标题 + 同城市 ----------
    groups = defaultdict(list)
    for r in rows:
        groups[(r["company_id"], tight_title(r["title"]), norm_city(r["location"]))].append(r)
    alive = {}
    with con:
        for key, items in groups.items():
            items.sort(key=lambda x: (x["last_seen"] or "", priority(x["source"])), reverse=True)
            kept = []
            for j in items:
                target = None
                for k in kept:
                    same_url = j["url"] and j["url"] == k["url"]
                    if same_url or exact_jaccard(desc_set(j["id"], j["description"]),
                                                 desc_set(k["id"], k["description"])) >= args.same_title_threshold:
                        target = k
                        break
                if target:
                    stats["merged_same_title"] += 1
                    if len(stats["samples"]) < 12:
                        stats["samples"].append({"type": "same-title", "keep": target["title"],
                                                 "drop": j["title"], "company": j["company"]})
                    if not args.dry_run:
                        merge(con, target, j, "same-title")
                else:
                    kept.append(j)
            for j in kept:
                alive[j["id"]] = j

    # ---------- B. 同公司内跨标题 ----------
    by_company = defaultdict(list)
    for j in alive.values():
        by_company[j["company_id"]].append(j)

    for cid, items in by_company.items():
        sigs = {j["id"]: fingerprint(j["description"] or "") for j in items}
        for i in range(len(items)):
            for k in range(i + 1, len(items)):
                a, b = items[i], items[k]
                if a["id"] not in alive or b["id"] not in alive:
                    continue
                if estimate(sigs[a["id"]], sigs[b["id"]]) < args.candidate:
                    continue
                stats["pairs_checked"] += 1
                same_title = tight_title(a["title"]) == tight_title(b["title"])
                same_url = a["url"] and a["url"] == b["url"]
                if not same_url and not (cities(a["location"]) & cities(b["location"])):
                    continue
                sim = exact_jaccard(desc_set(a["id"], a["description"]),
                                    desc_set(b["id"], b["description"]))
                need = args.same_title_threshold if same_title else args.cross_title_threshold
                ta, tb = tight_title(a["title"]), tight_title(b["title"])
                contained = ta in tb or tb in ta
                pa, pb = parens(a["title"]), parens(b["title"])
                if pa or pb:
                    if pa != pb:
                        continue
                if not same_title and not contained:
                    continue
                if sim < need:
                    continue
                keep, drop = (a, b) if (a["last_seen"] or "", priority(a["source"])) >= \
                                          (b["last_seen"] or "", priority(b["source"])) else (b, a)
                if keep["id"] not in alive or drop["id"] not in alive:
                    continue
                stats["merged_cross_title"] += 1
                if len(stats["samples"]) < 12:
                    stats["samples"].append({"type": "cross-title", "keep": keep["title"],
                                             "drop": drop["title"], "sim": round(sim, 3),
                                             "company": drop["company"]})
                if not args.dry_run:
                    merge(con, keep, drop, "near-dup")
                alive.pop(drop["id"], None)

        con.commit()

    stats["open_after"] = len(alive) if args.dry_run else con.execute(
        "SELECT count(*) FROM jobs WHERE status='open'").fetchone()[0]
    text = json.dumps(stats, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    con.close()


if __name__ == "__main__":
    main()




