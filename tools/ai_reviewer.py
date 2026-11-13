#!/usr/bin/env python
"""AI 岗位核查台 - 候选大模型岗位智能审查与自动放行引擎 (AI Reviewer).

核心引擎架构：
  1. 本地大模型引擎 (默认): 直连 D:\\JiuGuan 本地部署的 Qwen3.5-9B-DARK-ROAST 服务 (http://127.0.0.1:8080/v1)，支持自动探测与一键拉起。
  2. 在线大模型引擎 (可选): 支持接入 OpenAI / DeepSeek 等兼容协议。
  3. 铁律门禁:
     - 强制中国大陆境内工作地点核验 (classify_location == 'domestic')，非境内坚决拦截。
     - 审查原因、置信度与详细证据全量落盘持久化至 ai_reason，严格满足 AGENTS.md 规范。
  4. 进度追踪与单条研判:
     - 全局 REVIEW_PROGRESS 状态对象，供管理后台轮询实时百分比。
     - review_single_job 支持对单个疑难岗位进行毫秒级 Qwen 试判。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from tools.job_geography import classify_location, is_domestic_only
except ImportError:
    from job_geography import classify_location, is_domestic_only

DB = ROOT / "ai_jobs.db"

QWEN_DEFAULT_URL = "http://127.0.0.1:8080/v1"
QWEN_DEFAULT_MODEL = "Qwen3.5-9B-DARK-ROAST"
LLAMA_DIR = r"D:\JiuGuan\llama.cpp"
MODEL_PATH = r"D:\JiuGuan\models\Qwen3.5-9B-DARK-ROAST-Q4_K_M.gguf"
QWEN_LOG_PATH = r"D:\JiuGuan\qwen_server.log"

_progress_lock = threading.Lock()
REVIEW_PROGRESS = {
    "running": False,
    "total": 0,
    "current": 0,
    "current_title": "",
    "current_company": "",
    "approved": 0,
    "rejected": 0,
    "last_message": "空闲",
    "updated_at": ""
}


def get_review_progress() -> dict:
    with _progress_lock:
        return dict(REVIEW_PROGRESS)


def update_review_progress(**kwargs):
    with _progress_lock:
        REVIEW_PROGRESS.update(kwargs)
        REVIEW_PROGRESS["updated_at"] = datetime.now(timezone.utc).isoformat()


def ensure_backup(tag="auto-review", target_db=None) -> Path:
    try:
        from tools.backup_manager import ensure_backup as mgr_backup
    except ModuleNotFoundError:
        from backup_manager import ensure_backup as mgr_backup
    return mgr_backup(tag, target_db=target_db)


def check_qwen_alive(url: str = QWEN_DEFAULT_URL) -> bool:
    """探测本地 Qwen 服务是否处于就绪状态。"""
    try:
        check_url = f"{url.rstrip('/')}/models"
        req = urllib.request.Request(check_url, headers={"User-Agent": "AI-Job-Auditor/1.0"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_qwen_running() -> bool:
    """Compatibility name: observe readiness only; never start a model process."""
    return check_qwen_alive()


def review_job_with_llm(
    title: str,
    description: str,
    company: str,
    location: str,
    api_base: str = QWEN_DEFAULT_URL,
    api_key: str = "",
    model: str = QWEN_DEFAULT_MODEL
) -> dict:
    """使用大模型执行深度认知审查（含中国大陆地理门禁与强化负例Prompt）。"""
    # 1. 严格中国大陆地理门禁 (AGENTS.md 硬边界)
    geo_cat = classify_location(location)
    if geo_cat != "domestic":
        return {
            "decision": "reject",
            "confidence": 1.0,
            "reason": f"地理硬门禁隔离: 非中国大陆境内岗位 (分类={geo_cat}, 地点={location or '空'})"
        }

    # 2. 构造精细化负例研判 Prompt
    url = f"{api_base.rstrip('/')}/chat/completions"
    clean_desc = (description or "")[:450]
    prompt = f"""你是一名资深 AI 架构师兼招聘核查专家。请根据以下岗位信息，严谨研判该岗位是否属于“真正的大模型/AI核心研发或AI紧密支撑技术岗位”。

