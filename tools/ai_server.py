#!/usr/bin/env python
"""Minimal read-only API + static server for the AI jobs dashboard."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__:
    from .employment_signals import list_signals, storage_status
    from .job_geography import is_domestic_only, is_mixed_location, is_unknown_location
    from .job_fields import register_read_view, recent_since
    from .export_console import build as build_payload
    from .job_taxonomy import enrich_job
    from .job_details import source_details, duplicate_records
    from .employment_events import event_payload
    from .job_catalog import normalized_filters, select_jobs, summarize_jobs
else:
    from employment_signals import list_signals, storage_status
    from job_geography import is_domestic_only, is_mixed_location, is_unknown_location
    from job_fields import register_read_view, recent_since
    from export_console import build as build_payload
    from job_taxonomy import enrich_job
    from job_details import source_details, duplicate_records
    from employment_events import event_payload
    from job_catalog import normalized_filters, select_jobs, summarize_jobs

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
WEB = ROOT / "web"
HOST, PORT = "127.0.0.1", 8792
AI_TIERS = "('build','adjacent')"
DATE_FLOOR = "2026-06-01"
TZ = timezone(timedelta(hours=8))
_payload_lock = threading.Lock()
_payload_cache = None
_payload_cache_signature = None
_pipeline_proc = None
_pipeline_lock = threading.Lock()
PIPELINE_LOG_FILE = Path.home() / '.codex' / 'documents' / 'ai-jobs-validation-20261102' / 'pipeline-live.log'


def default_since():
    return recent_since()


def connect():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.create_function("is_domestic_only", 1, is_domestic_only, deterministic=True)
    con.create_function("is_mixed_location", 1, is_mixed_location, deterministic=True)
    con.create_function("is_unknown_location", 1, is_unknown_location, deterministic=True)
    register_read_view(con)
    return con


def _database_signature():
    signature = []
    for path in (DB, Path(str(DB) + "-wal")):
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append((str(path), None, None))
    return tuple(signature)


def get_payload(con):
    """Reuse the read-only export across API routes until SQLite files change."""
    global _payload_cache, _payload_cache_signature
    signature = _database_signature()
    if _payload_cache is not None and signature == _payload_cache_signature:
        return _payload_cache
    with _payload_lock:
        signature = _database_signature()
        if _payload_cache is not None and signature == _payload_cache_signature:
            return _payload_cache
        payload = build_payload(con)
        final_signature = _database_signature()
        if signature == final_signature:
            _payload_cache = payload
            _payload_cache_signature = final_signature
        else:
            _payload_cache = None
            _payload_cache_signature = None
        return payload


def stats(con, since=None, query=None):
    filters = normalized_filters(query or {'since': [since or '']})
    payload = get_payload(con)
    rows = select_jobs(payload['jobs'], filters)
    review_filters = dict(filters, status='review')
    review_rows = select_jobs(payload['jobs'], review_filters)
    return summarize_jobs(rows, payload['stats'], filters, review_rows)


def jobs(con, q, company, ai, level, status, limit, offset, since=None, query=None):
    filters = normalized_filters(query or {k: [v or ''] for k, v in
        dict(q=q, company=company, ai=ai, level=level, status=status, since=since).items()})
    payload = get_payload(con)
    rows = select_jobs(payload['jobs'], filters)
    return {'total': len(rows), 'limit': limit, 'offset': offset, 'since': filters['since'],
            'date_basis': 'first_seen', 'meta': payload['meta'], 'items': rows[offset:offset+limit]}


class Handler(BaseHTTPRequestHandler):
    server_version = "AIJobs/0.3"

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        global _pipeline_proc
        if path == '/api/admin/pipeline/trigger':
            with _pipeline_lock:
                if _pipeline_proc is not None and _pipeline_proc.poll() is None:
                    return self.send_json({'error': 'pipeline is already running', 'pid': _pipeline_proc.pid}, 409)
                PIPELINE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(PIPELINE_LOG_FILE, 'w', encoding='utf-8')
                py = sys.executable
                daemon_py = ROOT / 'tools' / 'pipeline_daemon.py'
                _pipeline_proc = subprocess.Popen([py, '-B', str(daemon_py), '--once'], stdout=log_fh, stderr=subprocess.STDOUT)
                return self.send_json({'status': 'dispatched', 'pid': _pipeline_proc.pid, 'started_at': datetime.now(TZ).isoformat()})
        return self.send_json({'error': 'not found'}, 404)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path, query = u.path, urllib.parse.parse_qs(u.query)
        since = (query.get("since", [""])[0] or "").strip() or None
        if since and since != 'all':
            try:
                datetime.strptime(since, '%Y-%m-%d')
            except ValueError:
                return self.send_json({'error':'invalid date'},400)
        con = None
        try:
            if path == '/api/employment-events':
                return self.send_json(event_payload(query))
            if path == '/admin' or path == '/admin/':
                admin_html = WEB / 'admin' / 'index.html'
                if not admin_html.exists():
                    return self.send_json({'error': 'admin panel html not found'}, 404)
                body = admin_html.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if path == '/api/admin/status':
                con = connect()
                try:
                    counts = dict(con.execute('SELECT status, count(*) FROM jobs GROUP BY status').fetchall())
                    source_stats = [dict(r) for r in con.execute('SELECT source, count(*) as count, max(last_seen) as last_seen FROM jobs GROUP BY source ORDER BY count DESC').fetchall()]
                    total_jobs = con.execute('SELECT count(*) FROM jobs').fetchone()[0]
                finally:
                    con.close()
                sched_path = ROOT / '.request-scheduler.json'
                sched_state = json.loads(sched_path.read_text('utf-8')) if sched_path.exists() else {}
                running = (_pipeline_proc is not None and _pipeline_proc.poll() is None)
                return self.send_json({
                    'total_jobs': total_jobs,
                    'status_counts': counts,
                    'source_stats': source_stats,
                    'scheduler': sched_state,
                    'pipeline_running': running,
                    'server_time': datetime.now(TZ).isoformat(),
                })
            if path == '/api/admin/pipeline/logs':
                logs = []
                if PIPELINE_LOG_FILE.exists():
                    try:
                        with open(PIPELINE_LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                            logs = f.readlines()[-100:]
                    except Exception:
                        pass
                running = (_pipeline_proc is not None and _pipeline_proc.poll() is None)
                return self.send_json({'running': running, 'logs': logs})
            if path in {"/api/stats", "/api/jobs", "/api/report", "/api/sources"}:
                normalized_filters(query)
            if path == "/api/stats":
                con = connect()
                return self.send_json(stats(con, since, query))
            if path == "/api/jobs":
                con = connect()
                return self.send_json(jobs(
                    con, (query.get("q", [""])[0] or "").strip(),
                    (query.get("company", [""])[0] or "").strip(),
                    (query.get("ai", [""])[0] or "").strip(),
                    (query.get("level", [""])[0] or "").strip(),
                    (query.get("status", [""])[0] or "open").strip(),
                    min(100, max(1, int(query.get("limit", ["50"])[0]))),
                    max(0, int(query.get("offset", ["0"])[0])), since, query))
            if path.startswith("/api/jobs/"):
                con = connect()
                jid = path.split("/")[-1]
                row = con.execute(
                    "SELECT j.*,c.name company FROM job_records j JOIN companies c ON c.id=j.company_id WHERE j.id=? AND is_domestic_only(j.location)=1",
                    (jid,)).fetchone()
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                snaps = [dict(r) for r in con.execute(
                    "SELECT seen_at,status,location FROM job_snapshots WHERE job_id=? ORDER BY seen_at DESC LIMIT 10", (jid,))]
                changes = [dict(r) for r in con.execute(
                    "SELECT change_type,changed_at,note FROM job_changes WHERE job_id=? ORDER BY changed_at DESC LIMIT 10", (jid,))]
                job = enrich_job(dict(row), source_details(con,jid).get(jid))
                job['duplicate_records'] = duplicate_records(con,jid).get(jid,[])
                return self.send_json({"job": job, "snapshots": snaps, "changes": changes})
            if path == "/api/signals":
                con = connect()
                try:
                    items = list_signals(
                        con, review=query.get("review", ["approved"])[0],
                        since=since, until=query.get("until", [None])[0],
                        category=query.get("category", [None])[0],
                        limit=query.get("limit", ["50"])[0])
                    storage=storage_status(con)
                except ValueError as error:
                    return self.send_json({"error": str(error)}, 400)
                finally:
                    con.close()
                return self.send_json({"items": items,"storage":storage})
            if path == "/api/report":
                con = connect()
                s = stats(con, since, query)
                return self.send_json({
                    "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                    "window_since": s["window_since"],
                    "total": s["ai_total"], "open": s["open"], "build": s["build"],
                    "adjacent": s["adjacent"], "verified": s["verified"],
                    "pending": s["ai_total"] - s["verified"], "excluded": s["excluded"],
                    "closed": s["closed"], "merged": s["merged"], "today_new": s["today_new"],
                    "companies": s["companies"], "intern": s["intern"], "regular": s["regular"],
                    "unknown": s["unknown"], "other_employment": s["other_employment"],
                    "top_companies": s["top_companies"][:5],
                    "signals": len(list_signals(con, review="approved", limit=200)),
                    "signal_candidates": len(list_signals(con, review="pending", limit=200)),
                    "note": "岗位记录按首次发现时间筛选，来源发布时间独立保存；今日新增按北京时间统计当前筛选内首次入库的唯一岗位。"
                            "链接核验通过仅代表最近一次 URL 核验通过，不保证当前仍可投递；剔除、合并、关闭及长期未见为库内累计。",
                })
            if path == "/api/sources":
                con = connect()
                try:
                    payload = get_payload(con)
                finally:
                    con.close()
                filters = normalized_filters(query)
                rows = select_jobs(payload['jobs'], filters)
                review_rows = select_jobs(payload['jobs'], dict(filters, status='review'))
                items = []
                for source in payload['sources']:
                    selected = [j for j in rows if j['company_id'] == source['company_id']]
                    pending = [j for j in review_rows if j['company_id'] == source['company_id']]
                    items.append(dict(source, jobs=len(selected), pending_review=len(pending),
                        verified=sum(isinstance(j.get('http_status'), int) and 200 <= j['http_status'] < 300 for j in selected),
                        last_seen=max((j['last_seen'] for j in selected if j.get('last_seen')), default=None)))
                return self.send_json({'items': items,'health':payload['source_health']})
            # static
            static_root = WEB
            if path.startswith('/_preview/'):
                static_root = ROOT / 'site'
                path = path[len('/_preview'):]
            if path == "/":
                path = "/index.html"
            target = (static_root / path.lstrip("/")).resolve()
            if not target.is_relative_to(static_root.resolve()) or not target.is_file():
                return self.send_json({"error": "not found"}, 404)
            ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8"}.get(target.suffix, "application/octet-stream")
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, 500)
        finally:
            if con is not None:
                con.close()


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"http://{HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()

