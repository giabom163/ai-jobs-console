#!/usr/bin/env python
"""AI 岗位判定词典与规则（单一事实源）。

设计目标（2026-10-22 用户要求）：
1. 只把"真正做 AI / 直接支撑 AI"的岗位判为 AI，其余一律 none（前端默认不展示）。
2. 排除"（AI 驱动）""（AI 工具）"这类把 AI 当工具用的括号限定语。
3. 招聘/人事/财务/税务/法务/公事务/销售/门店/配送等职能岗，即使标题带 AI 也不进 AI 库，
   除非同时出现算法/大模型等强技术词。
4. 英文 "ai"/"ml" 必须整词匹配，避免 retail/training/chain 之类误命中。

分级：
  build    直接研发 AI（算法、大模型、训练/推理、Agent 开发、具身智能、自动驾驶…）
  adjacent 直接支撑 AI（AI 产品/运营/评测/治理/战略、AI 数据、算力/平台…）
  none     其余（剔除）

用法：
  from tools.ai_lexicon import classify
  tier, conf, reason = classify(title, description, department)
"""
from __future__ import annotations

import re

# —— 强技术词：出现即说明岗位本身在做 AI ——
STRONG_TERMS = (
    "算法", "大模型", "机器学习", "深度学习", "强化学习", "自然语言处理", "计算机视觉",
    "多模态", "aigc", "生成式", "具身智能", "自动驾驶", "智能驾驶", "无人配送",
    "视觉生成", "视频生成", "世界模型", "基座模型", "智能体", "大语言模型",
    "推荐算法", "搜索算法", "广告算法", "风控算法", "运筹优化", "知识图谱",
    "数据挖掘", "模型训练", "模型推理", "推理优化", "向量检索",
    "语音识别", "语音合成", "感知算法", "规划算法", "控制算法", "定位算法",
    "预测算法", "决策算法", "算法工程师", "算法专家", "算法研究员", "算法科学家",
    "transformer", "pytorch", "tensorflow",
    # 英文岗位（ATS 海外 board 用）；只放 AI 专有词，避免误伤普通工程岗
    "machine learning", "deep learning", "computer vision", "natural language",
    "reinforcement learning", "generative ai", "genai", "foundation model",
    "large language model", "multimodal", "recommender", "recsys",
    "ranking model", "perception", "autonomy", "ml engineer", "ai engineer",
    "applied scientist", "research scientist", "model training", "fine-tuning",
    "cuda", "distributed training",
)

# —— 明确的技术研发动作（用于判断 AI 岗位属于 build 还是 adjacent）——
BUILD_ROLE_WORDS = ("开发", "工程师", "研发", "架构", "算法", "科学家", "工程化", "全栈", "工程")
# —— 应用/支撑动作 ——
ADJACENT_ROLE_WORDS = ("产品", "运营", "评测", "治理", "战略", "分析", "数据", "生态",
                       "增长", "策略", "解决方案", "交付", "顾问", "研究")

# —— 明确不算 AI 的职能岗（标题出现这些且没有强技术词 → none）——
NON_TECH_FUNCTIONS = ("招聘", "人事", "人力资源", "hrbp", "人才发展", "财务", "税务", "法务", "公事务",
                      "采购", "行政", "销售", "商务", "渠道", "品牌", "公关",
                      "薪酬", "培训", "门店", "店长", "配送", "骑手", "仓", "客服",
                      "内容运营", "新媒体", "客户经理", "投资", "融资")

# —— AI 业务的商务/管理职能：仍与 AI 相关，但不做研发 → adjacent ——
ADJACENT_FUNCTIONS = ("商业化", "合规", "负责人", "经理", "总监", "主管",
                      "compliance", "program manager", "tpm", "operations manager")

# —— 把 AI 当工具用的括号限定语：判定前先剥掉 ——
QUALIFIER_RE = re.compile(r"[（(][^）)]*(?:驱动|工具|辅助|赋能)[^）)]*[）)]")

# 英文整词匹配
AI_TOKEN_RE = re.compile(r"(?<![a-z])(ai|ml|llm|nlp|aigc|agent|rag)(?![a-z])")
# 英文职能岗必须整词匹配：否则 "hr" 会命中 chrome/thread，"rag" 会命中 storage
NON_TECH_EN = re.compile(
    r"\b(hr|legal|counsel|recruiter|talent|finance|accounting|tax|sales|marketing|"
    r"communications|policy|paralegal)\b")


