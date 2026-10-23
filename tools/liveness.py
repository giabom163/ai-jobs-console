#!/usr/bin/env python
"""岗位「存活/过期」判定（只读取，不写库）。

参考成熟实现：career-ops 的 classifyLiveness（HTTP 状态 + 关闭横幅 + 申请按钮 +
跳转身份 + 截止日期 + 反爬识别，三态 active/expired/uncertain，且 uncertain 永不
当成关闭）。本项目按中文招聘页改写，并保留同一条原则：**只把硬证据判为过期**。

重要边界（2026-10-22 实测）：美团详情页是前端渲染的 SPA，原始 HTML 只有外壳，
连不存在的岗位也会渲染出「立即投递」模板文本 —— 因此**不能**用页面文本判断美团
岗位死活。美团走详情接口 `POST /api/official/job/getJobDetail`
（`data:null` + 「职位已下线或不存在」= 关闭）。页面文本判定只用于服务端渲染的
静态招聘页。

三态：
  active     页面有可见的投递入口，且没有关闭标识
  expired    404/410、关闭横幅、截止日期已过、跳到列表页且丢了岗位 ID
  uncertain  反爬/限流/5xx/内容不足/无投递入口 —— 只记录，不当关闭

用法：
  from tools.liveness import classify, fetch_and_classify, check_job
"""
from __future__ import annotations

import re
import unicodedata
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# —— 明确说"这个岗位没了"的中文/英文横幅 ——
HARD_EXPIRED = [
    r"职位已下线", r"岗位已下线", r"该职位已下线", r"该岗位已下线",
    r"职位已关闭", r"岗位已关闭", r"该职位已关闭", r"该岗位已关闭",
    r"已停止招聘", r"停止招聘", r"招聘已结束", r"已结束招聘",
    r"职位已过期", r"岗位已过期", r"该职位已失效", r"职位不存在", r"岗位不存在",
    r"页面不存在", r"页面走丢", r"您访问的页面.*(不存在|已删除)",
    r"该职位已招满", r"职位已下架", r"已下架",
    r"job (is )?no longer available", r"no longer accepting applications",
    r"this (job|role|position)( listing)? is closed", r"job (listing )?not found",
    r"this job has expired", r"position has been filled",
]

# —— 弱信号：可能是"相似岗位"卡片或 FAQ 里的话，只作 uncertain ——
SOFT_EXPIRED = [r"\bjob expired\b", r"招聘中.*已结束"]

# —— 可见投递入口（Meituan/常见 ATS 的中文按钮）——
APPLY = [r"立即投递", r"立即申请", r"申请职位", r"投递简历", r"我要投递",
         r"在线投递", r"简历投递", r"开始申请", r"\bapply now\b", r"\bapply\b",
         r"submit application", r"easy apply"]

# —— 反爬/风控页：绝不能当过期 ——
BOT_WALL = [r"just a moment", r"安全验证", r"滑动验证", r"访问验证", r"人机验证",
            r"verify you are (a |not a )?human", r"checking your browser",
            r"attention required.*cloudflare", r"\bcf-ray\b", r"ray id",
            r"请稍候.*验证", r"访问过于频繁"]

