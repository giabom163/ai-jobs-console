"""性别议题分类内核（纯函数、无模型、无网络）。

**一个内核，所有接口共用**：`classify / classify_tier / explain / is_relevant /
is_gender_related` 与 `collection_policy.gate_candidate` 都只调用本文件的 `analyze()`，
不再各自实现一套优先级，避免接口之间口径漂移。

四轴分开（不得互相替代）：
- `involves_conflict`：内容是否涉及性别对立/冲突（规则分层结果，非作者态度）；
- `author_stance`：作者立场，规则层无证据 → `unknown`，不以负面情感替代；
- `intensity`：冲突强度，无校准证据 → `unknown`，不以词命中数冒充实测强度；
- `heat`：传播热度，只接受显式来源整数，未知保持 `unknown`。

`conflict_score` 仅为**内容线索排序分**（历史签名保留），不含播放量加成，
不是校准风险分；历史库中的旧值未重算。
"""
from __future__ import annotations

import re
import unicodedata

import content_filter  # 噪声门共用：游戏实况/剧情/AI 故事不因“彩礼/家暴/女儿”混入

# 规则版本随任何词表/优先级变化递增；分歧队列按此版本记录“当前规则”。
RULE_VERSION = "classification-core-v4"

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

# 强特征：**不依赖主体词共现**也能判定为冲突内容；「打拳/拳师」故意不放这里（拳击/健身会误放）。
STRONG_TERMS = (
    "性别对立", "男女对立", "性别战争", "性别矛盾", "女拳", "男拳",
    "厌女", "媚男", "仇女", "仇男", "重男轻女", "性骚扰", "性侵",
    "极端女权", "极端男权",
)

# 生理科普/两性技巧/医学常识：即使出现性别词，也不属于性别对立（强特征优先）。
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

# 实质议题（政策/制度/权益/法律/职场）：仅凭“女子/男人”主体词 + 普通刑案/生活
# 事件不足以判定为 issue，必须另有这些议题信号或冲突词，才保留为 gender 议题。
ISSUE_TERMS = (
    "政策", "制度", "立法", "法律", "法规", "法院", "判决", "起诉", "维权",
    "权益", "平权", "平等", "歧视", "保障", "规定", "职场", "就业", "招聘",
    "薪酬", "同工同酬", "产假", "陪产假", "育儿", "社保", "养老", "退休",
    "补贴", "津贴", "公共", "户籍", "入学", "医疗", "庇护", "救助", "保护",
    "互助", "权益保护", "性别平等", "公共政策", "社会福利",
)

# 窄关系组合（不把“特权/共谋/举报”等单词放进强词）：
# 1) 双性别群体 + 特权/失权；2) 女权 + 女吸血鬼 + 共谋。
# 只有组合命中才判冲突，单词本身不构成冲突。
MALE_GROUP_TERMS = ("男性", "男人", "男生", "男方", "男子", "男孩", "男员工",
                    "男乘客", "男领导", "男同事", "男博主")
FEMALE_GROUP_TERMS = ("女性", "女人", "女生", "女方", "女子", "女孩", "女员工",
                      "女乘客", "女领导", "女同事", "女博主")
POWER_RELATION_TERMS = ("特权", "失权")
FEMINISM_TERMS = ("女权", "女权主义", "女性主义")
FEMALE_VAMPIRE_TERMS = ("女吸血鬼",)
COLLUSION_TERMS = ("共谋", "合谋", "勾结")
# 议题行动组合：同时出现“反对”与“举报”，且明确指向男权/女权，才保留为 issue。
# 单词“反对/举报”不单独构成议题或冲突。
IDEOLOGY_ACTION_TERMS = ("反对", "举报")

# 匹配依据口径：所有命中都在 NFKC + lower + 去空白 的规范化文本上计算，
# 记录 term / field / variant，便于复核时区分“原字段片段”与“规范化词命中”。
MATCH_VARIANT = "normalized_nfkc_lower_nospace"

CATEGORIES = (
    ("gender", GENDER_TERMS),
    ("conflict", CONFLICT_TERMS),
    ("strong", STRONG_TERMS),
    ("exclude", EXCLUDE_TERMS),
    ("context", CONTEXT_TERMS),
    ("issue", ISSUE_TERMS),
    ("male_group", MALE_GROUP_TERMS),
    ("female_group", FEMALE_GROUP_TERMS),
    ("power_relation", POWER_RELATION_TERMS),
    ("feminism", FEMINISM_TERMS),
    ("female_vampire", FEMALE_VAMPIRE_TERMS),
    ("collusion", COLLUSION_TERMS),
    ("ideology_action", IDEOLOGY_ACTION_TERMS),
)


