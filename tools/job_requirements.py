"""Extract source-backed requirement bullets locally; never invent missing facts."""
from __future__ import annotations

import html
import re

VERSION = 'requirements-20261029-v2'
DIMENSIONS = (
    ('education', '学历与专业', r'学历|本科|硕士|博士|专业|bachelor|master|degree'),
    ('experience', '经验与项目', r'经验|经历|项目|实战|工作年限|years?'),
    ('coding', '编程与工程', r'编程|代码|开发语言|python|java|c\+\+|typescript|golang|sql|框架'),
    ('ai', 'AI与模型能力', r'大模型|算法|机器学习|深度学习|模型|llm|rag|agent|prompt'),
    ('data', '数据与分析', r'数据|统计|分析|实验|标注|评测'),
    ('product', '产品与业务', r'产品|业务|用户|需求|商业'),
    ('research', '研究与成果', r'论文|研究|竞赛|开源|专利|发表'),
    ('communication', '沟通与协作', r'沟通|交流|协作|合作|协调|表达'),
    ('learning', '学习与解决问题', r'学习|好奇心|自驱|解决问题|逻辑|求知'),
    ('availability', '语言与到岗', r'英语|英文|语言能力|到岗|出勤|每周|实习时长|实习.*[月天]'),
)


def plain_text(value):
    text = re.sub(r'<(?:br\s*/?|/p|/div|/li|/h[1-6])\s*>', '\n', str(value or ''), flags=re.I)
    return html.unescape(re.sub(r'<[^>]*>', '', text)).replace('\r\n', '\n').replace('\r', '\n')


def _source_items(section, base):
    """Yield source-backed bullet text and its exact offset, including inline bullets."""
    for line in re.finditer(r'[^\n]+', section):
        raw = line.group()
        markers = list(re.finditer(r'(?<!\S)[\-•·▪●]\s*', raw))
        if not markers:
            value = raw.strip()
            if value:
                yield base + line.start() + len(raw) - len(raw.lstrip()), value
            continue
        for index, marker in enumerate(markers):
            start = marker.end()
            end = markers[index + 1].start() if index + 1 < len(markers) else len(raw)
            fragment = raw[start:end]
            value = fragment.strip()
            if value:
                leading = len(fragment) - len(fragment.lstrip())
                yield base + line.start() + start + leading, value


def summarize_requirements(description, limit=10):
    """Prefer explicit sections; historical numbered boundaries are marked for review."""
    text = plain_text(description)
    # Only accept a section heading, not phrases such as “符合岗位要求的同学”.
    heading = r'(?:岗位要求|任职要求|任职资格|招聘要求|职位要求|职责要求|基本要求|核心要求|(?:Basic[ \t]+)?Qualifications|Requirements)'
    prefix = r'(?:(?:^[ \t]*|(?<=[。；;])[ \t]*)(?:(?:#{1,6}|[一二三四五六七八九十]+[、.])[ \t]*)?)'
    suffix = r'[】\]]?[ \t\u200b\ufeff]*(?:[：:]|(?=$)|(?=1[.、．)）]|教育背景[：:]))'
    explicit_inline = r'(?<=\s)[【\[]?' + heading + r'[】\]]?[ \t\u200b\ufeff]*(?:[：:]|(?=1[.、．)）]|[-●•]))'
    start = re.search(r'(?:' + prefix + r'[【\[]?' + heading + suffix + r'|' + explicit_inline + r')', text, re.I | re.M)
    inferred = False
    if start:
        begin = start.end()
    else:
        # The historical Meituan collector concatenated duty + requirement without
        # their headings. A second numbered list plus qualification language is
        # a recoverable boundary, but remains explicitly marked for review.
        begin, seen_number = None, False
        for line in re.finditer(r'(?m)^\s*(\d+)[.、．)）]\s*([^\n]+)', text):
            number, body = int(line[1]), line[2].strip()
            if number == 1 and seen_number and not re.match(r'负责|参与|建设|推动|协助|跟进', body):
                if re.search(r'具备|熟练|熟悉|学历|本科|硕士|博士|掌握|精通|经验|能力|专业', body[:60]):
                    begin, inferred = line.start(), True
                    break
            seen_number = True
        if begin is None:
            return {'status': 'unstructured', 'items': [], 'version': VERSION,
                    'note': '原文要求边界不明确，暂不把职责推断为要求。'}
    stop = re.search(r'(?:岗位职责|工作职责|岗位亮点|职位亮点|职位描述|岗位福利|薪资福利|Responsibilities|Benefits|^[ \t]*投递说明(?:[：:]|[ \t]*$))', text[begin:], re.I | re.M)
    end = begin + stop.start() if stop else len(text)
    section = text[begin:end]
    pieces = list(_source_items(section, begin))
    items, seen, preferred = [], set(), False
    for evidence_start, raw in pieces:
        if not raw or re.fullmatch(r'[\s【】\[\]:：]+', raw):
            continue
        if re.fullmatch(r'[【\[]?(?:基本背景|基本条件|岗位要求|任职要求)[】\]]?[：:]?', raw) or re.match(r'^(?:来源更新时间|更新时间|发布时间|发布日期)[：:]',raw):
            continue
        if re.search(r'以下.*优先|加分项|优先条件|Preferred Qualifications|Nice to have', raw, re.I):
            preferred = True
            if len(raw) < 40 and not re.match(r'\s*\d', raw):
                continue
        line = re.sub(r'^[\s【】\[\]•·\-]*(?:\d+[.、．)）]\s*)?', '', raw).strip()
        line = line.rstrip('；;。')
        if len(line) < 5 or line in seen:
            continue
        seen.add(line)
        evidence = raw
        tags = [label for _, label, pattern in DIMENSIONS if re.search(pattern, line, re.I)]
        preference = r'优先|加分|preferred|nice to have'
        priority = 'preferred' if preferred or re.search(preference, line, re.I) else 'required'
        segments = re.split(r'[；;，,]', line)
        if not preferred and priority == 'preferred' and any(len(s.strip()) > 5 and not re.search(preference, s, re.I) for s in segments):
            priority = 'mixed'
        items.append({'text': line, 'brief': line if len(line) <= 150 else line[:150] + '…', 'priority': priority,
                      'dimensions': tags, 'evidence': evidence,
                      'start': evidence_start, 'end': evidence_start + len(evidence)})
    # Bound output by source bullets rather than padding to ten synthetic claims.
    cap = min(10, max(1, limit))
    return {'status': ('boundary_inferred' if inferred else 'extracted') if items else 'empty', 'items': items[:cap],
            'total_source_bullets': len(items), 'truncated': len(items) > cap,
            'version': VERSION, 'note': ('待复核：历史文本未保留章节名，按编号重新开始及资格措辞识别要求。' if inferred else '') +
            '原文要点摘录；要求/优先依原文标记，完整要求请看原文。'}
