"""话题分类：规则词表把标题归到稳定类别（支持多标签），再用 jieba 抽类别关键词。

类别是产品口径，不是模型结论；多标签总和可以大于内容数。
"""
from __future__ import annotations

from datetime import timedelta

CATEGORIES = (
    ("彩礼/婚姻法", ("彩礼", "婚姻法", "结婚率", "婚房", "嫁妆", "离婚", "领证", "婚前")),
    ("性别对立/打拳", ("性别对立", "男女对立", "打拳", "拳师", "女拳", "男拳", "性别矛盾")),
    ("女权/男权", ("女权", "男权", "田园女权", "女性主义")),
    ("厌女/性别歧视", ("厌女", "媚男", "性别歧视", "重男轻女", "物化", "规训", "双标")),
    ("家暴/性侵害", ("家暴", "性骚扰", "性侵", "虐待", "殴打", "尾随")),
    ("大龄/婚恋", ("大龄", "剩女", "相亲", "催婚", "逼婚", "婚恋", "单身")),
)
FALLBACK = "其他"
CATEGORY_NAMES = tuple(name for name, _ in CATEGORIES) + (FALLBACK,)
BASIS = ("count 是标签命中行数（同一 canonical 的重复行也会计入），records 按 canonical_id 去重；"
         "同一内容命中多个标签会重复计入 count，因此 count 总和可大于内容数。")


def classify_all(title, summary=""):
    text = f"{title or ''} {summary or ''}"
    labels = []
    for name, terms in CATEGORIES:
        if any(term in text for term in terms):
            labels.append(name)
    return labels


def classify(title):
    labels = classify_all(title)
    return "、".join(labels) if labels else FALLBACK


def keywords(titles, topk=4):
    try:
        import jieba.analyse
        return [w for w in jieba.analyse.extract_tags("。".join(titles), topK=topk)
                if len(w) > 1][:topk]
    except Exception:
        return []


def _select_rows(db, hours, now=None, with_feed=False):
    """返回 `[(row, moment_or_None), ...]`。

    - `hours` 为具体小时数：严格发表时间窗口，只 time_quality=valid 且真实 `[start, end)`，
      绝不回退 fetched_at、不用原始字符串排序；
    - `hours=None`（all）：**保留全部原始记录**（含 unknown/future），与 `storage.overview_stats` 的
      all 全库母体一致；moment 取真实发表时刻（仅 valid），其余为 None，供排序时有效时刻在前、unknown 置后。
    """
    import activity
    end = now.astimezone(activity.timezone.utc) if now is not None else activity.utc_now()
    cols = ("r.title, r.summary, r.canonical_id, m.topic, r.published_at, r.time_quality")
    if with_feed:
        cols = ("r.title, r.summary, r.url, r.published_at, f.name, r.canonical_id, m.topic, r.time_quality")
    join = " FROM records r JOIN feeds f ON f.id=r.feed_id" if with_feed else " FROM records r"
    rows = db.execute("SELECT " + cols + join +
                      " LEFT JOIN model_annotations m ON m.record_id=r.id").fetchall()
    if hours is None:
        return [(row, activity.valid_published(row)[0]) for row in rows]
    start = end - timedelta(hours=int(hours))
    return activity.select(rows, start, end)[0]


def _ordered_rows(pairs):
    """排序：有效发表时刻降序在前，unknown/无法解析的稳定置后（不凭 fetched 伪发表）。"""
    valid = sorted([p for p in pairs if p[1] is not None], key=lambda p: p[1], reverse=True)
    unknown = [p for p in pairs if p[1] is None]
    return [row for row, _ in valid + unknown]


def categories(db, hours=24, now=None):
    """按最近 hours 小时分类（严格发表时间窗口；hours=None 为 all，保留全库原始记录）；返回
    [{name,count,records,keywords,examples}]。"""
    rows = [row for row, _ in _select_rows(db, hours, now)]
    label_counts = {name: 0 for name in CATEGORY_NAMES}
    seen = {name: set() for name in CATEGORY_NAMES}
    titles = {name: [] for name in CATEGORY_NAMES}
    example_seen = {name: set() for name in CATEGORY_NAMES}
    total = 0
    for row in rows:
        total += 1
        model_topic = row[3] if row[3] in CATEGORY_NAMES else None
        labels = [model_topic] if model_topic else (classify_all(row[0], row[1] if len(row) > 1 else "") or [FALLBACK])
        identity = row[2] or row[0]
        for name in labels:
            label_counts[name] += 1
            seen[name].add(identity)
            if len(titles[name]) < 3 and row[0] not in example_seen[name]:
                example_seen[name].add(row[0])
                titles[name].append(row[0])
    out = []
    for name in CATEGORY_NAMES:
        if not label_counts[name]:
            continue
        out.append({"name": name, "count": label_counts[name], "records": len(seen[name]),
                    "keywords": keywords(titles[name]), "examples": titles[name][:3]})
    out.sort(key=lambda item: (-item["records"], item["name"]))
    return {"categories": out, "total_contents": total, "basis": BASIS}


def records_for_category(db, hours, category, limit=50, now=None):
    """返回某个类别（或 __all__）下的内容（按 canonical_id 去重）。

    `hours` 为具体小时数时严格按发表时间窗口；`hours=None`（all）保留全部原始记录（含 unknown/future）。
    排序：有效发表时刻降序在前，unknown/无法解析的稳定置后。`now` 固定窗口右边界。
    """
    if category != "__all__" and category not in CATEGORY_NAMES:
        raise ValueError("category 不在白名单：" + "/".join(CATEGORY_NAMES + ("__all__",)))
    rows = _ordered_rows(_select_rows(db, hours, now, with_feed=True))
    items, seen, total = [], set(), 0
    truncated = len(rows) >= 5000
    for row in rows:
        model_topic = row[6] if row[6] in CATEGORY_NAMES else None
        labels = [model_topic] if model_topic else (classify_all(row[0], row[1] or "") or [FALLBACK])
        if category != "__all__" and category not in labels:
            continue
        identity = row[5] or row[0]
        if identity in seen:
            continue
        seen.add(identity)
        total += 1
        if len(items) < limit:
            items.append({"title": row[0], "url": row[2],
                          "published_at": row[3], "source": row[4]})
    return {"items": items, "total": total, "has_more": total > len(items),
            "truncated": truncated}
