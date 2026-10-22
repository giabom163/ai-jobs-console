"""采集层统一噪声过滤（所有入口共用，不是只放 CLI 端）。

拦截：短剧/影视/小说/剧情解说、游戏实况与角色“攒彩礼”打卡、音声/ASMR、
AI 故事/听书故事、明显广告、B 站充电视频。

原则：
- 只挡“明显不是舆情讨论”的内容，不做主题判断；性别/生育议题一律保留。
- **不泛禁**“游戏 / bl / 剧情”这类单词——真实讨论游戏圈性别歧视、BL 厌女
  仍要能入库，因此只按可解释的专名/组合标记拦截。
- 只看标题（summary 仅作后备），避免正文误杀。
- 有限例外：**明确讨论/批评作品中的性别歧视**（短剧/剧情/影视语境 + 讨论标记 +
  歧视标记，且无连载/广告/音声/充电等硬标记）不按噪声拦截。例外不放宽单词层面的
  全部剧情，也不覆盖 game/audio/ad/charged 硬噪声。
"""
from __future__ import annotations

import re

FICTION_TERMS = (
    "短剧", "微短剧", "小说", "免费版", "电视剧", "全集", "在线观看",
    "大结局", "爽文", "神豪", "穿越", "重生", "连载", "番外", "漫画", "动漫",
    # 本轮新增：明显剧情 / AI 故事 / 听书故事（来自真实漏网样本归纳）
    "剧情", "假故事", "ai故事", "ai全民制作人", "听别人说故事", "故事频道",
    "民间故事", "抢先版", "说故事", "故事会",
)

# 游戏实况 / 角色攒彩礼打卡：**单字不判**，只在“行为/计数标记”出现时才判 game——
# 避免误杀“现在年轻人攒彩礼有多难？”“女玩家通关率被质疑：游戏圈性别歧视”“女性职场打卡”。
ACTIVITY_TERMS = ("实况", "初见流程", "流程解说", "游戏实况", "实机演示")
CHECKIN_TERMS = ("攒彩礼", "打卡", "通关", "抽卡", "开箱", "苟卡")
GAME_NAME_TERMS = (
    "原神", "三国杀", "暗黑地牢", "刺客信条", "第五人格", "蛋仔派对",
    "王者荣耀", "崩坏", "星穹铁道", "明日方舟", "英雄联盟",
)
# 计数/连载标记：第N天/期/日/集/话/季，或 N天
COUNTER_RE = re.compile(r"第\s*[零一二三四五六七八九十百千两\d]+\s*[天期日集话季]|\d+\s*天")

AUDIO_TERMS = (
    "音声", "asmr", "助眠", "广播剧", "白噪音", "催眠",
)

DRAMA_MARKERS = ("痛悔", "跪求妻子", "跪求老婆")

# 窄剧情组合：家庭关系角色 + 病弱青梅/病弱白月光 + 坐不住/跪求/痛悔。
# 不单禁“青梅/白月光/病弱”；三个条件同时出现才算 drama（可审计），
# 明确批评该类剧情的性别歧视仍走既有有限讨论例外。
DRAMA_ROLE_TERMS = ("丈夫", "妻子", "老公", "老婆", "婆婆", "岳母", "未婚夫", "未婚妻")
DRAMA_FRAGILE_TERMS = ("病弱青梅", "病弱白月光")
DRAMA_REACTION_TERMS = ("坐不住", "跪求", "痛悔")

AD_TERMS = (
    "商务合作", "恰饭", "带货", "优惠码", "领券", "优惠券",
    "点击查看", "扫码", "加微信", "代购", "购买链接",
    "广告合作", "广告推广", "投放广告", "好物推荐", "限时领取",
)
CHARGED_TERMS = (
    "充电视频", "充电专属", "充电福利", "充电计划", "充电动态",
    "充电专属视频", "【充电】",
)
# 第 N 集/话/季：季用于【2~3季】这类连载短剧
EPISODE_RE = re.compile(r"第\s*[一二三四五六七八九十百零两\d]+\s*[集话季]")
# 【...抢先版...】/【...第N季...】连载短剧标记
SERIAL_RE = re.compile(r"【[^】]{0,16}(?:抢先版|抢先|全集|第\s*[一二三四五六七八九十\d]+\s*季|\d+\s*[~～\-]\s*\d+\s*季)[^】]{0,24}】")

