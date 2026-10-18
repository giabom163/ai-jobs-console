"""性别对立相关度粗筛（本地规则，无模型）。

两个口径：
- gender_related：性别/生育/婚恋议题（宽），用于采集保留；
- opposition：同时出现性别主体词与冲突词，或命中强特征（严），用于预警。
采集宽进严出，分类与预警再用 opposition。
"""
from __future__ import annotations

GENDER_TERMS = (
    "女性", "男性", "女生", "男生", "女人", "男人", "性别", "女权", "男权",
    "女性主义", "性别歧视", "性别对立", "男女对立", "厌女", "媚男", "彩礼",
    "婚恋", "生育", "堕胎", "怀孕", "备孕", "家务", "重男轻女", "女子", "妇女", "女童",
    "宝妈", "剩女", "雌竞", "男方", "女方", "女博主", "女乘客", "女员工", "女学生", "男女", "闺蜜", "师兄", "师姐", "姐妹", "室友", "女领导", "男领导", "女同事", "男同事", "前男友", "前女友", "渣男", "小三", "绿茶", "普信男", "小仙女", "龟男", "捞女", "凤凰男", "扶弟魔", "妈宝男", "女主",
    "婚姻", "结婚", "离婚", "择偶", "老公", "老婆", "婆婆", "岳母",
)

CONFLICT_TERMS = (
    "对立", "冲突", "骂战", "互撕", "打拳", "拳师", "女拳", "男拳", "撕裂",
    "大战", "开战", "嘲讽", "双标", "仇女", "仇男", "歧视", "攻击",
    "互喷", "对骂", "性别战争", "性别矛盾", "厌女", "媚男", "纠纷", "起诉", "退还",
    "家暴", "性骚扰", "性侵", "殴打", "尾随", "骚扰", "羞辱", "物化", "规训",
    "重男轻女", "催婚", "逼婚", "荡妇", "受害者有罪",
)

# 强特征：不依赖共现也能判定；「打拳/拳师」故意不放这里（拳击/健身会误放）
# 生理科普/两性技巧/医学常识：即使出现性别词，也不属于性别对立
EXCLUDE_TERMS = (
    "生理科普", "生理知识", "两性知识", "性教育", "性技巧", "情趣",
    "妇科", "男科", "医学科普", "健康科普", "月经", "前列腺",
    "勃起", "高潮", "性生活", "性行为", "生殖", "私处",
)

CONTEXT_TERMS = (
    "婚姻", "结婚", "离婚", "择偶", "老公", "老婆", "婆婆", "岳母", "相亲",
    "生育", "怀孕", "备孕", "婚恋", "彩礼", "嫁妆", "婚房", "分手", "出轨",
    "小三", "恋爱", "同居", "领证", "催婚", "逼婚", "前任",
)

STRONG_TERMS = (
    "性别对立", "男女对立", "性别战争", "性别矛盾", "女拳", "男拳",
    "厌女", "媚男", "仇女", "仇男", "重男轻女", "性骚扰", "性侵",
    "极端女权", "极端男权",
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
    # 分别归一化再拼接，避免 "他家"+"暴了" 跨字段造出 "家暴"
    text = _normalize(title or "") + "\n" + _normalize(summary or "")
    if not text.strip():
        return False, 0, "empty"
    excluded = [t for t in EXCLUDE_TERMS if t in text]
    strong = [t for t in STRONG_TERMS if t in text]
    if excluded and not strong:
        # 出现任意冲突词时仍保留（如“家暴科普”“性别争议科普”）
        if not any(t in text for t in CONFLICT_TERMS):
            return False, 0, "excluded:" + ",".join(excluded[:3])
    if strong:
        return True, 2 + len(strong), "strong:" + ",".join(strong[:3])
    gender = [t for t in GENDER_TERMS if t in text]
    conflict = [t for t in CONFLICT_TERMS if t in text]
    if gender and conflict:
        return True, 1 + len(gender) + len(conflict), "pair:" + ",".join((gender + conflict)[:4])
    if gender:
        return False, 0, "gender-only"
    return False, 0, "unrelated"


def classify_tier(title, summary=""):
    """conflict / issue / unrelated：冲突库只收主体词+冲突词。"""
    text = _normalize(title or "") + "\n" + _normalize(summary or "")
    if not text.strip():
        return "unrelated"
    subject = any(t in text for t in GENDER_TERMS)
    conflict = any(t in text for t in CONFLICT_TERMS) or any(t in text for t in STRONG_TERMS)
    context = any(t in text for t in CONTEXT_TERMS)
    if subject and conflict:
        return "conflict"
    if subject and context:
        return "issue"
    if subject:
        return "issue"
    return "unrelated"


def conflict_score(title, summary="", views=0):
    """冲突库排序分：冲突词命中 + 播放量归一化；非冲突为 0。"""
    if classify_tier(title, summary) != "conflict":
        return 0.0
    text = _normalize(title or "") + "\n" + _normalize(summary or "")
    hits = sum(1 for t in CONFLICT_TERMS if t in text) + sum(1 for t in STRONG_TERMS if t in text)
    try:
        views = max(0, int(views or 0))
    except (TypeError, ValueError):
        views = 0
    return round(hits * 10 + min(views / 10000.0, 30.0), 2)


def classify(title, summary=""):
    """返回 gender_related / opposition 两个独立口径。

    gender_related：性别/生育/婚恋议题（宽），用于采集保留；
    opposition：性别对立/冲突（严），用于预警和展示。
    """
    text = _normalize(title or "") + "\n" + _normalize(summary or "")
    if not text.strip():
        return {"gender_related": False, "opposition": False, "reason": "empty"}
    excluded = [t for t in EXCLUDE_TERMS if t in text]
    strong = [t for t in STRONG_TERMS if t in text]
    gender = [t for t in GENDER_TERMS if t in text]
    conflict = [t for t in CONFLICT_TERMS if t in text]
    if excluded and not strong and not conflict:
        return {"gender_related": False, "opposition": False,
                "reason": "excluded:" + ",".join(excluded[:3])}
    gender_related = bool(strong or gender)
    opposition, _, reason = explain(title, summary)
    return {"gender_related": gender_related, "opposition": opposition,
            "reason": reason}


def is_relevant(title, summary=""):
    """采集保留口径（2026-10-18 收紧）：必须命中性别对立/冲突，普通性别/生育议题不自动入库。"""
    return classify(title, summary)["opposition"]


def is_gender_related(title, summary=""):
    """宽口径：性别/生育/婚姻议题，仅供候选池或人工复核。"""
    return classify(title, summary)["gender_related"]
