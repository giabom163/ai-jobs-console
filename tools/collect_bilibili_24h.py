"""B 站最近 N 小时视频：纯 HTTP（WBI 签名），不打开浏览器、不播放、不读 cookie。

用法：
  python tools/collect_bilibili_24h.py --hours 24 --pages 3 --dry-run
  python tools/collect_bilibili_24h.py --hours 24 --pages 3
严格模式（默认）：标题必须命中“性别对立/女权/彩礼/厌女/性别歧视/重男轻女/打拳”等词。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relevance  # noqa: E402
import server  # noqa: E402
import topics  # noqa: E402
import content_filter  # noqa: E402
import storage  # noqa: E402

MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
         33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
         61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
         11, 36, 20, 34, 44, 52]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
STRICT = ("性别对立", "男女对立", "女权", "女拳", "男权", "彩礼", "厌女", "媚男",
          "性别歧视", "重男轻女", "打拳", "拳师", "性别矛盾")

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _mixin_key():
    nav = _get("https://api.bilibili.com/x/web-interface/nav")
    img = nav["data"]["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = nav["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    return "".join((img + sub)[i] for i in MIXIN)[:32]


def search(keyword, page, mixin):
    params = {"search_type": "video", "keyword": keyword, "page": page,
              "page_size": 30, "order": "pubdate", "wts": str(int(time.time()))}
    query = "&".join(f"{k}={urllib.parse.quote(str(params[k]), safe='')}" for k in sorted(params))
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    url = ("https://api.bilibili.com/x/web-interface/wbi/search/type?"
           + urllib.parse.urlencode(params))
    data = _get(url)
    return (data.get("data") or {}).get("result") or []


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--keywords", default="性别,女权,彩礼,家暴,性别对立,厌女,性别歧视,男女")
    parser.add_argument("--per-category-limit", type=int, default=30,
                        help="每个类别最多保留多少条（默认 30）")
    parser.add_argument("--max-total", type=int, default=100,
                        help="本次最多新增多少条（默认 100）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=ROOT / "sentiment.db")
    args = parser.parse_args(argv)

    if not 1 <= args.per_category_limit <= 200:
        parser.error("--per-category-limit 必须在 1..200")
    if not 1 <= args.max_total <= 100:
        parser.error("--max-total 必须在 1..100")
    mixin = _mixin_key()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    found = {}
    category_counts = {}
    base_applied = False
    if not args.dry_run:
        try:
            db0 = storage.connect(args.db)
            for item in topics.categories(db0, args.hours)["categories"]:
                category_counts[item["name"]] = item["records"]
            db0.close()
            base_applied = True
        except Exception as exc:
            print(f"warning: 读取已有分类基数失败，配额只按本轮计数：{exc}", file=sys.stderr)
    for keyword in [k.strip() for k in args.keywords.split(",") if k.strip()]:
        for page in range(1, args.pages + 1):
            try:
                items = search(keyword, page, mixin)
            except Exception as exc:
                print(f"search fail {keyword} p{page}: {exc}", file=sys.stderr)
                break
            for item in items:
                if len(found) >= args.max_total:
                    break
                bvid, pub = item.get("bvid"), item.get("pubdate")
                if not bvid or not pub:
                    continue
                published = datetime.fromtimestamp(pub, timezone.utc)
                if published < cutoff:
                    continue
                title = html.unescape(re.sub(r"<[^>]+>", "", item.get("title") or ""))
                if not any(t in title for t in STRICT):
                    continue
                if content_filter.is_noise(title):
                    continue
                if bvid in found:
                    continue
                labels = topics.classify_all(title) or [topics.FALLBACK]
                if any(category_counts.get(label, 0) >= args.per_category_limit
                       for label in labels):
                    continue
                for label in labels:
                    category_counts[label] = category_counts.get(label, 0) + 1
                found[bvid] = {"title": title, "url": f"https://www.bilibili.com/video/{bvid}",
                               "published_at": published.isoformat()}
            if len(found) >= args.max_total:
                break
            time.sleep(0.4)

    result = {"hours": args.hours, "pages": args.pages, "strict": True,
              "per_category_limit": args.per_category_limit,
              "base_applied": base_applied,
              "category_counts": category_counts,
              "found": len(found), "dry_run": args.dry_run,
              "items": list(found.values())[:20]}
    if args.dry_run or not found:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    db = storage.connect(args.db)
    feed = db.execute("select id from feeds where name='bilibili-search'").fetchone()
    if feed is None:
        with db:
            cursor = db.execute("insert into feeds(name,url,added_at) values(?,?,?)",
                                ("bilibili-search", "https://search.bilibili.com/all?keyword=",
                                 storage.now()))
        feed_id = cursor.lastrowid
    else:
        feed_id = feed["id"]
    run_id = storage.start_run(db, feed_id, "bilibili-search", max(1, min(len(found), 100)))
    rows = [{"title": item["title"], "summary": "", "url": item["url"],
             "source_id": bvid, "published_at": item["published_at"]}
            for bvid, item in found.items()]
    outcome = storage.import_batch(db, ROOT, run_id, rows, server.classify, noise=False)
    db.close()
    result["import"] = {k: outcome[k] for k in
                        ("input_count", "added", "duplicates", "near_duplicates", "rejected")}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
