"""日报 P0 统一报告构造 / 验证 / Markdown 渲染（仅标准库，只读取数）。

口径（与产品规则一致，绝不改写 records / 标注）：
- 时间窗口：daily = UTC+8 当天 00:00 至真实 now；weekly = 本日及之前 6 个自然日至 now；
  monthly = 当月 1 日至 now。一律用带时区的 datetime、[start, end) 比较。
- 样本时间基准只用 published_at 且 time_quality=valid（绝不把 fetched_at 当作发表时间）；
  其它任何时间质量（missing / legacy_unverified / invalid / timezone_missing / 未知取值）
  一律保守排除；历史、未来记录同样不入样本，只按原因计数保留可复算证据。
- 只保留 conflict（relevance_tier='conflict'）且落入四个议题类别的记录：
  彩礼/婚姻法、女权/男权、厌女/性别歧视、性别对立/打拳。
- 议题优先合法模型值（model_annotations.topic）：合法且在四类内直接用，合法但不在四类内
  直接排除（不再用规则改判）；只有模型缺失或非法时才按 topics 规则回退并记录 label_source。
- 情感优先合法五档模型值（强正面/偏正面/中性/偏负面/强负面），未标注或非法一律记未标注，
  绝不伪装成中性。
- 快照保存全部入选成员（含 ID、发表时间/质量、tier、标签及来源、原始标题/URL 等审计字段）；
  selected_ids、分布与 label_source 均可由成员精确复算。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

import topics

TZ = timezone(timedelta(hours=8))
KINDS = ("daily", "weekly", "monthly")
CONFLICT_TOPICS = ("彩礼/婚姻法", "女权/男权", "厌女/性别歧视", "性别对立/打拳")
VALID_SENTIMENTS = ("强正面", "偏正面", "中性", "偏负面", "强负面")
UNLABELED = "未标注"
REPORT_SCHEMA = "report-p0"
RULE_VERSION = "report-p0-v1"
TEMPLATE_VERSION = "report-md-v1"
TIME_BASIS = "published_at (time_quality=valid)"
EVIDENCE_BOUNDARY = "仅原帖标题与来源摘要；无当日互动增量证据，只作线索，不构成热度或群体风险判断"
EXCLUDE_REASONS = (
    "time_missing", "time_legacy_unverified", "time_invalid", "time_timezone_missing",
    "history", "future", "tier_not_conflict", "topic_out_of_scope",
)
MEMBER_FIELDS = (
    "record_id", "title", "url", "published_at", "time_quality", "relevance_tier",
    "source", "topic", "topic_source", "sentiment", "sentiment_source",
)
EVIDENCE_FIELDS = (
    "record_id", "title", "url", "published_at", "source",
    "topic", "topic_source", "sentiment", "sentiment_source",
)
TIME_QUALITY_REASONS = {
    "missing": "time_missing",
    "legacy_unverified": "time_legacy_unverified",
    "invalid": "time_invalid",
    "timezone_missing": "time_timezone_missing",
}


def local_iso(moment):
    return moment.astimezone(TZ).isoformat()


def local_text(moment):
    return moment.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _aware(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def window_for(kind, now=None):
    """返回 (start, end, period_key, title)；start/end 均为带时区（UTC+8）的 datetime。"""
    if kind not in KINDS:
        raise ValueError("kind 必须是 daily / weekly / monthly")
    now = now or datetime.now(TZ)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须是带时区的 datetime，拒绝 naive 时间")
    now = now.astimezone(TZ)
    if kind == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_key = start.strftime("%Y%m%d")
        title = f"议题与表达线索日报 · {start.strftime('%Y-%m-%d')}"
    elif kind == "weekly":
        start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        period_key = start.strftime("%Y%m%d")
        title = f"议题与表达线索周报 · {start.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}"
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_key = start.strftime("%Y%m")
        title = f"议题与表达线索月报 · {start.strftime('%Y-%m')}"
    return start, now, period_key, title


def resolve_topic(title, model_topic, summary=""):
    """返回 (topic, label_source)。合法模型值优先；合法但非四类直接排除；否则规则回退。"""
    if model_topic in topics.CATEGORY_NAMES:
        if model_topic in CONFLICT_TOPICS:
            return model_topic, "model"
        return None, "out_of_scope"
    labels = topics.classify_all(title or "", summary or "")
    for name in labels:
        if name in CONFLICT_TOPICS:
            return name, "rule"
    return None, "out_of_scope"


def resolve_sentiment(record_sentiment, model_sentiment):
    """返回 (sentiment, label_source)。合法五档模型值优先，其次规则值，否则未标注。"""
    if model_sentiment in VALID_SENTIMENTS:
        return model_sentiment, "model"
    if record_sentiment in VALID_SENTIMENTS:
        return record_sentiment, "record"
    return None, "unlabeled"


def parse_published(row):
    """只接受 time_quality=valid 且带时区的 published_at；其它质量一律保守排除。"""
    quality = row["time_quality"]
    raw = row["published_at"]
    if quality != "valid":
        return None, TIME_QUALITY_REASONS.get(quality, "time_invalid")
    if not raw:
        return None, "time_missing"
    moment = _aware(raw)
    if moment is None:
        return None, "time_invalid"
    return moment.astimezone(TZ), "valid"


def safe_href(url):
    """只接受不含认证信息/空白/控制字符的 http(s) URL；javascript 等协议一律拒绝。"""
    from urllib.parse import urlsplit
    if not isinstance(url, str):
        return None
    value = url.strip()
    if not value or len(value) > 2048:
        return None
    if any(ch.isspace() for ch in value):
        return None
    if any(ch in value for ch in '<>"\\'):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return value


def _plain(text):
    return " ".join(str(text or "").split())


def _display(text, limit=60):
    """正文展示用：折叠换行/空白、安全截断，并把竖线换成全角，避免破坏表格结构。"""
    value = _plain(text)
    if limit and len(value) > limit:
        value = value[: limit - 1] + "…"
    return value.replace("|", "｜")


def _escape_md(text):
    """转义 Markdown 链接/转义所需字符；配套渲染器会把 \\x 还原为字面量 x。"""
    out = []
    for char in text:
        out.append("\\" + char if char in "\\[]()" else char)
    return "".join(out)


def _cell(text, limit=60):
    return _escape_md(_display(text, limit))


def _md_href(url):
    """链接目标做最小百分号编码，括号/竖线/方括号都不会破坏 Markdown 表格与链接。"""
    for char, code in (("|", "%7C"), ("(", "%28"), (")", "%29"),
                       ("[", "%5B"), ("]", "%5D"), ("<", "%3C"), (">", "%3E")):
        url = url.replace(char, code)
    return url


def _candidate_rows(db):
    return db.execute(
        "SELECT r.id, r.title, r.summary, r.url, r.published_at, r.time_quality, "
        "r.relevance_tier, r.sentiment, f.name AS source, "
        "m.topic AS model_topic, m.sentiment AS model_sentiment "
        "FROM records r JOIN feeds f ON f.id=r.feed_id "
        "LEFT JOIN model_annotations m ON m.record_id=r.id"
    ).fetchall()


def select_sample(rows, start, end):
    """按顺序判定排除原因，返回 (members, excluded_counts, excluded_ids, candidate_total)。"""
    selected = []
    excluded_counts = {reason: 0 for reason in EXCLUDE_REASONS}
    excluded_ids = {reason: [] for reason in EXCLUDE_REASONS}
    for row in rows:
        moment, reason = parse_published(row)
        if moment is not None:
            if moment < start:
                reason = "history"
            elif moment >= end:
                reason = "future"
            else:
                reason = None
        if reason is None and row["relevance_tier"] != "conflict":
            reason = "tier_not_conflict"
        topic = None
        topic_source = None
        sentiment = None
        sentiment_source = None
        if reason is None:
            topic, topic_source = resolve_topic(row["title"], row["model_topic"], row["summary"])
            if topic is None:
                reason = "topic_out_of_scope"
        if reason is None:
            sentiment, sentiment_source = resolve_sentiment(row["sentiment"], row["model_sentiment"])
            selected.append({
                "record_id": row["id"],
                "title": row["title"],
                "url": row["url"],
                "published_at": local_iso(moment),
                "time_quality": row["time_quality"],
                "relevance_tier": row["relevance_tier"],
                "source": row["source"],
                "topic": topic,
                "topic_source": topic_source,
                "sentiment": sentiment,
                "sentiment_source": sentiment_source,
            })
            continue
        excluded_counts[reason] += 1
        excluded_ids[reason].append(row["id"])
    return selected, excluded_counts, excluded_ids, len(rows)


def _distribution(members, field):
    counts = {}
    for item in members:
        key = item[field] if field != "sentiment" else (item["sentiment"] or UNLABELED)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _sorted_topics(counts):
    return [{"label": name, "value": counts[name]}
            for name in sorted(counts, key=lambda name: (-counts[name], name))]


def _sentiment_rows(counts):
    rows = [{"label": name, "value": counts[name]} for name in VALID_SENTIMENTS if name in counts]
    if UNLABELED in counts:
        rows.append({"label": UNLABELED, "value": counts[UNLABELED]})
    return rows


def _label_sources(members):
    topic_sources = {"model": 0, "rule": 0}
    sentiment_sources = {"model": 0, "record": 0, "unlabeled": 0}
    for item in members:
        topic_sources[item["topic_source"]] += 1
        sentiment_sources[item["sentiment_source"]] += 1
    return {"topic": topic_sources, "sentiment": sentiment_sources}


def _recompute(members):
    """由成员精确复算分布与 label_source（发布闸门与快照构造共用）。"""
    topics_counts = _distribution(members, "topic")
    platform_counts = _distribution(members, "source")
    sentiment_counts = _distribution(members, "sentiment")
    return {
        "topics": _sorted_topics(topics_counts),
        "platforms": _sorted_topics(platform_counts),
        "sentiments": _sentiment_rows(sentiment_counts),
        "label_sources": _label_sources(members),
    }


def build_snapshot(db, kind, now=None, top_k=5):
    """构造报告快照（只读）。返回 dict；不做持久化。"""
    start, end, period_key, title = window_for(kind, now)
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k 必须为正整数")
    rows = _candidate_rows(db)
    selected, excluded_counts, excluded_ids, candidate_total = select_sample(rows, start, end)
    # 成员按发表时间与 ID 稳定排序，保证内容哈希与顺序可复现
    members = sorted(selected, key=lambda item: (item["published_at"], item["record_id"]))
    computed = _recompute(members)
    evidence = []
    for member in list(reversed(members))[:top_k]:
        item = {field: member[field] for field in EVIDENCE_FIELDS}
        item["evidence_boundary"] = EVIDENCE_BOUNDARY
        evidence.append(item)

    generated_at = (now or datetime.now(TZ)).astimezone(TZ)
    snapshot = {
        "schema": REPORT_SCHEMA,
        "kind": kind,
        "title": title,
        "period_key": period_key,
        "rule_version": RULE_VERSION,
        "template_version": TEMPLATE_VERSION,
        "time_zone": "UTC+8",
        "time_basis": TIME_BASIS,
        "window": {"start": local_iso(start), "end": local_iso(end)},
        "generated_at": local_iso(generated_at),
        "counts": {
            "candidate_total": candidate_total,
            "sample_total": len(members),
            "excluded": excluded_counts,
            "excluded_ids": excluded_ids,
        },
        "selected_ids": [item["record_id"] for item in members],
        "label_sources": computed["label_sources"],
        "topics": computed["topics"],
        "platforms": computed["platforms"],
        "sentiments": computed["sentiments"],
        "members": members,
        "evidence": evidence,
        "notes": [
            "样本分布是标题/来源摘要的计数，不等于群体立场；情感是粗筛标签，不等于风险。",
            "无当日可比互动增量证据，不生成跨平台综合榜，也不按历史累计播放量冒充当日热度。",
            "未知发表时间的记录只在数据说明中计数，不混入当日趋势。",
        ],
    }
    return snapshot


def stable_core(snapshot):
    """参与内容哈希的字段：除 content_sha256 / report_id 外的全部快照内容（含生成时间）。"""
    return {key: value for key, value in snapshot.items()
            if key not in ("content_sha256", "report_id")}


def content_hash(snapshot):
    core = stable_core(snapshot)
    return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def report_id_for(snapshot, digest):
    return f"report-{snapshot['kind']}-{snapshot['period_key']}-{digest[:10]}"


def _check_member(member, start, end):
    if not isinstance(member, dict):
        raise ValueError("入选成员必须是对象")
    if set(member) != set(MEMBER_FIELDS):
        raise ValueError("入选成员字段不完整或含未受控字段")
    record_id = member.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("入选成员 ID 必须非空")
    if not isinstance(member.get("title"), str):
        raise ValueError("入选成员标题必须是字符串")
    source = member.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("入选成员来源必须非空")
    if safe_href(member.get("url")) is None:
        raise ValueError("入选成员链接不是安全的 http(s) URL")
    if member.get("time_quality") != "valid":
        raise ValueError("入选成员时间质量必须为 valid")
    if member.get("relevance_tier") != "conflict":
        raise ValueError("入选成员 tier 必须为 conflict")
    moment = _aware(member.get("published_at"))
    if moment is None:
        raise ValueError("入选成员发表时间必须带时区")
    if not start <= moment < end:
        raise ValueError("入选成员发表时间必须在 [开始, 截止) 内")
    if member.get("topic") not in CONFLICT_TOPICS:
        raise ValueError("入选成员议题必须属于四个类别")
    if member.get("topic_source") not in ("model", "rule"):
        raise ValueError("入选成员议题来源不合法")
    sentiment = member.get("sentiment")
    sentiment_source = member.get("sentiment_source")
    if sentiment is not None and sentiment not in VALID_SENTIMENTS:
        raise ValueError("入选成员情感不在合法五档内")
    if sentiment_source not in ("model", "record", "unlabeled"):
        raise ValueError("入选成员情感来源不合法")
    if sentiment is None and sentiment_source != "unlabeled":
        raise ValueError("未标注情感必须对应 unlabeled 来源")
    if sentiment is not None and sentiment_source == "unlabeled":
        raise ValueError("有情感值不能标记为 unlabeled")


def validate_snapshot(snapshot):
    """发布闸门：任何不一致抛 ValueError；失败即失败，不返回半成品。"""
    if not isinstance(snapshot, dict):
        raise ValueError("快照必须是对象")
    if snapshot.get("schema") != REPORT_SCHEMA:
        raise ValueError("快照 schema 不正确")
    kind = snapshot.get("kind")
    if kind not in KINDS:
        raise ValueError("快照 kind 不合法")
    window = snapshot.get("window") or {}
    start = _aware(window.get("start"))
    end = _aware(window.get("end"))
    if start is None or end is None:
        raise ValueError("快照时间窗必须带时区")
    if start >= end:
        raise ValueError("快照时间窗必须 start < end")
    if start.utcoffset() != timedelta(hours=8) or end.utcoffset() != timedelta(hours=8):
        raise ValueError("快照时间窗必须使用 UTC+8")
    if snapshot.get("time_zone") != "UTC+8":
        raise ValueError("快照 time_zone 必须是 UTC+8")
    expected_start, _expected_end, expected_key, expected_title = window_for(kind, now=end)
    if start != expected_start:
        raise ValueError("快照开始时间与 kind 的自然日窗口不一致")
    if snapshot.get("period_key") != expected_key:
        raise ValueError("快照 period_key 与 kind/窗口不一致")
    if snapshot.get("title") != expected_title:
        raise ValueError("快照标题与 kind/窗口不一致")

    counts = snapshot.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("快照缺少 counts")
    sample_total = counts.get("sample_total")
    candidate_total = counts.get("candidate_total")
    if type(sample_total) is not int or sample_total < 0:
        raise ValueError("sample_total 必须为非负整数")
    if type(candidate_total) is not int or candidate_total < 0:
        raise ValueError("candidate_total 必须为非负整数")

    members = snapshot.get("members")
    if not isinstance(members, list) or len(members) != sample_total:
        raise ValueError("members 数量与 sample_total 不一致")
    seen_ids = set()
    for member in members:
        _check_member(member, start, end)
        if member["record_id"] in seen_ids:
            raise ValueError("入选成员 ID 必须唯一")
        seen_ids.add(member["record_id"])

    selected_ids = snapshot.get("selected_ids")
    if not isinstance(selected_ids, list) or selected_ids != [m["record_id"] for m in members]:
        raise ValueError("selected_ids 必须与入选成员一一对应")

    computed = _recompute(members)
    for group in ("topics", "platforms", "sentiments"):
        if snapshot.get(group) != computed[group]:
            raise ValueError(f"{group} 与入选成员复算值不一致")
    if snapshot.get("label_sources") != computed["label_sources"]:
        raise ValueError("label_sources 与入选成员复算值不一致")

    excluded = counts.get("excluded")
    excluded_ids = counts.get("excluded_ids")
    if not isinstance(excluded, dict) or not isinstance(excluded_ids, dict):
        raise ValueError("排除统计格式不正确")
    if set(excluded) != set(EXCLUDE_REASONS) or set(excluded_ids) != set(EXCLUDE_REASONS):
        raise ValueError("排除原因必须是受控词表")
    seen_excluded = set()
    total_excluded = 0
    for reason in EXCLUDE_REASONS:
        amount = excluded[reason]
        ids = excluded_ids[reason]
        if type(amount) is not int or amount < 0:
            raise ValueError(f"排除原因 {reason} 的计数必须为非负整数")
        if not isinstance(ids, list) or len(ids) != amount:
            raise ValueError(f"排除原因 {reason} 的 ID 数量与计数不一致")
        for record_id in ids:
            if not isinstance(record_id, str) or not record_id.strip():
                raise ValueError(f"排除原因 {reason} 的 ID 必须非空")
            if record_id in seen_excluded:
                raise ValueError("排除 ID 必须唯一")
            if record_id in seen_ids:
                raise ValueError("排除 ID 不得与入选成员重叠")
            seen_excluded.add(record_id)
        total_excluded += amount
    if total_excluded + sample_total != candidate_total:
        raise ValueError("候选总量与入选/排除数不一致")

    evidence = snapshot.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("evidence 必须是数组")
    by_id = {member["record_id"]: member for member in members}
    seen_evidence = set()
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("证据必须是对象")
        if set(item) != set(EVIDENCE_FIELDS) | {"evidence_boundary"}:
            raise ValueError("证据字段必须是成员的受控投影")
        record_id = item.get("record_id")
        if record_id not in by_id:
            raise ValueError("证据 record_id 必须是入选成员")
        if record_id in seen_evidence:
            raise ValueError("证据 record_id 不得重复")
        seen_evidence.add(record_id)
        member = by_id[record_id]
        for field in EVIDENCE_FIELDS:
            if item.get(field) != member.get(field):
                raise ValueError(f"证据 {field} 与入选成员不一致")
        if item.get("evidence_boundary") != EVIDENCE_BOUNDARY:
            raise ValueError("证据边界说明不合法")

    digest = content_hash(snapshot)
    if snapshot.get("content_sha256") not in (None, digest):
        raise ValueError("内容哈希与快照不一致")
    return digest


def _reason_label(reason):
    return {
        "time_missing": "发表时间未知",
        "time_legacy_unverified": "发表时间未经验证",
        "time_invalid": "发表时间非法",
        "time_timezone_missing": "发表时间缺少时区",
        "history": "早于统计范围",
        "future": "晚于统计范围",
        "tier_not_conflict": "不属于冲突议题库",
        "topic_out_of_scope": "不在四个议题类别内",
    }.get(reason, reason)


def render_markdown(snapshot):
    """由快照渲染对外 Markdown；与预览/PDF 复用同一份内容。

    对外不出现 record_id、内容哈希、规则/模板版本、published_at/time_quality、
    model/rule 等开发或审计用词；这些只保留在 JSON 快照中。
    """
    counts = snapshot["counts"]
    sample_total = counts["sample_total"]
    window = snapshot["window"]
    start_text = window["start"].replace("T", " ")[:19]
    end_text = window["end"].replace("T", " ")[:19]
    generated_text = snapshot["generated_at"].replace("T", " ")[:19]
    section = "一、今日摘要" if snapshot["kind"] == "daily" else "一、本期摘要"
    lines = [
        f"# {snapshot['title']}",
        "",
        f"- 截至时间：{end_text}（UTC+8）",
        f"- 统计范围：{start_text} 至 {end_text}（UTC+8，含起点不含终点）",
        f"- 生成时间：{generated_text}（UTC+8）",
        "",
        "## " + section,
        "",
    ]
    if sample_total:
        lines.append(f"本窗口内符合范围的原帖线索共 {sample_total} 条，覆盖 "
                     f"{len(snapshot['topics'])} 类议题、{len(snapshot['platforms'])} 个来源。")
    else:
        lines.append("本窗口无符合范围的内容。")

    lines += ["", "## 二、重点线索（原帖线索）", ""]
    if snapshot["evidence"]:
        lines.append("| 原帖标题 | 来源 | 发表时间 | 原文链接 |")
        lines.append("|---|---|---|---|")
        for item in snapshot["evidence"]:
            lines.append(f"| {_cell(item['title'])} | {_cell(item['source'], 30)} | "
                         f"{item['published_at'].replace('T', ' ')[:16]} | "
                         f"[原文]({_md_href(item['url'])}) |")
        lines += ["", f"> {EVIDENCE_BOUNDARY}"]
    else:
        lines.append("_本窗口无符合范围的内容。_")

    lines += ["", "## 三、议题样本分布", "", f"（分母：入选样本 {sample_total} 条）", "",
              "| 议题 | 样本数 |", "|---|---|"]
    lines += [f"| {_cell(row['label'], 40)} | {row['value']} |" for row in snapshot["topics"]] \
        or ["| （无） | 0 |"]
    lines += ["", "| 来源 | 样本数 |", "|---|---|"]
    lines += [f"| {_cell(row['label'], 40)} | {row['value']} |" for row in snapshot["platforms"]] \
        or ["| （无） | 0 |"]
    lines += ["", "| 情感（≠风险） | 样本数 |", "|---|---|"]
    lines += [f"| {_cell(row['label'], 40)} | {row['value']} |" for row in snapshot["sentiments"]] \
        or ["| （无） | 0 |"]

    lines += ["", "## 四、数据说明", "",
              f"- 候选记录总量：{counts['candidate_total']} 条；入选样本：{sample_total} 条。"]
    for reason in EXCLUDE_REASONS:
        lines.append(f"- 排除 · {_reason_label(reason)}：{counts['excluded'].get(reason, 0)} 条")
    lines += ["", "> 样本分布为标题与来源摘要的计数；情感为粗筛标签，不构成热度或群体风险判断。"]
    return "\n".join(lines) + "\n"


def build_report(db, kind, now=None, top_k=5):
    """同一生成入口：返回 (snapshot, markdown, report_id)。闸门不过即抛 ValueError。"""
    snapshot = build_snapshot(db, kind, now=now, top_k=top_k)
    digest = validate_snapshot(snapshot)
    snapshot["content_sha256"] = digest
    report_id = report_id_for(snapshot, digest)
    snapshot["report_id"] = report_id
    markdown = render_markdown(snapshot)
    return snapshot, markdown, report_id
