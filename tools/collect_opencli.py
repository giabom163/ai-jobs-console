#!/usr/bin/env python
"""S1 只读采集器：用本机已装的 OpenCLI 取公开热榜/推荐流，按声明式映射表投影为 records 字段。

当前端点（全部 access=read 的公开榜单，单次一次调用、无分页、无重试）：
  - toutiao hot / toutiao recommend（既有，本轮行为不变）
  - weibo hot / tieba hot / 36kr hot（本轮新增多来源）

复用优先（见 docs/S1-collector.md 的“复用清单与不新写的理由”）：
  - 取数：OpenCLI 站点适配器 `opencli <site> <cmd>`，只调用公开热榜/推荐流读取命令，
    无写动作、无登录、不复制 cookie。
  - 写库与快照：只调用 storage.py 既有函数 connect/start_run/import_batch/save_snapshot。
    不新建数据库、不新建表、不改表结构。
  - 情感粗筛：复用 server.py 既有 classify 词典函数（未复核初筛，不代表准确情感分析）。
本文件自己写的只有：CLI 定位、声明式字段映射表（ENDPOINTS）、参数解析与错误呈现。

只读硬约束：argv 由 ENDPOINTS 常量拼装，不接受任意子命令，因此不存在发布/评论/关注/删除路径。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402  复用已复核的粗略情感词典 classify
import storage  # noqa: E402  复用项目证据库既有函数

COLLECTOR = "s1-opencli"
DEFAULT_LIMIT = 3
MAX_LIMIT = 50  # 契约：单次 ≤3 页或 ≤50 条；OpenCLI 自身 limit 上限也是 50
DEFAULT_TIMEOUT = 60
DEFAULT_DB = ROOT / "sentiment.db"

# 唯一允许的读端点：声明式映射表。argv 只由这里拼装，调用方无法传入其它子命令。
#
# 每行字段含义（逐源声明，映射逻辑里没有任何 if/else 分支）：
#   argv                              opencli 子命令及固定参数；access=read，只读公开列表
#   feed_url                          来源分组标识，落 feeds.url（见 docs/S1-collector.md 的未验证项）
#   default_group                     默认分组名，落 feeds.name
#   fields                            本地 records 字段 -> 上游原始字段名：
#                                     None = 该端点没有这个字段，一律留 null/空串，
#                                     不从 URL、标题或其它字段反推（不猜）
#   summary_empty_when_same_as_title  summary 与 title 完全相同则置空（头条 hot 的 query 就是标题）
#
# 上游原始字段名：头条两个端点沿用上一轮实测（本轮未重跑）；新端点为 2026-10-16 实测
# （npm @jackwener/opencli 1.8.8，`-f json`）：
#   头条 hot:       rank/title/query/group_id/hot_value/label/image_url
#   头条 recommend: rank/title/abstract/group_id/published_at/comments/source/tag/image_url
#   微博 hot:       rank/word/category/hot_value/label/url（**没有 title 字段，标题在 word**）
#   贴吧 hot:       rank/title/description/discussions/url（**没有发布时间**）
#   36氪 hot:       rank/title/url（**实测只有这三个字段**）
ENDPOINTS = {
    "hot": {
        "argv": ["toutiao", "hot"],
        "feed_url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        "default_group": "toutiao-hot",
        "fields": {"title": "title", "url": "url", "summary": "query",
                   "source_id": "group_id", "published_at": None},
        "summary_empty_when_same_as_title": True,
    },
    "recommend": {
        "argv": ["toutiao", "recommend"],
        "feed_url": "https://www.toutiao.com/api/pc/feed/",
        "default_group": "toutiao-recommend",
        "fields": {"title": "title", "url": "url", "summary": "abstract",
                   "source_id": "group_id", "published_at": "published_at"},
        "summary_empty_when_same_as_title": False,
    },
    "weibo-hot": {
        "argv": ["weibo", "hot"],
        "feed_url": "https://s.weibo.com/top/summary",
        "default_group": "weibo-hot",
        "fields": {"title": "word", "url": "url", "summary": "category",
                   "source_id": None, "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
    "tieba-hot": {
        "argv": ["tieba", "hot"],
        "feed_url": "https://tieba.baidu.com/hottopic/browse/topicList",
        "default_group": "tieba-hot",
        "fields": {"title": "title", "url": "url", "summary": "description",
                   "source_id": None, "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
    "36kr-hot": {
        "argv": ["36kr", "hot"],
        "feed_url": "https://36kr.com/hot-list/catalog",
        "default_group": "36kr-hot",
        "fields": {"title": "title", "url": "url", "summary": None,
                   "source_id": None, "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
}

# 本地 schema 里必须有值、缺失即报错的字段（不静默丢弃，不用其它字段顶替）。
REQUIRED_FIELDS = ("title", "url")


def _validate_endpoints():
    """声明式映射表的完整性自检：少键/多键都在导入时立刻报错，而不是运行到一半才 KeyError。"""
    expected = {"argv", "feed_url", "default_group", "fields", "summary_empty_when_same_as_title"}
    for name, spec in ENDPOINTS.items():
        if set(spec) != expected:
            raise RuntimeError(f"端点 {name} 的声明键不完整：{sorted(spec)}")
        if set(spec["fields"]) != {"title", "url", "summary", "source_id", "published_at"}:
            raise RuntimeError(f"端点 {name} 的 fields 键不完整：{sorted(spec['fields'])}")
        if not all(spec["fields"].get(key) for key in REQUIRED_FIELDS):
            raise RuntimeError(f"端点 {name} 必须声明 {'/'.join(REQUIRED_FIELDS)} 的上游字段")


_validate_endpoints()

NOTES = [
    "本轮只执行 --dry-run；真实写库路径未在本轮执行，属未实证项。",
    "feed 行沿用既有 feeds 表；records/snapshot 全部经 storage.py 既有函数写入。",
    "上游每次轮询 hot_value / comments 变化会产生新的 raw_sha256，按 UNIQUE(feed_id,raw_sha256) 视为一次新观测。",
    "sentiment 仍是未复核词典粗筛，时间质量由 storage.publication 判定。",
    "多来源：weibo-hot/tieba-hot/36kr-hot 的字段名与头条不同，逐源映射见 ENDPOINTS；"
    "上游未提供的字段（发布时间、source_id、36kr 的 summary）一律留 null/空串，不从 URL 或标题反推。",
]


class CollectError(RuntimeError):
    """带阶段与原始错误文本的失败，便于原文呈现，不静默降级。"""

    def __init__(self, stage, message, exit_code, extra=None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.exit_code = exit_code
        self.extra = extra or {}


def utc():
    return datetime.now(timezone.utc).isoformat()


def _force_utf8_stdout():
    """Windows 控制台默认 GBK；上游正文含 emoji 时直接 print 会 UnicodeEncodeError。

    实机缺陷（本轮）:recommend 某条含 U+1F602，未修复前 exit 1 并抛 Traceback。
    这里把标准输出/错误切到 UTF-8（无法编码的字符用 replace 兜底），保证 JSON 一定打得出来。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _first_file(candidates):
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def node_candidates(explicit):
    out = []
    if explicit:
        out.append(Path(explicit))
    env = os.environ.get("OPENCLI_NODE")
    if env:
        out.append(Path(env))
    found = shutil.which("node")
    if found:
        out.append(Path(found))
    versions = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if versions.is_dir():
        out.extend(sorted((p / "node.exe" for p in versions.iterdir()), reverse=True))
    out.append(Path("C:/Program Files/nodejs/node.exe"))
    return out


