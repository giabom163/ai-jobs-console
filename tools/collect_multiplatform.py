"""多平台采集调度（真实多进程编排 + 唯一父 writer）。

对齐已批准方案第 6 条与首审 P0/P1：
- **真实 multiprocessing(spawn) worker**（1..3），worker 只取源/解析，不写库；
- **父进程唯一 writer + 配额仲裁**；父进程按来源发 **permit**（Queue/Pipe），
  B 站 worker 每个真实 HTTP 前也经父进程同一许可（不再先跑完子进程才补记）；
- 浏览器任务（OpenCLI）**同时只允许一个在跑**，公共 HTTP 任务可并行排队；
- 持久全局 ``CallGate``（JSON 账本，跨启动保留 last_call/cooldown；父进程独有，
  worker 不各持一份互相覆盖）；429/登录墙/断桥 → 停来源 + 持久 >=300s 冷却；
- 子限额分块：B 站单次 <=100、max_requests <=20；OpenCLI 单次 <=50；父进程累计
  **总 added <= --limit**，不重复页 1 凑数；
- 全来源（含 DOM）统一在父进程 gate 后严格比较上海 date 的 ``[start,end)``，拒绝未来/
  不明时间；dedup（URL/content hash/SimHash）对已存库先去重，旧 URL 换 feed 不算新增；
- 原生 OpenCLI 内部 HTTP 次数无法硬证实 → 标 ``native_http=unknown``，不宣称硬受控。
- 只准既有六平台；不采评论正文；不新增登录/凭据；禁止任意命令/URL/shell。
"""
from __future__ import annotations

import argparse
import io
import json
import multiprocessing as mp
import os
import queue
import sys
import time
from collections import deque
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collection_policy  # noqa: E402

MAX_LIMIT = 400
MAX_WORKERS = 3
MAX_CALLS_CAP = 60
DEFAULT_MAX_CALLS = 2
MIN_INTERVAL = 20
COOLDOWN_429 = 300
DEFAULT_TIMEOUT = 60
DEFAULT_DB = ROOT / "sentiment.db"
DEFAULT_LEDGER = ROOT / ".multiplatform-throttle.json"
LEDGER_VERSION = 1
SHANGHAI = timezone(timedelta(hours=8))

BILIBILI_SUBLIMIT = 100
BILIBILI_SUBREQ = 20
OPENCLI_SUBLIMIT = 50
# 单页条数证据：tools/collect_bilibili_24h.py search page_size=30；真实 ledger 该响应
# pagesize=30、result_len=30（前轮 9 页 270 条一致）。按此算页，不按未经核实的 20。
BILIBILI_PAGE_SIZE = 30

PLATFORMS = {
    "bilibili": {"kind": "bilibili"},
    "weibo": {"kind": "opencli", "endpoint": "weibo-hot"},
    "toutiao": {"kind": "opencli", "endpoint": "hot"},
    "tieba": {"kind": "opencli", "endpoint": "tieba-hot"},
    "zhihu": {"kind": "opencli", "endpoint": "zhihu-hot"},
    "xiaohongshu": {"kind": "opencli", "endpoint": "xiaohongshu-feed"},
}
PLATFORM_DOMAINS = {
    "bilibili": "bilibili.com", "weibo": "weibo.com", "toutiao": "toutiao.com",
    "tieba": "tieba.baidu.com", "zhihu": "zhihu.com", "xiaohongshu": "xiaohongshu.com",
}
STOP_SIGNALS = {"HTTP_429", "LOGIN_WALL", "AUTH_REQUIRED", "BROWSER_CONNECT",
                "COMMAND_EXEC", "RATE_LIMITED", "HTTP_5XX"}
# 共享浏览器不可用信号：命中即停所有浏览器（OpenCLI）任务，避免连续撞。
BROWSER_STOP = {"BROWSER_CONNECT", "COMMAND_EXEC", "LOGIN_WALL", "AUTH_REQUIRED"}
# 429/限流：本轮立即停全部来源请求 + 持久全局冷却（跨启动不能换 source 绕过）。
GLOBAL_STOP = {"HTTP_429", "RATE_LIMITED"}
DOM_ITEM_FIELDS = {"title", "url", "source_id", "published_at", "published_raw",
                   "observed_at", "source_timezone", "source_field", "precision",
                   "derived_from", "summary", "views"}
DOM_REQUIRED = {"title", "url"}


class PolicyError(RuntimeError):
    pass


class SourceError(PolicyError):
    """带结构化风控字段的来源失败（保留真实 signal/status/body，不吞成空表）。"""

    def __init__(self, message, signal=None, status=None, body=None):
        super().__init__(message)
        self.signal = signal
        self.status = status
        self.body = body


# --- 时间/账本 ---------------------------------------------------------------------------

