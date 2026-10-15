"""M1 evidence storage. Standard library, bounded manual batches, no model calls."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit
import uuid

RULE = "exact-text-v1"
SCHEMA = "m1-v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def normalized_text(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value))).strip()


def publication(value):
    if value is None or value == "":
        return None, "missing"
    if not isinstance(value, str):
        raise ValueError("published_at 必须为字符串或 null")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None, "invalid"
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None, "timezone_missing"
    try:
        return dt.astimezone(timezone.utc).isoformat(), "valid"
    except (ValueError, OverflowError):
        return None, "invalid"


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("url 必须为不超过 4000 字符的字符串")
    try:
        parts = urlsplit(value.strip())
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise ValueError("url 格式错误") from exc
    if parts.scheme.lower() not in ("http", "https") or not host or parts.username or parts.password:
        raise ValueError("记录 url 必须为不含认证信息的 http(s) URL")
    host = host.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port and not ((parts.scheme.lower() == "http" and port == 80) or
                     (parts.scheme.lower() == "https" and port == 443)):
        host += ":" + str(port)
    # Preserve query and path: do not guess that tracking-like parameters are disposable.
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS feeds(id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT UNIQUE NOT NULL, added_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS keywords(value TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS canonical_records(
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, dedup_rule TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS collection_runs(
        id TEXT PRIMARY KEY, feed_id INTEGER REFERENCES feeds(id), kind TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','interrupted')),
        started_at TEXT NOT NULL, finished_at TEXT, input_count INTEGER NOT NULL DEFAULT 0,
        added INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
        error TEXT, input_sha256 TEXT, limit_n INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS records(
        id TEXT PRIMARY KEY, feed_id INTEGER NOT NULL REFERENCES feeds(id), source_id TEXT,
        title TEXT NOT NULL, summary TEXT NOT NULL, url TEXT NOT NULL, canonical_url TEXT NOT NULL,
        content_sha256 TEXT NOT NULL, raw_sha256 TEXT NOT NULL, raw_path TEXT NOT NULL,
        published_at TEXT, published_raw TEXT, fetched_at TEXT NOT NULL,
        time_quality TEXT NOT NULL CHECK(time_quality IN ('valid','missing','invalid','timezone_missing','legacy_unverified')),
        canonical_id TEXT NOT NULL REFERENCES canonical_records(id), dedup_reason TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES collection_runs(id), sentiment TEXT NOT NULL,
        UNIQUE(feed_id, raw_sha256));
      CREATE INDEX IF NOT EXISTS records_url ON records(canonical_url);
      CREATE INDEX IF NOT EXISTS records_content ON records(content_sha256);
      CREATE INDEX IF NOT EXISTS records_published ON records(published_at);
      CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    return db


def start_run(db, feed_id, kind, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1–100 的整数")
    if db.execute("SELECT id FROM feeds WHERE id=?", (feed_id,)).fetchone() is None:
        raise ValueError("来源不存在")
    run_id = "run-" + uuid.uuid4().hex
    with db:
        db.execute("INSERT INTO collection_runs(id,feed_id,kind,status,started_at,limit_n) VALUES(?,?,?,'running',?,?)",
                   (run_id, feed_id, kind, now(), limit))
    return run_id


def fail_run(db, run_id, error):
    with db:
        db.execute("UPDATE collection_runs SET status='failed',finished_at=?,error=? WHERE id=? AND status='running'",
                   (now(), str(error)[:1000], run_id))


def save_snapshot(root, payload):
    raw = json_bytes(payload)
    sha = digest(raw)
    relative = Path("snapshots") / (sha + ".json")
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        pass
    if digest(path.read_bytes()) != sha:
        raise OSError("原文快照哈希回读不一致")
    return sha, relative.as_posix()


def import_batch(db, root, run_id, rows, classify):
    run = db.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run["status"] != "running":
        raise ValueError("批次不存在或已结束")
    try:
        if not isinstance(rows, list) or len(rows) > run["limit_n"]:
            raise ValueError("记录必须为数组，且不得超过批次 limit")
        prepared = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("每条记录必须为对象")
            if set(row) - {"title", "summary", "url", "source_id", "published_at", "raw_xml"}:
                raise ValueError("记录含未声明字段")
            title, summary = row.get("title"), row.get("summary", "")
            if not isinstance(title, str) or not isinstance(summary, str) or not normalized_text(title):
                raise ValueError("title 必须为非空文本，summary 必须为文本")
            if len(title) > 10000 or len(summary) > 100000:
                raise ValueError("记录文本超过限制")
            source_id = row.get("source_id")
            if source_id is not None and (not isinstance(source_id, str) or len(source_id) > 2000):
                raise ValueError("source_id 必须为不超过 2000 字符的字符串或 null")
            if "raw_xml" in row and (not isinstance(row["raw_xml"], str) or len(row["raw_xml"]) > 2000000):
                raise ValueError("raw_xml 格式或长度错误")
            url = canonical_url(row.get("url"))
            published_at, quality = publication(row.get("published_at"))
            title, summary = normalized_text(title), normalized_text(summary)
            content_hash = digest(json_bytes([title, summary]))
            prepared.append((row, title, summary, url, content_hash, published_at, quality, source_id))
        input_hash = digest(json_bytes(rows))
        added = duplicates = 0
        # Snapshots are immutable content-addressed evidence. A failed transaction may leave
        # an unreferenced snapshot; no destructive cleanup or partial record commit occurs.
        with db:
            for row, title, summary, url, content_hash, published_at, quality, source_id in prepared:
                raw_hash, raw_path = save_snapshot(root, row)
                if db.execute("SELECT id FROM records WHERE feed_id=? AND raw_sha256=?",
                              (run["feed_id"], raw_hash)).fetchone():
                    duplicates += 1
                    continue
                # Exact text clusters only. Same URL with edited text remains a separate
                # version and is linked by canonical_url rather than merging clusters.
                existing = db.execute("SELECT canonical_id FROM records WHERE content_sha256=? ORDER BY fetched_at,id LIMIT 1",
                                      (content_hash,)).fetchone()
                if existing:
                    canonical_id, reason = existing[0], "exact_text"
                else:
                    canonical_id, reason = "canonical-" + uuid.uuid4().hex, "independent"
                    db.execute("INSERT INTO canonical_records VALUES(?,?,?)", (canonical_id, now(), RULE))
                record_id = "record-" + uuid.uuid4().hex
                db.execute("""INSERT INTO records(id,feed_id,source_id,title,summary,url,canonical_url,
                    content_sha256,raw_sha256,raw_path,published_at,published_raw,fetched_at,time_quality,
                    canonical_id,dedup_reason,run_id,sentiment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (record_id, run["feed_id"], source_id, title, summary, row["url"], url,
                            content_hash, raw_hash, raw_path, published_at, row.get("published_at"), now(),
                            quality, canonical_id, reason, run_id, classify(title + " " + summary)))
                added += 1
            db.execute("""UPDATE collection_runs SET status='succeeded',finished_at=?,input_count=?,
                added=?,duplicates=?,input_sha256=? WHERE id=?""",
                       (now(), len(rows), added, duplicates, input_hash, run_id))
        return dict(db.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone())
    except Exception as exc:
        db.rollback()
        fail_run(db, run_id, exc)
        raise


