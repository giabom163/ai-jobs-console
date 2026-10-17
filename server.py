"""Local, manual-refresh RSS sentiment monitor; Python standard library only."""
from datetime import datetime, timezone
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
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
import storage
import analysis

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
# 任务流水线：桥 store 的只读汇总。默认清单可用环境变量 SENTIMENT_TASK_STORES 覆盖，
# 值必须是 JSON 数组，元素形如 {"key": "_s1", "label": "S1", "path": "D:\\...\\_s1\\store"}。
# 缺失的 store 只在响应里记 error，不影响其它 store，也不写任何 store 文件。
TASK_SCHEMA_VERSION = 1
TASK_STORES_ENV = "SENTIMENT_TASK_STORES"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_TASK_STORES = (
    {"key": "_p0", "label": "P0",
     "path": r"D:/DSH/output/2026-10-16/2026-10-16-18-03-28/_p0/store"},
    {"key": "_s1", "label": "S1 · 多源采集",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_s1\store"},
    {"key": "_s3", "label": "S3 · 相关性标注",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_s3\store"},
    {"key": "_s4", "label": "S4 · 事件聚类",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_s4\store"},
    {"key": "_s5", "label": "S5 · 立场分析",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_s5\store"},
    {"key": "_e2e", "label": "E2E · 端到端验收",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_e2e\store"},
    {"key": "bridge", "label": "Bridge · 桥任务",
     "path": r"C:\Users\Lenovo\Documents\Codex\bridge-work\data"},
)
WRITE_LOCK = threading.Lock()
REFRESH_LOCK = threading.Lock()
RUNTIME = {}
POSITIVE = {"增长", "上涨", "利好", "成功", "创新", "支持", "改善", "满意", "合作", "突破", "good", "growth", "success", "positive", "support", "improve", "win"}
NEGATIVE = {"下跌", "亏损", "投诉", "事故", "风险", "处罚", "下滑", "失败", "争议", "危机", "bad", "loss", "complaint", "risk", "fine", "decline", "failure", "crisis"}


@contextmanager
def connect():
    db = storage.connect(DB)
    try:
        analysis.init(db)
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


def task_store_list():
    """返回生效的 store 清单；环境变量覆盖时必须是结构合法的 JSON 数组。"""
    raw = os.environ.get(TASK_STORES_ENV, "").strip()
    if not raw:
        return [dict(item) for item in DEFAULT_TASK_STORES]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{TASK_STORES_ENV} 必须是 JSON 数组：{exc}") from exc
    if not isinstance(value, list) or not value:
        raise ValueError(f"{TASK_STORES_ENV} 必须是非空 JSON 数组")
    stores = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{TASK_STORES_ENV} 的每个元素必须是对象")
        key = str(item.get("key") or "").strip()
        path = str(item.get("path") or "").strip()
        if not key or not path:
            raise ValueError(f"{TASK_STORES_ENV} 的每个元素都需要非空 key 与 path")
        stores.append({"key": key, "label": str(item.get("label") or key), "path": path})
    return stores


def read_json(path):
    """只读解析 JSON；文件不存在返回 (None, None)，解析失败返回 (None, 错误说明)。"""
    if not path.is_file():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except OSError as exc:
        return None, f"{path.name} 读取失败：{exc}"
    except ValueError as exc:
        return None, f"{path.name} 不是合法 JSON：{exc}"


def task_detail_version(task_dir, attempt_id):
    """由 state/attempt/result 的实际字节 SHA 派生稳定版本号。

    只读取真实文件字节，不用 mtime/size；内容不变则版本不变。缺失的文件用固定占位，
    因此“未执行/未收尾”的任务也有稳定版本。"""
    paths = [task_dir / "state.json"]
    if isinstance(attempt_id, str) and TASK_ID_PATTERN.match(attempt_id) and ".." not in attempt_id:
        attempt_dir = task_dir / "attempts" / attempt_id
        paths.extend([attempt_dir / "attempt.json", attempt_dir / "result.json"])
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        try:
            data = path.read_bytes() if path.is_file() else None
        except OSError:
            data = None
        if data is None:
            digest.update(b"\x00missing\x00")
        else:
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        digest.update(b"\x01")
    return digest.hexdigest()


def task_summary(task_dir):
    """汇总一个任务的 state.json 与当前 attempt 的 attempt.json / result.json。"""
    state, problem = read_json(task_dir / "state.json")
    if not isinstance(state, dict):
        state = {}
        problem = problem or "state.json 不是 JSON 对象"
    attempt_id = state.get("current_attempt")
    attempt = result = None
    if isinstance(attempt_id, str) and TASK_ID_PATTERN.match(attempt_id) and ".." not in attempt_id:
        attempt_dir = task_dir / "attempts" / attempt_id
        attempt, attempt_error = read_json(attempt_dir / "attempt.json")
        result, result_error = read_json(attempt_dir / "result.json")
        # attempt.json / result.json 缺失属正常（尚未执行或未收尾），只有解析失败才算问题。
        problems = [x for x in (attempt_error, result_error) if x]
        if problems:
            problem = "；".join([x for x in (problem, *problems) if x])[:300]
    else:
        attempt_id = None
    review = state.get("product_review")
    human = state.get("human_accept")
    return {
        "id": task_dir.name,
        "status": state.get("status"),
        "acceptance": state.get("acceptance"),
        "revision": state.get("revision"),
        "product_review": review.get("verdict") if isinstance(review, dict) else None,
        "human": human.get("decision") if isinstance(human, dict) else None,
        "human_dismissed": bool(human.get("dismissed")) if isinstance(human, dict) else False,
        "attempt": attempt_id,
        "started": (attempt or {}).get("started_at") or state.get("started_at"),
        "finished": state.get("finished_at") or (result or {}).get("finished_at"),
        "cost": (result or {}).get("native_cost"),
        "detail_version": task_detail_version(task_dir, attempt_id),
    }, problem


def task_overview():
    stores = []
    for store in task_store_list():
        root = Path(store["path"])
        entry = {"key": store["key"], "label": store["label"], "path": store["path"],
                 "error": None, "tasks": []}
        if not root.is_dir():
            entry["error"] = "store 目录不存在"
            stores.append(entry)
            continue
        tasks_dir = root / "tasks"
        if not tasks_dir.is_dir():
            entry["error"] = "tasks 目录不存在"
            stores.append(entry)
            continue
        try:
            task_dirs = sorted((p for p in tasks_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
        except OSError as exc:
            entry["error"] = f"tasks 目录读取失败：{exc}"
            stores.append(entry)
            continue
        problems = []
        for task_dir in task_dirs:
            summary, problem = task_summary(task_dir)
            entry["tasks"].append(summary)
            if problem:
                problems.append(f"{task_dir.name}: {problem}")
        if problems:
            entry["error"] = "；".join(problems)[:500]
        stores.append(entry)
    return {"schema_version": TASK_SCHEMA_VERSION, "updated_at": storage.now(), "stores": stores}


def task_detail(store_key, task_id):
    """原样只读回传 state.json / attempt.json / result.json；返回 (payload, http_code)。"""
    store = next((item for item in task_store_list() if item["key"] == store_key), None)
    if store is None:
        return {"error": "store 不存在", "store": store_key}, 404
    tasks_dir = Path(store["path"]) / "tasks"
    if not tasks_dir.is_dir():
        return {"error": "store 的 tasks 目录不存在", "store": store_key}, 404
    try:
        task_dir = next((p for p in tasks_dir.iterdir() if p.is_dir() and p.name == task_id), None)
    except OSError as exc:
        return {"error": f"tasks 目录读取失败：{exc}", "store": store_key}, 500
    if task_dir is None:
        return {"error": "任务不存在", "store": store_key, "task_id": task_id}, 404
    state, state_error = read_json(task_dir / "state.json")
    if state_error:
        return {"error": state_error, "store": store_key, "task_id": task_id}, 500
    if state is None:
        return {"error": "state.json 不存在", "store": store_key, "task_id": task_id}, 404
    attempt_id = state.get("current_attempt") if isinstance(state, dict) else None
    attempt = result = None
    if isinstance(attempt_id, str) and TASK_ID_PATTERN.match(attempt_id) and ".." not in attempt_id:
        attempt_dir = task_dir / "attempts" / attempt_id
        attempt, attempt_error = read_json(attempt_dir / "attempt.json")
        result, result_error = read_json(attempt_dir / "result.json")
        errors = "；".join(x for x in (attempt_error, result_error) if x)
        if errors:
            return {"error": errors, "store": store_key, "task_id": task_id}, 500
    return {"state": state, "attempt": attempt, "result": result,
            "detail_version": task_detail_version(task_dir, attempt_id)}, 200


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
        if path == "/api/tasks":
            return self.send(200, task_overview())
        if path.startswith("/api/tasks/"):
            segments = [urllib.parse.unquote(x) for x in path[len("/api/tasks/"):].split("/")]
            if len(segments) != 2 or not all(segments):
                return self.send(404, {"error": "任务详情路径应为 /api/tasks/<store_key>/<task_id>"})
            payload, code = task_detail(segments[0], segments[1])
            return self.send(code, payload)
        if path == "/api/analysis":
            with connect() as db:
                return self.send(200, analysis.overview(db))
        if path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/tasks":
            return self.send(200, (ROOT / "tasks.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/app.js":
            return self.send(200, (ROOT / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/style.css":
            return self.send(200, (ROOT / "style.css").read_bytes(), "text/css; charset=utf-8")
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
            refreshing = urllib.parse.urlsplit(self.path).path == "/api/refresh"
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
            if path == "/api/analysis/decision":
                if set(value) != {'job_id', 'decision', 'reason'}:
                    raise ValueError("决定字段不符合契约")
                with connect() as db:
                    result = analysis.decide(db, value['job_id'], value['decision'], value['reason'])
                return self.send(200, result)
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
                clean_terms = {clean(str(x))[:80] for x in terms if clean(str(x))}
                with connect() as db:
                    db.execute("DELETE FROM keywords")
                    db.executemany("INSERT OR IGNORE INTO keywords(value) VALUES(?)", [(x,) for x in clean_terms])
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit 必须为 1–100；首次使用 --limit 3")
    LIMIT = args.limit
    RUNTIME = {"pid": os.getpid(), "started_at": storage.now(), "entry": str(Path(__file__).resolve()),
               "storage_module": str(Path(storage.__file__).resolve()), "database": str(DB),
               "snapshot_root": str(ROOT / "snapshots"), "manual_limit": LIMIT,
               "loaded_sha256": {name: storage.digest((ROOT / name).read_bytes())
                                 for name in ("server.py", "storage.py", "analysis.py")},
               "analysis_module": str(Path(analysis.__file__).resolve()), "production_adapter": "not_configured"}
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        with connect() as db:
            storage.recover_runs(db)
            analysis.recover(db)
        print(f"本地舆情面板：http://{HOST}:{PORT}（手动批次上限 {LIMIT}；Ctrl+C 停止）", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
