"""严格发表时间窗口 + 贴吧 24 小时回复增量准入（只读、无网络、无模型、无数据库写操作）。

两件事共用同一份真实证据，绝不外推、不补零、不用抓取时间冒充发表时间：

1. `valid_published` / `select`：严格 aware 发表时间窗口选择。
   只有 `time_quality=valid` 且带时区的 `published_at` 才参与；窗口是真实 `[start, end)`，
   按**解析后的 datetime** 比较（绝不用原始字符串排序，也绝不回退 `fetched_at`）。
   `start=None` 表示 `all`，保留全库。

2. `tieba_activity`：只读复用 `p1_evidence` 的追加历史，对 `records` 里的贴吧原帖逐帖
   计算滚动 24 小时回复净增准入。身份匹配优先 `platform=tieba` 且 `platform_id == records.source_id`，
   缺 id 才退回 `canonical_url`；绝不按标题匹配、绝不跨帖借用历史。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
TIME_BASIS = "published_at (time_quality=valid)"
TIEBA_PLATFORM = "tieba"
# 贴吧来源识别以「来源」为准：feed URL 指向 tieba.baidu.com，或 feeds.name 是贴吧别名。
# 真实库的 feeds.name 可能是 tieba-hot / 贴吧 / OpenCLI-贴吧 等，不能只匹配字面 "tieba"。
TIEBA_FEED_URL_MARKER = "tieba.baidu.com"
TIEBA_FEED_NAME_LIKES = ("tieba-%", "%贴吧%")

# 24 小时滚动窗口与回复净增阈值；判据是「净增 > 阈值」，因此 50 不达标、51 达标。
WINDOW_HOURS = 24
THRESHOLD = 50
STALE_MINUTES = 15

TIME_REASONS = {
    "missing": "time_missing",
    "legacy_unverified": "time_legacy_unverified",
    "invalid": "time_invalid",
    "timezone_missing": "time_timezone_missing",
}


def utc_now():
    return datetime.now(timezone.utc)


def to_aware(value):
    """解析为带时区 datetime；naive / 非法 / 空值一律返回 None（不做字符串比较）。"""
    if value is None or value == "":
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def valid_published(row):
    """严格发表时间：只 `time_quality=valid` 且带时区的 `published_at`，不回退 `fetched_at`。

    返回 `(moment_utc, reason)`：moment 非 None 时 reason 为 None；否则 reason 说明排除原因。
    """
    quality = row["time_quality"]
    if quality != "valid":
        return None, TIME_REASONS.get(quality, "time_invalid")
    raw = row["published_at"]
    if not raw:
        return None, "time_missing"
    moment = to_aware(raw)
    if moment is None:
        return None, "time_invalid"
    return moment.astimezone(timezone.utc), None


def select(rows, start, end):
    """严格 `[start, end)` 发表时间选择；`start=None` 表示不设下界（all）。

    返回 `(selected, excluded, unknown_total)`：

    - `selected`：`[(row, moment_utc), ...]`，moment 为解析后的真实发表时刻；
    - `excluded`：`{原因: 条数}`，含 `time_*` 与窗口外的 `history` / `future`；
    - `unknown_total`：因时间质量无法参与任何发表时间窗口的记录数（`time_*` 之和）。
    """
    selected = []
    excluded = {"time_missing": 0, "time_legacy_unverified": 0, "time_invalid": 0,
                "time_timezone_missing": 0, "history": 0, "future": 0}
    for row in rows:
        moment, reason = valid_published(row)
        if moment is None:
            excluded[reason] += 1
            continue
        if start is not None and moment < start:
            excluded["history"] += 1
            continue
        if end is not None and moment >= end:
            excluded["future"] += 1
            continue
        selected.append((row, moment))
    unknown_total = (excluded["time_missing"] + excluded["time_legacy_unverified"]
                     + excluded["time_invalid"] + excluded["time_timezone_missing"])
    return selected, excluded, unknown_total


# --- 贴吧 24 小时回复增量准入（只读） ------------------------------------------------------------


def _bucketize(evidence_rows):
    """按**解析后的 UTC 同一时刻**分桶（不同 offset 的同一时刻也会合并）；同刻不同值判冲突。"""
    buckets = {}
    for row in evidence_rows:
        moment = to_aware(row["observed_at"])
        key = moment.astimezone(timezone.utc) if moment is not None else ("raw", row["observed_at"])
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {"observed_at": row["observed_at"], "moment": moment, "counts": set()}
            buckets[key] = bucket
        bucket["counts"].add((row["comment_value"], row["comment_precision"], row["comment_source_field"]))
    order = sorted(buckets.values(),
                   key=lambda b: (b["moment"] is None, b["moment"] or datetime.min.replace(tzinfo=timezone.utc)))
    points = []
    for bucket in order:
        distinct = bucket["counts"]
        if len(distinct) > 1:
            value, precision, source_field = None, "conflict", ""
        else:
            value, precision, source_field = next(iter(distinct))
        points.append({"observed_at": bucket["observed_at"], "moment": bucket["moment"],
                       "value": value, "precision": precision, "source_field": source_field,
                       "conflict": len(distinct) > 1})
    return points


def _anomaly(point):
    """不可比点：同刻冲突 / 非 exact / 非 int / 负数 / source_field 为空 —— 一律阻断跨点借基线。"""
    if point["conflict"]:
        return "same_time_conflict"
    if point["precision"] != "exact":
        return point["precision"] if point["precision"] in ("unknown", "approximate") else "not_exact"
    if type(point["value"]) is not int or point["value"] < 0:
        return "invalid_value"
    if not (point["source_field"] or "").strip():
        return "source_missing"
    return None


def _comparable_run(points):
    """最新连续可比区段：向后重建；source 变化 / 时间不前进 / 回撤 / 异常点都重置基线。"""
    run = []
    for point in points:
        if _anomaly(point):
            run = []
            continue
        if not run:
            run = [point]
            continue
        prev = run[-1]
        if prev["source_field"] != point["source_field"]:
            run = [point]
        elif point["moment"] <= prev["moment"]:
            run = [point]
        elif point["value"] < prev["value"]:
            run = [point]  # 回撤：阻断跨点借用基线
        else:
            run.append(point)
    return run


def _pending(reason, detail=None):
    result = {"status": "pending", "reason": reason, "observed_delta": None, "lower_bound": None,
              "observed_start": None, "observed_end": None, "observed_span_seconds": None,
              "is_full_24h": None, "points_used": 0, "latest_observed_at": None}
    if detail:
        result.update(detail)
    return result


def admit(evidence_rows, now):
    """对单个贴吧原帖的带时区历史做滚动 24 小时准入。`evidence_rows` 必须已按身份匹配。

    判据只用**窗口内实测点**：`observed_delta = 窗口内末点 - 窗口内首点`。绝不把窗口外的点
    当基线（避免把 24 小时以前的增长冒充窗内净增），也绝不断言「整窗不达标」。
    净增是「24 小时内已观测净增下界」，不插值、不年化、不补 0。
    """
    now = now.astimezone(timezone.utc)
    window_start = now - timedelta(hours=WINDOW_HOURS)
    points = _bucketize(evidence_rows)
    if not points:
        return _pending("no_observation")
    last = points[-1]
    detail = {"latest_observed_at": last["observed_at"]}
    if last["moment"] is None:
        return _pending("time_invalid", detail)
    if last["moment"] > now:
        return _pending("future_last", detail)
    if (now - last["moment"]) > timedelta(minutes=STALE_MINUTES):
        return _pending("stale_last", detail)
    if _anomaly(last):
        return _pending(_anomaly(last), detail)

    run = _comparable_run(points)
    window_points = [p for p in run if p["moment"] >= window_start]
    if len(run) < 2:
        prev = points[-2] if len(points) >= 2 else None
        if prev is None:
            return _pending("single_point", detail)
        if _anomaly(prev):
            return _pending(_anomaly(prev), detail)
        if prev["source_field"] != last["source_field"]:
            return _pending("source_changed", detail)
        if last["moment"] <= prev["moment"]:
            return _pending("invalid_interval", detail)
        if last["value"] < prev["value"]:
            return _pending("retraction", detail)
        return _pending("single_point", detail)
    if len(window_points) < 2:
        return _pending("no_window_observation", detail)

    first = window_points[0]
    observed_delta = last["value"] - first["value"]
    detail.update({
        "observed_delta": observed_delta,
        "lower_bound": observed_delta,
        "observed_start": first["moment"].isoformat(),
        "observed_end": last["moment"].isoformat(),
        "observed_span_seconds": (last["moment"] - first["moment"]).total_seconds(),
        "is_full_24h": False,   # 只用窗内点到点，绝不把窗外基线当整窗覆盖
        "points_used": len(window_points),
    })
    if observed_delta > THRESHOLD:
        detail["status"] = "qualified"
        detail["reason"] = None
    else:
        # 只观测到部分区间，净增 <= 阈值：是下界，不能据此宣布整个 24 小时不达标
        detail["status"] = "pending"
        detail["reason"] = "partial_interval"
    return detail


def _record_identity(row):
    """records 原帖身份：优先 source_id，缺 id 才用 canonical_url。"""
    source_id = row["source_id"]
    if source_id:
        return ("id", source_id)
    return ("url", row["canonical_url"])


def tieba_activity(db, now=None):
    """只读计算 `records` 中全部贴吧原帖的滚动 24 小时回复净增准入。

    零网络、零模型、零数据库写操作；不把本榜与窗口内的新发表记录相加。
    """
    now = (now or utc_now()).astimezone(timezone.utc)
    window_start = now - timedelta(hours=WINDOW_HOURS)
    record_rows = list(db.execute(
        "SELECT r.id, r.source_id, r.canonical_url, r.title, r.url, f.name AS source "
        "FROM records r JOIN feeds f ON f.id=r.feed_id "
        "WHERE f.url LIKE ? OR lower(f.name) = ? OR lower(f.name) LIKE ? "
        "OR f.name = ? OR f.name LIKE ?",
        ("%" + TIEBA_FEED_URL_MARKER + "%", "tieba", TIEBA_FEED_NAME_LIKES[0],
         "贴吧", TIEBA_FEED_NAME_LIKES[1])))
    evidence_rows = list(db.execute(
        "SELECT platform, platform_id, canonical_url, observed_at, comment_value, comment_precision, "
        "comment_source_field FROM p1_evidence WHERE lower(platform) IN (?, ?)",
        (TIEBA_PLATFORM, "贴吧")))

    by_id, by_url = {}, {}
    for row in evidence_rows:
        if row["platform_id"]:
            by_id.setdefault(row["platform_id"], []).append(row)
        else:
            by_url.setdefault(row["canonical_url"], []).append(row)

    posts = {}
    for row in record_rows:
        posts.setdefault(_record_identity(row), row)  # 重复 record URL 只算一个原帖

    items = []
    summary = {"records_total": len(posts), "with_evidence": 0, "qualified": 0, "pending": 0}
    reasons = {}
    latest_observed_at = None
    latest_moment = None
    for ident, row in posts.items():
        matched = by_id.get(row["source_id"], []) if row["source_id"] else by_url.get(row["canonical_url"], [])
        if matched:
            summary["with_evidence"] += 1
        result = admit(matched, now)
        status = result["status"]
        summary[status] = summary.get(status, 0) + 1
        if result.get("reason"):
            reasons[result["reason"]] = reasons.get(result["reason"], 0) + 1
        if result.get("latest_observed_at"):
            moment = to_aware(result["latest_observed_at"])
            if moment is not None and (latest_moment is None or moment > latest_moment):
                latest_moment, latest_observed_at = moment, result["latest_observed_at"]
        items.append({
            "label": row["title"], "source": row["source"], "status": status, "reason": result.get("reason"),
            "observed_delta": result.get("observed_delta"), "lower_bound": result.get("lower_bound"),
            "observed_start": result.get("observed_start"), "observed_end": result.get("observed_end"),
            "observed_span_seconds": result.get("observed_span_seconds"),
            "is_full_24h": result.get("is_full_24h"), "points_used": result.get("points_used"),
            "latest_observed_at": result.get("latest_observed_at"),
        })
    items.sort(key=lambda item: (item["status"] != "qualified", -(item["observed_delta"] or 0), item["label"] or ""))
    freshness = {"latest_observed_at": latest_observed_at, "stale_minutes": STALE_MINUTES,
                 "latest_age_seconds": int((now - latest_moment).total_seconds()) if latest_moment else None}
    return {
        "schema_version": "tieba-activity-v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "window": {"start": window_start.isoformat(), "end": now.isoformat(), "hours": WINDOW_HOURS,
                   "time_basis": "observed_at (server aware now)"},
        "threshold": THRESHOLD,
        "summary": summary,
        "reasons": reasons,
        "freshness": freshness,
        "items": items,
        "notes": [
            "仅贴吧来源原帖；身份优先 platform=tieba 且 platform_id==source_id，缺 id 才用 canonical_url，不按标题匹配、不跨帖借历史",
            "只采信同身份、同 comment_source_field 的 exact 非负真实计数；净增是「24 小时内已观测净增下界」，不插值、不年化、不补 0",
            "准入只用窗口内实测点（窗口内末点 - 窗口内首点），不借用窗口外点当基线，is_full_24h 恒为 false；不足两点或末点超时只记待核",
            "本榜与窗口内新发表记录口径不同，不相加",
        ],
        "network_requests": 0,
        "model_calls": 0,
        "db_writes": 0,
    }
