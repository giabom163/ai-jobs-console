#!/usr/bin/env python
"""Minimal read-only API + static server for the AI jobs dashboard."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__:
    from .daemon_runtime import file_lock, inspect_record, process_identity, daemon_command, terminate_owned, execution_lock
    from .employment_signals import count_signals, list_signals, storage_status
    from .evidence_contract import APPROVED_EVENT_DISCOVERY_PROVIDERS, APPROVED_EVENT_SOURCE_STRATEGIES
    from .job_geography import is_domestic_only, is_mixed_location, is_unknown_location
    from .job_fields import register_read_view, recent_since
    from .export_console import build as build_payload
    from .job_taxonomy import enrich_job
    from .job_details import source_details, duplicate_records
    from .employment_events import event_payload
    from .employment_trends import programming_job_trend
    from .job_catalog import normalized_filters, select_jobs, summarize_jobs
    from .ai_reviewer import execute_auto_review, check_qwen_alive, ensure_qwen_running, get_review_progress, review_single_job
    from .backup_manager import ensure_backup, prune_backups, get_backup_stats
else:
    from daemon_runtime import file_lock, inspect_record, process_identity, daemon_command, terminate_owned, execution_lock
    from employment_signals import count_signals, list_signals, storage_status
    from evidence_contract import APPROVED_EVENT_DISCOVERY_PROVIDERS, APPROVED_EVENT_SOURCE_STRATEGIES
    from job_geography import is_domestic_only, is_mixed_location, is_unknown_location
    from job_fields import register_read_view, recent_since
    from export_console import build as build_payload
    from job_taxonomy import enrich_job
    from job_details import source_details, duplicate_records
    from employment_events import event_payload
    from employment_trends import programming_job_trend
    from job_catalog import normalized_filters, select_jobs, summarize_jobs
    from ai_reviewer import execute_auto_review, check_qwen_alive, ensure_qwen_running, get_review_progress, review_single_job
    from backup_manager import ensure_backup, prune_backups, get_backup_stats

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
_daemon_proc = None
_daemon_interval_min = 30
_daemon_started_at = None
PIPELINE_LOG_FILE = Path.home() / '.codex' / 'documents' / 'ai-jobs-validation-20261102' / 'pipeline-live.log'
DAEMON_RUN_FILE = ROOT / '.daemon-run.json'


def _is_pid_alive(pid: int) -> bool:
    # 旧调用方用保守值；身份未知不得解释为已退出。
    return process_identity(pid)['state'] != 'dead'


def _sync_daemon_state() -> dict:
    state = inspect_record(DAEMON_RUN_FILE, ROOT)
    if not state['running'] and not state['blocked']:
        try:
            with file_lock(DAEMON_RUN_FILE.with_suffix('.lease.lock')):
                pass
        except OSError:
            state.update(blocked=True, status='identity_unconfirmed', error='daemon lease is held')
        if _daemon_proc is not None and _daemon_proc.poll() is None:
            state.update(blocked=True, status='identity_unconfirmed', error='live child has no confirmed ledger')
    if state['running']:
        state['resumed'] = not (_daemon_proc is not None and _daemon_proc.pid == state['pid'] and _daemon_proc.poll() is None)
    return state


def _start_managed_daemon(interval_min=30, dry_run=False, offline=False):
    global _daemon_proc, _daemon_interval_min, _daemon_started_at
    if not offline:
        return {'error': 'online daemon runs require a finite explicit task plan; the admin endpoint does not accept one', 'status': 'plan_required'}, 409
    dry_run = dry_run and not offline
    state = _sync_daemon_state()
    if state['blocked']:
        return {'error': state['error'], 'status': 'identity_unconfirmed'}, 409
    if state['running']:
        return {'error': 'daemon is already running', 'pid': state['pid']}, 409
    if _pipeline_proc is not None and _pipeline_proc.poll() is None:
        return {'error': 'single pipeline task is currently running, cannot start daemon'}, 409
    try:
        with file_lock(execution_lock(ROOT, DAEMON_RUN_FILE, offline)):
            pass
    except OSError:
        return {'error': 'pipeline execution lease is held or unavailable'}, 409
    # 调用方持有control锁；清理只限经确认死亡且lease空闲的旧账本。
    with file_lock(DAEMON_RUN_FILE.with_suffix('.lease.lock')):
        if state.get('stale'):
            DAEMON_RUN_FILE.unlink()
    run_id = str(uuid.uuid4())
    cmd = daemon_command(ROOT.resolve(), DAEMON_RUN_FILE.resolve(), run_id, interval_min, offline, dry_run)
    PIPELINE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LOG_FILE.open('a', encoding='utf-8', buffering=1) as log_fh:
        env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        _daemon_proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
                                        env=env, start_new_session=(os.name != 'nt'))
    child = _daemon_proc
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            observed = inspect_record(DAEMON_RUN_FILE, ROOT)
            if observed['running'] and observed['record']['run_id'] == run_id and observed['pid'] == child.pid:
                _daemon_interval_min = interval_min
                _daemon_started_at = observed['started_at']
                return {'status': 'started', 'pid': child.pid, 'interval_min': interval_min,
                        'started_at': _daemon_started_at, 'offline': offline, 'dry_run': dry_run}, 200
            if child.poll() is not None:
                return {'error': 'managed daemon exited before identity confirmation', 'status': 'startup_failed',
                        'exit_code': child.returncode}, 500
            time.sleep(0.15)
        raise TimeoutError('daemon identity publication timed out')
    except Exception as exc:
        # Popen原生句柄只指向本次自建子进程，不接管账本中的未知PID。
        try:
            child.terminate()
            child.wait(timeout=5)
        except Exception:
            return {'error': str(exc), 'status': 'startup_cleanup_unconfirmed'}, 500
        return {'error': str(exc), 'status': 'startup_failed'}, 500


def _stop_managed_daemon():
    global _daemon_proc
    state = _sync_daemon_state()
    if state['blocked']:
        return {'error': state['error'], 'status': 'identity_unconfirmed'}, 409
    if not state['running']:
        return {'error': 'daemon is not running'}, 400
    record = state['record']
    try:
        # 再读OS身份；随后操作GUID命名Job，不对账本PID执行taskkill。
        confirmed = inspect_record(DAEMON_RUN_FILE, ROOT)
        if not confirmed['running'] or confirmed.get('record') != record:
            return {'error': 'daemon identity changed before stop', 'status': 'identity_unconfirmed'}, 409
        terminate_owned(record)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            observed = process_identity(record['pid'])
            if observed['state'] == 'dead':
                with file_lock(DAEMON_RUN_FILE.with_suffix('.lease.lock')):
                    current = json.loads(DAEMON_RUN_FILE.read_text(encoding='utf-8'))
                    if current != record:
                        return {'error': 'ledger changed during stop', 'status': 'termination_unconfirmed'}, 500
                    DAEMON_RUN_FILE.unlink()
                if _daemon_proc is not None:
                    _daemon_proc.wait(timeout=2)
                _daemon_proc = None
                return {'status': 'stopped', 'stopped_pid': record['pid']}, 200
            if observed['state'] != 'alive' or any(observed.get(k) != record[k] for k in ('created', 'exe', 'command')):
                break
            time.sleep(0.15)
    except Exception as exc:
        return {'error': str(exc), 'status': 'termination_unconfirmed'}, 500
    return {'error': 'daemon termination not confirmed', 'status': 'termination_unconfirmed'}, 500


def _trigger_managed_pipeline(dry_run=False, offline=False):
    global _pipeline_proc
    if not offline:
        return {'error': 'online pipeline runs require a finite explicit task plan; the admin endpoint does not accept one', 'status': 'plan_required'}, 409
    if _pipeline_proc is not None and _pipeline_proc.poll() is None:
        return {'error': 'pipeline is already running', 'pid': _pipeline_proc.pid}, 409
    state = _sync_daemon_state()
    if state['blocked']:
        return {'error': state['error'], 'status': 'identity_unconfirmed'}, 409
    if state['running']:
        return {'error': 'daemon is currently running, cannot trigger concurrent pipeline', 'pid': state['pid']}, 409
    try:
        with file_lock(execution_lock(ROOT, DAEMON_RUN_FILE, offline)):
            pass
    except OSError:
        return {'error': 'pipeline execution lease is held or unavailable'}, 409
    PIPELINE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, '-u', '-B', str(ROOT / 'tools/pipeline_daemon.py'), '--once']
    if offline:
        cmd.append('--offline')
    elif dry_run:
        cmd.append('--dry-run')
    with PIPELINE_LOG_FILE.open('w', encoding='utf-8', buffering=1) as log_fh:
        _pipeline_proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8', PYTHONUTF8='1'))
    return {'status': 'dispatched', 'pid': _pipeline_proc.pid, 'started_at': datetime.now(TZ).isoformat(),
            'dry_run': dry_run, 'offline': offline}, 200


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



def admin_reviews(q=None, source=None, limit=20, offset=0, status="review"):
    con = connect()
    try:
        valid_status = status if status in ("review", "excluded", "open") else "review"
        where = ["j.status = ?"]
        params = [valid_status]
        if q:
            where.append("(j.title LIKE ? OR c.name LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if source:
            where.append("j.source = ?")
            params.append(source)

        where_clause = " AND ".join(where)
        cur = con.cursor()
        cur.execute(f"SELECT count(*) FROM jobs j LEFT JOIN companies c ON j.company_id = c.id WHERE {where_clause}", params)
        total = cur.fetchone()[0]

        sql = f"""
            SELECT j.id, j.title, c.name as company, j.location, j.salary_min, j.salary_max,
                   j.currency, j.source, j.ai_relevance, j.ai_reason, j.url, j.first_seen
            FROM jobs j
            LEFT JOIN companies c ON j.company_id = c.id
            WHERE {where_clause}
            ORDER BY j.first_seen DESC, j.id DESC
            LIMIT ? OFFSET ?
        """
        cur.execute(sql, params + [limit, offset])
        items = []
        for r in cur.fetchall():
            sal = ""
            if r["salary_min"] or r["salary_max"]:
                sal = f"{r['salary_min'] or ''}-{r['salary_max'] or ''}"
            items.append({
                "id": r["id"],
                "title": r["title"],
                "company": r["company"] or "未知企业",
                "location": r["location"] or "全国",
                "salary": sal,
                "source": r["source"],
                "ai_relevance": r["ai_relevance"],
                "ai_reason": r["ai_reason"] or "",
                "url": r["url"] or "",
                "first_seen": r["first_seen"] or ""
            })
        return {"total": total, "limit": limit, "offset": offset, "items": items}
    finally:
        con.close()


def admin_review_action(job_ids, action):
    global _payload_cache
    if action not in ('approve', 'approve_all', 'reject', 'restore'):
        return {'error': 'unsupported review action'}, 400
    if not isinstance(job_ids, list) or len(job_ids) > 200 or not all(isinstance(x, str) and x for x in job_ids):
        return {'error': 'bounded job_ids list required'}, 400
    now_iso = datetime.now(timezone.utc).isoformat()
    backup_file = ensure_backup(f"review-{action}", target_db=DB)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        if action == "approve_all":
            # 严格境内门禁与 AI 分类门禁：仅放行明确境内地点且属于 build/adjacent 的岗位 (R3)
            cur.execute("SELECT id, location, ai_relevance, status, content_hash FROM jobs WHERE status = 'review'")
            candidates = cur.fetchall()
            valid_ids = [
                r for r in candidates
                if is_domestic_only(r["location"] or "") and r["ai_relevance"] in ("build", "adjacent")
            ]
            affected = 0
            if valid_ids:
                for candidate in valid_ids:
                    jid = candidate["id"]
                    cur.execute(
                        "UPDATE jobs SET status = 'open' WHERE id = ? AND status = 'review' "
                        "AND content_hash = ? AND location = ? AND ai_relevance IN ('build','adjacent')",
                        (jid, candidate["content_hash"], candidate["location"])
                    )
                    if cur.rowcount > 0:
                        affected += 1
                        cur.execute(
                            "INSERT INTO job_changes(job_id, change_type, changed_at, note) VALUES(?,?,?,?)",
                            (jid, "status_change", now_iso, "manual_approve_all:domestic_and_relevance_verified")
                        )
            con.commit()
            skipped = len(candidates) - affected

        else:
            if not job_ids or not isinstance(job_ids, list):
                return {"error": "job_ids must be a non-empty list"}, 400
            if action not in ("approve", "reject", "restore"):
                return {"error": "action must be 'approve', 'reject' or 'approve_all'"}, 400

            placeholders = ",".join("?" for _ in job_ids)
            cur.execute(f"SELECT id, location, status, ai_relevance, content_hash FROM jobs WHERE id IN ({placeholders})", job_ids)
            target_map = {r["id"]: r for r in cur.fetchall()}

            if action == "approve":
                approved_ids = []
                denied_errors = []
                for jid in job_ids:
                    r = target_map.get(jid)
                    if not r:
                        denied_errors.append(f"{jid}: 未找到对应岗位记录")
                    elif r["status"] != "review":
                        denied_errors.append(f"{jid}: 当前状态为 {r['status']} (非预期待审 review，可能已被并发修改)")
                    elif not is_domestic_only(r["location"] or ""):
                        denied_errors.append(f"{jid}: 工作地点 {r['location']} 非明确境内工作地点，门禁拒绝放行")
                    elif r["ai_relevance"] not in ("build", "adjacent"):
                        denied_errors.append(f"{jid}: 岗位AI分类为 {r['ai_relevance']} (非 build/adjacent)，门禁拒绝放行")
                    else:
                        approved_ids.append(jid)

                # 整批严格拒绝策略：若存在任何不合规或并发冲突项，直接返回 400 并详列原因，绝不假装成功 (R3)
                if denied_errors:
                    return {
                        "error": "审批请求被安全门禁拦截: 存在不满足放行条件的岗位",
                        "denied_errors": denied_errors,
                        "valid_candidates_count": len(approved_ids)
                    }, 400

                affected = 0
                conflict_count = 0
                for jid in approved_ids:
                    r_item = target_map[jid]
                    cur.execute(
                        "UPDATE jobs SET status = 'open' WHERE id = ? AND status = 'review' AND content_hash = ? AND location = ?",
                        (jid, r_item["content_hash"], r_item["location"])
                    )
                    if cur.rowcount > 0:
                        affected += 1
                        cur.execute(
                            "INSERT INTO job_changes(job_id, change_type, changed_at, note) VALUES(?,?,?,?)",
                            (jid, "status_change", now_iso, "manual_approve:domestic_verified")
                        )
                    else:
                        conflict_count += 1
                        cur.execute(
                            "INSERT INTO job_changes(job_id, change_type, changed_at, note) VALUES(?,?,?,?)",
                            (jid, "review_conflict_skipped", now_iso, "审批期间记录内容/地点发生并发变化，放弃放行")
                        )
                con.commit()

            elif action == "restore":
                affected = 0
                for jid in job_ids:
                    cur.execute("UPDATE jobs SET status = 'review', ai_reason = 'restored_to_review' WHERE id = ?", (jid,))
                    if cur.rowcount > 0:
                        affected += 1
                        cur.execute(
                            "INSERT INTO job_changes(job_id, change_type, changed_at, note) VALUES(?,?,?,?)",
                            (jid, "status_change", now_iso, "manual_restore_to_review")
                        )
                con.commit()

            else:  # reject
                affected = 0
                for jid in job_ids:
                    cur.execute("UPDATE jobs SET status = 'excluded', ai_reason = 'manual_rejected' WHERE id = ? AND status = 'review'", (jid,))
                    if cur.rowcount > 0:
                        affected += 1
                        cur.execute(
                            "INSERT INTO job_changes(job_id, change_type, changed_at, note) VALUES(?,?,?,?)",
                            (jid, "status_change", now_iso, "manual_reject")
                        )
                con.commit()

    finally:
        con.close()

    with _payload_lock:
        _payload_cache = None

    if os.environ.get("AI_JOBS_TESTING") != "1":
        try:
            py = sys.executable
            build_py = ROOT / "tools" / "build_site.py"
            subprocess.Popen([py, "-B", str(build_py)], cwd=str(ROOT))
        except Exception:
            pass

    return {
        "status": "success",
        "action": action,
        "affected": affected,
        "backup": backup_file.name,
        **({"skipped_unqualified": skipped} if action == "approve_all" else {})
    }, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "AIJobs/0.3"

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        origin = self.headers.get('Origin', '')
        self.send_response(204)
        if origin and any(h in origin for h in ('127.0.0.1', 'localhost')):
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        global _pipeline_proc, _daemon_proc, _daemon_interval_min, _daemon_started_at, _payload_cache
        origin = self.headers.get('Origin', '')
        if origin:
            parsed_origin = urllib.parse.urlsplit(origin)
            if parsed_origin.hostname not in ('127.0.0.1', 'localhost'):
                return self.send_json({'error': 'Forbidden: cross-origin requests are rejected'}, 403)
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        body_bytes = b""
        try:
            content_len = int(self.headers.get('Content-Length', 0))
            if content_len > 0:
                body_bytes = self.rfile.read(content_len)
        except Exception:
            pass
        if path == '/api/admin/backups/prune':
            try:
                res = prune_backups(keep_count=10, max_age_days=3, dry_run=True)
                return self.send_json(res, 200)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 500)

        if path == '/api/admin/qwen/start':
            try:
                return self.send_json({'error': 'automatic model startup disabled; explicitly start it outside this tool', 'status': 'not_ready'}, 409)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 500)

        if path == '/api/admin/signals/search':
            try:
                provider = APPROVED_EVENT_DISCOVERY_PROVIDERS.get('deepseek-direct-search-v1')
                if not provider or provider.get('trigger') != 'operator_click' or provider.get('automatic_source_fetch'):
                    return self.send_json({'error': 'event search provider is not approved for operator-triggered discovery'}, 409)
                req = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                query = str(req.get('query') or '').strip()
                since, until = str(req.get('since') or ''), str(req.get('until') or '')
                limit = req.get('limit', 3)
                if not query or len(query) > 80 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', since) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', until) or since >= until or type(limit) is not int or not 1 <= limit <= provider['max_results']:
                    return self.send_json({'error': 'query(1..80), valid date range, and limit 1..3 required'}, 400)
                script = ROOT / 'tools' / 'deepseek_discovery.py'
                command = [sys.executable, '-B', str(script), '--query', query + ' 中国大陆 官方公告 原文', '--since', since, '--until', until, '--limit', str(limit), '--write-db', '--allow-paid']
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
                return self.send_json({'status': 'completed' if completed.returncode == 0 else 'failed', 'exit_code': completed.returncode, 'stdout': completed.stdout[-6000:], 'stderr': completed.stderr[-3000:], 'automatic_approval': False}, 200 if completed.returncode == 0 else 502)
            except subprocess.TimeoutExpired:
                return self.send_json({'error': 'search timed out; inspect run evidence before retrying'}, 504)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 500)

        if path == '/api/admin/signals/intake':
            try:
                req = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                url = str(req.get('source_url') or '').strip()
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                    return self.send_json({'error': 'source_url must be an HTTP(S) URL without embedded credentials'}, 400)
                title = str(req.get('title') or '').strip()
                content = str(req.get('content') or '').strip()
                reviewer = str(req.get('reviewer') or '').strip()
                category = str(req.get('event_type') or '')
                categories = {'hiring', 'layoff', 'shrink', 'freeze', 'reorg', 'other'}
                if not title or not content or len(title) > 240 or len(content) > 20000 or not reviewer or category not in categories:
                    return self.send_json({'error': 'title, original text, reviewer, and supported event type are required'}, 400)
                for key in ('published_at', 'event_date'):
                    value = req.get(key)
                    if value:
                        try:
                            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)): raise ValueError()
                            datetime.strptime(str(value), '%Y-%m-%d')
                        except ValueError:
                            return self.send_json({'error': key + ' must be a valid YYYY-MM-DD date'}, 400)
                import hashlib
                from tools.ingestion_contract import begin_batch, finish_batch, abort_batch
                stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                doc_id = 'manual-' + hashlib.sha256(url.encode()).hexdigest()[:24]
                digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
                evidence = {'manual_submission': True, 'entity': str(req.get('entity') or '').strip(),
                    'source_name': str(req.get('source_name') or parsed.hostname).strip(),
                    'submitted_by': reviewer, 'received_at': stamp,
                    'claimed_published_at': req.get('published_at') or None, 'event_date': req.get('event_date') or None,
                    'geography_evidence': str(req.get('geography_evidence') or '').strip() or None,
                    'source_verified': False, 'source_observation': None}
                con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; batch = None
                try:
                    batch = begin_batch(con, 'manual_event_intake', digest)
                    batch['document_ids'] = [doc_id]
                    con.execute('''INSERT INTO discovery_documents
                        (id,url,title,source,published_at,date_basis,first_seen,last_seen,content,content_sha256,query,category,ai_scope,claim_status,evidence_json,review_status,liveness,last_verified_at)
                        VALUES(?,?,?,?,?,'manual_submission',?,?,?,?,?,?,'pending','unverified',?,'pending','unknown',NULL)
                        ON CONFLICT(url) DO UPDATE SET title=excluded.title,source=excluded.source,published_at=excluded.published_at,date_basis='manual_submission',first_seen=excluded.first_seen,last_seen=excluded.last_seen,content=excluded.content,content_sha256=excluded.content_sha256,query=excluded.query,category=excluded.category,ai_scope='pending',claim_status='unverified',evidence_json=excluded.evidence_json,review_status='pending',liveness='unknown',last_verified_at=NULL''',
                        (doc_id,url,title,parsed.hostname,req.get('published_at') or None,stamp,stamp,content,digest,'manual-intake',category,json.dumps(evidence,ensure_ascii=False)))
                    finish_batch(con, batch); con.commit()
                except BaseException as error:
                    if batch: abort_batch(con, batch, error)
                    raise
                finally:
                    con.close()
                return self.send_json({'status':'pending_review','id':doc_id,'content_sha256':digest,'received_at':stamp,'published_at':req.get('published_at') or None,'event_date':req.get('event_date') or None,'source_verified':False}, 201)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 400)

        if path == '/api/admin/signals/fetch':
            try:
                req = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                doc_id = str(req.get('id') or '').strip()
                con = connect()
                try:
                    row = con.execute('SELECT url FROM discovery_documents WHERE id=? AND review_status=\'pending\'', (doc_id,)).fetchone()
                    if row is None:
                        return self.send_json({'error': 'pending candidate not found'}, 404)
                    source_url = row['url']
                finally:
                    con.close()
                host = urllib.parse.urlsplit(source_url).hostname
                approved = any(host in item.get('hosts', ()) for item in APPROVED_EVENT_SOURCE_STRATEGIES.values())
                if not approved:
                    return self.send_json({'error': 'source host is not approved for original-page verification'}, 403)
                social_hosts = {'www.v2ex.com', 'www.bilibili.com', 'www.douyin.com', 'www.youtube.com'}
                if host in social_hosts:
                    script = ROOT / 'tools' / 'fetch_social_signal.py'
                    command = [sys.executable, '-B', str(script), source_url, '--document-id', doc_id, '--db', str(DB)]
                    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
                    return self.send_json({'status': 'completed' if completed.returncode == 0 else 'failed', 'exit_code': completed.returncode, 'stdout': completed.stdout[-4000:], 'stderr': completed.stderr[-2500:]}, 200 if completed.returncode == 0 else 502)
                script = ROOT / 'tools' / 'deepseek_discovery.py'
                command = [sys.executable, '-B', str(script), '--fetch-url', source_url, '--limit', '1', '--max-requests', '1', '--write-db', '--allow-paid']
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
                return self.send_json({'status': 'completed' if completed.returncode == 0 else 'failed', 'exit_code': completed.returncode, 'stdout': completed.stdout[-4000:], 'stderr': completed.stderr[-2500:]}, 200 if completed.returncode == 0 else 502)
            except subprocess.TimeoutExpired:
                return self.send_json({'error': 'source fetch timed out; inspect run evidence before retrying'}, 504)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 500)

        if path == '/api/admin/signals/review':
            try:
                req = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                from tools.review_signals import apply_decisions
                result = apply_decisions(DB, req, apply=True)
                with _payload_lock:
                    _payload_cache = None
                return self.send_json(result, 200)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 400)

        if path == '/api/admin/reviews/auto_review':
            try:
                req_data = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                mode = req_data.get('mode', 'qwen')
                limit = req_data.get('limit', 3)
                if type(limit) is not int or not 1 <= limit <= 200:
                    return self.send_json({'error': 'limit must be an integer in 1..200'}, 400)
                api_base = req_data.get('api_base', '')
                api_key = req_data.get('api_key', '')
                model = req_data.get('model', '')
                auto_approve = req_data.get('auto_approve', False)
                if type(auto_approve) is not bool or auto_approve:
                    return self.send_json({'error': 'model recommendations cannot automatically publish jobs'}, 400)
                job_ids = req_data.get('job_ids')
                res = execute_auto_review(mode=mode, limit=limit, api_base=api_base, api_key=api_key, model=model, auto_approve=auto_approve, job_ids=job_ids)
                with _payload_lock:
                    _payload_cache = None
                return self.send_json(res, 200)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 500)

        if path == '/api/admin/reviews/single_ai_check':
            try:
                req_data = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                job_id = req_data.get('job_id', '')
                if not job_id:
                    return self.send_json({'error': 'job_id is required'}, 400)
                res = review_single_job(job_id)
                return self.send_json(res, 200)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 500)

        if path == '/api/admin/reviews/action':
            try:
                req_data = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                res_data, code = admin_review_action(req_data.get('job_ids', []), req_data.get('action', ''))
                return self.send_json(res_data, code)
            except Exception as exc:
                return self.send_json({'error': str(exc)}, 500)

        pipeline_actions = {
            '/api/admin/pipeline/daemon_start',
            '/api/admin/pipeline/daemon_stop',
            '/api/admin/pipeline/trigger',
        }
        if path in pipeline_actions:
            try:
                req_data = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
                dry_run = req_data.get('dry_run', False)
                offline = req_data.get('offline', False)
                if type(dry_run) is not bool or type(offline) is not bool:
                    raise ValueError('offline/dry_run must be boolean')
                interval = max(10, min(1440, int(req_data.get('interval_min', 30))))
            except (ValueError, TypeError, AttributeError) as exc:
                return self.send_json({'error': str(exc)}, 400)
            with _pipeline_lock:
                try:
                    with file_lock(DAEMON_RUN_FILE.with_suffix('.control.lock')):
                        if path.endswith('/daemon_start'):
                            result, code = _start_managed_daemon(interval, dry_run, offline)
                        elif path.endswith('/daemon_stop'):
                            result, code = _stop_managed_daemon()
                        else:
                            result, code = _trigger_managed_pipeline(dry_run, offline)
                except OSError as exc:
                    result, code = {'error': str(exc), 'status': 'control_lock_unavailable'}, 409
                except Exception as exc:
                    result, code = {'error': str(exc), 'status': 'pipeline_operation_failed'}, 500
            return self.send_json(result, code)
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
            if path == '/api/health':
                server_path = Path(__file__).resolve()
                server_sha256 = hashlib.sha256(server_path.read_bytes()).hexdigest()
                return self.send_json({'service': 'ai-employment-monitor', 'workspace': str(ROOT.resolve()), 'pid': os.getpid(), 'server_sha256': server_sha256})
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
                from tools.request_policy import DEFAULT_BUDGET
                from tools.task_plan import SOURCE_CAPABILITIES
                sched_state = DEFAULT_BUDGET.scheduler.snapshot()
                d_state = _sync_daemon_state()
                pipeline_proc_running = (_pipeline_proc is not None and _pipeline_proc.poll() is None)
                daemon_proc_running = d_state['running']
                any_pipeline_running = pipeline_proc_running or daemon_proc_running

                cursor_file = ROOT / '.collection-cursors.json'
                cursors_data = None
                if cursor_file.exists():
                    try:
                        cursors_data = json.loads(cursor_file.read_text(encoding='utf-8'))
                    except Exception:
                        pass

                runs_file = ROOT / '.task-plan-runs.json'
                last_plan = None
                if runs_file.exists():
                    try:
                        runs_data = json.loads(runs_file.read_text(encoding='utf-8'))
                        last_plan = runs_data.get('last_plan_outcome')
                    except Exception:
                        pass

                return self.send_json({
                    'total_jobs': total_jobs,
                    'status_counts': counts,
                    'source_stats': source_stats,
                    'scheduler': sched_state,
                    'source_capabilities': SOURCE_CAPABILITIES,
                    'cursors': cursors_data,
                    'last_plan_outcome': last_plan,
                    'requires_explicit_plan': True,
                    'pipeline_running': any_pipeline_running,
                    'pipeline_task_running': pipeline_proc_running,
                    'daemon': {
                        'running': daemon_proc_running,
                        'pid': d_state['pid'],
                        'interval_min': d_state['interval_min'],
                        'started_at': d_state['started_at'],
                        'resumed': d_state.get('resumed', False),
                        'blocked': d_state.get('blocked', False),
                        'status': d_state.get('status'),
                        'error': d_state.get('error')
                    },
                    'backups': get_backup_stats(),
                    'server_time': datetime.now(TZ).isoformat(),
                })
            if path == '/api/admin/qwen/status':
                return self.send_json({
                    'alive': check_qwen_alive(),
                    'port': 8080,
                    'model': 'Qwen3.5-9B-DARK-ROAST',
                    'endpoint': 'http://127.0.0.1:8080/v1'
                })
            if path == '/api/admin/reviews/progress':
                return self.send_json(get_review_progress())

            if path == '/api/admin/signals/candidates':
                con = connect()
                try:
                    rows = con.execute("SELECT id,url,title,source,published_at,date_basis,first_seen,last_seen,content,content_sha256,category,claim_status,evidence_json FROM discovery_documents WHERE review_status='pending' ORDER BY last_seen DESC LIMIT 100").fetchall()
                    items = []
                    for row in rows:
                        item = dict(row)
                        item['evidence'] = json.loads(item.pop('evidence_json'))
                        items.append(item)
                    return self.send_json({'items': items, 'count': len(items), 'automatic_search_enabled': bool(os.environ.get('DEEPSEEK_API_KEY')), 'approved_source_strategies': ['cn-official-employment-notices-v1']})
                finally:
                    con.close()

            if path == '/api/admin/reviews':
                q_filter = (query.get('q', [''])[0] or '').strip() or None
                src_filter = (query.get('source', [''])[0] or '').strip() or None
                status_filter = (query.get('status', ['review'])[0] or 'review').strip()
                try:
                    limit = min(max(1, int(query.get('limit', [20])[0])), 100)
                    offset = max(0, int(query.get('offset', [0])[0]))
                except ValueError:
                    limit, offset = 20, 0
                return self.send_json(admin_reviews(q_filter, src_filter, limit, offset, status=status_filter))

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
                requested_review = query.get("review", ["approved"])[0]
                if requested_review != "approved":
                    return self.send_json({"error": "Forbidden: pending signals are restricted to local admin review", "items": []}, 403)
                con = connect()
                try:
                    items = list_signals(
                        con, review="approved",
                        since=since, until=query.get("until", [None])[0],
                        category=query.get("category", [None])[0],
                        limit=query.get("limit", ["50"])[0])
                    storage = storage_status(con)
                except ValueError as error:
                    return self.send_json({"error": str(error)}, 400)
                finally:
                    con.close()
                return self.send_json({"items": items, "storage": storage})
            if path == "/api/report":
                con = connect()
                try:
                    s = stats(con, since, query)
                    approved_signals_count = count_signals(
                        con, review="approved", since=since,
                        until=query.get("until", [None])[0])
                    signal_candidates_count = count_signals(
                        con, review="pending", since=since,
                        until=query.get("until", [None])[0])
                    event_query = dict(query)
                    event_until = query.get('until', [None])[0]
                    if event_until:
                        try:
                            event_query['until'] = [(datetime.strptime(event_until, '%Y-%m-%d').date()-timedelta(days=1)).isoformat()]
                        except ValueError:
                            return self.send_json({'error': 'invalid date bound'}, 400)
                    event_result = event_payload(event_query)
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
                        "signals": approved_signals_count,
                        "signal_candidates": signal_candidates_count,
                        "events": event_result['summary']['total'],
                        "event_items": event_result['items'],
                        "event_ai_relation_labels": event_result['ai_relation_labels'],
                        "programming_trend": programming_job_trend(con),
                        "event_window": f"{since or '起始未限定'} 至 {event_until or '结束未限定'}（结束日不含）",
                        "signal_source_status": "手动检索已启用；自动搜索仅由本地操作员显式触发；无后台定时请求",
                        "signal_note": event_result['coverage_note'],
                        "note": "岗位记录按首次发现时间筛选，来源发布时间独立保存；今日新增按北京时间统计当前筛选内首次入库的唯一岗位。"
                                "链接核验通过仅代表最近一次 URL 核验通过，不保证当前仍可投递；剔除、合并、关闭及长期未见为库内累计。"
                                "2026-10-22 记录的 3,020 条新增岗位变化属于存量数据初始化导入，来源未逐条核实前不代表市场招聘峰值。",
                    })
                finally:
                    con.close()
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

