# -*- coding: utf-8 -*-
"""隔离测试：验证过期岗位下架与全源日期门禁。
运行环境：纯临时隔离数据库，不影响真实生产数据库。
"""
import os
import sys
import shutil
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(r"D:\DSH\repos\ai-employment-monitor")
sys.path.insert(0, str(ROOT))

import importlib
from tools import export_console

ALI_IDS = [
    'job-5bb801bc9dac6c803485d76a',
    'job-d8294a0a5947e6619ba56f63',
    'job-37039a3661bb33dde31eff8b',
    'job-abf304ed717354a176b64664',
    'job-53754e73edd39dcf75d07dd5',
]
CMCC_ID = 'job-44f7b7c3337afd006a2ab6c8'

def run_isolated_test():
    real_db = ROOT / "ai_jobs.db"
    assert real_db.exists(), f"主库不存在: {real_db}"

    # 创建隔离临时数据库
    tmpdir = tempfile.mkdtemp()
    test_db = Path(tmpdir) / "test_ai_jobs.db"
    try:
        shutil.copy2(real_db, test_db)
        assert test_db.exists(), "测试数据库拷贝失败"

        # 1. 验证初态
        con = sqlite3.connect(test_db)
        cur = con.cursor()
        
        cur.execute("PRAGMA integrity_check;")
        res = cur.fetchone()
        assert res[0] == "ok", f"隔离库完整性异常: {res}"

        cur.execute("SELECT count(*) FROM jobs WHERE status='open'")
        initial_open = cur.fetchone()[0]
        assert initial_open == 744, f"期望初态 open 为 744，实际: {initial_open}"

        # 检查 5 条阿里初态
        for jid in ALI_IDS:
            cur.execute("SELECT status, source_updated_at FROM jobs WHERE id=?", (jid,))
            row = cur.fetchone()
            assert row is not None, f"未找到阿里岗位: {jid}"
            assert row[0] == "open", f"阿里岗位 {jid} 初态非 open: {row[0]}"
            assert row[1] < "2026-06-01", f"阿里岗位 {jid} 日期应早于 6 月: {row[1]}"

        # 检查 1 条移动初态
        cur.execute("SELECT status, application_deadline FROM jobs WHERE id=?", (CMCC_ID,))
        cmcc_row = cur.fetchone()
        assert cmcc_row is not None, f"未找到移动岗位: {CMCC_ID}"
        assert cmcc_row[0] == "open", f"移动岗位初态非 open: {cmcc_row[0]}"
        assert cmcc_row[1] == "2026-10-14", f"移动岗位截止日期异常: {cmcc_row[1]}"

        # 2. 执行状态流转事务
        now_iso = datetime.now(timezone.utc).isoformat()
        
        # 移动过期下架
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

        # 3. 校验变更后状态
        cur.execute("SELECT count(*) FROM jobs WHERE status='open'")
        after_open = cur.fetchone()[0]
        assert after_open == 738, f"期望变更后 open 为 738，实际: {after_open}"

        cur.execute("SELECT status, liveness FROM jobs WHERE id=?", (CMCC_ID,))
        assert cur.fetchone() == ('closed', 'expired'), "移动状态未正确流转"

        for jid in ALI_IDS:
            cur.execute("SELECT status, liveness FROM jobs WHERE id=?", (jid,))
            assert cur.fetchone() == ('verification_pending', 'uncertain'), f"阿里岗位 {jid} 状态未正确流转"

        cur.execute("PRAGMA integrity_check;")
        assert cur.fetchone()[0] == "ok", "事务提交后完整性校验失败"

        # 4. 验证 export_console.build 在隔离库上的输出
        orig_db = export_console.DB
        try:
            export_console.DB = test_db
            read_con = export_console.connect()
            bundle = export_console.build(read_con)
            read_con.close()

            jobs = bundle.get("jobs", [])
            print(f"[隔离测试] export_console 导出岗位数: {len(jobs)}")
            assert len(jobs) == 738, f"期望导出 738 条，实际: {len(jobs)}"
            
            # 强断言：导出的 738 条中严禁包含 ALI_IDS 或 CMCC_ID
            exported_ids = set(j["id"] for j in jobs)
            assert CMCC_ID not in exported_ids, "过期岗位 CMCC_ID 不应出现在导出列表中"
            for jid in ALI_IDS:
                assert jid not in exported_ids, f"阿里旧岗位 {jid} 不应出现在导出列表中"

            # 强断言：所有导出岗位的来源更新时间若存在，绝对不能早于 2026-06-01
            for j in jobs:
                sup = j.get("source_updated_at")
                if sup:
                    assert sup >= "2026-06-01", f"导出岗位 {j['id']} 来源更新日期过早: {sup}"
                dl = j.get("application_deadline")
                if dl:
                    assert dl >= "2026-11-05", f"导出岗位 {j['id']} 已过截止日期: {dl}"

            # 5. 反向测试（门禁代码实体测试）：
            # 即使有人在库中把 2025 年的阿里岗位设为 open，export_console 必须依据全源日期门禁拒之门外！
            cur.execute("UPDATE jobs SET status='open' WHERE id=?", (ALI_IDS[0],))
            con.commit()
            
            read_con2 = export_console.connect()
            bundle_neg = export_console.build(read_con2)
            read_con2.close()
            exported_neg_ids = set(j["id"] for j in bundle_neg.get("jobs", []))
            assert ALI_IDS[0] not in exported_neg_ids, (
                f"安全门禁失效：即使 status='open'，2025 年旧岗位 {ALI_IDS[0]} 必须被全源日期门禁拦截！"
            )
            print("[隔离测试] 反向门禁断言通过：强制 open 的 2025 年旧岗位被全源日期门禁正确拦截！")

        finally:
            export_console.DB = orig_db
            con.close()

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("[隔离测试] 全部断言 100% 通过！PASS")

if __name__ == "__main__":
    run_isolated_test()
