"""B 站指定日/最近 N 小时视频：纯 HTTP（WBI 签名），不打开浏览器、不播放、不读 cookie。

用法：
  python tools/collect_bilibili_24h.py --limit 3 --date 2026-10-18 --dry-run
  python tools/collect_bilibili_24h.py --limit 100 --date 2026-10-18 --max-requests 20 --pages 2 --keywords 性别,女权,... --per-category-limit 100

安全边界（本轮新增，不重写通用爬虫）：
  - `--limit` 必传 1..100，是**实际总上限**（兼容别名 `--max-total` 被忽略）；
  - `--date YYYY-MM-DD` 取上海自然日 `[00:00, 次日00:00)` 并**一次性**换算为 UTC；未来日期拒绝；
    与显式 `--hours`/`--min-age-hours` 互斥（同时显式传入即拒绝）；
  - `pubdate` 只接受**非 bool 的精确秒整数**；缺失/非整数/未来/窗口外一律排除并记理由，不猜、不用抓取时间；
  - 每个真实 HTTP 请求（含 nav）全局间隔 >= 20 秒，超时 <= 20 秒，响应体有上限；
  - `--max-requests` 默认 2、上限 20（含 nav）；`--pages` <=10；`--keywords` <=10；
    超预算或未达目标返回 partial，不伪完成；
  - HTTP 429 立即非 0 退出并记录冷却 300 秒（进程内不重试）；其它 HTTP 错误保留真实 status+有限 body；
    业务非 0 错误码不得变成空列表；
  - `--dry-run` 不写真实库，输出全部（最多 limit）合格候选与窗口/请求计数/过滤计数；
  - `--output` 写 JSON 证据；`--snapshot-root` 与 `--db` 必须对应，避免隔离库却把快照写回原 ROOT。
严格模式（默认）：标题必须命中“性别对立/女权/彩礼/厌女/性别歧视/重男轻女/打拳”等词。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import collection_policy  # noqa: E402  统一候选门与时间证据
import server  # noqa: E402
import storage  # noqa: E402
import topics  # noqa: E402

MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
         33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
         61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
         11, 36, 20, 34, 44, 52]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SHANGHAI = timezone(timedelta(hours=8))
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type?"
HTTP_TIMEOUT = 20
MAX_RESPONSE_BYTES = 2_000_000
MAX_BODY_HEAD = 800
COOLDOWN_SECONDS = 300
MAX_PAGES = 10
MAX_KEYWORDS = 10
DEFAULT_KEYWORDS = "性别,女权,彩礼,家暴,性别对立,厌女,性别歧视,男女"

# 可注入的时钟/休眠（检查器用纯函数夹具替换，无需真实网络/睡眠）。
_CLOCK = time.monotonic
_SLEEP = time.sleep
_LIMITS = {"max_requests": 2, "interval": 20.0}
_REQ = {"count": 0, "last": None, "cooldown_until": 0.0}


class CollectorError(RuntimeError):
    def __init__(self, stage, message, status=None, body=None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.status = status
        self.body = body


class HttpStatusError(CollectorError):
    def __init__(self, status, body=None):
        super().__init__("http", f"HTTP {status}", status, body)


class RateLimitError(CollectorError):
    def __init__(self, status=429, body=None, cooldown_seconds=COOLDOWN_SECONDS):
        super().__init__("http", f"HTTP {status}", status, body)
        self.cooldown_seconds = cooldown_seconds


class BusinessError(CollectorError):
    def __init__(self, code, message):
        super().__init__("business", message, None, None)
        self.code = code


class RequestBudgetError(CollectorError):
    def __init__(self, message):
        super().__init__("budget", message)


def _reset_requests(max_requests=2, interval=20.0, clock=None, sleep_fn=None):
    """重置进程内请求预算（测试夹具用；真实运行在 main 里调一次）。"""
    global _CLOCK, _SLEEP
    if clock is not None:
        _CLOCK = clock
    if sleep_fn is not None:
        _SLEEP = sleep_fn
    _LIMITS.update(max_requests=int(max_requests), interval=float(interval))
    _REQ.update(count=0, last=None, cooldown_until=0.0)


def _reserve_request():
    """请求预算闸门：冷却 -> 上限 -> 全局间隔，全在真实请求之前。返回累计计数。"""
    now = _CLOCK()
    if now < _REQ["cooldown_until"]:
        raise RateLimitError(status=429, cooldown_seconds=round(_REQ["cooldown_until"] - now, 3))
    if _REQ["count"] >= _LIMITS["max_requests"]:
        raise RequestBudgetError(f"请求已达上限 {_LIMITS['max_requests']}（含 nav）")
    if _REQ["last"] is not None:
        wait = _LIMITS["interval"] - (now - _REQ["last"])
        if wait > 0:
            _SLEEP(wait)
    _REQ["count"] += 1
    _REQ["last"] = _CLOCK()
    return _REQ["count"]


def _get(url):
    _reserve_request()
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com/"})
    # 明确禁用环境代理（不读代理凭据）；transport 错误转安全 CollectorError，由 main 以非 0 JSON 呈现。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        head = (exc.read(MAX_BODY_HEAD) or b"").decode("utf-8", "replace")
        body = head or None
        if exc.code == 429:
            _REQ["cooldown_until"] = _CLOCK() + COOLDOWN_SECONDS
            raise RateLimitError(status=429, body=body) from exc
        raise HttpStatusError(exc.code, body) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CollectorError("transport", f"HTTP 请求失败：{exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise CollectorError("response", f"响应体超过上限 {MAX_RESPONSE_BYTES} 字节")
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise CollectorError("parse", f"响应不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise CollectorError("schema", "响应 JSON 顶层不是对象")
    return payload


def _mixin_key():
    nav = _get(NAV_URL)
    code = nav.get("code")
    data = nav.get("data")
    if not isinstance(data, dict):
        raise CollectorError("schema", f"nav data 不是对象（code={code}）")
    img = data.get("wbi_img")
    if not isinstance(img, dict):
        raise CollectorError("schema", f"nav wbi_img 不是对象（code={code}）")
    img_url, sub_url = img.get("img_url"), img.get("sub_url")
    if not (isinstance(img_url, str) and isinstance(sub_url, str) and img_url and sub_url):
        raise BusinessError(code, f"nav 缺少完整 wbi_img（code={code}）")
    if code not in (0, -101):
        raise BusinessError(code, f"nav 业务错误码 {code}")
    imgid = img_url.rsplit("/", 1)[-1].split(".")[0]
    subid = sub_url.rsplit("/", 1)[-1].split(".")[0]
    if len(imgid + subid) < 64:
        raise CollectorError("schema", "nav wbi_img 拼接 key 少于 64 字符")
    return "".join((imgid + subid)[i] for i in MIXIN)[:32]


def search(keyword, page, mixin, order="pubdate"):
    params = {"search_type": "video", "keyword": keyword, "page": page,
              "page_size": 30, "order": order, "wts": str(int(time.time()))}
    query = "&".join(f"{k}={urllib.parse.quote(str(params[k]), safe='')}" for k in sorted(params))
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    url = SEARCH_URL + urllib.parse.urlencode(params)
    data = _get(url)
    if not isinstance(data, dict):
        raise CollectorError("schema", "search 响应不是对象")
    code = data.get("code")
    if code != 0:
        raise BusinessError(code, f"search 业务错误码 {code}（不当作空列表）")
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise CollectorError("schema", "search data 不是对象")
    result = payload.get("result")
    if not isinstance(result, list):
        raise CollectorError("schema", "search result 不是数组")
    return result


# --- 纯解析函数（检查器直接调用，无网络） -----------------------------------------------------

def parse_pubdate(value):
    """只接受非 bool 的精确秒整数；返回 (int_seconds|None, reason)。"""
    if type(value) is int:  # noqa: E721  bool 是 int 子类，必须用 type is int 排除
        return value, None
    if value is None or value == "":
        return None, "pubdate_missing"
    return None, "pubdate_not_int"


def date_window(date_str, now):
    """上海自然日 [00:00, 次日00:00) 一次性换算为 UTC；未来日期拒绝。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (date_str or "").strip())
    if not m:
        raise ValueError(f"--date 必须是 YYYY-MM-DD：{date_str!r}")
    y, mo, d = map(int, m.groups())
    try:
        start_local = datetime(y, mo, d, tzinfo=SHANGHAI)
    except ValueError as exc:
        raise ValueError(f"--date 不是合法日期：{date_str}") from exc
    if start_local.date() > now.astimezone(SHANGHAI).date():
        raise ValueError(f"--date 不能是未来日期：{date_str}")
    end_local = start_local + timedelta(days=1)
    return {"mode": "date", "date": f"{y:04d}-{mo:02d}-{d:02d}",
            "start_utc": start_local.astimezone(timezone.utc),
            "end_utc": end_local.astimezone(timezone.utc),
            "start_local": start_local, "end_local": end_local}


