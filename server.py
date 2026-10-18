"""Local, manual-refresh RSS sentiment monitor; Python standard library only."""
from datetime import datetime, timezone, timedelta
import argparse
from contextlib import contextmanager
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import ipaddress
import re
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
import storage
import analysis
import alertcenter
import admin_features
import pipeline
import orchestrator

ROOT = Path(__file__).resolve().parent
DB = ROOT / "sentiment.db"
HOST, PORT = "127.0.0.1", 8790
LIMIT = 3
# Exact local static whitelist for the vendored chart library. Only these fixed files are served:
# the path is compared as an equality key, never concatenated with user input, so no traversal.
VENDOR_STATIC = {
    "/vendor/echarts/echarts.min.js": "text/javascript; charset=utf-8",
    "/vendor/echarts/LICENSE": "text/plain; charset=utf-8",
    "/vendor/echarts/NOTICE": "text/plain; charset=utf-8",
    "/vendor/echarts/provenance.json": "application/json; charset=utf-8",
    "/vendor/echarts/licenses/LICENSE-d3": "text/plain; charset=utf-8",
}
HISTORY_QUERY_KEYS = {"evidence_id", "from", "to", "limit", "offset"}
WRITE_LOCK = threading.Lock()
REFRESH_LOCK = threading.Lock()
ORCH_LOCK = threading.Lock()
RUNTIME = {}

# 采集调度：默认 20 分钟一轮，轮转 6 个已验证平台；冷却账与熔断仍由采集器本体负责。
COLLECT_LOCK = threading.Lock()
COLLECT_STATE = {"running": False, "started_at": None, "trigger": None}
COLLECT_INTERVAL_MIN = 20
COLLECT_LIMIT = 20
COLLECT_TIMEOUT = 900
COLLECT_ENDPOINTS = ["hot", "weibo-hot", "zhihu-hot", "tieba-hot", "36kr-hot", "xiaohongshu-feed"]
OPENCLI_MAIN = ""
POSITIVE = {"增长", "上涨", "利好", "成功", "创新", "支持", "改善", "满意", "合作", "突破", "good", "growth", "success", "positive", "support", "improve", "win"}
NEGATIVE = {"下跌", "亏损", "投诉", "事故", "风险", "处罚", "下滑", "失败", "争议", "危机", "bad", "loss", "complaint", "risk", "fine", "decline", "failure", "crisis"}


@contextmanager
def connect():
    db = storage.connect(DB)
    try:
        analysis.init(db)
        alertcenter.init(db)          # 与 analysis.init 同处：每次连接/启动都保证预警表存在（IF NOT EXISTS）
        admin_features.init(db)       # settings / reports / events 表
        pipeline.init(db)             # pipeline_tasks 表
        orchestrator.init(db)         # 分析流水线 run/state 表
        with db:
            yield db
    finally:
        db.close()


