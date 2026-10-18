"""规则版实体抽取：jieba 词性标注，取人名/地名/机构名/专名。

不引入模型，不保证召回；只作为结构化线索，不参与结论判断。
"""
from __future__ import annotations

import re

GENERIC = {"视频", "用户", "网友", "记者", "编辑", "来源", "中国", "美国", "日本",
           "今天", "昨天", "现在", "我们", "他们", "她们", "自己"}


def extract_entities(text, limit=10):
    text = (text or "").strip()
    if not text:
        return []
    try:
        import jieba.posseg as pseg
        words = pseg.cut(text)
    except Exception:
        # 无 jieba 时退化为“2-4 字中文 + 常见后缀”的粗糙规则
        return sorted(set(re.findall(r"[\u4e00-\u9fff]{2,4}(?:大学|公司|集团|政府|法院|警方)", text)))[:limit]
    found = []
    seen = set()
    for word, flag in words:
        if flag not in ("nr", "ns", "nt", "nz"):
            continue
        word = word.strip()
        if len(word) < 2 or word in GENERIC or word in seen:
            continue
        seen.add(word)
        found.append(word)
        if len(found) >= limit:
            break
    return found