# —— 截止日期（中文页面常见写法）——
DEADLINE = re.compile(
    r"(?:截止日期|报名截止|投递截止|申请截止)[：:\s]*(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
JOB_ID = re.compile(r"(?:jobUnionId=|/job/|/jobs/|positionId=|jobId=)([A-Za-z0-9_-]{4,})")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", " ", text.replace("\u2019", "'").replace("\u201c", '"')).strip()


def _first(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m
    return None


def _deadline_passed(text: str, today: str):
    m = DEADLINE.search(text)
    if not m:
        return None
    try:
        iso = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    except ValueError:
        return None
    return iso if iso < today else None


def classify(status: int | None, requested_url: str, final_url: str,
             body_text: str, today: str) -> tuple[str, str, str]:
    """返回 (verdict, code, reason)。"""
    text = normalize(body_text)

    if status in (404, 410):
        return "expired", "http_gone", f"HTTP {status}"
    if _first(BOT_WALL, text):
        return "uncertain", "bot_wall", "命中反爬/验证页"
    if status in (401, 403, 429) or (status or 0) >= 500 or status is None:
        return "uncertain", "access_blocked", f"HTTP {status}（限流/封禁/网络错误，不等于关闭）"

    hit = _first(HARD_EXPIRED, text)
    if hit:
        return "expired", "closed_banner", f"关闭横幅：{hit.group(0)}"

    m1, m2 = JOB_ID.search(requested_url or ""), JOB_ID.search(final_url or "")
    if m1 and (not m2 or m2.group(1) != m1.group(1)):
        return "uncertain", "redirected_off_posting", "跳转后丢失岗位 ID，页面可能是列表页"

    deadline = _deadline_passed(text, today)
    if deadline:
        return "expired", "deadline_passed", f"截止日期已过：{deadline}"

    if _first(APPLY, text):
        return "active", "apply_visible", "页面有可见投递入口"

    soft = _first(SOFT_EXPIRED, text)
    if soft:
        return "uncertain", "soft_expired_signal", f"弱过期信号：{soft.group(0)}"

    if len(text) < 300:
        return "uncertain", "insufficient_content", f"正文仅 {len(text)} 字，可能未加载"

    return "uncertain", "no_apply_control", "有正文但未识别到投递入口"


def fetch_and_classify(url: str, today: str, timeout: int = 20):
    """GET 页面并判定；跟随重定向，记录最终 URL 与状态码。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(600_000)
            try:
                body = raw.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = raw.decode("gbk", "replace")
            verdict, code, reason = classify(resp.status, url, resp.geturl(), body, today)
            return {"verdict": verdict, "code": code, "reason": reason,
                    "http_status": resp.status, "final_url": resp.geturl()}
    except urllib.error.HTTPError as exc:
        return {"verdict": "expired" if exc.code in (404, 410) else "uncertain",
                "code": f"http_{exc.code}", "reason": f"HTTP {exc.code}",
                "http_status": exc.code, "final_url": url}
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "uncertain", "code": "fetch_error", "reason": str(exc)[:200],
                "http_status": None, "final_url": url}


MEITUAN_DETAIL = "https://zhaopin.meituan.com/api/official/job/getJobDetail"


def meituan_detail(job_union_id: str, timeout: int = 20):
    """美团官方详情接口：data 为空 = 岗位已下线（权威信号，不受 SPA 影响）。"""
    import json as _json
    body = _json.dumps({"jobUnionId": str(job_union_id)}).encode()
    req = urllib.request.Request(MEITUAN_DETAIL, data=body, headers={
        "User-Agent": UA, "Content-Type": "application/json",
        "Referer": "https://zhaopin.meituan.com/web/position"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"verdict": "uncertain", "code": f"http_{exc.code}",
                "reason": f"HTTP {exc.code}", "http_status": exc.code, "final_url": ""}
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "uncertain", "code": "fetch_error", "reason": str(exc)[:200],
                "http_status": None, "final_url": ""}

    data = payload.get("data")
    message = (payload.get("message") or "").strip()
    if data:
        status_code = str(data.get("jobStatus") or "")
        return {"verdict": "active", "code": f"job_status:{status_code or 'unknown'}",
                "reason": f"详情接口返回岗位：{data.get('name', '')}", "http_status": 200,
                "final_url": ""}
    if "下线" in message or "不存在" in message or "not found" in message.lower():
        return {"verdict": "expired", "code": "meituan_gone", "reason": message,
                "http_status": 200, "final_url": ""}
    return {"verdict": "uncertain", "code": "meituan_empty", "reason": message or "空响应",
            "http_status": 200, "final_url": ""}


def check_job(source: str, external_id: str, url: str, today: str):
    """按来源选择判定方式：美团走详情接口，其余走页面文本判定。"""
    if source == "official:meituan" and external_id:
        return meituan_detail(external_id)
    return fetch_and_classify(url, today)