def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def shanghai_window(date_str, now):
    """上海自然日 [00:00, 次日00:00) → UTC；未来日期拒绝。"""
    import re
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (date_str or "").strip())
    if not match:
        raise PolicyError(f"--date 必须是 YYYY-MM-DD：{date_str!r}")
    y, mo, d = map(int, match.groups())
    try:
        start = datetime(y, mo, d, tzinfo=SHANGHAI)
    except ValueError as exc:
        raise PolicyError(f"--date 不是合法日期：{date_str}") from exc
    if start.date() > now.astimezone(SHANGHAI).date():
        raise PolicyError(f"--date 不能是未来日期：{date_str}")
    return {"date": f"{y:04d}-{mo:02d}-{d:02d}",
            "start": start.astimezone(timezone.utc),
            "end": (start + timedelta(days=1)).astimezone(timezone.utc)}


def load_ledger(path):
    if not path.is_file():
        return {"version": LEDGER_VERSION, "updated_at": None, "last_call_at": None,
                "sources": {}}, "absent"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"许可/冷却账不可读，为避免放开限流已停止：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise PolicyError("许可/冷却账结构不符合预期（缺少 sources 对象）")
    for name, entry in data["sources"].items():
        if not isinstance(entry, dict):
            raise PolicyError(f"许可/冷却账来源条目不是对象：{name}")
        if parse_iso(entry.get("last_call_at")) is None and parse_iso(entry.get("cooldown_until")) is None:
            raise PolicyError(f"许可/冷却账来源条目缺少可解析时间：{name}")
    data.setdefault("last_call_at", None)
    return data, "loaded"


def save_ledger(path, data):
    payload = dict(data)
    payload["version"] = LEDGER_VERSION
    payload["updated_at"] = _iso(datetime.now(timezone.utc))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)
    return payload