def opencli_candidates(explicit):
    rel = Path("node_modules/@jackwener/opencli/dist/src/main.js")
    out = []
    if explicit:
        out.append(Path(explicit))
    env = os.environ.get("OPENCLI_MAIN")
    if env:
        out.append(Path(env))
    profiles = Path.home() / ".dsh" / "profiles"
    if profiles.is_dir():
        out.extend(sorted((p / rel for p in profiles.iterdir()), reverse=True))
    out.append(Path.home() / rel)
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "npm" / rel)
    return out


def run_opencli(node, main, endpoint, limit, timeout):
    """唯一一次上游调用：read-only 公开端点，无分页、无重试、无并发。

    失败时带出该站点的 endpoint 名、真实退出码与 stderr 原文，不用别的站点数据顶替。
    """
    argv = [str(node), str(main), *ENDPOINTS[endpoint]["argv"], "--limit", str(limit), "-f", "json"]
    started = utc()
    clock = time.monotonic()
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired as exc:
        raise CollectError("opencli", f"opencli 超时（{timeout}s），已停止，不自动重试",
                           3, {"endpoint": endpoint, "argv": argv,
                               "stdout_head": (exc.stdout or "")[:2000]}) from exc
    except OSError as exc:
        raise CollectError("opencli", f"无法启动 opencli：{exc}", 2,
                           {"endpoint": endpoint, "argv": argv}) from exc
    finished = utc()
    call = {"endpoint": endpoint, "argv": argv, "cwd": str(ROOT), "timeout_seconds": timeout,
            "started_at": started, "finished_at": finished,
            "duration_seconds": round(time.monotonic() - clock, 3), "return_code": done.returncode}
    if done.returncode != 0:
        raise CollectError("opencli", f"opencli 退出码非 0：{done.returncode}",
                           3, {"call": call, "stderr": done.stderr.strip()[:4000],
                               "stdout_head": done.stdout.strip()[:4000]})
    text = done.stdout.strip()
    if not text:
        raise CollectError("opencli", "opencli 退出码为 0 但没有输出", 4,
                           {"call": call, "stderr": done.stderr.strip()[:4000]})
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CollectError("parse", f"opencli 输出不是合法 JSON：{exc}", 4,
                           {"call": call, "stdout_head": text[:2000]}) from exc
    if isinstance(payload, dict):
        code = (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else None
        raise CollectError("opencli", f"opencli 返回错误信封：{json.dumps(payload, ensure_ascii=False)[:2000]}",
                           3, {"call": call, "error_code": code})
    if not isinstance(payload, list):
        raise CollectError("parse", "opencli JSON 顶层不是数组", 4,
                           {"call": call, "stdout_head": text[:2000]})
    if not payload:
        # OpenCLI 自身对空列表抛 EmptyResultError（退出码非 0），这里只是第二道防线。
        raise CollectError("empty", "上游返回空列表", 5, {"call": call})
    return payload[:limit], call


def take(item, field_name):
    """按映射表取上游字段原文：字段未声明（None）或上游缺失/非文本时返回 None。

    不填默认值、不从其它字段反推（不猜）；返回的字符串保持既有行为做首尾去空白。
    """
    if field_name is None:
        return None
    value = item.get(field_name)
    return value.strip() if isinstance(value, str) else None


def map_rows(endpoint, items):
    """按声明式映射表把 OpenCLI 条目投影为 records 行 + 供 dry-run 展示的字段预览。

    原始字段名只从 ENDPOINTS[endpoint]["fields"] 读取；缺 title/url 直接报错并附原始条目，
    不静默丢弃；未声明的字段留 null/空串。
    """
    spec = ENDPOINTS[endpoint]
    fields = spec["fields"]
    rows, previews = [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise CollectError("map", f"第 {index + 1} 条不是对象：{json.dumps(item, ensure_ascii=False)[:500]}", 6)
        title = take(item, fields["title"]) or ""
        url = take(item, fields["url"]) or ""
        if not title or not url:
            raise CollectError("map", f"第 {index + 1} 条缺少 title/url：{json.dumps(item, ensure_ascii=False)[:500]}", 6)
        source_id = take(item, fields["source_id"]) or None
        summary = take(item, fields["summary"]) or ""
        if spec["summary_empty_when_same_as_title"] and summary == title:
            summary = ""
        published_at = take(item, fields["published_at"])  # 上游没有该字段时保持 None，不猜发布时间
        raw_xml = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = {"title": title, "summary": summary, "url": url, "source_id": source_id,
               "published_at": published_at, "raw_xml": raw_xml}
        normalized, quality = storage.publication(published_at)
        previews.append({
            "id": "(写库时由 storage.import_batch 生成 record-<uuid4>)",
            "feed_id": "(写库时按来源分组解析/创建)",
            "canonical_id": "(写库时按 exact-text-v1 精确正文规则分配)",
            "raw_sha256": storage.digest(storage.json_bytes(row)),
            "raw_path": "snapshots/<raw_sha256>.json",
            "title": storage.normalized_text(title),
            "summary": storage.normalized_text(summary),
            "url": item.get(fields["url"]),
            "canonical_url": storage.canonical_url(url),
            "source_id": source_id,
            "published_at": normalized,
            "published_raw": published_at,
            "time_quality": quality,
            "rank": item.get("rank"),
            "raw_sha256_method": "sha256(storage.json_bytes(row))，与 save_snapshot 落盘字节一致",
        })
        rows.append(row)
    return rows, previews


def ensure_feed(db, group_id, feed_url):
    """按 url 复用既有 feeds 行；缺失时用与 server.py 相同的 INSERT 语句创建（不改表结构）。"""
    existing = db.execute("SELECT id FROM feeds WHERE url=?", (feed_url,)).fetchone()
    if existing:
        return existing["id"], "reused"
    with db:
        cursor = db.execute("INSERT INTO feeds(name,url,added_at) VALUES(?,?,?)",
                            (group_id[:100], feed_url, storage.now()))
    return cursor.lastrowid, "created"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", choices=sorted(ENDPOINTS), default="hot",
                        help="读端点（白名单）：hot（头条热榜，默认）/ recommend（头条推荐流）/ "
                             "weibo-hot（微博热搜）/ tieba-hot（贴吧热议）/ 36kr-hot（36氪热榜）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"返回条数 1-{MAX_LIMIT}（默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--dry-run", action="store_true", help="只采集并打印映射结果，不写数据库")
    parser.add_argument("--group-id", default=None,
                        help="来源分组标识（落 feeds.name），默认按端点取 "
                             + " / ".join(f"{name}={ENDPOINTS[name]['default_group']}" for name in sorted(ENDPOINTS)))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="既有证据库路径，默认 <仓库>/sentiment.db")
    parser.add_argument("--node", default=None, help="node 可执行文件；默认按环境变量与已知路径查找")
    parser.add_argument("--opencli", default=None, help="OpenCLI 的 dist/src/main.js 路径；默认按已知 profile 路径查找")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"单次调用超时秒数（默认 {DEFAULT_TIMEOUT}）")
    return parser


