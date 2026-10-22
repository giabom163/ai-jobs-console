#!/usr/bin/env python
"""Minimal read-only API + static server for the AI jobs dashboard."""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"
WEB = ROOT / "web"
HOST, PORT = "127.0.0.1", 8792


def connect():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def stats(con):
    def one(sql, args=()):
        return con.execute(sql, args).fetchone()[0]
    total = one("SELECT count(*) FROM jobs WHERE status='open'")
    return {
        "total": total,
        "open": total,
        "merged": one("SELECT count(*) FROM jobs WHERE status='merged'"),
        "build": one("SELECT count(*) FROM jobs WHERE status='open' AND ai_relevance='build'"),
        "adjacent": one("SELECT count(*) FROM jobs WHERE status='open' AND ai_relevance='adjacent'"),
        "verified": one("SELECT count(*) FROM jobs WHERE status='open' AND authenticity_level='已验证'"),
        "companies": one("SELECT count(*) FROM companies"),
        "today_new": one("SELECT count(*) FROM job_changes WHERE change_type='new' AND date(changed_at)=date('now')"),
        "levels": [dict(r) for r in con.execute(
            "SELECT authenticity_level label,count(*) value FROM jobs WHERE status='open' GROUP BY authenticity_level ORDER BY value DESC")],
        "top_companies": [dict(r) for r in con.execute(
            "SELECT c.name label,count(*) value FROM jobs j JOIN companies c ON c.id=j.company_id "
            "WHERE j.status='open' GROUP BY c.name ORDER BY value DESC LIMIT 8")],
        "top_roles": [dict(r) for r in con.execute(
            "SELECT title label,count(*) value FROM jobs WHERE status='open' GROUP BY title ORDER BY value DESC LIMIT 8")],
        "trend": [dict(r) for r in con.execute(
            "SELECT date(changed_at) day,sum(change_type='new') new_jobs,sum(change_type='closed') closed_jobs "
            "FROM job_changes WHERE date(changed_at) >= date('now','-14 day') GROUP BY day ORDER BY day")],
    }


def jobs(con, q, company, ai, level, status, limit, offset):
    where, args = [], []
    if q:
        where.append("(j.title LIKE ? OR c.name LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if company:
        where.append("c.name = ?")
        args.append(company)
    if ai:
        where.append("j.ai_relevance = ?")
        args.append(ai)
    if level:
        where.append("j.authenticity_level = ?")
        args.append(level)
    if status:
        where.append("j.status = ?")
        args.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    base = (" FROM jobs j JOIN companies c ON c.id=j.company_id" + clause)
    total = con.execute("SELECT count(*)" + base, args).fetchone()[0]
    rows = con.execute(
        "SELECT j.id,j.title,c.name company,j.location,j.remote,j.salary_min,j.salary_max,"
        "j.currency,j.ai_relevance,j.authenticity_score,j.authenticity_level,j.status,"
        "j.first_seen,j.last_seen,j.url,j.source" + base +
        " ORDER BY j.first_seen DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "items": [dict(r) for r in rows]}


class Handler(BaseHTTPRequestHandler):
    server_version = "AIJobs/0.1"

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
        try:
            if path == "/api/stats":
                con = connect()
                return self.send_json(stats(con))
            if path == "/api/jobs":
                con = connect()
                return self.send_json(jobs(
                    con, (query.get("q", [""])[0] or "").strip(),
                    (query.get("company", [""])[0] or "").strip(),
                    (query.get("ai", [""])[0] or "").strip(),
                    (query.get("level", [""])[0] or "").strip(),
                    (query.get("status", [""])[0] or "open").strip(),
                    min(100, max(1, int(query.get("limit", ["50"])[0]))),
                    max(0, int(query.get("offset", ["0"])[0]))))
            if path.startswith("/api/jobs/"):
                con = connect()
                jid = path.split("/")[-1]
                row = con.execute(
                    "SELECT j.*,c.name company FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?",
                    (jid,)).fetchone()
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                snaps = [dict(r) for r in con.execute(
                    "SELECT seen_at,status,http_status,location FROM job_snapshots WHERE job_id=? ORDER BY seen_at DESC LIMIT 10", (jid,))]
                changes = [dict(r) for r in con.execute(
                    "SELECT change_type,changed_at,note FROM job_changes WHERE job_id=? ORDER BY changed_at DESC LIMIT 10", (jid,))]
                return self.send_json({"job": dict(row), "snapshots": snaps, "changes": changes})
            if path == "/api/signals":
                con = connect()
                tag = (query.get("tag", [""])[0] or "").strip()
                sql = "SELECT id,title,url,source,published_at,summary,tag FROM signals"
                args = []
                if tag:
                    sql += " WHERE tag=?"
                    args.append(tag)
                sql += " ORDER BY published_at DESC LIMIT 50"
                return self.send_json({"items": [dict(r) for r in con.execute(sql, args)]})
            if path == "/api/report":
                con = connect()
                s = stats(con)
                return self.send_json({
                    "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                    "total": s["total"], "open": s["open"], "build": s["build"],
                    "verified": s["verified"], "pending": s["total"] - s["verified"], "merged": s["merged"],
                    "today_new": s["today_new"], "companies": s["companies"],
                    "top_companies": s["top_companies"][:5],
                    "signals": con.execute("SELECT count(*) FROM signals").fetchone()[0],
                    "note": "真实性为来源可信度；已验证仅代表 apply URL 当日校验通过，不代表雇主承诺。",
                })
            if path == "/api/sources":
                con = connect()
                rows = con.execute(
                    "SELECT c.name,c.ats_type,count(j.id) jobs,max(j.last_seen) last_seen,"
                    "sum(j.authenticity_level='已验证') verified FROM companies c "
                    "LEFT JOIN jobs j ON j.company_id=c.id GROUP BY c.id ORDER BY c.name").fetchall()
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