def resolve_window(date, hours, min_age_hours, now):
    """--date 与显式 --hours/--min-age-hours 互斥；返回固定一次的真实窗口。"""
    if date is not None:
        if hours is not None or min_age_hours is not None:
            raise ValueError("--date 与 --hours/--min-age-hours 互斥，不能同时显式传入")
        return date_window(date, now)
    h = 24 if hours is None else hours
    ma = 0 if min_age_hours is None else min_age_hours
    if h < 1:
        raise ValueError("--hours 必须 >= 1")
    if ma < 0:
        raise ValueError("--min-age-hours 必须 >= 0")
    start = now - timedelta(hours=h)
    end = now - timedelta(hours=ma)
    if end <= start:
        raise ValueError("窗口为空：--min-age-hours 使 end <= start")
    return {"mode": "hours", "hours": h, "min_age_hours": ma,
            "start_utc": start, "end_utc": end,
            "start_local": start.astimezone(SHANGHAI), "end_local": end.astimezone(SHANGHAI)}


def _ro_connect(path):
    uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _load_existing(db):
    urls, hashes, sims = set(), set(), []
    for row in db.execute("SELECT canonical_url,content_sha256,simhash FROM records"):
        urls.add(row[0])
        hashes.add(row[1])
        if row[2]:
            try:
                sims.append(int(row[2], 16))
            except (TypeError, ValueError):
                pass
    return {"urls": urls, "hashes": hashes, "sims": sims}