def main(argv=None):
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    group_id = args.group_id or ENDPOINTS[args.endpoint]["default_group"]
    spec = ENDPOINTS[args.endpoint]
    context = {"endpoint": args.endpoint, "group_id": group_id, "dry_run": args.dry_run}
    node = main_js = None
    try:
        if not 1 <= args.limit <= MAX_LIMIT:
            raise CollectError("args", f"--limit 必须在 1-{MAX_LIMIT} 之间，收到 {args.limit}", 2)
        node = _first_file(node_candidates(args.node))
        if node is None:
            raise CollectError("env", "未找到 node 可执行文件，请用 --node 指定",
                               2, {"searched": [str(p) for p in node_candidates(args.node)]})
        main_js = _first_file(opencli_candidates(args.opencli))
        if main_js is None:
            raise CollectError("env", "未找到 OpenCLI 的 dist/src/main.js，请用 --opencli 指定",
                               2, {"searched": [str(p) for p in opencli_candidates(args.opencli)]})
        items, call = run_opencli(node, main_js, args.endpoint, args.limit, args.timeout)
        rows, previews = map_rows(args.endpoint, items)
    except CollectError as exc:
        extra = dict(context)
        if node is not None:
            extra["node"] = str(node)
        if main_js is not None:
            extra["opencli"] = str(main_js)
        return emit_error(exc, extra)
    report = {
        "collector": COLLECTOR,
        "status": "ok",
        "endpoint": args.endpoint,
        "group_id": group_id,
        "limit": args.limit,
        "dry_run": args.dry_run,
        "node": str(node),
        "opencli": str(main_js),
        "opencli_version_expectation": "1.8.8 (npm @jackwener/opencli；实测版本见 docs/S1-collector.md)",
        "access": "read",
        "request_count": 1,
        "call": call,
        "feed_url": spec["feed_url"],
        "field_map": dict(spec["fields"]),
        "returned_count": len(items),
        "db": {"path": str(args.db), "present": args.db.is_file(), "touched": False},
        "records": previews,
        "notes": NOTES,
    }
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    try:
        db = storage.connect(args.db)
    except Exception as exc:  # sqlite3.Error / OSError
        return emit_error(CollectError("db", f"打开证据库失败：{exc}", 7, {"db": str(args.db)}), report)
    try:
        feed_id, feed_state = ensure_feed(db, group_id, spec["feed_url"])
        run_id = storage.start_run(db, feed_id, "opencli-" + args.endpoint, args.limit)
        try:
            outcome = storage.import_batch(db, ROOT, run_id, rows, server.classify)
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = {"stage": "import", "run_id": run_id, "db": str(args.db),
                               "type": type(exc).__name__, "message": str(exc)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 7
        report["db"] = {"path": str(args.db), "present": True, "touched": True}
        report["feed"] = {"id": feed_id, "state": feed_state, "name": group_id, "url": spec["feed_url"]}
        report["run"] = {key: outcome[key] for key in
                         ("id", "status", "input_count", "added", "duplicates", "input_sha256", "started_at", "finished_at")}
    finally:
        db.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def emit_error(exc, context=None):
    payload = {"collector": COLLECTOR, "status": "blocked" if exc.exit_code == 2 else "failed",
               "error": {"stage": exc.stage, "exit_code": exc.exit_code, "message": exc.message,
                         **exc.extra}}
    if context:
        payload.update(context)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
