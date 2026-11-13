# -*- coding: utf-8 -*-
"""生产数据库更新：下架已过截止日期与 2025 年旧岗位。
包含：自动物理一致性备份、完整性检查、事务执行与变更账本记录。
"""
import os
import sys
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(r"D:\DSH\repos\ai-employment-monitor")
sys.path.insert(0, str(ROOT))

from tools import backup_manager

ALI_IDS = [
    'job-5bb801bc9dac6c803485d76a',
    'job-d8294a0a5947e6619ba56f63',
    'job-37039a3661bb33dde31eff8b',
    'job-abf304ed717354a176b64664',
    'job-53754e73edd39dcf75d07dd5',
]
CMCC_ID = 'job-44f7b7c3337afd006a2ab6c8'

def get_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def main():
    db_path = ROOT / "ai_jobs.db"
    assert db_path.exists(), f"生产数据库不存在: {db_path}"

    sha_before = get_file_sha256(db_path)
    print(f"[生产更新] 执行前生产库 SHA-256: {sha_before}")

    # 1. 物理备份
    backup_path = backup_manager.ensure_backup("retire-expired-20261105")
    backup_sha = get_file_sha256(backup_path)
    print(f"[生产更新] 物理一致性备份已创建: {backup_path.name}")
    print(f"[生产更新] 备份库 SHA-256: {backup_sha}")

    # 2. 连接生产库并核验初态
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    cur.execute("PRAGMA integrity_check;")
    res = cur.fetchone()
    assert res[0] == "ok", f"生产库完整性异常: {res}"

    cur.execute("SELECT count(*) FROM jobs WHERE status='open'")
    open_before = cur.fetchone()[0]
    assert open_before == 744, f"期望初态 open 为 744，实际: {open_before}"

    # 3. 事务执行状态更新
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"[生产更新] 开始执行状态更新事务 (时间: {now_iso})...")

    # 移动岗位下架为 closed
    cur.execute("""
        UPDATE jobs
        SET status='closed', liveness='expired', 
            liveness_reason='application deadline passed: 2026-10-14',
            last_checked_at=?
        WHERE id=?
    """, (now_iso, CMCC_ID))
    cur.execute("""
        INSERT INTO job_changes (job_id, change_type, changed_at, note)
        VALUES (?, 'closed', ?, 'application deadline passed (2026-10-14)')
    """, (CMCC_ID, now_iso))

    # 阿里 5 条 2025 年旧岗位移入 verification_pending
    for jid in ALI_IDS:
        cur.execute("""
            UPDATE jobs
            SET status='verification_pending', liveness='uncertain',
                liveness_reason='source_updated_at older than 2026-06-01',
                last_checked_at=?
            WHERE id=?
        """, (now_iso, jid))
        cur.execute("""
            INSERT INTO job_changes (job_id, change_type, changed_at, note)
            VALUES (?, 'changed', ?, 'source_updated_at < 2026-06-01: moved to verification_pending')
        """, (jid, now_iso))

    con.commit()
    print("[生产更新] 事务提交成功！")

    # 4. 改后状态断言
    cur.execute("PRAGMA integrity_check;")
    res_after = cur.fetchone()
    assert res_after[0] == "ok", f"改后完整性校验失败: {res_after}"

    cur.execute("SELECT count(*) FROM jobs WHERE status='open'")
    open_after = cur.fetchone()[0]
    assert open_after == 738, f"期望改后 open 为 738，实际: {open_after}"

    cur.execute("SELECT status, liveness FROM jobs WHERE id=?", (CMCC_ID,))
    assert cur.fetchone() == ('closed', 'expired'), "移动岗位状态未正确变更"

    for jid in ALI_IDS:
        cur.execute("SELECT status, liveness FROM jobs WHERE id=?", (jid,))
        assert cur.fetchone() == ('verification_pending', 'uncertain'), f"阿里岗位 {jid} 状态未正确变更"

    con.close()

    sha_after = get_file_sha256(db_path)
    print(f"[生产更新] 改后生产库 SHA-256: {sha_after}")
    print(f"[生产更新] 岗位库状态已对齐：open 数由 {open_before} -> {open_after}，下架 1 条过期岗，转入复核 5 条 2025 旧岗。PASS！")

if __name__ == "__main__":
    main()
