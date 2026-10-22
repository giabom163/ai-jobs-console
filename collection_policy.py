"""统一候选准入与来源时间证据（薄层，所有采集入口共用）。

本模块只做两件事：
1. **候选门**：噪声（content_filter）、性别相关度（relevance）、URL 规范化；
2. **时间证据归一**：把来源发布字段解析成可追溯的 UTC，或明确 reject/ quarantine。

时间规则（不猜、不补）：
- ``epoch``：非 bool 的精确秒整数 → UTC，quality=valid，precision=second；
- ``iso``：带时区的 ISO8601 / HTTP-date → UTC，quality=valid；
  缺时区 → timezone_missing；缺年份/无法解析 → invalid；
- ``relative``：仅当**同时**具备
  (a) 解析基准 observed_at 为 aware（带 tz），
  (b) 明确来源时区 source_timezone（本批只接受 Asia/Shanghai / UTC），
  (c) 来源发布语义字段 source_field（编辑时间等不算），
  (d) 可判定的精度 precision（minute / hour）
  时，才把“昨天 18:35 / 3 小时前”解析为精确时间；否则 reject。
  只有日期精度（“昨天”“前天”无时刻）→ reject(day_precision_only)，
  绝不补成精确小时。
- ID 派生日期、抓取时间、缺年份：一律不采用。

归一化证据随 raw_xml 快照保存（无 schema 迁移）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import relevance
import storage

# 只接受这些明确、固定偏移的来源时区（不引入 zoneinfo/tzdata 依赖；无 DST 歧义）。
SOURCE_TIMEZONES = {
    "Asia/Shanghai": timezone(timedelta(hours=8)),
    "UTC": timezone.utc,
    "Etc/UTC": timezone.utc,
}

# 发布语义字段白名单（编辑/更新字段不算发布时间）。
PUBLISH_FIELDS = {
    "published_at", "publish_time", "pubdate", "publish_date",
    "create_time", "created_at", "time", "发布时间", "发布日期",
}
EDIT_FIELDS = {"edited_at", "updated_at", "modified_at", "edit_time", "update_time", "修改时间"}

REL_MINUTE_RE = re.compile(r"^(\d+)\s*(?:分钟|分|min)前$")
REL_HOUR_RE = re.compile(r"^(\d+)\s*(?:小时|時|h|hr)前$")
REL_SECOND_RE = re.compile(r"^(\d+)\s*(?:秒|s|sec)前$")
REL_JUST_RE = re.compile(r"^(?:刚刚|刚才|just now|just)$")
REL_DAYTIME_RE = re.compile(r"^(昨天|昨天\s*|前天|今天)\s*(\d{1,2}):(\d{2})?$")
REL_DAYONLY_RE = re.compile(r"^(昨天|前天|今天)$")
REL_DIGIT_DAY_RE = re.compile(r"^(\d+)\s*(?:天|日)前$")
ABS_NO_YEAR_RE = re.compile(r"^\d{1,2}月\d{1,2}日")


def _parse_aware(value):
    """解析带时区的 ISO 字符串为 aware datetime；失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def _evidence(raw, mode, source_field=None, source_timezone=None,
              observed_at=None, precision=None, derived_from=None):
    return {
        "raw": raw,
        "mode": mode,
        "source_field": source_field,
        "source_timezone": source_timezone,
        "observed_at": observed_at,
        "precision": precision,
        "derived_from": derived_from,
    }


def parse_epoch(value):
    """非 bool 精确秒整数 → (utc_iso, evidence) 或 (None, evidence+reason)。"""
    if type(value) is int:  # noqa: E721  bool 是 int 子类，必须排除
        try:
            moment = datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None, _evidence(value, "epoch", precision="second"), "epoch_out_of_range"
        return moment.isoformat(), _evidence(value, "epoch", precision="second"), None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return None, _evidence(value, "epoch"), "epoch_string_not_used"
    return None, _evidence(value, "epoch"), "not_epoch"


def parse_iso(value):
    """带时区 ISO → (utc_iso, evidence, None)；否则 (None, evidence, reason)。"""
    normalized, quality = storage.publication(value)
    if normalized is not None and quality == "valid":
        return normalized, _evidence(value, "iso", precision="second"), None
    return None, _evidence(value, "iso"), "iso_" + quality