def non_tech_hits(text: str) -> list[str]:
    hits = _hits(text, NON_TECH_FUNCTIONS)
    m = NON_TECH_EN.search(text)
    if m:
        hits.append(m.group(1))
    return hits

# —— 支撑 AI 的相邻词（本身不构成 build）——
ADJACENT_TERMS = ("mlops", "算力", "推理平台", "模型平台", "向量数据库", "gpu集群",
                  "数据标注", "标注平台", "评测体系", "模型评测", "ai治理", "ai安全",
                  "ai伦理", "提示词", "prompt", "agent平台", "智能体平台", "开发者生态",
                  "搜推", "搜索推荐", "推荐系统", "数据科学家",
                  "data scientist", "ai product", "applied ai", "forward deployed",
                  "inference optimization", "model evaluation")


def _norm(text) -> str:
    # 保留空格：英文整词（ai / agent）需要边界，去掉空格会把 AI Agent 并成 aigent
    if isinstance(text, (list, tuple)):
        text = " ".join(str(x) for x in text)
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def _hits(text: str, terms) -> list[str]:
    return [t for t in terms if t in text]


def classify(title: str, description: str = "", department: str = ""):
    """返回 (tier, confidence, reason)。tier ∈ build/adjacent/none。"""
    raw_title = title or ""
    # 先剥掉"（AI 驱动）"这类限定语，只看岗位本体
    stripped = QUALIFIER_RE.sub("", raw_title)
    head = _norm(stripped) + _norm(department)

    strong = _hits(head, STRONG_TERMS)
    token = AI_TOKEN_RE.search(head)
    adjacent_hits = _hits(head, ADJACENT_TERMS)

    # 1) 纯职能岗：没有强技术词，也没有 AI 相邻技术词 → 直接 none
    if not strong and not adjacent_hits:
        non_tech = non_tech_hits(head)
        if non_tech and not token:
            return "none", 0.1, "function:" + non_tech[0]

    # 2) 强技术词命中
    if strong:
        tech_core = _hits(head, ("算法", "工程师", "研发", "科学家", "架构", "工程化"))
        func = non_tech_hits(head)
        if func and not tech_core:
            return "none", 0.2, "function:" + func[0]
        adj_func = _hits(head, ADJACENT_FUNCTIONS)
        if adj_func and not tech_core:
            return "adjacent", 0.7, "business:" + adj_func[0] + "+title:" + strong[0]
        ops_role = _hits(head, ("运营", "产品", "内容", "活动", "生态", "评测", "开发者", "社区"))
        dev = "开发" in head and "开发者" not in head
        tech = bool(_hits(head, ("算法", "研发", "工程师"))) or dev
        role = "运营" if ops_role and not tech else "研发"
        if role == "运营":
            return "adjacent", 0.75, "title:" + ",".join(strong[:2]) + "+ops"
        return "build", 0.9, "title:" + ",".join(strong[:2])

    # 3) 相邻技术词（算力/MLOps/AI 治理…）
    if adjacent_hits:
        return "adjacent", 0.7, "title:" + ",".join(adjacent_hits[:2])

    # 4) 只有 AI/ML/LLM 这类 token：按岗位动作分级；职能岗仍排除
    if token:
        non_tech = non_tech_hits(head)
        en_build = _hits(head, ("engineer", "developer", "scientist", "researcher", "architect"))
        en_service = _hits(head, ("support", "deployed", "solutions", "sales", "success",
                                  "advocate", "tutor", "evangelist"))
        if non_tech and not (_hits(head, BUILD_ROLE_WORDS) or en_build):
            return "none", 0.15, "function:" + non_tech[0]
        if (_hits(head, BUILD_ROLE_WORDS) or en_build) and not en_service:
            return "build", 0.8, "title:" + token.group(1)
        if _hits(head, ADJACENT_ROLE_WORDS) or en_service:
            return "adjacent", 0.7, "title:" + token.group(1)
        return "adjacent", 0.55, "title:" + token.group(1)

    # 5) 标题没有 AI：一律 none。
    # 2026-10-22 撤掉"描述里强词多就升 adjacent"的兜底：机器人电子电气架构师、
    # 功能安全工程师这类岗位 JD 里会写具身智能/多模态，被误升成 AI 岗。
    # 宁可漏，不可错——类型必须能在标题上说清楚。
    return "none", 0.1, "no-match"





