"""采集前只读准备导出（离线 CLI，仅标准库）。

做三件事，全部**只读**，不联网、不调模型、不写业务库：
1. 按当前分类内核重放记录，导出「存储 tier / 模型 opposition / 当前规则」分歧队列；
   队列按 ``record_id + input_hash`` 标识，``resolution_status=pending``（不是人工真值）。
2. 输出机器可读的就绪检查（schema、旧分数、未知热度、缺口）。
3. 只读复用 ``activity.admit`` 给出贴吧 24h 回复增量的观测现状与下一次采样要求；
   不声称已启动真实采样。

约束：
- 必须显式 ``--db`` 与 ``--output-dir``；库不存在直接失败（本身永不联网）；
- SQLite 以 ``mode=ro`` 打开并置 ``PRAGMA query_only=1``，不调用 ``storage.connect``、
  不迁移、不建表、不写 WAL 检查点；
- 输出三个新文件；目标已存在则整体拒绝（不覆盖、不删除旧产物）。

用法：
    python tools/prepare_collection.py --db <只读库> --output-dir <新目录> --limit 3

``--limit`` 是**只读分块/检查保护的批大小**，不是导出上限：全部记录按 id 稳定顺序
用 fetchmany(limit) 扫描，分歧队列不截断；贴吧候选仍最多 3 个。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import activity  # noqa: E402  只读复用贴吧 admit，不启采样
import relevance  # noqa: E402

SCHEMA_VERSION = "precollection-readiness-v1"
QUEUE_SCHEMA = "precollection-queue-v1"
DEFAULT_LIMIT = 3
MAX_LIMIT = 200
QUEUE_FILE = "precollection-queue.json"
READINESS_FILE = "precollection-readiness.json"
SUMMARY_FILE = "precollection-summary.md"
REQUIRED_RECORD_COLUMNS = ("id", "title", "summary", "published_at", "time_quality",
                           "relevance_tier")
OPTIONAL_RECORD_COLUMNS = ("url", "canonical_url", "source_id", "feed_id", "views",
                           "conflict_score", "fetched_at")
TIEBA_CANDIDATE_LIMIT = 3
MODEL_COLUMNS = ("record_id", "model", "sentiment", "opposition", "relevant",
                 "gender_related", "created_at")


class PrepareError(RuntimeError):
    """结构化失败（缺库/缺表/参数错误/输出已存在），调用方转成非零退出码。"""

    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def build_parser():
    parser = argparse.ArgumentParser(
        description="采集前只读准备：分歧队列 + 就绪检查 + 贴吧观测现状（不联网、不写库）")
    parser.add_argument("--db", required=True, help="现有业务库路径（只读打开）")
    parser.add_argument("--output-dir", required=True, help="新输出目录（不得覆盖已有产物）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"只读分块/检查保护的批大小（默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}）；"
                             "全部记录都会按稳定顺序扫描，不是导出上限")
    return parser


def validate_args(args):
    if type(args.limit) is not int or not 1 <= args.limit <= MAX_LIMIT:
        raise PrepareError("bad_limit", f"--limit 必须为 1–{MAX_LIMIT} 的整数")
    db = Path(args.db)
    if not db.is_file():
        raise PrepareError("db_missing", f"库不存在或不是文件：{db}（本工具不建库、不联网）")
    return db


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect_readonly(path):
    uri = "file:" + quote(Path(path).resolve().as_posix(), safe="/:") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=1")
    return db


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(db, table):
    if table not in tables(db):
        return set()
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def input_hash(title, summary):
    payload = json.dumps([title or "", summary or ""], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _int_or_none(value):
    return value if type(value) is int else None


def _model_bit(value):
    """model.opposition 只接受 0/1 整数；其余原值保留但不可比较。"""
    if type(value) is int and value in (0, 1):  # noqa: E721 排除 bool
        return bool(value), True
    return None, False


def model_entry(row, available):
    # LEFT JOIN 未命中时 m.record_id 为 NULL；表不可用时同样为 NULL，但原因不同。
    present = bool(available and row is not None and row.get("model_record_id") is not None)
    if not present:
        return {"present": False, "table_usable": bool(available),
                "source": "no_row" if available else "table_missing",
                "model": None, "sentiment": None, "opposition": None, "opposition_valid": None,
                "opposition_raw": None, "relevant": None, "gender_related": None,
                "created_at": None}
    opposition, opposition_valid = _model_bit(row["opposition"])
    return {"present": True, "table_usable": True, "source": "model_annotations",
            "model": row["model"], "sentiment": row["sentiment"], "opposition": opposition,
            "opposition_valid": opposition_valid, "opposition_raw": row["opposition"],
            "relevant": row["relevant"], "gender_related": row["gender_related"],
            "created_at": row["created_at"]}


def current_disagreements(stored_tier, model, analysis, summary):
    """与**当前规则**的分歧（接口口径），不含旧值之间的原始分歧。"""
    codes = []
    if stored_tier and stored_tier != analysis["tier"]:
        codes.append("stored_tier_vs_current_rule")
    if not model["present"]:
        codes.append("model_annotation_missing")
    else:
        if model["opposition_valid"] and model["opposition"] != analysis["opposition"]:
            codes.append("model_opposition_vs_current_rule")
        if model["relevant"] is not None and bool(model["relevant"]) != analysis["gender_related"]:
            # 历史 relevant 由 gender_related 派生，不能当 opposition 用。
            codes.append("model_relevant_vs_current_gender_related")
        if (model["gender_related"] is not None
                and bool(model["gender_related"]) != analysis["gender_related"]):
            codes.append("model_gender_related_vs_current_rule")
    if not (summary or "").strip():
        codes.append("summary_missing")
    return codes


def legacy_disagreements(stored_tier, legacy_score, model):
    """旧库内部的原始分歧：旧 stored tier vs 旧 model opposition，与当前规则分开统计。"""
    codes = ["legacy_conflict_score_not_recomputed"] if legacy_score is not None and legacy_score > 0 else []
    if legacy_score is not None and legacy_score > 0 and stored_tier != "conflict":
        codes.append("legacy_score_without_conflict_tier")
    if not model["present"]:
        return codes
    if not model["opposition_valid"]:
        codes.append("model_opposition_invalid_type")
        return codes
    if stored_tier and bool(model["opposition"]) != (stored_tier == "conflict"):
        codes.append("legacy_tier_vs_model_opposition")
    return codes


def record_cursor(db):
    """全库稳定顺序游标（按 id）；调用方用 fetchmany 分块，不做 SQL LIMIT。"""
    record_cols = columns(db, "records")
    missing = [name for name in REQUIRED_RECORD_COLUMNS if name not in record_cols]
    if missing:
        raise PrepareError("schema_missing", "records 缺少必需列：" + "、".join(missing))
    model_available = set(MODEL_COLUMNS).issubset(columns(db, "model_annotations"))
    select = ["r.id", "r.title", "r.summary", "r.published_at", "r.time_quality",
              "r.relevance_tier"]
    for name in OPTIONAL_RECORD_COLUMNS:
        select.append(f"r.{name}" if name in record_cols else f"NULL AS {name}")
    has_feeds = "feeds" in tables(db) and "feed_id" in record_cols
    join = "LEFT JOIN feeds f ON f.id=r.feed_id" if has_feeds else ""
    feed = "f.name AS source_name" if has_feeds else "NULL AS source_name"
    model_join = "LEFT JOIN model_annotations m ON m.record_id=r.id" if model_available else ""
    # 显式取 m.record_id：LEFT JOIN 未命中时该列为 NULL，用于区分“表可用但本行无标注”。
    model_select = ("m.record_id AS model_record_id, m.model, m.sentiment, m.opposition, "
                    "m.relevant, m.gender_related, m.created_at" if model_available
                    else "NULL AS model_record_id, NULL AS model, NULL AS sentiment, "
                         "NULL AS opposition, NULL AS relevant, NULL AS gender_related, "
                         "NULL AS created_at")
    sql = (f"SELECT {', '.join(select)}, {feed}, {model_select} FROM records r "
           f"{join} {model_join} ORDER BY r.id")
    return db.execute(sql), model_available


def build_queue(db, chunk_size):
    """全库稳定顺序扫描（按 id）；fetchmany(chunk_size) 只是读块保护，不是导出上限。"""
    cursor, model_available = record_cursor(db)
    total = db.execute("SELECT count(*) FROM records").fetchone()[0]
    queue = []
    inventory = []
    current_counts, legacy_counts = {}, {}
    axes = {"involves_conflict": 0, "author_stance_unknown": 0, "intensity_unknown": 0,
            "heat_known": 0, "heat_unknown": 0}
    tier_counts = {}
    models_present = 0
    legacy_scores = 0
    unknown_time = 0
    scanned = 0
    while True:
        chunk = cursor.fetchmany(chunk_size)
        if not chunk:
            break
        for raw in chunk:
            row = dict(raw)
            scanned += 1
            views = _int_or_none(row.get("views"))
            legacy_score = row.get("conflict_score")
            if legacy_score is not None and not isinstance(legacy_score, (int, float)):
                legacy_score = None
            model = model_entry(row, model_available)
            analysis = relevance.analyze(row["title"], row["summary"], views=views,
                                          views_explicit=False)
            current_codes = current_disagreements(row["relevance_tier"], model, analysis,
                                                  row["summary"])
            legacy_codes = legacy_disagreements(row["relevance_tier"], legacy_score, model)
            tier_counts[analysis["tier"]] = tier_counts.get(analysis["tier"], 0) + 1
            if analysis["involves_conflict"]:
                axes["involves_conflict"] += 1
            if analysis["author_stance"] == "unknown":
                axes["author_stance_unknown"] += 1
            if analysis["intensity"] == "unknown":
                axes["intensity_unknown"] += 1
            # 旧 views 无来源凭据：一律 unknown，不因旧正整数就当已证实热度。
            axes["heat_unknown"] += 1
            if model["present"]:
                models_present += 1
            if legacy_score is not None and legacy_score > 0:
                legacy_scores += 1
            if row["time_quality"] != "valid":
                unknown_time += 1
            for code in current_codes:
                current_counts[code] = current_counts.get(code, 0) + 1
            for code in legacy_codes:
                legacy_counts[code] = legacy_counts.get(code, 0) + 1
            inventory.append({
                "record_id": row["id"],
                "input_hash": input_hash(row["title"], row["summary"]),
                "stored_tier": row["relevance_tier"],
                "current_tier": analysis["tier"],
                "legacy_conflict_score": legacy_score,
                "model_present": model["present"],
                "has_disagreement": bool(current_codes or legacy_codes),
                "resolution_status": "pending",
            })
            if not current_codes and not legacy_codes:
                continue
            queue.append({
                "record_id": row["id"],
                "input_hash": input_hash(row["title"], row["summary"]),
                "input_hash_basis": "sha256(json[title,summary])",
                "title": row["title"],
                "url": row.get("url"),
                "source_name": row.get("source_name"),
                "source_id": row.get("source_id"),
                "published_at": row["published_at"],
                "time_quality": row["time_quality"],
                "legacy": {
                    "relevance_tier": row["relevance_tier"],
                    "conflict_score": legacy_score,
                    "views": views,
                    "views_heat": relevance.observed_heat(views, explicit=False),
                    "views_heat_reason": "legacy_views_have_no_source_provenance",
                },
                "model": model,
                "current": {
                    "rule_version": analysis["rule_version"],
                    "tier": analysis["tier"],
                    "involves_conflict": analysis["involves_conflict"],
                    "author_stance": analysis["author_stance"],
                    "intensity": analysis["intensity"],
                    "heat": analysis["heat"],
                    "reason": analysis["reason"],
                    "matched": analysis["matched"],
                },
                "current_disagreement": current_codes,
                "legacy_disagreement": legacy_codes,
                "disagreement": current_codes + legacy_codes,
                "resolution_status": "pending",
                "resolution_basis": ("pending_review: 当前规则只统一接口口径；原模型含义未复核，"
                                     "不得批量覆盖历史标注或当作人工真值"),
            })
    stored_tier = {row[0]: row[1] for row in db.execute(
        "SELECT relevance_tier, count(*) FROM records GROUP BY relevance_tier "
        "ORDER BY relevance_tier")}
    stats = {
        "records_total": total,
        "records_scanned": scanned,
        "chunk_size": chunk_size,
        "queue_items": len(queue),
        "model_present": models_present,
        "model_missing": scanned - models_present,
        "model_table_usable": model_available,
        "legacy_conflict_score_nonzero": legacy_scores,
        "unknown_time_quality": unknown_time,
        "stored_tier": stored_tier,
        "current_tier": tier_counts,
        "current_disagreement_counts": current_counts,
        "legacy_disagreement_counts": legacy_counts,
        "axes": axes,
    }
    return queue, stats


def tieba_section(db, chunk_size, now):
    """只读复用 activity.admit；不写库、不启采样、不把不足两点当整窗不达标。

    贴吧候选最多 ``TIEBA_CANDIDATE_LIMIT`` 个（不随全库扫描放宽）；
    ``chunk_size`` 只作为读取批大小。
    """
    candidates = min(max(1, chunk_size), TIEBA_CANDIDATE_LIMIT)
    section = {
        "schema_version": "tieba-preparation-v1",
        "real_sampling": "not_started",
        "adapter_verified": False,
        "threshold": activity.THRESHOLD,
        "window_hours": activity.WINDOW_HOURS,
        "posts_total": 0,
        "posts_examined": 0,
        "candidate_limit": TIEBA_CANDIDATE_LIMIT,
        "statuses": {},
        "reasons": {},
        "labels": {},
        "items": [],
        "next_sampling": [
            "同一原帖至少两条同身份（platform=tieba + platform_id==source_id，缺 id 才用 canonical_url）"
            "的 exact 非负真实回复计数，且同一 comment_source_field",
            "窗口内首末点均落在真实 24h 观测区间；末点距 now 不超过 15 分钟；计数不得回撤",
            "短区间观测净增 >50 即可作为“窗口内已观测净增”的下界达标；<=50 只能说明该观测区间"
            "未达阈值，不能推出整个 24 小时不达标",
            "当前总回复数、发帖时间、单点计数不能替代观测净增",
        ],
        "notes": ["不把“没观测到”写成“整窗不达标”；部分窗 <=50 只记下界待核；不跨帖借历史；不设发帖时间限制"],
    }
    available = {"feeds", "records", "p1_evidence"}.issubset(tables(db))
    if not available:
        section["status"] = "evidence_table_missing"
        return section
    record_cols = columns(db, "records")
    if "source_id" not in record_cols or "canonical_url" not in record_cols:
        section["status"] = "record_identity_columns_missing"
        return section
    posts = [dict(row) for row in db.execute(
        "SELECT r.id, r.source_id, r.canonical_url, r.title, f.name AS source "
        "FROM records r JOIN feeds f ON f.id=r.feed_id "
        "WHERE f.url LIKE ? OR lower(f.name) = ? OR lower(f.name) LIKE ? "
        "OR f.name = ? OR f.name LIKE ? ORDER BY r.id LIMIT ?",
        ("%" + activity.TIEBA_FEED_URL_MARKER + "%", "tieba",
         activity.TIEBA_FEED_NAME_LIKES[0], "贴吧", activity.TIEBA_FEED_NAME_LIKES[1], candidates))]
    section["posts_total"] = db.execute(
        "SELECT count(*) FROM records r JOIN feeds f ON f.id=r.feed_id "
        "WHERE f.url LIKE ? OR lower(f.name) = ? OR lower(f.name) LIKE ? OR f.name = ? "
        "OR f.name LIKE ?",
        ("%" + activity.TIEBA_FEED_URL_MARKER + "%", "tieba",
         activity.TIEBA_FEED_NAME_LIKES[0], "贴吧", activity.TIEBA_FEED_NAME_LIKES[1])).fetchone()[0]
    evidence = list(db.execute(
        "SELECT platform, platform_id, canonical_url, observed_at, comment_value, "
        "comment_precision, comment_source_field FROM p1_evidence "
        "WHERE lower(platform) IN (?, ?)", (activity.TIEBA_PLATFORM, "贴吧")))
    by_id, by_url = {}, {}
    for row in evidence:
        key = row["platform_id"] if row["platform_id"] else None
        if key:
            by_id.setdefault(key, []).append(row)
        else:
            by_url.setdefault(row["canonical_url"], []).append(row)
    label_map = {
        "qualified": "window_net_gain_gt_50",
        "partial_interval": "window_net_gain_le_50_partial",
        "single_point": "single_point",
        "no_window_observation": "insufficient_window_points",
        "no_observation": "no_count_evidence",
        "stale_last": "stale_observation",
        "retraction": "count_retracted",
        "source_changed": "source_field_changed",
        "same_time_conflict": "same_time_conflict",
        "invalid_value": "invalid_count_value",
        "future_last": "future_observation",
        "time_invalid": "invalid_observation_time",
    }
    labels = {}
    items = []
    for post in posts:
        matched = by_id.get(post["source_id"], []) if post["source_id"] else \
            by_url.get(post["canonical_url"], [])
        result = activity.admit(matched, now)
        status, reason = result["status"], result.get("reason")
        section["statuses"][status] = section["statuses"].get(status, 0) + 1
        if reason:
            section["reasons"][reason] = section["reasons"].get(reason, 0) + 1
        label = label_map.get(reason or status, reason or status)
        labels[label] = labels.get(label, 0) + 1
        items.append({"record_id": post["id"], "title": post["title"], "source": post["source"],
                      "status": status, "reason": reason, "label": label,
                      "observed_delta": result.get("observed_delta"),
                      "observed_span_seconds": result.get("observed_span_seconds"),
                      "is_full_24h": result.get("is_full_24h"),
                      "points_used": result.get("points_used")})
    section["labels"] = labels
    section["posts_examined"] = len(posts)
    section["items"] = items
    section["status"] = "evidence_observed" if any(
        section["statuses"].get(key) for key in ("qualified", "pending")) else "no_observation"
    if not evidence:
        section["status"] = "no_count_evidence"
    return section


def gui_section():
    return {
        "status": "not_verified",
        "real_gui_evidence": "absent",
        "reason": ("本副本无真实浏览器/桌面证据；Ego 指南、静态检查、HTTP 抓取都不能替代真实 GUI 验收"),
        "required_paths": [
            "本机舆情前端真实标签页：按产品路径选择候选/分组 → 打开证据详情 → 观察四轴是否分开显示",
            "日报完整链路：生成日报 → 预览 → 下载落盘（核对真实文件路径/字节）→ 打开 PDF 并做视觉核对",
            "旧来源恢复：已选历史 → 制造真实失败 → 切观察清单 → 旧表/图/标题必须清旧并可重读恢复",
            "窄屏 390px 与桌面各自回读 innerWidth/innerHeight、组件尺寸后截图",
            "每次验收记录 PID/创建时间/端口、前端与脚本字节 SHA，结束再核一次",
        ],
        "authority": "none（本准备不授予浏览器/采集/模型执行权）",
    }


def build_outputs(db_path, chunk_size):
    sha_before = file_sha256(db_path)
    db = connect_readonly(db_path)
    now = datetime.now(timezone.utc)
    try:
        db.execute("BEGIN")  # 单一显式只读事务；结束 rollback，不提交任何写
        try:
            queue, stats = build_queue(db, chunk_size)
            tieba = tieba_section(db, chunk_size, now)
            present = tables(db)
        finally:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass
    sha_after = file_sha256(db_path)
    missing_optional = [name for name in ("model_annotations", "p1_evidence")
                        if name not in present]
    checks = [
        {"check": "db_readonly", "status": "ok", "detail": "sqlite mode=ro + PRAGMA query_only=1"},
        {"check": "db_hash_unchanged", "status": "ok" if sha_before == sha_after else "fail",
         "detail": {"sha256_before": sha_before, "sha256_after": sha_after}},
        {"check": "rules_replay", "status": "ok",
         "detail": f"{stats['records_scanned']} 条按 {relevance.RULE_VERSION} 全库重放"},
        {"check": "disagreements_pending",
         "status": "warn" if stats["queue_items"] else "ok",
         "detail": f"{stats['queue_items']} 条待复核（pending，不是人工裁决）；"
                   "当前规则分歧与旧值分歧分开统计"},
        {"check": "legacy_conflict_score",
         "status": "warn" if stats["legacy_conflict_score_nonzero"] else "ok",
         "detail": ("历史 conflict_score 未重算，仅作 legacy 记录；不与新规则比大小"
                    if stats["legacy_conflict_score_nonzero"] else "无旧分")},
        {"check": "heat_unknown",
         "status": "warn" if stats["axes"]["heat_unknown"] else "ok",
         "detail": f"{stats['axes']['heat_unknown']} 条无来源凭据（全部 legacy views 一律 unknown，"
                   "正整数也不当有凭据）"},
        {"check": "model_annotations",
         "status": "warn" if "model_annotations" in missing_optional or stats["model_missing"] else "ok",
         "detail": f"缺模型标注 {stats['model_missing']} 条（以 m.record_id 是否存在判定，"
                   f"表可用={stats['model_table_usable']}）"},
        {"check": "tieba_sampling",
         "status": "not_started" if tieba["real_sampling"] == "not_started" else "unknown",
         "detail": "未启动真实适配器/持续采样；仅只读观测现状（候选最多 3 个）"},
        {"check": "gui_real_evidence", "status": "absent",
         "detail": "无真实 GUI 证据，见 gui.required_paths"},
        {"check": "execution_grants", "status": "ok",
         "detail": "collection/model/browser/db_write 均为 false（preflight 默认不授权）"},
    ]
    readiness_doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "rule_version": relevance.RULE_VERSION,
        "db": {
            "path": str(Path(db_path)),
            "read_mode": "sqlite mode=ro + query_only；单一显式 BEGIN 只读事务，结束 ROLLBACK",
            "sha256_before": sha_before,
            "sha256_after": sha_after,
            "unchanged": sha_before == sha_after,
            "wal_note": "哈希只覆盖主库文件；-wal/-shm 不改写也不据此声称完整快照",
        },
        "limits": {"chunk_size": chunk_size, "scan_scope": "all_records",
                   "records_total": stats["records_total"],
                   "records_scanned": stats["records_scanned"],
                   "queue_items": stats["queue_items"],
                   "tieba_candidates_max": TIEBA_CANDIDATE_LIMIT,
                   "tieba_posts_examined": tieba["posts_examined"]},
        "schema": {"tables_present": sorted(present), "optional_missing": missing_optional,
                   "required_ok": True},
        "classification": {
            "stored_tier": stats["stored_tier"],
            "current_tier": stats["current_tier"],
            "current_disagreement_counts": stats["current_disagreement_counts"],
            "legacy_disagreement_counts": stats["legacy_disagreement_counts"],
            "model_present": stats["model_present"],
            "model_missing": stats["model_missing"],
            "legacy_conflict_score_nonzero": stats["legacy_conflict_score_nonzero"],
            "unknown_time_quality": stats["unknown_time_quality"],
            "model_table_usable": stats["model_table_usable"],
        },
        "axes": stats["axes"],
        "tieba": tieba,
        "gui": gui_section(),
        "checks": checks,
        "grants": {"collection": False, "model": False, "browser": False,
                   "db_write": False, "overwrite_artifacts": False},
        "network_requests": 0,
        "model_calls": 0,
        "db_writes": 0,
        "queue_file": QUEUE_FILE,
        "notes": [
            "分歧队列是待复核线索，不是 52 条（或任意条数）语义裁决；resolution_status=pending",
            "当前规则分歧与旧 stored tier/model opposition 原始分歧分开统计，不混为一个数",
            "当前规则只统一接口口径；原模型输出含义未复核，不能批量覆盖历史标注",
            "旧 views 可能是历史占位、也没有来源凭据，heat 一律 unknown；旧 conflict_score 未重算",
            "model.relevant 历史保存的是 gender_related 语义，只与当前 gender_related 比较",
        ],
        "errors": [],
        "warnings": ([f"缺少可选表：{'、'.join(missing_optional)}"] if missing_optional else []),
    }
    queue_doc = {
        "schema_version": QUEUE_SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "rule_version": relevance.RULE_VERSION,
        "db_sha256": sha_before,
        "chunk_size": chunk_size,
        "scan_scope": "all_records",
        "records_total": stats["records_total"],
        "records_scanned": stats["records_scanned"],
        "queue_items": len(queue),
        "queue_kind": "disagreement_and_change_pending",
        "resolution_status": "pending",
        "items": queue,
    }
    return queue_doc, readiness_doc, stats


def summary_markdown(readiness_doc, stats):
    classification = readiness_doc["classification"]
    tieba = readiness_doc["tieba"]
    current_counts = classification["current_disagreement_counts"] or {}
    legacy_counts = classification["legacy_disagreement_counts"] or {}
    lines = [
        "# 采集前准备摘要（只读导出）",
        "",
        f"- 生成时间：{readiness_doc['generated_at']}；规则版本：`{readiness_doc['rule_version']}`",
        f"- 库：`{readiness_doc['db']['path']}`（只读；哈希未变：{readiness_doc['db']['unchanged']}）",
        f"- 扫描：全库 {readiness_doc['limits']['records_scanned']}/"
        f"{readiness_doc['limits']['records_total']} 条（按 id 稳定顺序，`--limit` 仅为分块大小），"
        f"队列 {readiness_doc['limits']['queue_items']} 条（`resolution_status=pending`，不是人工真值）",
        "",
        "## 与当前规则的分歧",
    ]
    if current_counts:
        for code in sorted(current_counts):
            lines.append(f"- `{code}`：{current_counts[code]}")
    else:
        lines.append("- 全库无当前规则分歧")
    lines.append("")
    lines.append("## 旧值之间的原始分歧（与当前规则分开）")
    if legacy_counts:
        for code in sorted(legacy_counts):
            lines.append(f"- `{code}`：{legacy_counts[code]}")
    else:
        lines.append("- 无旧值分歧")
    lines += [
        "",
        "## 四轴与旧值",
        f"- 涉及冲突（规则层）：{stats['axes']['involves_conflict']} 条；"
        f"作者立场 unknown：{stats['axes']['author_stance_unknown']} 条；"
        f"强度 unknown：{stats['axes']['intensity_unknown']} 条",
        f"- 热度未知：{stats['axes']['heat_unknown']} 条（全部 legacy views 均无来源凭据）；"
        f"旧 conflict_score>0：{classification['legacy_conflict_score_nonzero']} 条（未重算）",
        "",
        "## 贴吧准备",
        f"- 状态：`{tieba.get('status')}`；真实采样：`{tieba.get('real_sampling')}`；"
        f"检查原帖 {tieba.get('posts_examined', 0)}/{tieba.get('posts_total', 0)}"
        f"（候选最多 {tieba.get('candidate_limit', 0)} 个）",
        f"- 观测标签：{json.dumps(tieba.get('labels', {}), ensure_ascii=False)}",
        "- 达标条件：同帖窗口内两条以上可比 exact 计数、末点 15 分钟内、无回撤；"
        "短区间观测净增 >50 即作为窗内下界达标，<=50 只能说明该区间未达阈值，不能推出整窗不达标；"
        "总回复数、发帖时间、单点计数不能替代。",
        "",
        "## GUI 与授权",
        "- 真实 GUI 证据：absent；必需路径见 `precollection-readiness.json` 的 `gui.required_paths`。",
        "- preflight 默认不授予 collection/model/browser/db_write；本工具未联网、未调模型、未写库。",
        "",
        "## 未做 / 未验证",
        "- 未跑真实来源、未启持续采样、未做语义人工裁决、未做真实 GUI 验收。",
        "- 本轮 3 组离线检查由主线程执行；本文件不声称已运行。",
    ]
    if readiness_doc["warnings"]:
        lines += ["", "## 警告"] + [f"- {item}" for item in readiness_doc["warnings"]]
    return "\n".join(lines) + "\n"


def write_outputs(output_dir, documents):
    directory = Path(output_dir)
    targets = {name: directory / name for name in documents}
    existing = [str(path) for path in targets.values() if path.exists()]
    if existing:
        raise PrepareError("output_exists",
                           "目标文件已存在，拒绝覆盖或删除：" + "、".join(existing))
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in documents.items():
        with open(targets[name], "x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    return {name: str(targets[name]) for name in documents}


def run(args):
    db_path = validate_args(args)
    queue_doc, readiness_doc, stats = build_outputs(db_path, args.limit)
    documents = {
        QUEUE_FILE: json.dumps(queue_doc, ensure_ascii=False, indent=2) + "\n",
        READINESS_FILE: json.dumps(readiness_doc, ensure_ascii=False, indent=2) + "\n",
        SUMMARY_FILE: summary_markdown(readiness_doc, stats),
    }
    paths = write_outputs(args.output_dir, documents)
    return {
        "status": "ok",
        "rule_version": relevance.RULE_VERSION,
        "chunk_size": args.limit,
        "records_total": stats["records_total"],
        "records_scanned": stats["records_scanned"],
        "queue_items": stats["queue_items"],
        "tieba_posts_examined": readiness_doc["tieba"]["posts_examined"],
        "db_unchanged": readiness_doc["db"]["unchanged"],
        "outputs": paths,
        "network_requests": 0,
        "model_calls": 0,
        "db_writes": 0,
        "grants": readiness_doc["grants"],
    }


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        args = build_parser().parse_args(argv)
        report = run(args)
    except PrepareError as exc:
        print(json.dumps({"status": "error", "kind": exc.kind, "detail": exc.detail},
                         ensure_ascii=False, indent=2))
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(json.dumps({"status": "error", "kind": type(exc).__name__, "detail": str(exc)},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
