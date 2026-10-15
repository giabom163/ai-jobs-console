"""Local, manual-refresh RSS sentiment monitor; Python standard library only."""
from datetime import datetime, timezone
import argparse
from contextlib import contextmanager
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

ROOT = Path(__file__).resolve().parent
DB = ROOT / "sentiment.db"
HOST, PORT = "127.0.0.1", 8790
LIMIT = 3
WRITE_LOCK = threading.Lock()
RUNTIME = {}
POSITIVE = {"增长", "上涨", "利好", "成功", "创新", "支持", "改善", "满意", "合作", "突破", "good", "growth", "success", "positive", "support", "improve", "win"}
NEGATIVE = {"下跌", "亏损", "投诉", "事故", "风险", "处罚", "下滑", "失败", "争议", "危机", "bad", "loss", "complaint", "risk", "fine", "decline", "failure", "crisis"}


@contextmanager
def connect():
    db = storage.connect(DB)
    try:
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
        except (OSError, sqlite3.Error, ValueError) as exc:
            return self.send(500, {"error": str(exc)})

    def _get(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/runtime":
            return self.send(200, RUNTIME)
        if path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
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
        return self.send(404, {"error": "Not found"})

    def do_POST(self):
        with WRITE_LOCK:
            return self._post()

    def _post(self):
        if self.headers.get("Origin") != f"http://127.0.0.1:{PORT}" or self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, {"error": "只允许本地页面操作"})
        try:
            value = self.body()
            path = urllib.parse.urlsplit(self.path).path
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
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        return self.send(422, {"error": str(exc), "run_id": run_id, "status": "failed"})
                return self.send(201, result)
            if path == "/api/refresh":
                with connect() as db:
                    feeds = [dict(x) for x in db.execute("SELECT * FROM feeds ORDER BY id LIMIT ?", (LIMIT,))]
                result = []
                for feed in feeds:
                    with connect() as db:
                        run_id = storage.start_run(db, feed["id"], "rss", LIMIT)
                    try:
                        rows = fetch_feed(feed, LIMIT)
                        with connect() as db:
                            outcome = storage.import_batch(db, ROOT, run_id, rows, classify)
                        result.append({"name": feed["name"], **outcome})
                    except (OSError, ValueError, ET.ParseError, sqlite3.Error) as exc:
                        with connect() as db:
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
                                 for name in ("server.py", "storage.py")}}
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        with connect() as db:
            storage.recover_runs(db)
        print(f"本地舆情面板：http://{HOST}:{PORT}（手动批次上限 {LIMIT}；Ctrl+C 停止）", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