class CallGate:
    """父进程唯一的全局许可/冷却闸门（测试可注入 clock；实采用真实时钟 + interval>=20）。"""

    def __init__(self, ledger, *, max_calls, interval, cooldown_seconds=COOLDOWN_429,
                 clock=None):
        self.ledger = ledger
        self.max_calls = int(max_calls)
        self.interval = float(interval)
        self.cooldown_seconds = float(cooldown_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.calls_used = 0

    def _now(self, now):
        return now or self.clock()

    def remaining(self):
        return max(0, self.max_calls - self.calls_used)

    def check(self, source, now=None):
        now = self._now(now)
        # 全局冷却：429 后本轮停全部来源，跨启动也不能换 source 绕过。
        global_until = parse_iso(self.ledger.get("global_cooldown_until"))
        if global_until is not None and now < global_until:
            return False, "cooldown", (global_until - now).total_seconds()
        if self.calls_used >= self.max_calls:
            return False, "budget", None
        entry = self.ledger["sources"].get(source) or {}
        until = parse_iso(entry.get("cooldown_until"))
        if until is not None and now < until:
            return False, "cooldown", (until - now).total_seconds()
        last = parse_iso(self.ledger.get("last_call_at"))
        if last is not None:
            return True, "ok", max(0.0, self.interval - (now - last).total_seconds())
        return True, "ok", 0.0

    def arm_global(self, now=None, seconds=None, signal="HTTP_429"):
        now = self._now(now)
        self.ledger["global_cooldown_until"] = _iso(now + timedelta(seconds=seconds or self.cooldown_seconds))
        self.ledger["global_last_signal"] = signal

    def record(self, source, status, now=None, error=None, count=1):
        now = self._now(now)
        entry = self.ledger["sources"].get(source)
        if not isinstance(entry, dict):
            entry = {}
        entry.update({"last_call_at": _iso(now), "last_status": status,
                      "last_error": str(error)[:500] if error else entry.get("last_error")})
        self.ledger["sources"][source] = entry
        self.ledger["last_call_at"] = _iso(now)
        self.calls_used += int(count)

    def arm_cooldown(self, source, now=None, seconds=None, signal="HTTP_429"):
        """来源级冷却：同时写 last_call_at 与 cooldown_until（修此前只写 cooldown_until 的坏账）。"""
        now = self._now(now)
        entry = self.ledger["sources"].get(source)
        if not isinstance(entry, dict):
            entry = {}
        entry["last_call_at"] = entry.get("last_call_at") or _iso(now)
        entry["cooldown_until"] = _iso(now + timedelta(seconds=seconds or self.cooldown_seconds))
        entry["last_signal"] = signal
        entry["last_status"] = "cooldown"
        self.ledger["sources"][source] = entry


# --- DOM 证据（严格 schema + 平台域匹配） -------------------------------------------------

def load_dom_evidence(path):
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"DOM 证据不可读或非 JSON：{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("DOM 证据顶层必须是对象")
    platform = data.get("platform")
    if platform not in PLATFORMS:
        raise PolicyError(f"DOM 证据 platform 必须是六既有平台之一：{platform!r}")
    items = data.get("items")
    if not isinstance(items, list):
        raise PolicyError("DOM 证据缺少 items 数组")
    rows = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PolicyError(f"DOM 证据第 {index + 1} 条不是对象")
        extra = set(item) - DOM_ITEM_FIELDS
        if extra:
            raise PolicyError(f"DOM 证据第 {index + 1} 条含未声明字段：{sorted(extra)}")
        if not DOM_REQUIRED <= set(item):
            raise PolicyError(f"DOM 证据第 {index + 1} 条缺少 {'/'.join(sorted(DOM_REQUIRED))}")
        url = item.get("url")
        if not isinstance(url, str):
            raise PolicyError(f"DOM 证据第 {index + 1} 条 url 必须是字符串")
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            raise PolicyError(f"DOM 证据第 {index + 1} 条 url 必须为 http(s)：{parts.scheme!r}")
        host = (parts.hostname or "").lower()
        domain = PLATFORM_DOMAINS[platform]
        # 严格：host == 域 或 host 以 “.域” 结尾（拒绝 evilxiaohongshu.com）。
        if not (host == domain or host.endswith("." + domain)):
            raise PolicyError(f"DOM 证据第 {index + 1} 条来源域与平台不匹配：{host!r} vs {domain}")
        rows.append({**item, "platform": platform})
    return {"platform": platform, "items": rows,
            "observed_at": data.get("observed_at"),
            "source_timezone": data.get("source_timezone")}


# --- 子限额分块与计划 --------------------------------------------------------------------

def plan_bilibili_calls(limit, keywords):
    """把父目标拆成 B 站子任务：每个任务 <=100 保留上限、pages<=10、max_requests<=20。

    参数上允许累计 >= limit（4×100=400），不是天然小于 400；关键词轮换避免重复页 1
    （同一关键词才分页，不同任务用不同关键词）。不做窗口/准入扩张。
    """
    kws = [k.strip() for k in (keywords or "").split(",") if k.strip()] or ["性别"]
    target = min(limit, MAX_LIMIT)
    tasks, capacity, i = [], 0, 0
    cursor = {k: 1 for k in kws}
    while capacity < target and i < 24:
        keyword = kws[i % len(kws)]
        child_limit = min(BILIBILI_SUBLIMIT, target - capacity)
        pages = min(10, max(1, -(-child_limit // BILIBILI_PAGE_SIZE)))   # ceil，<=10
        page_start = cursor[keyword]
        cursor[keyword] = page_start + pages       # 同关键词后续任务从新页起，不重复同页
        tasks.append({"keywords": keyword, "pages": pages, "page_start": page_start,
                      "child_limit": child_limit,
                      "child_max_requests": min(BILIBILI_SUBREQ, pages + 1),
                      "per_category_limit": 100})
        capacity += child_limit
        i += 1
    return tasks


def plan_batches(rows, size=100):
    size = max(1, int(size))
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def aggregate(decisions):
    out = {"candidates": len(decisions), "accepted": 0, "time_invalid": 0,
           "noise": 0, "out_of_window": 0, "duplicates": 0, "other_rejected": 0}
    for d in decisions:
        if d["accepted"]:
            out["accepted"] += 1
        elif d["reason"].startswith("time:"):
            out["time_invalid"] += 1
        elif d["reason"].startswith("noise:"):
            out["noise"] += 1
        elif d["reason"] == "out_of_window":
            out["out_of_window"] += 1
        elif d["reason"] == "duplicate":
            out["duplicates"] += 1
        else:
            out["other_rejected"] += 1
    return out


def evaluate_target(added, target):
    return {"target": target, "added": added, "target_met": added >= target,
            "status": "ok" if added >= target else "partial"}


def apply_window(published_iso, window, now):
    """父进程统一日期窗 + 未来拒绝；返回 (accepted, reason)。"""
    moment = parse_iso(published_iso)
    if moment is None:
        return False, "time:missing"
    if moment > now:
        return False, "future"
    if window and not (window["start"] <= moment < window["end"]):
        return False, "out_of_window"
    return True, "ok"


# --- worker 侧（spawn；只取源/解析，绝不写库） --------------------------------------------

def _permit(permit_q, grant_q, worker_id, counter, source):
    counter[0] += 1
    token = f"{worker_id}:{counter[0]}"
    permit_q.put({"token": token, "worker": worker_id, "source": source})
    while True:
        reply = grant_q.get()
        if reply.get("token") == token:
            return reply


def _fetch_dom(task, config):
    # 把 DOM 级 observed_at/source_timezone 回灌到每行，供 relative 时间解析追溯。
    observed = task.get("observed_at")
    tz = task.get("source_timezone")
    rows = [{**item, "observed_at": item.get("observed_at") or observed,
             "source_timezone": item.get("source_timezone") or tz}
            for item in task["items"]]
    return rows, {"kind": "dom", "observed_at": observed, "source_timezone": tz}


def _fetch_bilibili(task, config, worker_id, permit_q, grant_q, counter):
    import collect_bilibili_24h as b24
    original = b24._reserve_request

    def gated():
        reply = _permit(permit_q, grant_q, worker_id, counter, "bilibili")
        if not reply.get("granted"):
            if reply.get("reason") == "cooldown":
                raise b24.RateLimitError()
            raise b24.RequestBudgetError("父进程许可预算用尽")
        return original()

    b24._reserve_request = gated
    argv = ["--limit", str(task["child_limit"]), "--dry-run", "--full-items",
            "--pages", str(task.get("pages", 1)),
            "--page-start", str(task.get("page_start", 1)),
            "--max-requests", str(task["child_max_requests"]),
            "--per-category-limit", str(task.get("per_category_limit", 100)),
            "--keywords", task.get("keywords") or "性别"]
    if config.get("date"):
        argv += ["--date", config["date"]]
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            code = b24.main(argv)
    finally:
        b24._reserve_request = original
    text = buffer.getvalue().strip()
    report = None
    try:
        report = json.loads(text)
    except ValueError:
        start = text.find("{")
        if start >= 0:
            try:
                report = json.loads(text[start:])
            except ValueError:
                report = None
    if report is None:
        raise SourceError(f"B 站子采集器输出不可解析（code={code}）：{text[:400]}",
                          signal="SCHEMA")
    status = report.get("status")
    if status == "cooldown" or report.get("http_status") == 429:
        raise SourceError("B 站来源触发 429/冷却", signal="HTTP_429",
                          status=report.get("http_status"), body=report.get("body"))
    if status not in ("dry_run",):
        signal = report.get("error_code") or "SCHEMA"
        if status == "failed" and report.get("stage") == "args":
            signal = "BAD_ARGS"
        raise SourceError(f"B 站子采集器 status={status}：{report.get('message')}",
                          signal=str(signal), status=report.get("http_status"),
                          body=report.get("body"))
    rows = [{"title": it["title"], "url": it["url"], "source_id": it["bvid"],
             "published_at": it["published_at"], "published_raw": it.get("pubdate"),
             "source_field": "pubdate", "precision": "second",
             "time_evidence": it.get("time_evidence"), "views": it.get("views"),
             "summary": ""} for it in report.get("items", [])]
    return rows, {"kind": "bilibili", "request_count": int(report.get("request_count") or 0),
                  "stopped": report.get("stopped"), "filters": report.get("filters")}


def _fetch_opencli(task, config, worker_id, permit_q, grant_q, counter):
    import collect_opencli
    reply = _permit(permit_q, grant_q, worker_id, counter, task["source"])
    if not reply.get("granted"):
        raise SourceError("父进程许可未授予：" + str(reply.get("reason")),
                          signal="RATE_LIMITED" if reply.get("reason") == "cooldown" else None)
    node = collect_opencli._first_file(collect_opencli.node_candidates(None))
    main_js = collect_opencli._first_file(collect_opencli.opencli_candidates(None))
    if node is None or main_js is None:
        raise SourceError("未找到 node / OpenCLI 入口", signal="ENV")
    try:
        items, call = collect_opencli.run_opencli(node, main_js, task["endpoint"],
                                                  task["child_limit"], DEFAULT_TIMEOUT, task.get("keyword"))
    except collect_opencli.CollectError as exc:
        signal = collect_opencli.risk_signal(exc) or "ERROR"
        raise SourceError(exc.message, signal=signal,
                          status=exc.extra.get("http_status"), body=exc.extra.get("stderr")) from exc
    rows, _previews, invalid = collect_opencli.map_rows(task["endpoint"], items)
    rows = [{"title": r["title"], "url": r["url"], "summary": r.get("summary") or "",
             "source_id": r.get("source_id"), "published_at": r.get("published_at"),
             "raw_xml": r.get("raw_xml")}
            for r in rows]
    return rows, {"kind": "opencli", "invalid_rows": invalid, "call": call,
                  "native_http": "unknown"}


def worker_loop(worker_id, task_q, result_q, permit_q, grant_q, config):
    counter = [0]
    while True:
        task = task_q.get()
        if task is None:
            break
        try:
            kind = task["kind"]
            if kind == "dom":
                rows, meta = _fetch_dom(task, config)
            elif kind == "fixture":
                if not config.get("allow_fixture"):
                    raise SourceError("fixture 任务未获授权", signal="SCHEMA")
                fixture = task.get("fixture") or {}
                if fixture.get("permit"):
                    reply = _permit(permit_q, grant_q, worker_id, counter, task["source"])
                    if not reply.get("granted"):
                        raise SourceError("fixture permit 未授予", signal="RATE_LIMITED")
                if fixture.get("die"):
                    os._exit(3)     # 模拟 worker 异常退出（真实进程死亡）
                if fixture.get("crash"):
                    raise RuntimeError("fixture crash (worker abnormal exit simulation)")
                if fixture.get("signal"):
                    raise SourceError("fixture signal", signal=fixture["signal"])
                rows, meta = list(fixture.get("rows", [])), {"kind": "fixture"}
            elif kind == "bilibili":
                rows, meta = _fetch_bilibili(task, config, worker_id, permit_q, grant_q, counter)
            else:
                rows, meta = _fetch_opencli(task, config, worker_id, permit_q, grant_q, counter)
            result_q.put({"task_id": task["id"], "worker": worker_id, "pid": os.getpid(),
                          "ok": True, "rows": rows, "meta": meta})
        except SourceError as exc:
            result_q.put({"task_id": task["id"], "worker": worker_id, "pid": os.getpid(),
                          "ok": False, "error": {"type": "SourceError", "message": str(exc),
                                                 "signal": exc.signal, "status": exc.status,
                                                 "body": str(exc.body)[:500] if exc.body else None}})
        except Exception as exc:  # noqa: BLE001
            result_q.put({"task_id": task["id"], "worker": worker_id, "pid": os.getpid(),
                          "ok": False, "error": {"type": type(exc).__name__, "message": str(exc)[:500]}})


# --- 父进程调度 --------------------------------------------------------------------------

def build_tasks(platforms, limit, dom_evidence_paths, keywords):
    tasks = []
    task_id = 0
    for platform in platforms:
        spec = PLATFORMS[platform]
        if spec["kind"] == "bilibili":
            for chunk in plan_bilibili_calls(limit, keywords):
                tasks.append({"id": task_id, "kind": "bilibili", "platform": platform,
                              "source": "bilibili", "child_limit": chunk["child_limit"],
                              "child_max_requests": chunk["child_max_requests"],
                              "pages": chunk["pages"], "page_start": chunk["page_start"],
                              "per_category_limit": chunk["per_category_limit"],
                              "keywords": chunk["keywords"], "browser": False})
                task_id += 1
        else:
            tasks.append({"id": task_id, "kind": "opencli", "platform": platform,
                          "source": "opencli:" + spec["endpoint"], "endpoint": spec["endpoint"],
                          "child_limit": min(OPENCLI_SUBLIMIT, max(1, limit)),
                          "keyword": None, "browser": True})
            task_id += 1
    for path in dom_evidence_paths:
        evidence = load_dom_evidence(path)
        tasks.append({"id": task_id, "kind": "dom", "platform": evidence["platform"],
                      "source": "dom:" + str(path), "browser": False,
                      "items": evidence["items"], "observed_at": evidence.get("observed_at"),
                      "source_timezone": evidence.get("source_timezone"),
                      "child_limit": max(1, limit), "child_max_requests": 0, "keywords": ""})
        task_id += 1
    return tasks


def run_scheduler(tasks, workers, gate, config, report):
    """父进程调度：唯一 permit 仲裁 + 唯一结果收集；worker 只取源。

    - 浏览器任务同时只允许一个在跑，完成后复位标志；
    - 失败即处理：命中风控信号 → 立刻停来源 + arm_cooldown + 持久化（不等全部结束）；
    - worker 死亡/任务超时 → 回收该 worker 并重启，任务如实失败，不无限 busy；
    - permit 每次授予**即时**持久化账本；--dry-run 同样节流（不跳过 sleep）。
    """
    ctx = mp.get_context("spawn")
    task_qs = [ctx.Queue() for _ in range(workers)]
    grant_qs = [ctx.Queue() for _ in range(workers)]
    permit_q = ctx.Queue()
    result_q = ctx.Queue()
    ledger_path = config.get("ledger_path")
    task_timeout = float(config.get("task_timeout", 300.0))

    def spawn_worker(wid):
        proc = ctx.Process(target=worker_loop,
                           args=(wid, task_qs[wid], result_q, permit_q, grant_qs[wid], config))
        proc.start()
        return proc

    procs = [spawn_worker(i) for i in range(workers)]
    free = list(range(workers))
    busy = {}
    started = {}
    browser_busy = [False]
    stop_all = [False]
    pending = deque(tasks)
    outputs = {}
    stopped_sources = set()
    worker_pids = set()
    parent_pid = os.getpid()

    def persist():
        if ledger_path:
            save_ledger(Path(ledger_path), gate.ledger)

    def on_failure(task, error):
        signal = error.get("signal")
        if signal in GLOBAL_STOP or error.get("status") == 429:
            # 429/限流：立即停本轮**全部**来源请求，并持久全局冷却 >=300s。
            stop_all[0] = True
            gate.arm_global(now=gate.clock(), signal=signal or "HTTP_429")
            persist()
            return
        if signal in STOP_SIGNALS:
            stopped_sources.add(task["source"])
            gate.arm_cooldown(task["source"], now=gate.clock(), signal=signal)
            persist()
            if signal in BROWSER_STOP:
                for other in tasks:
                    if other["browser"]:
                        stopped_sources.add(other["source"])

    def drain_results():
        while True:
            try:
                res = result_q.get_nowait()
            except queue.Empty:
                return
            wid = res["worker"]
            task = busy.pop(wid, None)
            started.pop(wid, None)
            if wid not in free:
                free.append(wid)
            if res.get("pid"):
                worker_pids.add(res["pid"])
            if task is not None:
                if task["browser"]:
                    browser_busy[0] = False
                if not res.get("ok"):
                    on_failure(task, res.get("error") or {})
                outputs[task["id"]] = res

    def serve_permits():
        while True:
            try:
                req = permit_q.get_nowait()
            except queue.Empty:
                return
            source = req["source"]
            if stop_all[0] or source in stopped_sources:
                grant_qs[req["worker"]].put({"token": req["token"], "granted": False,
                                             "reason": "stopped"})
                continue
            allowed, reason, wait = gate.check(source)
            if not allowed:
                grant_qs[req["worker"]].put({"token": req["token"], "granted": False,
                                             "reason": reason})
                continue
            # 间隔等待分段进行：期间持续收结果/风控，429 一到就立即全局停。
            remaining_wait = float(wait or 0.0)
            while remaining_wait > 0 and not stop_all[0]:
                slice_s = min(0.2, remaining_wait)
                time.sleep(slice_s)
                remaining_wait -= slice_s
                drain_results()
            drain_results()
            if stop_all[0] or source in stopped_sources:      # 授予前再看一次全局停
                grant_qs[req["worker"]].put({"token": req["token"], "granted": False,
                                             "reason": "stopped"})
                continue
            allowed, reason, _ = gate.check(source)
            if not allowed:
                grant_qs[req["worker"]].put({"token": req["token"], "granted": False,
                                             "reason": reason})
                continue
            gate.record(source, "ok")
            persist()                                    # 每次 permit 即时持久化
            grant_qs[req["worker"]].put({"token": req["token"], "granted": True})

    def reclaim(now_mono):
        for wid in list(busy):
            dead = not procs[wid].is_alive()
            slow = (now_mono - started.get(wid, now_mono)) > task_timeout
            if not (dead or slow):
                continue
            task = busy.pop(wid)
            started.pop(wid, None)
            if task["browser"]:
                browser_busy[0] = False
            outputs[task["id"]] = {"task_id": task["id"], "ok": False,
                                   "error": {"type": "Worker", "signal": "WORKER",
                                             "message": "worker died" if dead else "worker timeout"}}
            if not dead:
                procs[wid].terminate()
                procs[wid].join(timeout=5)
            procs[wid] = spawn_worker(wid)
            if wid not in free:
                free.append(wid)

    while pending or busy:
        drain_results()
        serve_permits()
        reclaim(time.monotonic())
        while pending and free:
            task = next((c for c in pending if not (c["browser"] and browser_busy[0])), None)
            if task is None:
                break
            pending.remove(task)
            if stop_all[0] or task["source"] in stopped_sources:
                outputs[task["id"]] = {"task_id": task["id"], "ok": False,
                                       "error": {"type": "Skipped", "message": "source stopped"}}
                continue
            wid = free.pop(0)
            busy[wid] = task
            started[wid] = time.monotonic()
            if task["browser"]:
                browser_busy[0] = True
            task_qs[wid].put(task)
        if not pending and not busy:
            break
        time.sleep(0.02)

    for q in task_qs:
        q.put(None)
    for proc in procs:
        proc.join(timeout=30)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)

    report["scheduler"] = {"parent_pid": parent_pid, "worker_pids": sorted(worker_pids),
                           "workers": workers, "stopped_sources": sorted(stopped_sources),
                           "stop_all": stop_all[0],
                           "distinct_worker": len(worker_pids) > 0
                           and all(p != parent_pid for p in worker_pids)}
    return outputs


# --- 父进程门控 + 写库 -------------------------------------------------------------------

def gate_rows(task, rows, window, now, require="substantive"):
    decisions = []
    for row in rows:
        # 原始发布值优先（如 B 站 pubdate 原始秒），保留原始字段与精度。
        raw_value = row.get("published_raw")
        value = raw_value if raw_value not in (None, "") else row.get("published_at")
        published = collection_policy.normalize_published(
            value, observed_at=row.get("observed_at") or task.get("observed_at"),
            source_timezone=row.get("source_timezone") or task.get("source_timezone"),
            source_field=row.get("source_field"), precision=row.get("precision"),
            derived_from=row.get("derived_from"))
        decision = collection_policy.gate_candidate(
            title=row.get("title") or "", url=row.get("url") or "",
            summary=row.get("summary") or "", source_id=row.get("source_id"),
            published=published, require=require)
        if decision["accepted"]:
            ok, reason = apply_window(decision["time"]["published_at"], window, now)
            if not ok:
                decision = {**decision, "accepted": False, "reason": reason}
        decision["platform"] = task["platform"]
        decision["row"] = row
        decision["evidence"] = collection_policy.snapshot_evidence(decision)
        decisions.append(decision)
    return decisions


def load_existing_readonly(path):
    """入库前只读去重预检：失败 fail-closed（在发起任何网络请求之前）。

    列名以真实 storage schema 为准（records: canonical_url / content_sha256 / simhash）；
    simhash 等为 connect() 迁移补列，老库可能缺列 → 按 PRAGMA 实际列集合容错，不猜列。
    """
    if not Path(path).is_file():
        # 缺库必须 fail-closed：正式采集不得在缺库时联网/由 storage.connect 顺手建库。
        raise PolicyError(f"证据库不存在，正式采集拒绝在缺库时联网：{path}（isolated 测试请先显式 storage.connect 建临时库）")
    import sqlite3
    uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(records)")}
        if "canonical_url" not in columns or "content_sha256" not in columns:
            raise PolicyError("records 表缺少 canonical_url/content_sha256 列，无法只读去重")
        wanted = [c for c in ("canonical_url", "content_sha256", "simhash") if c in columns]
        urls, hashes, sims = set(), set(), []
        for row in conn.execute(f"SELECT {','.join(wanted)} FROM records"):
            record = dict(zip(wanted, row))
            if record.get("canonical_url"):
                urls.add(record["canonical_url"])
            if record.get("content_sha256"):
                hashes.add(record["content_sha256"])
            if record.get("simhash"):
                try:
                    sims.append(int(record["simhash"], 16))
                except (TypeError, ValueError):
                    pass
        return {"urls": urls, "hashes": hashes, "sims": sims}
    finally:
        conn.close()


def dedup(decisions, existing):
    """对已存库 URL/hash/SimHash 与本次运行内重复去重；旧 URL 换 feed 不算新增。"""
    import storage
    kept = []
    for d in decisions:
        if not d["accepted"]:
            kept.append(d)
            continue
        url = d["url"]
        row = d["row"]
        title = storage.normalized_text(row.get("title") or "")
        chash = storage.digest(storage.json_bytes([title, storage.normalized_text(row.get("summary") or "")]))
        sh = storage.simhash64(title + " " + storage.normalized_text(row.get("summary") or ""))
        if url in existing["urls"] or chash in existing["hashes"] or \
                any(storage.hamming64(sh, other) <= 4 for other in existing["sims"]):
            kept.append({**d, "accepted": False, "reason": "duplicate"})
            continue
        existing["urls"].add(url)
        existing["hashes"].add(chash)
        existing["sims"].append(sh)
        kept.append(d)
    return kept


def select_within_limit(decisions, existing, limit):
    """按父总目标选取 accepted（<= limit），其余保留为拒绝/暂存证据。"""
    selected, accepted_count = [], 0
    for d in decisions:
        if d["accepted"] and accepted_count < limit:
            selected.append(d)
            accepted_count += 1
        elif d["accepted"]:
            selected.append({**d, "accepted": False, "reason": "over_target_hold"})
        else:
            selected.append(d)
    return selected


def _feed_url(platform, url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def _to_row(decision):
    row = decision["row"]
    evidence = decision.get("evidence") or {}
    time_info = decision.get("time") or {}
    snapshot = {
        "source": decision.get("source"), "platform": decision["platform"],
        "gate": evidence, "views_known": row.get("views") is not None,
        "raw_time": row.get("published_raw") if row.get("published_raw") not in (None, "")
        else row.get("published_at"),
        "source_field": row.get("source_field"),
        "observed_at": row.get("observed_at") or time_info.get("evidence", {}).get("observed_at"),
        "source_timezone": row.get("source_timezone"),
        "precision": time_info.get("precision"),
        "time_quality": time_info.get("quality"),
        "time_evidence": row.get("time_evidence"),
        "source_raw_xml": row.get("raw_xml"),
        "row": {k: row.get(k) for k in ("title", "url", "source_id", "published_at")},
    }
    out = {"title": row.get("title") or "", "summary": row.get("summary") or "",
           "url": row.get("url") or "", "source_id": decision.get("source_id"),
           "published_at": time_info.get("published_at"),
           "raw_xml": json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}
    # views 未知不伪造 0：仅在确知为整数时写入（storage 对缺失走其既有默认）。
    views = row.get("views")
    if type(views) is int:
        out["views"] = views
    elif isinstance(views, str) and views.isdigit():
        out["views"] = int(views)
    return out


# --- CLI -------------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, required=True, help="必传，1..400，父进程总上限")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD（上海自然日；对全部来源生效）")
    parser.add_argument("--platform", action="append", choices=sorted(PLATFORMS), default=None)
    parser.add_argument("--dom-evidence", action="append", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=2, help=f"worker 进程数 1..{MAX_WORKERS}")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--interval", type=float, default=float(MIN_INTERVAL))
    parser.add_argument("--keywords", default="女权,彩礼,性别对立,厌女")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--snapshot-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def validate_args(args):
    if not 1 <= args.limit <= MAX_LIMIT:
        raise PolicyError(f"--limit 必须在 1..{MAX_LIMIT}，收到 {args.limit}")
    if not 1 <= args.workers <= MAX_WORKERS:
        raise PolicyError(f"--workers 必须在 1..{MAX_WORKERS}，收到 {args.workers}")
    if not 1 <= args.max_calls <= MAX_CALLS_CAP:
        raise PolicyError(f"--max-calls 必须在 1..{MAX_CALLS_CAP}，收到 {args.max_calls}")
    if args.interval < MIN_INTERVAL:
        raise PolicyError(f"--interval 必须 >= {MIN_INTERVAL}，收到 {args.interval}")


def emit(report, output, code):
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print(text)
    return code


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    explicit_platforms = list(dict.fromkeys(args.platform or []))
    if explicit_platforms:
        platforms = explicit_platforms
    elif args.dom_evidence:
        platforms = []          # 只处理已采 DOM，不强行访问默认 B 站/微博
    else:
        platforms = ["bilibili", "weibo"]
    now = datetime.now(timezone.utc)
    report = {"collector": "multiplatform", "status": "running", "dry_run": args.dry_run,
              "limit": args.limit, "date": args.date, "platforms": platforms,
              "workers": args.workers, "max_calls": args.max_calls, "interval": args.interval,
              "ledger": str(args.ledger), "sources": [], "aggregate": None, "target": None,
              "db": None, "notes": [
                  "worker 只取源/解析；唯一父进程写库（storage.import_batch，批 <=100）。",
                  "B 站每个真实 HTTP 前经父进程同一许可；原生 OpenCLI 内部 HTTP 标 unknown。",
                  "全部来源严格比较上海 date 的 [start,end)；未来/不明时间拒绝。",
                  "429/登录墙/断桥 → 停来源 + 持久 >=300s 冷却，不重试。"]}
    try:
        validate_args(args)
        window = shanghai_window(args.date, now) if args.date else None
    except PolicyError as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        return emit(report, args.output, 2)

    # 入库前只读去重预检：在网络之前 fail-closed。
    try:
        existing = load_existing_readonly(args.db)
        report["db"] = {"path": str(args.db), "present": Path(args.db).is_file(),
                        "readonly_ok": True, "touched": False}
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"只读去重预检失败，已在网络前停止：{exc}"
        return emit(report, args.output, 3)

    try:
        ledger, ledger_state = load_ledger(Path(args.ledger))
    except PolicyError as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        return emit(report, args.output, 7)
    report["ledger_state"] = ledger_state
    gate = CallGate(ledger, max_calls=args.max_calls, interval=args.interval)

    try:
        tasks = build_tasks(platforms, args.limit, args.dom_evidence or [], args.keywords)
    except PolicyError as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        return emit(report, args.output, 2)

    config = {"date": window["date"] if window else None, "dry_run": args.dry_run,
              "now": now, "ledger_path": str(args.ledger)}
    outputs = run_scheduler(tasks, args.workers, gate, config, report)
    # dry-run 也持久化账本（含冷却），不再只在结束时写一次。
    save_ledger(Path(args.ledger), ledger)

    decisions = []
    for task in tasks:
        result = outputs.get(task["id"])
        entry = {"source": task["source"], "platform": task["platform"], "kind": task["kind"],
                 "status": None, "reason": None, "candidate_count": 0,
                 "native_http": "unknown" if task["kind"] == "opencli" else "counted"}
        report["sources"].append(entry)
        if result is None or not result.get("ok"):
            error = (result or {}).get("error") or {}
            entry["status"], entry["reason"] = "failed", "source_error"
            entry["error"] = error
            signal = error.get("signal")
            if signal in STOP_SIGNALS or (error.get("status") == 429):
                entry["status"], entry["reason"] = "cooldown", "source_stopped"
                gate.arm_cooldown(task["source"], now=now, signal=signal or "HTTP_429")
            continue
        rows = result.get("rows", [])
        entry["candidate_count"] = len(rows)
        entry["meta"] = result.get("meta")
        if result.get("pid"):
            entry["worker_pid"] = result["pid"]
        entry["status"], entry["reason"] = "ok", "collected"
        decisions.extend(gate_rows(task, rows, window, now))

    decisions = dedup(decisions, existing)
    decisions = select_within_limit(decisions, existing, args.limit)
    summary = aggregate(decisions)
    report["aggregate"] = summary

    if args.dry_run:
        report["target"] = evaluate_target(0, args.limit)
        report["status"] = "dry_run"
        return emit(report, args.output, 0)

    added = 0
    by_platform = {}
    for d in decisions:
        if d["accepted"]:
            by_platform.setdefault(d["platform"], []).append(d)
    if by_platform:
        import server
        import storage
        snap_root = args.snapshot_root or Path(args.db).parent
        db = storage.connect(args.db)
        try:
            report["db"] = {**(report.get("db") or {}), "touched": True,
                            "writer_pid": os.getpid()}
            for platform, items in by_platform.items():
                rows_payload = [_to_row(d) for d in items]
                feed_url = _feed_url(platform, items[0]["url"])
                existing_feed = db.execute("SELECT id FROM feeds WHERE url=?", (feed_url,)).fetchone()
                if existing_feed:
                    feed_id = existing_feed["id"]
                else:
                    with db:
                        feed_id = db.execute("INSERT INTO feeds(name,url,added_at) VALUES(?,?,?)",
                                             (platform + "-multiplatform", feed_url, storage.now())).lastrowid
                for batch in plan_batches(rows_payload, 100):
                    run_id = storage.start_run(db, feed_id, "multiplatform-" + platform,
                                               max(1, min(len(batch), 100)))
                    outcome = storage.import_batch(db, snap_root, run_id, batch, server.classify,
                                                   noise=False)
                    added += outcome["added"]
                    summary["duplicates"] += outcome.get("duplicates", 0) + outcome.get("near_duplicates", 0)
        finally:
            db.close()

    summary["added"] = added
    report["target"] = evaluate_target(added, args.limit)
    report["status"] = report["target"]["status"]
    return emit(report, args.output, 0 if report["status"] == "ok" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