def _normalize(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", text)


def _dedupe(values):
    seen, out = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _field_hits(title, summary):
    """逐字段匹配，避免跨字段拼接造词（如“他家”+“暴了”）。"""
    hits = {name: [] for name, _ in CATEGORIES}
    evidence = []
    for field, raw in (("title", title), ("summary", summary)):
        text = _normalize(raw or "")
        if not text:
            continue
        for name, terms in CATEGORIES:
            for term in terms:
                if term in text:
                    hits[name].append(term)
                    evidence.append({"field": field, "term": term, "variant": MATCH_VARIANT})
    return {name: _dedupe(values) for name, values in hits.items()}, evidence


def observed_heat(value, source_field="views", *, explicit=True):
    """热度只接受显式来源非负整数；其余（None/0 占位/布尔/字符串）一律 unknown。

    `explicit=False` 用于历史库 views（旧默认 0 可能是未知占位），不当真实热度。
    """
    if not explicit:
        return {"known": False, "value": None, "source_field": None,
                "basis": "legacy_or_unknown_heat_field"}
    if type(value) is int and value >= 0:  # noqa: E721  bool 是 int 子类，必须排除
        return {"known": True, "value": value, "source_field": source_field,
                "basis": "explicit_source_integer"}
    if value is None or value == "":
        return {"known": False, "value": None, "source_field": None, "basis": "missing"}
    return {"known": False, "value": None, "source_field": None, "basis": "not_an_integer"}


def analyze(title, summary="", *, views=None, views_explicit=True):
    """分类内核：返回分层、四轴与可审计匹配依据。

    subject_only（仅主体词）是宽候选：gender_related=True，但 tier=unrelated、
    opposition=False，正式 substantive 不因此准入。
    `views` 只在显式来源整数的前提下进入 `heat`；不参与 tier/强度/内容分。
    """
    raw_title, raw_summary = title or "", summary or ""
    hits, evidence = _field_hits(raw_title, raw_summary)
    heat = observed_heat(views, explicit=views_explicit)
    noise = content_filter.noise_analysis(raw_title, raw_summary)

    def result(tier, reason, path, gender_related=False):
        involves = tier == "conflict"
        return {
            "rule_version": RULE_VERSION,
            "tier": tier,
            "gender_related": bool(gender_related),
            "opposition": involves,
            "involves_conflict": involves,
            "author_stance": "unknown",
            "author_stance_basis": "rule_layer_has_no_stance_evidence",
            "intensity": "unknown",
            "intensity_basis": "no_calibrated_evidence",
            "heat": heat,
            "reason": reason,
            "path": path,
            "matched": hits,
            "evidence": {
                "terms": evidence,
                "variant": MATCH_VARIANT,
                "priority_path": path,
                "rule": path,
                "noise": noise,
            },
        }

    if not _normalize(raw_title) and not _normalize(raw_summary):
        return result("unrelated", "empty", "empty")
    if noise["is_noise"]:
        return result("unrelated", "noise:" + str(noise["reason"]), "noise")
    strong = hits["strong"]
    exclude = hits["exclude"]
    conflict = hits["conflict"]
    gender = hits["gender"]
    context = hits["context"] + hits["issue"]
    if strong:
        # 强特征独立成立：不需要另有主体词（如单独出现“仇男”）。
        return result("conflict", "strong:" + ",".join(strong[:3]), "strong", True)
    if exclude and not conflict:
        return result("unrelated", "excluded:" + ",".join(exclude[:3]), "excluded")
    # 窄关系组合：双性别群体 + 特权/失权（单词不单独构成冲突）。
    if hits["male_group"] and hits["female_group"] and hits["power_relation"]:
        terms = (hits["male_group"] + hits["female_group"] + hits["power_relation"])
        return result("conflict", "power_relation:" + ",".join(terms[:5]),
                      "power_relation", True)
    # 窄关系组合：女权 + 女吸血鬼 + 共谋（三者同时出现才命中）。
    if hits["feminism"] and hits["female_vampire"] and hits["collusion"]:
        terms = hits["feminism"] + hits["female_vampire"] + hits["collusion"]
        return result("conflict", "collusion:" + ",".join(terms[:5]), "collusion", True)
    # 主体词 + 冲突词：优先于议题行动组合，避免“反对并举报…歧视”被降为 issue。
    if gender and conflict:
        return result("conflict", "pair:" + ",".join((gender + conflict)[:4]), "subject_conflict", True)
    # 极窄议题组合：同一字段同时含“反对”+“举报”，且同字段明确指向男权/女权。
    per_field = {}
    for item in evidence:
        per_field.setdefault(item["field"], set()).add(item["term"])
    same_field_action = any(
        {"反对", "举报"} <= terms and ({"男权", "女权"} & terms)
        for terms in per_field.values())
    if same_field_action:
        action = hits["ideology_action"]
        ideology = [term for term in ("男权", "女权") if term in hits["gender"]]
        return result("issue", "ideology_action:" + ",".join((action + ideology)[:4]),
                      "ideology_action", True)
    if gender and context:
        return result("issue", "context:" + ",".join((gender + context)[:4]), "subject_issue", True)
    if gender:
        # subject_only 仍是宽候选：gender_related=True（兼容旧宽口径），
        # 但 tier=unrelated / opposition=False，正式 substantive 不因此放开。
        return result("unrelated", "subject_only:" + ",".join(gender[:3]), "subject_only", True)
    return result("unrelated", "unrelated", "unrelated")


def explain(title, summary=""):
    """兼容旧签名：返回 (relevant, clue_score, reason)。

    `clue_score` 只是词命中的内容线索排序分，不是校准强度。
    """
    analysis = analyze(title, summary)
    if analysis["tier"] != "conflict":
        return False, 0, analysis["reason"]
    matched = analysis["matched"]
    if analysis["path"] == "strong":
        score = 2 + len(matched["strong"])
    else:
        score = 1 + len(matched["gender"]) + len(matched["conflict"])
    return True, score, analysis["reason"]


def classify_tier(title, summary=""):
    """conflict / issue / unrelated（统一内核口径）。"""
    return analyze(title, summary)["tier"]


def conflict_score(title, summary="", views=0):
    """**内容线索排序分**（历史签名保留）：仅冲突词/强特征去重命中 × 10。

    - 不含播放量加成（本轮移除；历史 DB 旧值未重算）；
    - 同词重复出现不重复计分；
    - 不是校准的冲突强度/风险等级。
    """
    _ = views  # 显式接收但有意不使用：热度不得混入内容分。
    if classify_tier(title, summary) != "conflict":
        return 0.0
    hits = set(term for term in CONFLICT_TERMS if term in _normalize(title or ""))
    hits |= {term for term in CONFLICT_TERMS if term in _normalize(summary or "")}
    hits |= {term for term in STRONG_TERMS if term in _normalize(title or "")}
    hits |= {term for term in STRONG_TERMS if term in _normalize(summary or "")}
    return round(len(hits) * 10.0, 2)


def classify(title, summary=""):
    """返回 gender_related / opposition 两个独立口径（主要键保留）。

    gender_related：性别/生育/婚恋议题（宽），用于候选池或人工复核；
    opposition：性别对立/冲突（严），用于预警和展示，等于 tier == conflict。
    仅主体词的记录 gender_related=True 但 tier=unrelated，不能进正式库。
    """
    analysis = analyze(title, summary)
    return {
        "gender_related": analysis["gender_related"],
        "opposition": analysis["opposition"],
        "reason": analysis["reason"],
        "tier": analysis["tier"],
        "rule_version": analysis["rule_version"],
        "involves_conflict": analysis["involves_conflict"],
        "author_stance": analysis["author_stance"],
        "intensity": analysis["intensity"],
        "heat": analysis["heat"],
    }


def is_relevant(title, summary=""):
    """采集保留口径：必须命中性别对立/冲突，普通性别/生育议题不自动入库。"""
    return analyze(title, summary)["opposition"]


def is_gender_related(title, summary=""):
    """宽口径：性别/生育/婚姻议题（含纯主体），仅供候选池或人工复核。

    subject_only 为 True 只代表“宽候选”，tier 仍 unrelated、opposition 仍 False，
    不能当正式 substantive 准入。
    """
    return analyze(title, summary)["gender_related"]
