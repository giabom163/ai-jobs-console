"""采集层统一噪声过滤：短剧/影视/小说、明显广告、B 站充电视频。

只挡“明显不是舆情讨论”的内容，不做主题判断；性别/生育议题保留。
"""
from __future__ import annotations

import re

FICTION_TERMS = (
    "短剧", "微短剧", "小说", "免费版", "电视剧", "全集", "在线观看",
    "大结局", "爽文", "神豪", "穿越", "重生", "连载", "番外", "漫画", "动漫",
)
AD_TERMS = (
    "商务合作", "恰饭", "带货", "优惠码", "领券", "优惠券",
    "点击查看", "扫码", "加微信", "代购", "购买链接",
    "广告合作", "广告推广", "投放广告", "好物推荐", "限时领取",
)
CHARGED_TERMS = (
    "充电视频", "充电专属", "充电福利", "充电计划", "充电动态",
    "充电专属视频", "【充电】",
)
EPISODE_RE = re.compile(r"第\s*[一二三四五六七八九十百零两\d]+\s*[集话]")


def noise_reason(title, summary=""):
    """返回 fiction / ad / charged / None；只看标题，避免正文误杀。"""
    text = re.sub(r"\s+", "", title or "")
    if not text:
        return None
    if any(term in text for term in CHARGED_TERMS):
        return "charged"
    if EPISODE_RE.search(text) or any(term in text for term in FICTION_TERMS):
        return "fiction"
    if any(term in text for term in AD_TERMS):
        return "ad"
    return None


def is_noise(title, summary=""):
    return noise_reason(title, summary) is not None
