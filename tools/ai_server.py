#!/usr/bin/env python
"""Minimal read-only API + static server for the AI jobs dashboard."""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__:
    from .employment_signals import list_signals
else:
    from employment_signals import list_signals

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
WEB = ROOT / "web"
HOST, PORT = "127.0.0.1", 8792
AI_TIERS = "('build','adjacent')"
DATE_FLOOR = "2026-06-01"
TZ = timezone(timedelta(hours=8))


def default_since():
    return DATE_FLOOR


def connect():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def stats(con, since=None):
    def one(sql, args=()):
        return con.execute(sql, args).fetchone()[0]
    if since == "all":
        since = "1970-01-01"
    since = since or default_since()
    win = f"j.status='open' AND j.ai_relevance IN {AI_TIERS} AND substr(j.posted_at,1,10) >= ?"
    return {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "window_since": "全部时间" if since == "1970-01-01" else since,
        "ai_total": one(f"SELECT count(*) FROM jobs j WHERE {win}", (since,)),
        "ai_all": one(f"SELECT count(*) FROM jobs j WHERE j.status='open' AND j.ai_relevance IN {AI_TIERS}"),
        "total": one(f"SELECT count(*) FROM jobs j WHERE {win}", (since,)),
        "open": one(f"SELECT count(*) FROM jobs j WHERE {win}", (since,)),
        "build": one(f"SELECT count(*) FROM jobs j WHERE {win} AND j.ai_relevance='build'", (since,)),
        "adjacent": one(f"SELECT count(*) FROM jobs j WHERE {win} AND j.ai_relevance='adjacent'", (since,)),
        "excluded": one("SELECT count(*) FROM jobs WHERE status='excluded'"),
        "closed": one("SELECT count(*) FROM jobs WHERE status='closed'"),
        "stale": one("SELECT count(*) FROM jobs WHERE status='stale'"),
        "liveness_checked": one(f"SELECT count(*) FROM jobs j WHERE {win} AND COALESCE(j.liveness,'') != ''", (since,)),
        "merged": one("SELECT count(*) FROM jobs WHERE status='merged'"),
        "verified": one(f"SELECT count(*) FROM jobs j WHERE {win} AND j.authenticity_level='已验证'", (since,)),
        "companies": one(f"SELECT count(DISTINCT j.company_id) FROM jobs j WHERE {win}", (since,)),
        "today_new": one("SELECT count(DISTINCT c.job_id) FROM job_changes c JOIN jobs j ON j.id=c.job_id "
                         f"WHERE c.change_type='new' AND date(c.changed_at,'+8 hours')=date('now','+8 hours') "
                         f"AND j.ai_relevance IN {AI_TIERS} AND substr(j.posted_at,1,10) >= ?", (since,)),
        "levels": [dict(r) for r in con.execute(
            f"SELECT j.authenticity_level label,count(*) value FROM jobs j WHERE {win} "
            "GROUP BY j.authenticity_level ORDER BY value DESC", (since,))],
        "top_companies": [dict(r) for r in con.execute(
            f"SELECT c.name label,count(*) value FROM jobs j JOIN companies c ON c.id=j.company_id "
            f"WHERE {win} GROUP BY c.name ORDER BY value DESC LIMIT 8", (since,))],
        "top_roles": [dict(r) for r in con.execute(
            f"SELECT j.title label,count(*) value FROM jobs j WHERE {win} "
            "GROUP BY j.title ORDER BY value DESC LIMIT 8", (since,))],
        "trend": [dict(r) for r in con.execute(
            "SELECT date(c.changed_at,'+8 hours') day,count(DISTINCT CASE WHEN c.change_type='new' THEN c.job_id END) new_jobs,"
            "count(DISTINCT CASE WHEN c.change_type='closed' THEN c.job_id END) closed_jobs "
            "FROM job_changes c JOIN jobs j ON j.id=c.job_id "
            f"WHERE date(c.changed_at,'+8 hours') BETWEEN date('now','+8 hours','-13 day') AND date('now','+8 hours') AND j.ai_relevance IN {AI_TIERS} "
            "AND substr(j.posted_at,1,10) >= ? GROUP BY day ORDER BY day", (since,))],
    }