# --- 有限例外：明确讨论/批评作品中的性别歧视 ------------------------------------------------
# 只有“讨论/批评”标记 **与** “歧视/物化”标记同时出现，且基础类别属于 fiction/drama，
# 且没有连载/计数/广告/音声/充电硬标记时，才放行。例：
#   “短剧里的性别歧视为何引发争议” → 保留（真实讨论）
#   “【抢先版】…痛悔家暴…跪求妻子（2）” → 仍为 fiction（硬标记）
#   “《xx》第3集：家暴剧情” → 仍为 fiction（连载标记）
DISCOURSE_TERMS = (
    "讨论", "争议", "批评", "反对", "质疑", "反思", "为什么", "为何", "是否",
    "如何看", "如何看待", "吐槽", "揭露", "抵制", "声讨", "评价", "声援", "驳斥",
)
DISCRIMINATION_TERMS = (
    "性别歧视", "厌女", "仇女", "媚男", "物化", "性骚扰", "家暴",
    "性别刻板", "刻板印象", "男性凝视", "辱女", "性别偏见", "性别不平等",
)
SOFT_NOISE_REASONS = ("fiction", "drama")


def _base_noise(text):
    """返回基础噪声类别或 None（不含有限例外）。"""
    low = text.lower()
    if any(term in text for term in CHARGED_TERMS):
        return "charged"
    if (EPISODE_RE.search(text) or SERIAL_RE.search(text)
            or any(term in low for term in FICTION_TERMS)):
        return "fiction"
    if any(term in low for term in ACTIVITY_TERMS):
        return "game"
    if any(term in low for term in CHECKIN_TERMS) and (
            any(term in low for term in GAME_NAME_TERMS) or COUNTER_RE.search(text)):
        return "game"
    if any(term in low for term in AUDIO_TERMS):
        return "audio"
    if any(term in text for term in DRAMA_MARKERS):
        return "drama"
    if (_drama_combo(text)):
        return "drama"
    if any(term in text for term in AD_TERMS):
        return "ad"
    return None


def _drama_combo(text):
    """家庭关系角色 + 病弱青梅/白月光 + 坐不住/跪求/痛悔，三者同时命中才判 drama。"""
    return (any(term in text for term in DRAMA_ROLE_TERMS)
            and any(term in text for term in DRAMA_FRAGILE_TERMS)
            and any(term in text for term in DRAMA_REACTION_TERMS))


def _discussion_exception(text, base):
    """有限、可解释的作品歧视讨论例外；返回命中的依据或 None。"""
    if base not in SOFT_NOISE_REASONS:
        return None
    # 硬标记一律不放行：连载/计数、广告、音声、充电。
    if EPISODE_RE.search(text) or SERIAL_RE.search(text) or COUNTER_RE.search(text):
        return None
    low = text.lower()
    if (any(term in text for term in AD_TERMS) or any(term in low for term in AUDIO_TERMS)
            or any(term in text for term in CHARGED_TERMS)):
        return None
    discourse = [term for term in DISCOURSE_TERMS if term in text]
    target = [term for term in DISCRIMINATION_TERMS if term in text]
    if not discourse or not target:
        return None
    return {"base_reason": base, "discourse": discourse[:3], "target": target[:3]}


def noise_analysis(title, summary=""):
    """返回可审计的噪声判定：{is_noise, reason, base_reason, exception, variant}。

    只看标题（与 noise_reason 一致），不因 summary 放开或拦截。
    """
    text = re.sub(r"\s+", "", title or "")
    if not text:
        return {"is_noise": False, "reason": None, "base_reason": None, "exception": None,
                "variant": "title_nospace_lower"}
    base = _base_noise(text)
    if base is None:
        return {"is_noise": False, "reason": None, "base_reason": None, "exception": None,
                "variant": "title_nospace_lower"}
    exception = _discussion_exception(text, base)
    if exception:
        return {"is_noise": False, "reason": None, "base_reason": base, "exception": exception,
                "variant": "title_nospace_lower"}
    return {"is_noise": True, "reason": base, "base_reason": base, "exception": None,
            "variant": "title_nospace_lower"}


def noise_reason(title, summary=""):
    """返回 fiction / game / audio / drama / ad / charged / None；只看标题。"""
    return noise_analysis(title, summary)["reason"]


def is_noise(title, summary=""):
    return noise_reason(title, summary) is not None
