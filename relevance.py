"""性别对立相关度粗筛（本地规则，无模型）。

口径：标题/摘要里同时出现「性别主体词」与「冲突/对立词」，或命中强特征短语，
才算与性别对立相关。宁可漏报，不把普通女性议题当成对立内容。
"""
from __future__ import annotations

GENDER_TERMS = (
    "女性", "男性", "女生", "男生", "女人", "男人", "性别", "女权", "男权",
    "女性主义", "性别歧视", "性别对立", "男女对立", "厌女", "媚男", "彩礼",
    "婚恋", "生育", "堕胎", "家务", "重男轻女", "女子", "妇女", "女童",
    "宝妈", "剩女", "雌竞", "女博主", "女乘客", "女员工", "女学生", "男女",
)

CONFLICT_TERMS = (
    "对立", "冲突", "骂战", "互撕", "打拳", "拳师", "女拳", "男拳", "撕裂",
    "争议", "大战", "开战", "嘲讽", "双标", "仇女", "仇男", "歧视", "攻击",
    "互喷", "对骂", "性别战争", "性别矛盾", "性别议题", "厌女", "媚男",
    "家暴", "性骚扰", "性侵", "殴打", "尾随", "骚扰", "羞辱", "物化", "规训",
    "重男轻女", "催婚", "逼婚", "荡妇", "受害者有罪",
)

# 强特征：不依赖共现也能判定；「打拳/拳师」故意不放这里（拳击/健身会误放）
STRONG_TERMS = (
    "性别对立", "男女对立", "性别战争", "性别矛盾", "女拳", "男拳",
    "厌女", "媚男", "仇女", "仇男", "重男轻女", "家暴", "性骚扰", "性侵",
)


def _hit(text, terms):
    return any(term in text for term in terms)


def _normalize(text):
    import re
    import unicodedata
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", text)


def explain(title, summary=""):
    """返回 (relevant, score, reason)；reason 用于审计，不做展示。"""
    text = _normalize(f"{title or ''} {summary or ''}")
    if not text.strip():
        return False, 0, "empty"
    strong = [t for t in STRONG_TERMS if t in text]
    if strong:
        return True, 2 + len(strong), "strong:" + ",".join(strong[:3])
    gender = [t for t in GENDER_TERMS if t in text]
    conflict = [t for t in CONFLICT_TERMS if t in text]
    if gender and conflict:
        return True, 1 + len(gender) + len(conflict), "pair:" + ",".join((gender + conflict)[:4])
    if gender:
        return False, 0, "gender-only"
    return False, 0, "unrelated"


def is_relevant(title, summary=""):
    return explain(title, summary)[0]