def jobs(con, q, company, ai, level, status, limit, offset, since=None):
    if since == "all":
        since = None
    elif since is None:
        since = default_since()
    where, args = ["j.status = ?"], [status or "open"]
    if status == "open":
        where.append(f"j.ai_relevance IN {AI_TIERS}")
    if since:
        where.append("substr(j.posted_at,1,10) >= ?")
        args.append(since)
    if q:
        where.append("(j.title LIKE ? OR c.name LIKE ? OR j.description LIKE ?)")
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if company:
        where.append("c.name = ?")
        args.append(company)
    if ai == "ai":
        where.append(f"j.ai_relevance IN {AI_TIERS}")
    elif ai:
        where.append("j.ai_relevance = ?")
        args.append(ai)
    if level:
        where.append("j.authenticity_level = ?")
        args.append(level)
    clause = " WHERE " + " AND ".join(where)
    base = (" FROM jobs j JOIN companies c ON c.id=j.company_id" + clause)
    total = con.execute("SELECT count(*)" + base, args).fetchone()[0]
    rows = con.execute(
        "SELECT j.id,j.title,c.name company,j.location,j.remote,j.salary_min,j.salary_max,"
        "j.currency,j.ai_relevance,j.ai_reason,j.liveness,j.liveness_reason,j.posted_at,"
        "j.authenticity_score,j.authenticity_level,j.status,"
        "j.first_seen,j.last_seen,j.url,j.source" + base +
        " ORDER BY COALESCE(j.posted_at,'') DESC, j.first_seen DESC LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "since": since,
            "items": [dict(r) for r in rows]}


class Handler(BaseHTTPRequestHandler):
    server_version = "AIJobs/0.3"

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path, query = u.path, urllib.parse.parse_qs(u.query)
        since = (query.get("since", [""])[0] or "").strip() or None
        try:
            if path == "/api/stats":
                con = connect()
                return self.send_json(stats(con, since))
            if path == "/api/jobs":
                con = connect()
                return self.send_json(jobs(
                    con, (query.get("q", [""])[0] or "").strip(),
                    (query.get("company", [""])[0] or "").strip(),
                    (query.get("ai", [""])[0] or "").strip(),
                    (query.get("level", [""])[0] or "").strip(),
                    (query.get("status", [""])[0] or "open").strip(),
                    min(100, max(1, int(query.get("limit", ["50"])[0]))),
                    max(0, int(query.get("offset", ["0"])[0])), since))
            if path.startswith("/api/jobs/"):
                con = connect()
                jid = path.split("/")[-1]
                row = con.execute(
                    "SELECT j.*,c.name company FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?",
                    (jid,)).fetchone()
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                snaps = [dict(r) for r in con.execute(
                    "SELECT seen_at,status,location FROM job_snapshots WHERE job_id=? ORDER BY seen_at DESC LIMIT 10", (jid,))]
                changes = [dict(r) for r in con.execute(
                    "SELECT change_type,changed_at,note FROM job_changes WHERE job_id=? ORDER BY changed_at DESC LIMIT 10", (jid,))]
                return self.send_json({"job": dict(row), "snapshots": snaps, "changes": changes})
            if path == "/api/signals":
                con = connect()
                try:
                    items = list_signals(
                        con, review=query.get("review", ["approved"])[0],
                        since=since, until=query.get("until", [None])[0],
                        category=query.get("category", [None])[0],
                        limit=query.get("limit", ["50"])[0])
                except ValueError as error:
                    return self.send_json({"error": str(error)}, 400)
                finally:
                    con.close()
                return self.send_json({"items": items})
            if path == "/api/report":
                con = connect()
                s = stats(con, since)
                return self.send_json({
                    "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                    "window_since": s["window_since"],
                    "total": s["ai_total"], "open": s["open"], "build": s["build"],
                    "adjacent": s["adjacent"], "verified": s["verified"],
                    "pending": s["ai_total"] - s["verified"], "excluded": s["excluded"],
                    "closed": s["closed"], "merged": s["merged"], "today_new": s["today_new"],
                    "companies": s["companies"],
                    "top_companies": s["top_companies"][:5],
                    "signals": len(list_signals(con, review="approved", limit=200)),
                    "signal_candidates": len(list_signals(con, review="pending", limit=200)),
                    "note": "在招岗位按页面窗口统计；今日新增按北京时间统计窗口内入库事件。"
                            "链接核验通过仅代表最近一次 URL 核验通过，不保证当前仍可投递；剔除、合并、关闭及长期未见为库内累计。",
                })
            if path == "/api/sources":
                con = connect()
                rows = con.execute(
                    "SELECT c.name,c.ats_type,count(j.id) jobs,max(j.last_seen) last_seen,"
                    "sum(CASE WHEN j.authenticity_level='已验证' AND j.ai_relevance IN ('build','adjacent') "
                    "THEN 1 ELSE 0 END) verified FROM companies c "
                    "LEFT JOIN jobs j ON j.company_id=c.id AND j.status='open' "
                    f"AND j.ai_relevance IN {AI_TIERS} AND substr(j.posted_at,1,10) >= ? "
                    "GROUP BY c.id ORDER BY c.name",
                    ("1970-01-01" if since == "all" else (since or default_since()),)).fetchall()
                return self.send_json({"items": [dict(r) for r in rows]})
            # static
            if path == "/":
                path = "/index.html"
            target = (WEB / path.lstrip("/")).resolve()
            if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
                return self.send_json({"error": "not found"}, 404)
            ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8"}.get(target.suffix, "application/octet-stream")
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, 500)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"http://{HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()