研判准则：
1. 【准入放行 (approve)】：
   - 大模型算法、多模态、NLP/CV预训练与微调、SFT/RLHF/DPO优化；
   - AI Infra、大模型分布式训练推理加速、算力调度优化；
   - AI Agent架构开发、RAG智能体搭建、AI Native系统研发；
   - 大模型行业落地核心技术工程师。

2. 【坚决排除 (reject)】：
   - 传统职能类：仅把 AI 当办公工具的传统销售、电话销售、客服、人事、普通文案、传统外包运维；
   - 机械硬件类：纯机械结构设计、模具开发、注塑焊接、非算法单片机硬件（无大模型或具身算法研发属性）；
   - 培训售课类：AI 卖课培训讲师、自媒体售课、少儿编程科普教学。

3. 输出要求：
   必须输出严格的标准 JSON 格式：{{"decision": "approve"|"reject", "confidence": 0.0-1.0, "reason": "50字内中文说明"}}

岗位信息：
- 职位名称：{title}
- 招聘企业：{company}
- 工作地点：{location}
- 岗位详情描述：{clean_desc}
"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or 'none'}"
    }
    payload = {
        "model": model or QWEN_DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "You are a specialized AI job auditor. Respond only with JSON."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 120
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            try:
                res = json.loads(content)
            except Exception as parse_err:
                return {
                    "decision": "error",
                    "confidence": 0.0,
                    "reason": f"模型响应非标准JSON，保持待审状态: {parse_err}"
                }
            raw_decision = str(res.get("decision", "")).strip().lower()
            if raw_decision in ("approve", "reject"):
                return {
                    "decision": raw_decision,
                    "confidence": float(res.get("confidence", 0.85)),
                    "reason": f"模型研判: {res.get('reason', '')}"
                }
            else:
                return {
                    "decision": "error",
                    "confidence": 0.0,
                    "reason": f"模型返回非预期的决策值 ({raw_decision})，保持待审状态"
                }
    except Exception as exc:
        # 模型超时、网络异常或服务未就绪，严格返回 error，保持 review 待审状态，绝不当作业务拒绝 (R2)
        return {
            "decision": "error",
            "confidence": 0.0,
            "reason": f"模型调用异常(保持待审): {exc}"
        }


def review_single_job(
    job_id: str,
    api_base: str = QWEN_DEFAULT_URL,
    api_key: str = "",
    model: str = QWEN_DEFAULT_MODEL
) -> dict:
    """对单个岗位执行实时 Qwen 研判（试判模式，不落库）。"""
    if "127.0.0.1:8080" in api_base or not api_base:
        if not check_qwen_alive():
            return {"error": "model_not_ready; start the model explicitly outside this tool"}

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT j.id, j.title, c.name as company, j.description, j.location, j.source
        FROM jobs j
        LEFT JOIN companies c ON j.company_id = c.id
        WHERE j.id = ?
    """, (job_id,))
    row = cur.fetchone()
    con.close()

    if not row:
        return {"error": f"找不到岗位 ID: {job_id}"}

    res = review_job_with_llm(
        title=row["title"] or "",
        description=row["description"] or "",
        company=row["company"] or "",
        location=row["location"] or "",
        api_base=api_base or QWEN_DEFAULT_URL,
        api_key=api_key,
        model=model or QWEN_DEFAULT_MODEL
    )

    return {
        "job_id": job_id,
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "decision": res.get("decision", "reject"),
        "confidence": res.get("confidence", 0.8),
        "reason": res.get("reason", "")
    }


def execute_auto_review(
    mode: str = "qwen",
    limit: int = 3,
    api_base: str = "",
    api_key: str = "",
    model: str = "",
    auto_approve: bool = False,
    job_ids: list[str] | None = None
) -> dict:
    """批量执行审查并自动放行/排除，支持全生命周期进度追踪。"""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('review limit must be 1..200; start with 3')
    if type(auto_approve) is not bool or auto_approve:
        raise ValueError('automatic model publication is disabled; recommendations require explicit human review')
    if job_ids is not None and (not isinstance(job_ids, list) or len(job_ids) > limit or
                                not all(isinstance(x, str) and x for x in job_ids)):
        raise ValueError('job_ids must be a bounded list of IDs')
    effective_api_base = api_base.strip() or QWEN_DEFAULT_URL
    effective_model = model.strip() or QWEN_DEFAULT_MODEL

    if "127.0.0.1:8080" in effective_api_base or "localhost:8080" in effective_api_base:
        if not check_qwen_alive(effective_api_base):
            return {"error": "model_not_ready; automatic startup is disabled"}

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        cur.execute(f"""
            SELECT j.id, j.title, c.name as company, j.description, j.location, j.source, j.status, j.ai_reason, j.ai_relevance, j.content_hash
            FROM jobs j
            LEFT JOIN companies c ON j.company_id = c.id
            WHERE j.id IN ({placeholders}) AND j.status = 'review'
        """, job_ids)
    else:
        cur.execute("""
            SELECT j.id, j.title, c.name as company, j.description, j.location, j.source, j.status, j.ai_reason, j.ai_relevance, j.content_hash
            FROM jobs j
            LEFT JOIN companies c ON j.company_id = c.id
            WHERE j.status = 'review'
            ORDER BY j.first_seen DESC, j.id DESC
            LIMIT ?
        """, (limit,))

    rows = cur.fetchall()
    con.close()

    total_count = len(rows)
    if total_count == 0:
        update_review_progress(running=False, total=0, current=0, last_message="无待审样本")
        return {"total": 0, "approved": 0, "rejected": 0, "records": [], "note": "当前没有匹配的待审候选岗位（仅限 status='review'）"}

    update_review_progress(
        running=True,
        total=total_count,
        current=0,
        approved=0,
        rejected=0,
        last_message=f"正在初始化审查管道，待审样本数: {total_count}"
    )

    approved_list = []
    rejected_list = []
    error_list = []
    audit_changes = []
    records = []

    print(f"[*] 开始执行自动审查，模式: {mode} (模型: {effective_model})，待审样本数: {total_count}")

    for idx, r in enumerate(rows):
        title = r["title"] or ""
        desc = r["description"] or ""
        comp = r["company"] or ""
        loc = r["location"] or ""
        jid = r["id"]
        orig_reason = r["ai_reason"] or ""
        orig_rel = r["ai_relevance"] or ""
        expected_hash = r["content_hash"] or ""

        update_review_progress(
            current=idx + 1,
            current_title=title,
            current_company=comp,
            last_message=f"[{idx+1}/{total_count}] 正在研判: {comp} - {title[:16]}"
        )

        res = review_job_with_llm(
            title=title,
            description=desc,
            company=comp,
            location=loc,
            api_base=effective_api_base,
            api_key=api_key,
            model=effective_model
        )

        decision = res.get("decision", "error")
        confidence = res.get("confidence", 0.0)
        reason = res.get("reason", "")
        now_iso = datetime.now(timezone.utc).isoformat()

        # 境内工作地点硬门禁校验与 AI relevance 检查 (R3)
        if decision == "approve":
            if not is_domestic_only(loc):
                decision = "reject"
                reason = f"非明确境内工作地点 ({loc})，门禁拒绝放行; 原建议: {reason}"
            elif orig_rel not in ("build", "adjacent"):
                decision = "reject"
                reason = f"岗位AI分类为 {orig_rel} (非 build/adjacent)，门禁拒绝放行; 原建议: {reason}"

        record = {
            "id": jid,
            "title": title,
            "company": comp,
            "location": loc,
            "decision": decision,
            "confidence": confidence,
            "reason": reason
        }
        records.append(record)

        if decision == "approve":
            approved_list.append((jid, effective_model, reason, now_iso, expected_hash, loc))
        elif decision == "reject":
            rejected_list.append((jid, effective_model, reason, now_iso, expected_hash, loc))
        else:
            # error/异常状态：保持 review 状态不变，绝不写入 excluded (R2)
            error_list.append((jid, reason))
            audit_changes.append((jid, "ai_review_error", now_iso, f"model={effective_model}; error={reason}"))

        update_review_progress(
            approved=len(approved_list),
            rejected=len(rejected_list)
        )

    # 若开启自动放行，写入主库
    backup_file_name = None
    actual_approved = 0
    actual_rejected = 0
    conflict_skipped = 0

    if auto_approve and (approved_list or rejected_list):
        update_review_progress(last_message="审查研判完成，正在创建物理备份并持久化落库...")
        # 显式传递 target_db=DB，确保测试或隔离环境绝对不篡改主库备份 (P2)
        backup_file = ensure_backup("auto-review-qwen", target_db=DB)
        backup_file_name = backup_file.name

        con_w = sqlite3.connect(DB)
        con_w.row_factory = sqlite3.Row
        cur_w = con_w.cursor()
        try:
            # 严格内容并发校验 (P3): 状态必须为 review、地点未被修改、正文哈希未被改动
            for jid, model_name, r_reason, t_iso, exp_hash, exp_loc in approved_list:
                cur_w.execute(
                    """UPDATE jobs SET status = 'open'
                       WHERE id = ? AND status = 'review' AND content_hash = ? AND location = ?""",
                    (jid, exp_hash, exp_loc)
                )
                if cur_w.rowcount > 0:
                    actual_approved += 1
                    audit_changes.append((jid, "ai_review_approved", t_iso, f"model={model_name}; reason={r_reason}"))
                else:
                    conflict_skipped += 1
                    audit_changes.append((jid, "review_conflict_skipped", t_iso, "审查期间状态/地点/正文发生并发变化，放弃旧放行建议"))

            for jid, model_name, r_reason, t_iso, exp_hash, exp_loc in rejected_list:
                cur_w.execute(
                    """UPDATE jobs SET status = 'excluded'
                       WHERE id = ? AND status = 'review' AND content_hash = ? AND location = ?""",
                    (jid, exp_hash, exp_loc)
                )
                if cur_w.rowcount > 0:
                    actual_rejected += 1
                    audit_changes.append((jid, "ai_review_rejected", t_iso, f"model={model_name}; reason={r_reason}"))
                else:
                    conflict_skipped += 1
                    audit_changes.append((jid, "review_conflict_skipped", t_iso, "审查期间状态/地点/正文发生并发变化，放弃旧排除建议"))

            if audit_changes:
                cur_w.executemany(
                    "INSERT INTO job_changes(job_id, change_type, changed_at, note) VALUES(?,?,?,?)",
                    audit_changes
                )
            con_w.commit()
            print(f"[OK] 审查完成并落库: 实放行 {actual_approved} 条，实排除 {actual_rejected} 条，异常待审 {len(error_list)} 条，并发冲突跳过 {conflict_skipped} 条")
        finally:
            con_w.close()

        # 触发静态大盘重构 (仅在非测试环境下运行，彻底杜绝测试副作用 P2)
        if os.environ.get("AI_JOBS_TESTING") != "1":
            update_review_progress(last_message="正在触发全站静态大盘增量重构...")
            try:
                subprocess.Popen([sys.executable, "-B", str(ROOT / "tools" / "build_site.py")], cwd=str(ROOT))
            except Exception:
                pass

    actual_app = actual_approved if auto_approve else len(approved_list)
    actual_rej = actual_rejected if auto_approve else len(rejected_list)

    update_review_progress(
        running=False,
        last_message=f"全部审查完成！实通过 {actual_app} 条，实排除 {actual_rej} 条，异常 {len(error_list)} 条"
    )

    return {
        "mode": "qwen_llm",
        "recommendation_only": True,
        "published": 0,
        "model": effective_model,
        "total": total_count,
        "approved": actual_app,
        "rejected": actual_rej,
        "errors": len(error_list),
        "conflict_skipped": conflict_skipped if auto_approve else 0,
        "backup": backup_file_name,
        "records": records[:15]
    }