def recover_runs(db):
    # Called once at startup only; do not interrupt live requests on every connection.
    with db:
        db.execute("UPDATE collection_runs SET status='interrupted',finished_at=?,error='服务上次退出，未自动重试' WHERE status='running'", (now(),))


def data(db):
    feeds = [dict(row) for row in db.execute("""SELECT f.*,
      (SELECT count(*) FROM records r WHERE r.feed_id=f.id) item_count,
      (SELECT max(finished_at) FROM collection_runs c WHERE c.feed_id=f.id AND c.status='succeeded') last_success_at,
      (SELECT id FROM collection_runs c WHERE c.feed_id=f.id ORDER BY started_at DESC,id DESC LIMIT 1) last_run_id
      FROM feeds f ORDER BY f.id DESC""")]
    items = [dict(row) for row in db.execute("""SELECT r.*,r.url link,r.published_at published,f.name feed_name
      FROM records r JOIN feeds f ON f.id=r.feed_id
      ORDER BY r.published_at IS NULL,r.published_at DESC,r.fetched_at DESC,r.id LIMIT 500""")]
    stats = dict(db.execute("""SELECT count(*) record_count,count(DISTINCT canonical_id) canonical_count,
      coalesce(sum(published_at IS NULL),0) unknown_time_count FROM records""").fetchone())
    stats["unknown_time_ratio"] = stats["unknown_time_count"] / stats["record_count"] if stats["record_count"] else None
    legacy = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='items'").fetchone()[0]
    legacy_count = db.execute("SELECT count(*) FROM items").fetchone()[0] if legacy else 0
    return {"schema_version": SCHEMA, "feeds": feeds, "items": items,
            "keywords": [r[0] for r in db.execute("SELECT value FROM keywords ORDER BY value")],
            "stats": stats, "items_limit": 500,
            "legacy_items_pending": legacy_count,
            "coverage": "sample_only", "analysis_status": "unreviewed_dictionary",
            "runs": [dict(r) for r in db.execute("SELECT * FROM collection_runs ORDER BY started_at DESC,id DESC LIMIT 100")]}
