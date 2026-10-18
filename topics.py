"""话题分类：规则词表把标题归到稳定类别，再用 jieba 抽类别关键词。

先分类、再抽词；类别是产品口径，不是模型结论。
"""
from __future__ import annotations

CATEGORIES = (
    ("彩礼/婚姻法", ("彩礼", "婚姻法", "结婚率", "婚房", "嫁妆", "离婚", "领证", "婚前")),
    ("性别对立/打拳", ("性别对立", "男女对立", "打拳", "拳师", "女拳", "男拳", "性别矛盾")),
    ("女权/男权", ("女权", "男权", "田园女权", "女性主义")),
    ("厌女/性别歧视", ("厌女", "媚男", "性别歧视", "重男轻女", "物化", "规训", "双标")),
    ("家暴/性侵害", ("家暴", "性骚扰", "性侵", "虐待", "殴打", "尾随")),
    ("大龄/婚恋", ("大龄", "剩女", "相亲", "催婚", "逼婚", "婚恋", "单身")),
)
FALLBACK = "其他"


def classify(title):
    text = title or ""
    for name, terms in CATEGORIES:
        if any(term in text for term in terms):
            return name
    return FALLBACK


def keywords(titles, topk=4):
    try:
        import jieba.analyse
        return [w for w in jieba.analyse.extract_tags("。".join(titles), topK=topk)
                if len(w) > 1][:topk]
    except Exception:
        return []


def categories(db, hours=24):
    """按最近 hours 小时的 records 标题分类；返回 [{name,count,keywords,examples}]。"""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.execute("SELECT title FROM records WHERE coalesce(published_at, fetched_at) >= ?",
                      (since,)).fetchall()
    buckets = {}
    for row in rows:
        name = classify(row[0])
        buckets.setdefault(name, []).append(row[0])
    out = []
    for name, titles in buckets.items():
        out.append({"name": name, "count": len(titles),
                    "keywords": keywords(titles),
                    "examples": titles[:3]})
    out.sort(key=lambda item: (-item["count"], item["name"]))
    return out
