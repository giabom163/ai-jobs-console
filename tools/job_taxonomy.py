"""Read-time role, employment and city facets; independent of AI inclusion rules."""
from __future__ import annotations

import re

if __package__:
    from .job_geography import is_domestic_only, location_evidence
    from .job_requirements import plain_text, summarize_requirements
    from .job_details import enrich_details
else:
    from job_geography import is_domestic_only, location_evidence
    from job_requirements import plain_text, summarize_requirements
    from job_details import enrich_details

VERSION = 'taxonomy-20261030-v2'
ROLES = {'research': 'AI算法与研究', 'application': 'AI应用工程', 'platform': 'AI平台与算力',
         'product': 'AI产品与设计', 'data': 'AI数据、训练与评测',
         'operations': 'AI运营与增长', 'solutions': 'AI解决方案与交付', 'unknown': '职能待确认'}
EMPLOYMENT = {'intern': '实习', 'regular': '正式岗', 'contract': '合同/项目', 'part_time': '兼职', 'unknown': '类型待确认'}
CITIES = {'beijing': '北京', 'shanghai': '上海', 'hangzhou': '杭州', 'shenzhen': '深圳'}
ROLE_RULES = (
    ('data', r'数据运营|数据标注|大模型数据实习生|大模型视频评测|训练师|语料|模型评测|评测.*(?:运营|专家)|数据质量|ai tutor|annotat'),
    ('platform', r'训练.*平台|推理.*平台|模型平台|AI平台|训推平台|训练.*框架|推理.*框架|机器学习.*框架|mlops|算力|gpu|基础架构|基础设施|分布式训练'),
    ('product', r'产品|交互设计|体验设计|product manager|ux designer'),
    ('solutions', r'解决方案|交付|实施|技术顾问|solutions|forward deployed'),
    ('operations', r'运营|增长|生态|operations|growth'),
    ('research', r'算法|研究|科学家|预训练|模型训练|research|scientist'),
    ('application', r'开发|全栈|后端|前端|应用.*工程师|agent.*工程师|ai.*工程师|ai engineer|software engineer'),
)


def enrich_job(row, source=None):
    job = dict(row)
    job = enrich_details(job, source)
    title = plain_text(job.get('title')).strip()
    description = plain_text(job.get('description'))
    role, reason = 'unknown', '标题未命中明确职能，需核对主要职责'
    for code, pattern in ROLE_RULES:
        match = re.search(pattern, title, re.I)
        if match:
            role, reason = code, '标题依据：' + match.group()
            break
    # An ambiguous title is not classified from scattered keywords in requirements.
    employment, evidence = 'unknown', '来源未提供可确认的招聘类型'
    explicit = job.get('employment_type')
    if explicit in EMPLOYMENT and explicit != 'unknown':
        employment, evidence = explicit, '来源结构化招聘类型'
    elif re.search(r'实习|\bintern(?:ship)?\b', title, re.I):
        employment, evidence = 'intern', '岗位标题明确实习'
    elif re.search(r'兼职|part[ -]?time', title, re.I):
        employment, evidence = 'part_time', '岗位标题明确兼职'
    elif re.search(r'外包|项目制|contractor|freelance', title, re.I):
        employment, evidence = 'contract', '岗位标题明确合同/项目'
    elif (job.get('source') == 'official:bytedance' and title and re.search(
        r'(?m)^' + re.escape(title) + r'\s*\n[^\n]{1,80}\n正式\s*\n[^\n]{1,80}\n职位 ID[：:]\s*[^\n]+\n职位描述', description)):
        employment, evidence = 'regular', '字节官网职位详情标注正式'
    elif (job.get('source') == 'official:baidu' and title and re.fullmatch(
        r'https://talent\.baidu\.com/jobs/detail/GRADUATE/[0-9a-f-]{36}', str(job.get('url') or ''))):
        header = re.search(r'(?m)^' + re.escape(title) + r'\s*\n([^\n]+)', description)
        if header and re.search(r'校招|AIDU项目', header.group(1)) and not re.search(r'实习', header.group(1)):
            employment, evidence = 'regular', '百度官网详情页标注校招/AIDU项目'
    elif re.search(r'(?:招聘类型|用工类型|岗位类型|工作性质)\s*[：:]\s*(?:正式|全职)|本岗位为正式岗位', description):
        employment, evidence = 'regular', '来源描述明确正式/全职岗位'
    location = str(job.get('location') or '')
    geography = location_evidence(location)
    city_codes = ([code for code, name in CITIES.items()
                   if name in location or re.search(r'\b' + code + r'\b', location, re.I)]
                  if is_domestic_only(location) else [])
    identity_confirmed = (job.get('presence_status') == 'confirmed' and
                          bool(job.get('source_response_sha256')))
    hiring_status = {'active': ('active', '来源状态已映射为在招'),
                     'expired': ('closed', '来源状态已映射为已结束')}.get(
                         job.get('source_status'), ('unknown', '未知（来源状态码未映射）'))
    job.update(identity_evidence_status='confirmed' if identity_confirmed else 'unknown',
               identity_evidence_checked_at=job.get('presence_checked_at'),
               hiring_status=hiring_status[0],
               hiring_status_label=hiring_status[1],
               source_date_status='known' if job.get('source_updated_at') else 'unknown',
               last_evidence_check_at=job.get('last_verified_at'),
               liveness_label=('历史存活观察' if job.get('liveness') == 'active' else
                               '链接已判定过期' if job.get('liveness') == 'expired' else
                               '链接状态未确认' if job.get('liveness') else '尚无链接检查'))
    job.update(role_family=role, role_label=ROLES[role], role_reason=reason,
               employment_type=employment, employment_label=EMPLOYMENT[employment], employment_evidence=evidence,
               city_codes=city_codes, location_raw=location, geography_status=geography['classification'],
               geography_version=geography['version'], geography_evidence=geography, taxonomy_version=VERSION,
               requirements_summary=summarize_requirements(description), search_text=title + '\n' + str(job.get('company') or '') + '\n' + str(job.get('department') or ''))
    return enrich_details(job, source)