def _emit(report, output, exit_code):
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print(text)
    return exit_code


def _error_report(exc, *, dry_run, output, window=None, now=None):
    status = "cooldown" if isinstance(exc, RateLimitError) else "failed"
    report = {"collector": "bilibili-search-24h", "status": status, "dry_run": dry_run,
              "stage": exc.stage, "message": exc.message, "http_status": exc.status,
              "body": exc.body,
              "cooldown_seconds": getattr(exc, "cooldown_seconds", None) if status == "cooldown" else None,
              "error_code": getattr(exc, "code", None) if isinstance(exc, BusinessError) else None,
              "request_count": _REQ["count"]}
    if window:
        report["window"] = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in window.items()}
    if now:
        report["now"] = now.isoformat()
    return _emit(report, output, 3)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, required=True, help="必传，1..100，实际总上限")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD，上海自然日窗口")
    parser.add_argument("--hours", type=int, default=None, help="最近 N 小时（与 --date 互斥）")
    parser.add_argument("--min-age-hours", type=int, default=None)
    parser.add_argument("--max-requests", type=int, default=2, help="本轮 HTTP 请求上限（含 nav），1..20")
    parser.add_argument("--pages", type=int, default=1, help="每个关键词页数，1..10")
    parser.add_argument("--page-start", type=int, default=1, help="起始页（供父调度器分页，默认 1）")
    parser.add_argument("--keywords", default=DEFAULT_KEYWORDS, help="逗号分隔，最多 10 个")
    parser.add_argument("--order", default="pubdate", choices=("pubdate", "click"))
    parser.add_argument("--per-category-limit", type=int, default=30, help="每类保留上限（默认 30）")
    parser.add_argument("--max-total", type=int, default=None, help="兼容别名，忽略；上限由 --limit 决定")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full-items", action="store_true",
                        help="dry-run 输出完整原标题/views/pubdate（供父调度器落库，不截断）")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--snapshot-root", type=Path, default=None)
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    db_path = args.db or (ROOT / "sentiment.db")
    snap_root = args.snapshot_root or db_path.parent
    if args.snapshot_root is not None and Path(snap_root).resolve() != Path(db_path).parent.resolve():
        return _emit({"collector": "bilibili-search-24h", "status": "failed", "stage": "args",
                      "message": f"--snapshot-root 必须与 --db 的目录对应：{snap_root} vs {db_path.parent}"},
                     args.output, 2)
    for cond, msg in ((1 <= args.limit <= 100, "--limit 必须在 1..100"),
                      (1 <= args.max_requests <= 20, "--max-requests 必须在 1..20"),
                      (1 <= args.pages <= MAX_PAGES, f"--pages 必须在 1..{MAX_PAGES}"),
                      (1 <= args.page_start <= 50, "--page-start 必须在 1..50"),
                      (1 <= args.per_category_limit <= 200, "--per-category-limit 必须在 1..200")):
        if not cond:
            return _emit({"collector": "bilibili-search-24h", "status": "failed", "stage": "args",
                          "message": msg}, args.output, 2)
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if len(keywords) > MAX_KEYWORDS:
        return _emit({"collector": "bilibili-search-24h", "status": "failed", "stage": "args",
                      "message": f"--keywords 最多 {MAX_KEYWORDS} 个"}, args.output, 2)
    if not keywords:
        return _emit({"collector": "bilibili-search-24h", "status": "failed", "stage": "args",
                      "message": "--keywords 不能为空"}, args.output, 2)
    try:
        window = resolve_window(args.date, args.hours, args.min_age_hours, now)
    except ValueError as exc:
        return _emit({"collector": "bilibili-search-24h", "status": "failed", "stage": "args",
                      "message": str(exc)}, args.output, 2)

    _reset_requests(args.max_requests, 20.0)

    # 已有原帖 URL / 内容 hash / SimHash（尽量拿新记录）；dry-run 只读、不存在则不建。
    existing = {"urls": set(), "hashes": set(), "sims": []}
    db = None
    if db_path.is_file():
        ro = None
        try:
            ro = _ro_connect(db_path)
            existing = _load_existing(ro)
        except Exception as exc:
            return _error_report(CollectorError("database", f"只读预检失败，已停止：{exc}"),
                                 dry_run=args.dry_run, output=args.output, window=window, now=now)
        finally:
            if ro is not None:
                ro.close()
        db = None

    start, end = window["start_utc"], window["end_utc"]
    filters = {}
    category_counts = {}
    candidates, seen = [], set()

    def bump(reason):
        filters[reason] = filters.get(reason, 0) + 1

    mixin = None
    stopped = "exhausted"
    try:
        mixin = _mixin_key()
    except RequestBudgetError:
        stopped = "budget"
    except CollectorError as exc:
        if db:
            db.close()
        return _error_report(exc, dry_run=args.dry_run, output=args.output, window=window, now=now)

    if mixin is not None:
        done = False
        for keyword in keywords:
            if done:
                break
            for page in range(args.page_start, args.page_start + args.pages):
                try:
                    items = search(keyword, page, mixin, args.order)
                except RequestBudgetError:
                    stopped = "budget"
                    done = True
                    break
                except CollectorError as exc:
                    if db:
                        db.close()
                    return _error_report(exc, dry_run=args.dry_run, output=args.output,
                                         window=window, now=now)
                for item in items:
                    if not isinstance(item, dict):
                        bump("not_object")
                        continue
                    # 时间统一走 collection_policy：非 bool 精确秒 epoch 才 valid 并留证据。
                    time_info = collection_policy.normalize_published(item.get("pubdate"), mode="epoch")
                    if not time_info["accepted"]:
                        bump("time_" + str(time_info["reason"]))
                        continue
                    pub = item.get("pubdate")
                    moment = datetime.fromisoformat(time_info["published_at"])
                    if moment > now:
                        bump("future")
                        continue
                    if moment < start or moment >= end:
                        bump("out_of_window")
                        continue
                    bvid = item.get("bvid")
                    if not isinstance(bvid, str) or not bvid:
                        bump("no_bvid")
                        continue
                    raw_title = item.get("title")
                    if not isinstance(raw_title, str):
                        bump("title_type")
                        continue
                    title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
                    url = f"https://www.bilibili.com/video/{bvid}"
                    # 统一门：时间可追溯 + 非噪声（游戏实况/剧情/AI 故事）+ 性别相关 + URL 合法。
                    decision = collection_policy.gate_candidate(
                        title=title, url=url, summary="", source_id=bvid, published=time_info)
                    if not decision["accepted"]:
                        bump(decision["reason"])
                        continue
                    if bvid in seen:
                        bump("dup_bvid")
                        continue
                    canon = decision["url"]
                    ntitle = storage.normalized_text(title)
                    chash = storage.digest(storage.json_bytes([ntitle, ""]))
                    if canon in existing["urls"] or chash in existing["hashes"]:
                        bump("dup_existing")
                        continue
                    sh = storage.simhash64(ntitle + " ")
                    if any(storage.hamming64(sh, other) <= 4 for other in existing["sims"]):
                        bump("near_dup_existing")
                        continue
                    labels = topics.classify_all(title) or [topics.FALLBACK]
                    if any(category_counts.get(label, 0) >= args.per_category_limit for label in labels):
                        bump("per_category_full")
                        continue
                    for label in labels:
                        category_counts[label] = category_counts.get(label, 0) + 1
                    seen.add(bvid)
                    existing["urls"].add(canon)
                    existing["hashes"].add(chash)
                    existing["sims"].append(sh)
                    raw_play = item.get("play")
                    # 缺失/非数字 play → None（不造 0）；由视图层以 known=false 呈现。
                    views = raw_play if type(raw_play) is int else (
                        int(raw_play) if isinstance(raw_play, str) and raw_play.isdigit() else None)
                    published = moment.isoformat()
                    candidates.append({"bvid": bvid, "title": ntitle, "url": url,
                                       "pubdate": pub, "published_at": published,
                                       "published_local": moment.astimezone(SHANGHAI).isoformat(),
                                       "views": views, "labels": labels,
                                       "time_evidence": collection_policy.snapshot_evidence(decision)})
                    if len(candidates) >= args.limit:
                        done = True
                        stopped = "target"
                        break
                if done:
                    break

    def show(item):
        title = item["title"]
        return {"bvid": item["bvid"], "url": item["url"], "pubdate": item["pubdate"],
                "published_at": item["published_at"], "published_local": item["published_local"],
                "title": title if args.full_items else (title[:60] + ("…" if len(title) > 60 else "")),
                "views": item["views"], "labels": item["labels"],
                "time_evidence": item.get("time_evidence")}

    report = {
        "collector": "bilibili-search-24h", "dry_run": args.dry_run,
        "window": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in window.items()},
        "now": now.isoformat(), "limit": args.limit, "per_category_limit": args.per_category_limit,
        "max_requests": args.max_requests, "request_count": _REQ["count"], "filter_kinds": len(filters),
        "filters": filters, "candidates_found": len(candidates), "stopped": stopped,
        "items": [show(item) for item in candidates],
    }

    if args.dry_run:
        report["status"] = "dry_run"
        report["notes"] = ["dry-run：未写真实库/快照；上面为全部（最多 limit）合格候选"]
        if db:
            db.close()
        return _emit(report, args.output, 0)

    if db is None:
        db = storage.connect(db_path)
    try:
        feed = db.execute("SELECT id FROM feeds WHERE name='bilibili-search'").fetchone()
        if feed is None:
            with db:
                cur = db.execute("INSERT INTO feeds(name,url,added_at) VALUES(?,?,?)",
                                 ("bilibili-search", "https://search.bilibili.com/all?keyword=",
                                  storage.now()))
            feed_id = cur.lastrowid
        else:
            feed_id = feed["id"]
        if not candidates:
            report["status"] = "partial"
            report["import"] = None
            report["target_met"] = False
            return _emit(report, args.output, 2)
        rows = []
        for c in candidates:
            row = {"title": c["title"], "summary": "", "url": c["url"],
                   "source_id": c["bvid"], "published_at": c["published_at"],
                   "raw_xml": json.dumps({"source": "bilibili-search", "bvid": c["bvid"],
                                          "pubdate": c["pubdate"],
                                          "time_evidence": c.get("time_evidence")},
                                         ensure_ascii=False, sort_keys=True)}
            if c["views"] is not None:      # 未知播放量不进快照（不伪造 0）
                row["views"] = c["views"]
            rows.append(row)
        run_id = storage.start_run(db, feed_id, "bilibili-search", max(1, min(len(rows), 100)))
        outcome = storage.import_batch(db, snap_root, run_id, rows, server.classify, noise=False)
        report["import"] = {k: outcome[k] for k in
                            ("input_count", "added", "duplicates", "near_duplicates",
                             "rejected", "noise_rejected")}
        report["target"] = args.limit
        report["target_met"] = outcome["added"] >= args.limit
        report["status"] = "ok" if report["target_met"] else "partial"
    finally:
        db.close()
    return _emit(report, args.output, 0 if report["status"] == "ok" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