def clean(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()


def tag(node, name):
    for child in node:
        if child.tag.rsplit("}", 1)[-1].lower() == name.lower():
            return clean(child.text or "")
    return ""


def classify(text):
    words = re.findall(r"[a-z]+", text.lower())
    positive = sum(text.count(w) for w in POSITIVE if not w.isascii()) + sum(w in POSITIVE for w in words)
    negative = sum(text.count(w) for w in NEGATIVE if not w.isascii()) + sum(w in NEGATIVE for w in words)
    if positive > negative:
        return "偏正面"
    if negative > positive:
        return "偏负面"
    return "中性/待判"


def safe_url(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise ValueError("RSS 地址必须是 http(s) 公网 URL，不能包含账号密码")
    host = parts.hostname.lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise ValueError("不能使用本机地址作为 RSS 源")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("不能使用私有或回环地址作为 RSS 源")
    return value


def assert_public_host(url):
    host = urllib.parse.urlsplit(url).hostname
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
        except OSError as exc:
            raise ValueError("RSS 域名无法解析") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("RSS 域名解析到非公网地址，已拒绝")


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_host(safe_url(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_feed(feed, limit):
    url = safe_url(feed["url"])
    assert_public_host(url)
    request = urllib.request.Request(url, headers={"User-Agent": "LocalSentimentMonitor/0.1", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    opener = urllib.request.build_opener(PublicRedirect())
    with opener.open(request, timeout=12) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError("RSS 响应超过 2 MB")
    root = ET.fromstring(body)
    if root.tag.rsplit("}", 1)[-1].lower() not in ("rss", "feed", "rdf"):
        raise ValueError("响应不是 RSS/Atom 文档")
    rows = []
    for node in root.iter():
        kind = node.tag.rsplit("}", 1)[-1].lower()
        if kind not in ("item", "entry"):
            continue
        title = tag(node, "title")
        link = ""
        if kind == "entry":
            for child in node:
                if child.tag.rsplit("}", 1)[-1].lower() == "link":
                    link = child.attrib.get("href", child.text or "")
                    if child.attrib.get("rel", "alternate") == "alternate":
                        break
        else:
            link = tag(node, "link")
        summary = tag(node, "summary") or tag(node, "description") or tag(node, "content")
        published = tag(node, "published") or tag(node, "pubdate")
        if title and link:
            rows.append({"title": title, "summary": summary, "url": link,
                         "source_id": tag(node, "guid") or tag(node, "id") or None,
                         "published_at": published or None,
                         "raw_xml": ET.tostring(node, encoding="unicode")})
        if len(rows) >= limit:
            break
    return rows


# ---------- 采集调度（P1） ----------

def port_in_use(host, port):
    """启动前探测端口：已被占用就不启动，避免两个实例同时服务。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.6)
        return probe.connect_ex((host, port)) == 0


def default_opencli_main():
    """OpenCLI 目前装在独立目录（不进 profile，避免 pnpm install 剪掉）；多路径兜底。"""
    candidates = []
    env = os.environ.get("OPENCLI_MAIN")
    if env:
        candidates.append(Path(env))
    candidates.extend([
        Path("D:/DSH/tools/opencli-1.8.8/node_modules/@jackwener/opencli/dist/src/main.js"),
        Path.home() / "node_modules/@jackwener/opencli/dist/src/main.js",
        Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@jackwener/opencli/dist/src/main.js",
    ])
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    return ""


def collect_cooldown_seconds():
    """平台冷却取调度间隔的 60%，给串行采完留余量；关闭调度时退化为 60 秒。"""
    if COLLECT_INTERVAL_MIN <= 0:
        return 60
    return max(60, int(COLLECT_INTERVAL_MIN * 60 * 0.6))


def today_start_iso():
    """Asia/Shanghai 当天 00:00 对应的 UTC ISO 时间。"""
    local = datetime.now(timezone(timedelta(hours=8)))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


def insert_collect_log(trigger, endpoints, status, started_at, finished_at,
                       added, duplicates, returned, error, report):
    """采集日志单独开连接写，失败不影响主流程。"""
    try:
        db = storage.connect(DB)
        try:
            with db:
                db.execute(
                    "INSERT INTO collector_logs(trigger,endpoints,status,started_at,finished_at,"
                    "added,duplicates,returned,error,report) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (trigger, ",".join(endpoints), status, started_at, finished_at,
                     int(added), int(duplicates), int(returned), error,
                     json.dumps(report, ensure_ascii=False)[:200000]))
                # 只保留最近 200 条日志（20 分钟一轮约 72 行/天）
                db.execute("DELETE FROM collector_logs WHERE id NOT IN "
                           "(SELECT id FROM collector_logs ORDER BY id DESC LIMIT 200)")
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        pass


def collect_status():
    with connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id,trigger,endpoints,status,started_at,finished_at,added,duplicates,returned,error,report "
            "FROM collector_logs ORDER BY id DESC LIMIT 10")]
    last_results = []
    if rows:
        raw = rows[0].pop("report", None)
        if raw:
            try:
                report = json.loads(raw)
                last_results = [
                    {"endpoint": item.get("endpoint"), "status": item.get("status"),
                     "reason": item.get("reason"), "returned": item.get("returned_count")}
                    for item in report.get("results") or []
                ]
            except (TypeError, ValueError):
                last_results = []
    return {"running": COLLECT_STATE["running"], "started_at": COLLECT_STATE["started_at"],
            "trigger": COLLECT_STATE["trigger"], "interval_minutes": COLLECT_INTERVAL_MIN,
            "cooldown_seconds": collect_cooldown_seconds(),
            "endpoints": COLLECT_ENDPOINTS, "logs": rows, "last_results": last_results}


def run_collection(trigger="manual", lock_held=False):
    """跑一轮采集；同一时间只允许一个实例。lock_held=True 表示调用方已持有锁。"""
    if not lock_held and not COLLECT_LOCK.acquire(blocking=False):
        return {"status": "busy", "message": "已有采集在运行"}
    started_at = storage.now()
    COLLECT_STATE.update(running=True, started_at=started_at, trigger=trigger)
    status, added, duplicates, returned, error, report = "failed", 0, 0, 0, None, {}
    finished_at = started_at
    task = None
    try:
        try:
            task = pipeline.start(DB, "crawler", {"trigger": trigger, "endpoints": COLLECT_ENDPOINTS})
        except Exception:
            task = None
        main_js = OPENCLI_MAIN or default_opencli_main()
        if not main_js or not Path(main_js).is_file():
            raise RuntimeError("未找到 OpenCLI 的 dist/src/main.js；用 --opencli 指定")
        cmd = [sys.executable, "-B", str(ROOT / "tools" / "collect_opencli.py")]
        for endpoint in COLLECT_ENDPOINTS:
            cmd += ["--endpoint", endpoint]
        cmd += ["--limit", str(COLLECT_LIMIT),
                # 一轮必须容得下所有端点，否则后几个端点会永远被 max_per_run 截断
                "--max-per-run", str(COLLECT_LIMIT * len(COLLECT_ENDPOINTS)),
                "--gap-seconds", "25",
                # 冷却必须短于调度间隔：一轮串行采完本身要 2 分钟左右，若冷却=间隔，
                # 最后采的平台到下一轮还没解冻，会被整轮 throttle（实测 0 条）。
                "--min-interval", str(collect_cooldown_seconds()),
                "--opencli", str(main_js)]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=COLLECT_TIMEOUT)
        raw = (proc.stdout or "").strip().lstrip("\ufeff")
        if "{" not in raw:
            raise RuntimeError(f"采集器未返回 JSON（退出码 {proc.returncode}）：{(proc.stderr or '')[:300]}")
        report = json.loads(raw[raw.index("{"):])
        reported = report.get("status")
        status = reported if reported in ("ok", "throttled", "skipped") else "failed"
        for item in report.get("results") or []:
            run = item.get("run") or {}
            added += int(run.get("added") or 0)
            duplicates += int(run.get("duplicates") or 0)
            returned += int(item.get("returned_count") or 0)
        if status == "failed":
            messages = [(i.get("error") or {}).get("message") for i in report.get("results") or []
                        if i.get("status") == "failed"]
            error = "; ".join(m for m in messages if m)[:1000] or f"采集器退出码 {proc.returncode}"
    except subprocess.TimeoutExpired:
        status, error = "timeout", f"采集超时（>{COLLECT_TIMEOUT}s），进程已终止"
    except Exception as exc:  # 环境/解析/磁盘
        status, error = "failed", f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        finished_at = storage.now()
        if task:
            try:
                if status in ("ok", "throttled", "skipped"):
                    pipeline.finish(DB, task["id"], {"added": added, "duplicates": duplicates})
                else:
                    pipeline.fail(DB, task["id"], error or status)
            except Exception:
                pass
        try:
            insert_collect_log(trigger, COLLECT_ENDPOINTS, status, started_at, finished_at,
                               added, duplicates, returned, error, report)
        except Exception:
            pass  # 日志写入失败也必须释放锁，否则后续采集全部被拒
        COLLECT_STATE.update(running=False, started_at=None, trigger=None)
        COLLECT_LOCK.release()
    return {"status": status, "trigger": trigger, "started_at": started_at,
            "finished_at": finished_at, "added": added, "duplicates": duplicates,
            "returned": returned, "error": error}


def seconds_since_last_collect():
    """距最近一轮采集结束的秒数；没有历史或解析失败返回 None。"""
    try:
        with connect() as db:
            row = db.execute("SELECT finished_at FROM collector_logs ORDER BY id DESC LIMIT 1").fetchone()
        if not row or not row[0]:
            return None
        last = datetime.fromisoformat(row[0])
        return (datetime.now(timezone.utc) - last).total_seconds()
    except (ValueError, TypeError, sqlite3.Error, OSError):
        return None


def start_collection_scheduler():
    """常驻线程：每轮读取当前 interval。0 = 空转等待，改回 >0 后会自动恢复采集。"""
    def loop():
        time.sleep(5)
        while True:
            interval = COLLECT_INTERVAL_MIN
            if interval <= 0:
                time.sleep(30)
                continue
            elapsed = seconds_since_last_collect()
            target = collect_cooldown_seconds() + 60
            if elapsed is not None and elapsed < target:
                time.sleep(target - elapsed)
                continue
            try:
                run_collection("scheduler")
            except Exception:
                pass
            time.sleep(interval * 60)

    thread = threading.Thread(target=loop, daemon=True, name="collector-scheduler")
    thread.start()
    return thread


# ---------- 全库检索（P1） ----------

def search_records(db, query, sentiment, feed_id, date_from, date_to, limit, offset):
    where, params = [], []
    join = ""
    fts_ready = False
    if query and len(query) >= 3:
        row = db.execute("SELECT value FROM schema_meta WHERE key='fts_version'").fetchone()
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='records_fts'").fetchone()
        fts_ready = bool(row) and row[0] == storage.FTS_VERSION and table is not None
    if fts_ready:
        # trigram 索引：短语匹配，双引号转义后交给 FTS5
        where.append("records_fts MATCH ?")
        params.append('"' + query.replace('"', '""') + '"')
        join = " JOIN records_fts ON records_fts.rowid = r.rowid"
    elif query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        where.append("(r.title LIKE ? ESCAPE '\\' OR r.summary LIKE ? ESCAPE '\\' OR r.url LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    if sentiment:
        where.append("r.sentiment = ?")
        params.append(sentiment)
    if feed_id:
        where.append("r.feed_id = ?")
        params.append(feed_id)
    if date_from:
        where.append("date(r.fetched_at,'+8 hours') >= ?")
        params.append(date_from)
    if date_to:
        where.append("date(r.fetched_at,'+8 hours') <= ?")
        params.append(date_to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.execute("SELECT count(*) FROM records r" + join + clause, params).fetchone()[0]
    rows = db.execute(
        "SELECT r.id,r.feed_id,f.name AS source,r.title,r.summary,r.url,r.fetched_at,"
        "r.sentiment,r.canonical_id FROM records r JOIN feeds f ON f.id=r.feed_id" + join
        + clause + " ORDER BY r.fetched_at DESC, r.id LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "fts": bool(fts_ready),
            "items": [dict(x) for x in rows]}


def _stage_task(trace_id, stage, fn):
    """给没有自带记账的阶段（sentiment / alert）补一条 pipeline_tasks；task_id 固定，重试复用同一行。"""
    task = pipeline.start(DB, stage, {}, trace_id=trace_id, task_id=f"task-{trace_id}-{stage}")
    try:
        result = fn()
    except Exception as exc:
        pipeline.fail(DB, task["id"], exc)
        raise
    pipeline.finish(DB, task["id"], result if isinstance(result, dict) else {"result": result})
    return result


def _stage_sentiment():
    with connect() as db:
        return analysis.run_rule_sentiment(db, 500)


def _stage_alert():
    with connect() as db:
        return alertcenter.evaluate(db)


def _stage_report(trace_id):
    with connect() as db:
        return admin_features.generate_report(db, "daily", trace_id=trace_id,
                                              task_id=f"task-{trace_id}-report")


def pipeline_handlers(trace_id):
    return {
        "sentiment": lambda: _stage_task(trace_id, "sentiment", _stage_sentiment),
        "topic": lambda: admin_features.rebuild_events(200, trace_id=trace_id,
                                                       task_id=f"task-{trace_id}-topic"),
        "alert": lambda: _stage_task(trace_id, "alert", _stage_alert),
        "report": lambda: _stage_report(trace_id),
    }


def run_analysis_pipeline(trace_id, stages):
    try:
        orchestrator.run(DB, trace_id, pipeline_handlers(trace_id), stages)
    except Exception as exc:
        try:
            orchestrator.fail_run(DB, trace_id, exc)
        except Exception:
            pass
    finally:
        ORCH_LOCK.release()


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    """禁止端口复用：同一端口只能有一个实例，避免新旧进程同时服务。"""
    allow_reuse_address = False
    daemon_threads = True



class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, value, kind="application/json; charset=utf-8"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 1_000_000:
            raise ValueError("请求内容为空或超过限制")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("请求格式错误")
        return value

    def do_GET(self):
        try:
            return self._get()
        except ValueError as exc:
            return self.send(400, {"error": str(exc)})
        except (OSError, sqlite3.Error) as exc:
            return self.send(500, {"error": str(exc)})

    def _get(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/runtime":
            return self.send(200, RUNTIME)
        if path == "/api/settings":
            with connect() as db:
                return self.send(200, admin_features.get_settings(db))
        if path == "/api/reports":
            with connect() as db:
                return self.send(200, {"reports": admin_features.list_reports(db)})
        if path.startswith("/api/reports/") and path.endswith("/preview"):
            report_id = path[len("/api/reports/"):-len("/preview")]
            with connect() as db:
                row = db.execute("SELECT path,title FROM reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                return self.send(404, {"error": "报告不存在"})
            path = Path(row["path"])
            if not path.is_file():
                return self.send(404, {"error": "报告文件缺失（数据库有记录但文件不存在）"})
            body = path.read_text(encoding="utf-8")
            from html import escape
            page = ("<!doctype html><meta charset='utf-8'><title>" + escape(row["title"]) +
                    "</title><style>body{max-width:900px;margin:32px auto;font:14px/1.7 system-ui;"
                    "background:#f7f9fc;color:#1f2d3d}pre{white-space:pre-wrap;background:#fff;padding:24px;"
                    "border:1px solid #e7edf4;border-radius:10px}</style><pre>" + escape(body) + "</pre>")
            return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path.startswith("/api/reports/") and path.endswith("/download"):
            report_id = path[len("/api/reports/"):-len("/download")]
            with connect() as db:
                row = db.execute("SELECT path,title FROM reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                return self.send(404, {"error": "报告不存在"})
            path = Path(row["path"])
            if not path.is_file():
                return self.send(404, {"error": "报告文件缺失（数据库有记录但文件不存在）"})
            return self.send(200, path.read_bytes(), "text/markdown; charset=utf-8")
        if path == "/api/events":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            raw = (query.get("limit", ["50"])[0] or "").strip()
            if not raw.isdigit() or not 1 <= int(raw) <= 200:
                raise ValueError("limit 必须是 1..200 的整数")
            with connect() as db:
                return self.send(200, {"active_topics": admin_features.active_topics(db),
                                       "events": admin_features.list_events(db, int(raw))})
        if path == "/api/topic-analysis":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            raw = (query.get("window", ["7"])[0] or "").strip()
            if raw not in ("7", "14", "30"):
                raise ValueError("window 只接受 7 / 14 / 30")
            with connect() as db:
                return self.send(200, admin_features.topic_analysis(db, int(raw)))
        if path == "/api/pipeline/runs":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            raw = (query.get("limit", ["10"])[0] or "").strip()
            if not raw.isdigit() or not 1 <= int(raw) <= 50:
                raise ValueError("limit 必须是 1..50 的整数")
            with connect() as db:
                return self.send(200, {"runs": orchestrator.list_runs(db, int(raw))})
        if path == "/api/pipeline/state":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            trace = (query.get("trace", [""])[0] or "").strip()
            if not trace:
                raise ValueError("trace 不能为空")
            with connect() as db:
                data = orchestrator.state(db, trace)
            return self.send(200, data) if data else self.send(404, {"error": "trace 不存在"})
        if path == "/api/pipeline/tasks":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            raw = (query.get("limit", ["50"])[0] or "").strip()
            if not raw.isdigit() or not 1 <= int(raw) <= 200:
                raise ValueError("limit 必须是 1..200 的整数")
            with connect() as db:
                return self.send(200, {"tasks": pipeline.list_tasks(db, int(raw))})
        if path == "/api/collect/status":
            return self.send(200, collect_status())
        if path == "/api/search":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            text = (query.get("q", [""])[0] or "").strip()[:200]
            sentiment = (query.get("sentiment", [""])[0] or "").strip()[:40]
            feed_raw = (query.get("feed_id", [""])[0] or "").strip()
            if feed_raw and not feed_raw.isdigit():
                raise ValueError("feed_id 必须是正整数")
            feed_id = int(feed_raw) if feed_raw else None
            date_from = (query.get("from", [""])[0] or "").strip()
            date_to = (query.get("to", [""])[0] or "").strip()
            for value in (date_from, date_to):
                if value:
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        raise ValueError("from / to 必须是有效的 YYYY-MM-DD") from None
            limit_raw = (query.get("limit", ["20"])[0] or "").strip()
            offset_raw = (query.get("offset", ["0"])[0] or "").strip()
            if not limit_raw.isdigit() or not 1 <= int(limit_raw) <= 100:
                raise ValueError("limit 必须是 1..100 的整数")
            if not offset_raw.isdigit():
                raise ValueError("offset 必须是非负整数")
            with connect() as db:
                return self.send(200, search_records(db, text, sentiment, feed_id,
                                                     date_from or None, date_to or None,
                                                     int(limit_raw), int(offset_raw)))
        if path == "/api/topics":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            raw = query.get("top_k", ["10"])
            if len(raw) != 1 or not raw[0].isdigit() or not 1 <= int(raw[0]) <= 50:
                raise ValueError("top_k 必须是 1..50 的整数")
            with connect() as db:
                return self.send(200, analysis.topics_overview(db, int(raw[0])))
        if path == "/api/analysis":
            with connect() as db:
                return self.send(200, analysis.overview(db))
        if path == "/api/alert-rules":
            with connect() as db:
                return self.send(200, alertcenter.rules(db))
        if path == "/api/alerts":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            raw = query.get("limit", ["100"])
            if len(raw) != 1 or not raw[0].isdigit() or not 1 <= int(raw[0]) <= 500:
                raise ValueError("limit 必须是 1..500 的整数")
            with connect() as db:
                return self.send(200, alertcenter.alerts(db, int(raw[0])))
        if path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/app.js":
            return self.send(200, (ROOT / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/overview-charts.js":
            return self.send(200, (ROOT / "overview-charts.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/topics-panel.js":
            return self.send(200, (ROOT / "topics-panel.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/alert-panel.js":
            return self.send(200, (ROOT / "alert-panel.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/corpus-search.js":
            return self.send(200, (ROOT / "corpus-search.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/reports.js":
            return self.send(200, (ROOT / "reports.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/settings.js":
            return self.send(200, (ROOT / "settings.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/events.js":
            return self.send(200, (ROOT / "events.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/topic-analysis.js":
            return self.send(200, (ROOT / "topic-analysis.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/pipeline-panel.js":
            return self.send(200, (ROOT / "pipeline-panel.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/style.css":
            return self.send(200, (ROOT / "style.css").read_bytes(), "text/css; charset=utf-8")
        if path == "/api/overview-stats":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            raw = query.get("window", ["14d"])
            if len(raw) != 1:
                raise ValueError("window 只接受一个值")
            value = raw[0].strip()
            if value == "all":
                days = None
            elif re.fullmatch(r"\d{1,3}d", value):
                days = int(value[:-1])
            else:
                raise ValueError("window 只接受 all 或形如 7d / 14d / 30d 的值")
            with connect() as db:
                stats = storage.overview_stats(db, days)
                stats["kpi"]["today_added"] = db.execute(
                    "SELECT count(*) FROM records WHERE fetched_at >= ?",
                    (today_start_iso(),)).fetchone()[0]
                stats["kpi"]["negative"] = db.execute(
                    "SELECT count(*) FROM records WHERE sentiment='偏负面'").fetchone()[0]
                stats["kpi"]["open_alerts"] = db.execute(
                    "SELECT count(*) FROM alerts WHERE status='open'").fetchone()[0]
                stats["kpi"]["active_topics"] = admin_features.active_topics(db)
            return self.send(200, stats)
        if path == "/api/data":
            with connect() as db:
                result = storage.data(db)
            result["manual_limit"] = LIMIT
            return self.send(200, result)
        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/")
            with connect() as db:
                run = db.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone()
            return self.send(200, dict(run)) if run else self.send(404, {"error": "批次不存在"})
        if path.startswith("/api/records/"):
            record_id = path.removeprefix("/api/records/")
            with connect() as db:
                record = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                return self.send(404, {"error": "记录不存在"})
            try:
                raw = (ROOT / record["raw_path"]).read_bytes()
                if storage.digest(raw) != record["raw_sha256"]:
                    raise OSError("原文快照哈希不一致")
                return self.send(200, {"record": dict(record), "raw_snapshot": json.loads(raw)})
            except (OSError, ValueError) as exc:
                return self.send(409, {"error": str(exc), "record_id": record_id})
        if path == "/api/p1/overview":
            # keep_blank_values: ?top_k= / ?top_k=1&top_k= must reach the len==1 与 1..3 校验，
            # 而不是被静默丢弃后按默认 top_k=3 返回 200。
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            values = query.get("top_k", ["3"])
            if len(values) != 1:
                raise ValueError("top_k 只接受一个值")
            with connect() as db:
                return self.send(200, storage.p1_overview(db, values[0]))
        if path.startswith("/api/p1/evidence/"):
            evidence_id = path[len("/api/p1/evidence/"):]
            with connect() as db:
                try:
                    detail = storage.p1_detail(db, ROOT, evidence_id)
                except (OSError, ValueError) as exc:
                    return self.send(409, {"error": str(exc), "evidence_id": evidence_id})
            return self.send(200, detail) if detail else self.send(404, {"error": "证据不存在"})
        if path == "/api/p1/watchlist":
            with connect() as db:
                return self.send(200, storage.p1_watchlist(db))
        if path == "/api/p1/history":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            for key in query:
                if key not in HISTORY_QUERY_KEYS:
                    raise ValueError(f"history 不接受查询参数 {key}")
                if len(query[key]) != 1:
                    raise ValueError(f"history 查询参数 {key} 只接受一个值")
            if not query.get("evidence_id", [""])[0].strip():
                raise ValueError("history 必须提供非空 evidence_id")
            for key in ("from", "to"):
                if key in query and query[key][0] == "":
                    raise ValueError(f"{key} 不得为空值（缺省即不按该边界筛选）")
            limit_raw = query.get("limit", ["500"])[0]
            offset_raw = query.get("offset", ["0"])[0]
            if not limit_raw.isdigit():
                raise ValueError("limit 必须为整数")
            if not offset_raw.isdigit():
                raise ValueError("offset 必须为整数")
            with connect() as db:
                try:
                    result = storage.p1_history(db, query["evidence_id"][0], query.get("from", [None])[0],
                                                query.get("to", [None])[0], int(limit_raw), int(offset_raw))
                except storage.P1NotFound as exc:
                    return self.send(404, {"error": str(exc), "status": "rejected"})
                except storage.P1Invalid as exc:
                    return self.send(422, {"error": str(exc), "status": "rejected"})
            return self.send(200, result)
        if path in VENDOR_STATIC:
            target = (ROOT / path.lstrip("/")).resolve()
            if not target.is_relative_to(ROOT.resolve()) or not target.is_file():
                return self.send(404, {"error": "Not found"})
            return self.send(200, target.read_bytes(), VENDOR_STATIC[path])
        return self.send(404, {"error": "Not found"})

    def do_POST(self):
        try:
            refreshing = urllib.parse.urlsplit(self.path).path in ("/api/refresh", "/api/topics/rebuild")
        except ValueError as exc:
            return self.send(400, {"error": str(exc)})
        if refreshing:
            if not REFRESH_LOCK.acquire(blocking=False):
                return self.send(409, {"error": "已有采集批次运行，请查看运行状态"})
            try:
                return self._post()
            finally:
                REFRESH_LOCK.release()
        with WRITE_LOCK:
            return self._post()

    def _post(self):
        if self.headers.get("Origin") != f"http://127.0.0.1:{PORT}" or self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, {"error": "只允许本地页面操作"})
        try:
            value = self.body()
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/analysis":
                if set(value) != {'group_id', 'record_ids'}:
                    raise ValueError("只接受 group_id 和 record_ids")
                with connect() as db:
                    result = analysis.create_job(db, ROOT, value['group_id'], value['record_ids'], LIMIT)
                    analysis.candidate(db, result['id'])
                return self.send(201, result)
            if path == "/api/analysis/sentiment-model":
                limit = value.get("limit", analysis.MODEL_BATCH_LIMIT)
                if not isinstance(limit, int) or limit <= 0 or limit > 50:
                    raise ValueError("limit 必须是 1..50 的整数（默认 20）")
                with connect() as db:
                    return self.send(200, analysis.run_model_sentiment(db, limit))
            if path == "/api/analysis/sentiment":
                limit = value.get("limit")
                if limit is not None and (not isinstance(limit, int) or limit <= 0):
                    raise ValueError("limit 必须是正整数或缺省")
                with connect() as db:
                    return self.send(200, analysis.run_rule_sentiment(db, limit))
            if path == "/api/analysis/decision":
                if set(value) != {'job_id', 'decision', 'reason'}:
                    raise ValueError("决定字段不符合契约")
                with connect() as db:
                    result = analysis.decide(db, value['job_id'], value['decision'], value['reason'])
                return self.send(200, result)
            if path == "/api/alert-rules":
                with connect() as db:
                    return self.send(201, alertcenter.create_rule(db, value))
            if path == "/api/alerts/run":
                with connect() as db:
                    return self.send(200, alertcenter.evaluate(db))
            if path.startswith("/api/alerts/") and path.endswith("/ack"):
                alert_id = path[len("/api/alerts/"):-len("/ack")]
                with connect() as db:
                    return self.send(200, alertcenter.ack(db, alert_id))
            if path == "/api/feeds":
                name = clean(str(value.get("name", "")))[:100]
                url = safe_url(str(value.get("url", "")).strip())
                if not name:
                    raise ValueError("请填写来源名称")
                with connect() as db:
                    db.execute("INSERT INTO feeds(name,url,added_at) VALUES(?,?,?)", (name, url, datetime.now(timezone.utc).isoformat()))
                return self.send(201, {"ok": True})
            if path == "/api/keywords":
                terms = value.get("keywords", [])
                if not isinstance(terms, list) or len(terms) > 100:
                    raise ValueError("关键词格式错误")
                clean_terms = [clean(str(x))[:80] for x in terms if clean(str(x))]
                with connect() as db:
                    admin_features.set_keywords(db, clean_terms)
                return self.send(200, {"ok": True})
            if path == "/api/import":
                if set(value) != {"feed_id", "records"}:
                    raise ValueError("导入请求只能且必须包含 feed_id 和 records")
                feed_id = value.get("feed_id")
                if type(feed_id) is not int or feed_id < 1:
                    raise ValueError("feed_id 必须为正整数")
                with connect() as db:
                    run_id = storage.start_run(db, feed_id, "import", LIMIT)
                    try:
                        result = storage.import_batch(db, ROOT, run_id, value.get("records"), classify)
                    except ValueError as exc:
                        return self.send(422, {"error": str(exc), "run_id": run_id, "status": "failed"})
                    except (OSError, sqlite3.Error) as exc:
                        return self.send(500, {"error": str(exc), "run_id": run_id, "status": "failed"})
                return self.send(201, result)
            if path == "/api/p1/import":
                if set(value) != {"records"}:
                    raise ValueError("P1 导入请求只能且必须包含 records")
                with connect() as db:
                    try:
                        result = storage.p1_import(db, ROOT, value.get("records"), LIMIT)
                    except storage.P1LimitExceeded as exc:
                        return self.send(409, {"error": str(exc), "status": "rejected"})
                    except ValueError as exc:
                        return self.send(422, {"error": str(exc), "status": "rejected"})
                    except (OSError, sqlite3.Error) as exc:
                        return self.send(500, {"error": str(exc), "status": "failed"})
                return self.send(201, result)
            if path == "/api/p1/watchlist":
                if set(value) != {"evidence_id", "reason"}:
                    raise ValueError("观察请求只能且必须包含 evidence_id 和 reason")
                with connect() as db:
                    try:
                        watch = storage.p1_watch_add(db, value.get("evidence_id"), value.get("reason"))
                    except storage.P1NotFound as exc:
                        return self.send(404, {"error": str(exc), "status": "rejected"})
                    except storage.P1Conflict as exc:
                        return self.send(409, {"error": str(exc), "status": "rejected"})
                    except storage.P1Invalid as exc:
                        return self.send(422, {"error": str(exc), "status": "rejected"})
                    except (OSError, sqlite3.Error) as exc:
                        return self.send(500, {"error": str(exc), "status": "failed"})
                return self.send(201, {"watch": watch})
            if path == "/api/p1/watchlist/exit":
                if set(value) != {"watch_id", "reason"}:
                    raise ValueError("退出观察请求只能且必须包含 watch_id 和 reason")
                with connect() as db:
                    try:
                        watch = storage.p1_watch_exit(db, value.get("watch_id"), value.get("reason"))
                    except storage.P1NotFound as exc:
                        return self.send(404, {"error": str(exc), "status": "rejected"})
                    except storage.P1Conflict as exc:
                        return self.send(409, {"error": str(exc), "status": "rejected"})
                    except storage.P1Invalid as exc:
                        return self.send(422, {"error": str(exc), "status": "rejected"})
                    except (OSError, sqlite3.Error) as exc:
                        return self.send(500, {"error": str(exc), "status": "failed"})
                return self.send(200, {"watch": watch})
            if path == "/api/settings":
                global COLLECT_INTERVAL_MIN, COLLECT_LIMIT
                with connect() as db:
                    saved = admin_features.save_settings(db, value)
                COLLECT_INTERVAL_MIN = int(saved["collect_interval"])
                COLLECT_LIMIT = int(saved["collect_limit"])
                RUNTIME["collect_interval_minutes"] = COLLECT_INTERVAL_MIN
                RUNTIME["collect_limit"] = COLLECT_LIMIT
                return self.send(200, saved)
            if path == "/api/reports/generate":
                kind = str(value.get("kind", "daily"))
                with connect() as db:
                    return self.send(201, admin_features.generate_report(db, kind))
            if path == "/api/topics/rebuild":
                limit = value.get("limit", 200)
                if type(limit) is not int:
                    raise ValueError("limit 必须是整数")
                return self.send(200, admin_features.rebuild_events(limit))
            if path.startswith("/api/alert-rules/") and path.endswith("/toggle"):
                rule_id = int(path[len("/api/alert-rules/"):-len("/toggle")])
                enabled = value.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必须是布尔值")
                with connect() as db:
                    with db:
                        cursor = db.execute("UPDATE alert_rules SET enabled=? WHERE id=?",
                                            (1 if enabled else 0, rule_id))
                if cursor.rowcount == 0:
                    return self.send(404, {"error": "规则不存在"})
                return self.send(200, {"id": rule_id, "enabled": enabled})
            if path == "/api/pipeline/run":
                if not ORCH_LOCK.acquire(blocking=False):
                    return self.send(409, {"error": "已有分析流水线在运行", "status": "busy"})
                requested = str(value.get("trace_id") or "").strip()
                try:
                    with connect() as db:
                        if requested:
                            row = db.execute("SELECT stages FROM pipeline_runs WHERE trace_id=?",
                                             (requested,)).fetchone()
                            if not row:
                                raise LookupError("trace 不存在")
                            stages = json.loads(row["stages"])
                            trace_id = requested
                        else:
                            stages = list(orchestrator.STAGES)
                            trace_id = orchestrator.create_run(db, stages)
                except LookupError as exc:
                    ORCH_LOCK.release()
                    return self.send(404, {"error": str(exc)})
                except Exception:
                    ORCH_LOCK.release()
                    raise
                try:
                    threading.Thread(target=run_analysis_pipeline, args=(trace_id, stages),
                                     daemon=True, name="analysis-pipeline").start()
                except Exception as exc:
                    try:
                        orchestrator.fail_run(DB, trace_id, exc)
                    except Exception:
                        pass
                    ORCH_LOCK.release()
                    raise
                return self.send(202, {"trace_id": trace_id, "stages": stages})
            if path == "/api/collect":
                # 先抢锁再起线程，避免两个请求同时看到 running=False 而各起一轮。
                if not COLLECT_LOCK.acquire(blocking=False):
                    return self.send(409, {"error": "已有采集在运行", "status": "busy"})
                threading.Thread(target=run_collection, args=("manual",),
                                 kwargs={"lock_held": True}, daemon=True,
                                 name="collect-manual").start()
                return self.send(202, {"started": True, "endpoints": COLLECT_ENDPOINTS,
                                       "limit": COLLECT_LIMIT,
                                       "interval_minutes": COLLECT_INTERVAL_MIN})
            if path == "/api/refresh":
                with connect() as db:
                    feeds = [dict(x) for x in db.execute("SELECT * FROM feeds ORDER BY id LIMIT ?", (LIMIT,))]
                result = []
                for feed in feeds:
                    with WRITE_LOCK, connect() as db:
                        run_id = storage.start_run(db, feed["id"], "rss", LIMIT)
                    try:
                        rows = fetch_feed(feed, LIMIT)
                        with WRITE_LOCK, connect() as db:
                            outcome = storage.import_batch(db, ROOT, run_id, rows, classify)
                        result.append({"name": feed["name"], **outcome})
                    except (OSError, ValueError, ET.ParseError, sqlite3.Error) as exc:
                        with WRITE_LOCK, connect() as db:
                            storage.fail_run(db, run_id, exc)
                        result.append({"name": feed["name"], "run_id": run_id, "status": "failed", "error": str(exc)[:1000]})
                return self.send(200, {"results": result})
            return self.send(404, {"error": "Not found"})
        except sqlite3.IntegrityError:
            return self.send(409, {"error": "来源或关键词已存在"})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send(400, {"error": str(exc)})
        except (OSError, sqlite3.Error) as exc:
            return self.send(500, {"error": str(exc)})
        except RuntimeError as exc:
            busy = "运行" in str(exc)
            return self.send(409 if busy else 500, {"error": str(exc), "status": "busy" if busy else "failed"})
        except Exception as exc:  # 聚类子进程等运行时错误，不能直接断连接
            return self.send(500, {"error": f"{type(exc).__name__}: {exc}"[:500]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--collect-interval", type=int, default=20,
                        help="自动采集间隔（分钟，0=关闭；默认 20）")
    parser.add_argument("--collect-limit", type=int, default=20,
                        help="每个端点每轮条数 1–50（默认 20）")
    parser.add_argument("--opencli", default=None,
                        help="OpenCLI 的 dist/src/main.js 路径；默认查 D:\\DSH\\tools\\opencli-1.8.8")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit 必须为 1–100；首次使用 --limit 3")
    if not 0 <= args.collect_interval <= 1440:
        parser.error("--collect-interval 必须为 0–1440 分钟（0=关闭自动采集）")
    if not 1 <= args.collect_limit <= 50:
        parser.error("--collect-limit 必须为 1–50")
    LIMIT = args.limit
    COLLECT_INTERVAL_MIN = args.collect_interval
    COLLECT_LIMIT = args.collect_limit
    OPENCLI_MAIN = args.opencli or default_opencli_main()
    if args.collect_interval > 0 and not (OPENCLI_MAIN and Path(OPENCLI_MAIN).is_file()):
        print("警告：未找到 OpenCLI，自动采集会记失败；可用 --opencli 指定，或用 --collect-interval 0 关闭。",
              file=sys.stderr, flush=True)
    RUNTIME = {"pid": os.getpid(), "started_at": storage.now(), "entry": str(Path(__file__).resolve()),
               "storage_module": str(Path(storage.__file__).resolve()), "database": str(DB),
               "snapshot_root": str(ROOT / "snapshots"), "manual_limit": LIMIT,
               "collect_interval_minutes": COLLECT_INTERVAL_MIN,
               "collect_limit": COLLECT_LIMIT,
               "collect_endpoints": COLLECT_ENDPOINTS,
               "opencli_main": OPENCLI_MAIN,
               "loaded_sha256": {name: storage.digest((ROOT / name).read_bytes())
                                 for name in ("server.py", "storage.py", "analysis.py")},
               "analysis_module": str(Path(analysis.__file__).resolve()), "production_adapter": ("rule:" + analysis.SENTIMENT_RULE) if not os.environ.get(analysis.PRODUCTION_KEY_ENV) else ("model:" + analysis.PRODUCTION_MODEL)}
    if port_in_use(HOST, PORT):
        print(f"端口 {HOST}:{PORT} 已被占用：已有实例在运行。请先停止旧进程再启动。",
              file=sys.stderr, flush=True)
        raise SystemExit(3)
    try:
        server = SingleInstanceHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"无法绑定 {HOST}:{PORT}：{exc}", file=sys.stderr, flush=True)
        raise SystemExit(3)
    try:
        with connect() as db:
            storage.recover_runs(db)
            analysis.recover(db)
            # 一次性迁移：首次启动把 10-17 冒烟规则打开，之后尊重用户的启停选择
            seeded = db.execute("SELECT value FROM settings WHERE key='alerts_seeded'").fetchone()
            if seeded is None:
                with db:
                    db.execute("UPDATE alert_rules SET enabled=1")
                    db.execute("INSERT INTO settings(key,value,updated_at) VALUES('alerts_seeded','true',?)",
                               (storage.now(),))
            saved = admin_features.get_settings(db)
            COLLECT_INTERVAL_MIN = int(saved["collect_interval"])
            COLLECT_LIMIT = int(saved["collect_limit"])
        RUNTIME["collect_interval_minutes"] = COLLECT_INTERVAL_MIN
        RUNTIME["collect_limit"] = COLLECT_LIMIT
        # 启动期调度：alertcenter.init 已由 connect() 内的调用完成；此处只启动 60 秒 daemon 线程。
        try:
            fts_db = storage.connect(DB)
            try:
                storage.reset_fts(fts_db)   # 'unavailable' 每次启动重试一次
            finally:
                fts_db.close()
        except Exception as exc:
            print(f"FTS 初始化重试失败：{exc}", file=sys.stderr, flush=True)
        try:
            pipeline.recover(DB, RUNTIME["started_at"])
        except Exception as exc:
            print(f"pipeline 启动恢复失败：{exc}", file=sys.stderr, flush=True)
        try:
            with connect() as db:
                orchestrator.recover(db, RUNTIME["started_at"])
        except Exception as exc:
            print(f"orchestrator 启动恢复失败：{exc}", file=sys.stderr, flush=True)
        alertcenter.start_scheduler(DB)
        start_collection_scheduler()
        print(f"本地舆情面板：http://{HOST}:{PORT}（手动批次上限 {LIMIT}；"
              f"自动采集 {COLLECT_INTERVAL_MIN} 分钟/轮，0=关闭；Ctrl+C 停止）", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