def parse_relative(value, *, observed_at, source_timezone, source_field, precision):
    """在完整可追溯前提下解析相对时间；否则返回 (None, evidence, reason)。"""
    ev = _evidence(value, "relative", source_field, source_timezone, observed_at, precision)
    if not isinstance(value, str) or not value.strip():
        return None, ev, "relative_empty"
    text = value.strip()
    if ABS_NO_YEAR_RE.match(text):
        # 缺年份的“10月18日 12:30”：不猜年份，先于时区/字段校验拒绝。
        return None, ev, "missing_year"
    if source_timezone not in SOURCE_TIMEZONES:
        return None, ev, "relative_unknown_timezone"
    tz = SOURCE_TIMEZONES[source_timezone]
    if source_field is None:
        return None, ev, "relative_missing_source_field"
    if source_field in EDIT_FIELDS:
        return None, ev, "relative_edit_field"
    if source_field not in PUBLISH_FIELDS:
        return None, ev, "relative_unknown_source_field"
    anchor = _parse_aware(observed_at)
    if anchor is None:
        return None, ev, "relative_missing_observed_at"

    def use(inferred):
        """精度必须可追溯：未传则按格式推断并记录，传入但不符则拒绝。"""
        if precision not in (None, inferred):
            return False
        ev["precision"] = inferred
        return True

    for regex, seconds, inferred in ((REL_SECOND_RE, 1, "second"),
                                     (REL_MINUTE_RE, 60, "minute"),
                                     (REL_HOUR_RE, 3600, "hour")):
        match = regex.match(text)
        if match:
            if not use(inferred):
                return None, ev, "relative_precision_mismatch"
            moment = anchor.astimezone(tz) - timedelta(seconds=int(match.group(1)) * seconds)
            return moment.astimezone(timezone.utc).isoformat(), ev, None
    if REL_JUST_RE.match(text):
        if not use("second"):
            return None, ev, "relative_precision_mismatch"
        return anchor.astimezone(tz).astimezone(timezone.utc).isoformat(), ev, None

    match = REL_DAYTIME_RE.match(text)
    if match:
        day = match.group(1).strip()
        hour, minute = int(match.group(2)), int(match.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            # 坏时刻（如 25:00）结构化拒绝，不让异常中断整批。
            return None, ev, "invalid_time_of_day"
        inferred = "minute" if match.group(3) is not None else "hour"
        if not use(inferred):
            return None, ev, "relative_precision_mismatch"
        base = anchor.astimezone(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = {"昨天": -1, "前天": -2, "今天": 0}[day]
        moment = base + timedelta(days=delta)
        return moment.astimezone(timezone.utc).isoformat(), ev, None
    if REL_DAYONLY_RE.match(text):
        # 日期粒度：不伪造成精确小时，也不擅自取 00:00。
        return None, ev, "day_precision_only"
    match = REL_DIGIT_DAY_RE.match(text)
    if match:
        return None, ev, "day_precision_only"
    return None, ev, "relative_unparsed"


def normalize_published(value, *, mode=None, observed_at=None, source_timezone=None,
                        source_field=None, precision=None, derived_from=None):
    """统一入口：返回 {accepted, published_at, quality, precision, mode, reason, evidence}。"""
    evidence = _evidence(value, mode, source_field, source_timezone, observed_at, precision,
                         derived_from)
    if derived_from == "id":
        return {"accepted": False, "published_at": None, "quality": "rejected",
                "precision": None, "mode": mode, "reason": "id_derived", "evidence": evidence}
    if value is None or value == "":
        return {"accepted": False, "published_at": None, "quality": "missing",
                "precision": None, "mode": mode, "reason": "missing", "evidence": evidence}

    if mode in (None, "auto"):
        if type(value) is int:
            mode = "epoch"
        elif isinstance(value, str):
            mode = "iso" if re.search(r"[-:T]", value) and value[:4].isdigit() else "relative"
        else:
            mode = "unknown"

    if mode == "epoch":
        published, evidence, reason = parse_epoch(value)
    elif mode == "iso":
        published, evidence, reason = parse_iso(value)
    elif mode == "relative":
        published, evidence, reason = parse_relative(
            value, observed_at=observed_at, source_timezone=source_timezone,
            source_field=source_field, precision=precision)
    else:
        published, reason = None, "unsupported_mode:" + str(mode)

    quality = "valid" if published else ("missing" if reason == "missing" else "rejected")
    if evidence is not None:      # 显式来源字段/精度必须落到证据里（不因模式分支丢失）
        if evidence.get("source_field") is None:
            evidence["source_field"] = source_field
        if evidence.get("precision") is None:
            evidence["precision"] = precision
    return {"accepted": published is not None, "published_at": published, "quality": quality,
            "precision": (evidence or {}).get("precision"), "mode": mode,
            "reason": reason or "ok", "evidence": evidence}


# 候选门只接受这三个明确枚举；拼错/大小写不同/非字符串一律结构化拒绝，
# 绝不让未知值落入“非 gender_related 即 substantive”的隐式分支而静默宽进。
REQUIRE_ENUM = ("substantive", "relevant", "gender_related")
REQUIRE_DOC = {
    "substantive": "正式入库默认：classify_tier ∈ {conflict, issue}",
    "relevant": "仅 conflict（opposition 为真）",
    "gender_related": "宽候选（含纯主体词）：仅用于拒绝/暂存证据，不进正式库",
}


def gate_candidate(*, title, url, summary="", source_id=None, published,
                   require="substantive"):
    """统一候选门：时间可追溯 + 非噪声 + 相关度 + URL 合法。

    require 只接受 ``substantive`` / ``relevant`` / ``gender_related``（见 REQUIRE_ENUM）；
    未知值（含拼写错误、大小写差异、非字符串）返回
    ``reason="unknown_require"`` 的结构化拒绝，不参与后续宽进判断。
    published 可以是 normalize_published 的结果，或原始值（自动归一）。
    返回 {accepted, reason, time, url, source_id, require, classification?}。
    """
    if not isinstance(require, str) or require not in REQUIRE_ENUM:
        return {"accepted": False, "reason": "unknown_require",
                "require_value": require, "allowed_requires": list(REQUIRE_ENUM),
                "time": None, "url": None, "source_id": source_id}
    if isinstance(published, dict) and "accepted" in published:
        time_info = published
    else:
        time_info = normalize_published(published)
    if not time_info["accepted"]:
        return {"accepted": False, "reason": "time:" + str(time_info["reason"]),
                "time": time_info, "url": None, "source_id": source_id, "require": require}
    if not isinstance(title, str) or not storage.normalized_text(title):
        return {"accepted": False, "reason": "empty_title", "time": time_info,
                "url": None, "source_id": source_id, "require": require}
    # 单一分类内核调用：噪声、tier、宽口径都来自同一份判定，不再二次分类。
    analysis = relevance.analyze(title, summary)
    noise = analysis["evidence"]["noise"]
    if noise["is_noise"]:
        return {"accepted": False, "reason": "noise:" + str(noise["reason"]),
                "time": time_info, "url": None, "source_id": source_id, "require": require}
    if require == "gender_related":
        if not analysis["gender_related"]:
            return {"accepted": False, "reason": "not_gender_related", "time": time_info,
                    "url": None, "source_id": source_id, "require": require}
    elif require == "relevant":
        if not analysis["opposition"]:
            return {"accepted": False, "reason": "not_opposition", "time": time_info,
                    "url": None, "source_id": source_id, "require": require}
    elif analysis["tier"] not in ("conflict", "issue"):
        return {"accepted": False, "reason": "not_substantive", "time": time_info,
                "url": None, "source_id": source_id, "require": require}
    try:
        canonical = storage.canonical_url(url)
    except ValueError:
        return {"accepted": False, "reason": "bad_url", "time": time_info,
                "url": None, "source_id": source_id, "require": require}
    return {"accepted": True, "reason": "ok", "time": time_info,
            "url": canonical, "source_id": source_id, "require": require,
            "classification": {"rule_version": analysis["rule_version"],
                               "tier": analysis["tier"],
                               "involves_conflict": analysis["involves_conflict"],
                               "author_stance": analysis["author_stance"],
                               "intensity": analysis["intensity"],
                               "heat": analysis["heat"],
                               "reason": analysis["reason"],
                               "evidence": analysis["evidence"]}}


def snapshot_evidence(decision):
    """把归一化依据压成可写入 raw_xml 快照的证据块。

    有效候选额外携带 ``classification``（rule_version/tier/四轴/reason/evidence），
    使采集快照能区分新旧规则与判定依据；不改 storage/collector 结构。
    """
    if not isinstance(decision, dict):
        return {}
    time_info = decision.get("time") or {}
    snapshot = {"gate_reason": decision.get("reason"),
                "time_reason": time_info.get("reason"),
                "time_quality": time_info.get("quality"),
                "time_precision": time_info.get("precision"),
                "time_evidence": time_info.get("evidence")}
    classification = decision.get("classification")
    if classification is not None:
        snapshot["classification"] = classification
    return snapshot
