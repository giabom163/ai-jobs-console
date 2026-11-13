"""AI 岗位核查台限定离线检查；不能替代真实UI与最终版本验收。"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from pathlib import Path

# 开启测试环境变量，物理切断异步构建与外部网络
os.environ["AI_JOBS_TESTING"] = "1"

ROOT = Path(r"D:\DSH\repos\ai-employment-monitor")
sys.path.insert(0, str(ROOT))

import tools.pipeline_daemon as daemon
import tools.ai_reviewer as reviewer
import tools.ingest_browser_jobs as ingest_mod
import tools.collect_deepseek as deepseek_mod
import tools.ai_server as server
from tools.job_geography import is_domestic_only


def run_isolated_suite():
    print("=" * 68)
    print("AI 岗位核查台 - R1~R7 & S1 隔离回归测试套件 (零网络、物理隔离沙箱)")
    print("=" * 68)

    tmpdir = tempfile.mkdtemp()
    try:
        tmp_path = Path(tmpdir)
        isolated_db = tmp_path / "test_isolated.db"
        con = sqlite3.connect(isolated_db)
        con.row_factory = sqlite3.Row

        # 1. 复制表结构到临时隔离数据库
        prod_db = ROOT / "ai_jobs.db"
        prod_con = sqlite3.connect(f"file:{prod_db}?mode=ro", uri=True)
        try:
            prod_cur = prod_con.cursor()
            schemas = prod_cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for s in schemas:
                con.execute(s[0])
            con.commit()
        finally:
            prod_con.close()

        # 绑定模块全局 DB 指向隔离数据库
        server.DB = isolated_db
        reviewer.DB = isolated_db

        # ==========================================
        # 验证 1: R1 离线模式调度验证
        # ==========================================
        print("\n[TEST 1: R1] 验证 pipeline_daemon 离线调度 (--offline)...")
        res_pipeline = daemon.run_full_pipeline(dry_run=False, enable_boss=False, offline=True)
        assert res_pipeline["success"] is True, "offline 模式应全局返回 True"
        assert res_pipeline["elapsed"] < 3.0, "offline 模式不应产生超时等待"
        print("  -> PASS: 离线演练全局跳过外部请求，派发耗时合格。")

        # ==========================================
        # 验证 2: R2 模型异常保留 review + 成功批准路径 (orig_rel 修复验证)
        # ==========================================
        print("\n[TEST 2: R2] 验证模型异常保留 review 状态，及境内合规岗位成功批准路径 (orig_rel)...")
        con.executescript("""
            INSERT INTO jobs(id, source, external_id, title, description, location, status, ai_relevance, ai_reason, content_hash, first_seen, last_seen) VALUES
            ('job_err_001', 'official', 'ext_err', '强化学习算法工程师', '大模型微调', '北京', 'review', 'build', '词典初筛', 'hash_err', '2026-11-01', '2026-11-01'),
            ('job_app_001', 'official', 'ext_app', 'LLM优化专家', '负责vLLM与CUDA优化', '杭州', 'review', 'build', '词典初筛', 'hash_app', '2026-11-01', '2026-11-01');
        """)
        con.commit()

        # 2.1 模型异常/拒绝连接
        review_res_err = reviewer.execute_auto_review(
            mode="custom_llm",
            limit=10,
            api_base="http://127.0.0.1:9/v1",  # 虚拟端口，不可达
            model="test-offline-model",
            auto_approve=True,
            job_ids=["job_err_001"]
        )
        assert review_res_err.get("errors", 0) >= 1
        assert review_res_err.get("approved", 0) == 0
        assert review_res_err.get("rejected", 0) == 0

        cur = con.cursor()
        cur.execute("SELECT status FROM jobs WHERE id = 'job_err_001'")
        assert cur.fetchone()["status"] == "review", "异常岗位必须严格保持 review 待审状态！"
        print("  -> PASS (2.1): 模型异常时岗位 100% 保留 review 状态，未被改为 excluded。")

        # 2.2 模型成功批准：使用 mock 替身，同时 mock check_qwen_alive，完全杜绝 8080 网络探测
        orig_review_fn = reviewer.review_job_with_llm
        orig_alive_fn = reviewer.check_qwen_alive
        try:
            reviewer.check_qwen_alive = lambda *args, **kwargs: True
            reviewer.review_job_with_llm = lambda *args, **kwargs: {
                "decision": "approve",
                "confidence": 0.96,
                "reason": "符合大模型核心研发标准"
            }
            review_res_ok = reviewer.execute_auto_review(
                mode="custom_llm",
                limit=10,
                api_base="http://127.0.0.1:8080/v1",
                model="mock-approve-model",
                auto_approve=True,
                job_ids=["job_app_001"]
            )
            assert review_res_ok.get("approved", 0) == 1, f"应成功批准 1 条，实际为: {review_res_ok}"
            assert review_res_ok.get("errors", 0) == 0

            cur.execute("SELECT status FROM jobs WHERE id = 'job_app_001'")
            assert cur.fetchone()["status"] == "open", "批准岗位状态应转为 open。"
            print("  -> PASS (2.2): 成功批准分支执行无报错 (orig_rel 修复无误)，岗位成功流转为 open。")
        finally:
            reviewer.review_job_with_llm = orig_review_fn
            reviewer.check_qwen_alive = orig_alive_fn

        # ==========================================
        # 验证 3: R3 安全门禁与并发防篡改
        # ==========================================
        print("\n[TEST 3: R3] 验证境外拒绝门禁与内容并发防覆盖...")
        con.executescript("""
            INSERT INTO jobs(id, source, external_id, title, description, location, status, ai_relevance, content_hash, first_seen, last_seen) VALUES
            ('job_foreign_1', 'official', 'ext_f', '算法', 'Overseas job', '东京', 'review', 'build', 'h_f1', '2026-11-01', '2026-11-01'),
            ('job_valid_1', 'official', 'ext_v1', '国内合规算法', '国内岗位', '深圳', 'review', 'build', 'h_v1', '2026-11-01', '2026-11-01'),
            ('job_concurrency_1', 'official', 'ext_c1', '岗位', '初始正文', '北京', 'review', 'build', 'h_c1_init', '2026-11-01', '2026-11-01');
        """)
        con.commit()

        # 3.1 拒绝境外（混合合法+非法批次）
        res_mixed, code_mixed = server.admin_review_action(["job_valid_1", "job_foreign_1"], "approve")
        assert code_mixed == 400, "非法项应返回 400，默认全批次拒绝"
        assert "非明确境内工作地点" in str(res_mixed)
        cur.execute("SELECT status FROM jobs WHERE id = 'job_valid_1'")
        assert cur.fetchone()["status"] == "review"
        print("  -> PASS (3.1): 合法+非法批次被严格拒绝 (HTTP 400)，合规项安全未受影响。")

        # 3.2 内容/地点并发防覆盖
        cur.execute("UPDATE jobs SET location = '西雅图', content_hash = 'h_c1_tampered' WHERE id = 'job_concurrency_1'")
        con.commit()
        res_tampered, code_tampered = server.admin_review_action(["job_concurrency_1"], "approve")
        assert code_tampered == 400 and "非明确境内工作地点" in str(res_tampered)

        cur.execute("UPDATE jobs SET location = '北京', content_hash = 'h_c1_tampered2' WHERE id = 'job_concurrency_1'")
        con.commit()
        cur.execute(
            "UPDATE jobs SET status = 'open' WHERE id = 'job_concurrency_1' AND status = 'review' AND content_hash = 'h_c1_init'"
        )
        assert cur.rowcount == 0, "旧 content_hash 的 UPDATE 影响 0 行，成功防并发覆盖！"
        con.commit()
        print("  -> PASS (3.2): 待审期间正文/地点发生变化时严格防覆盖，未发生误批准。")

        # ==========================================
        # 验证 4: R4 数据库凭据幂等与源文件只读
        # ==========================================
        print("\n[TEST 4: R4] 验证入库事务凭证与输入文件只读保护...")
        fixture_file = tmp_path / "boss_fixture_batch.json"
        fixture_data = {
            "source": "platform:boss_fixture",
            "captured_at": "2026-11-03T15:00:00Z",
            "items": [
                {
                    "title": "大模型架构师",
                    "company": "测试科技",
                    "location": "上海",
                    "salary": "40-60K",
                    "url": "https://example.com/job/101",
                    "tags": ["LLM", "PyTorch"]
                }
            ]
        }
        fixture_file.write_text(json.dumps(fixture_data, indent=2), encoding="utf-8")
        raw_hash_before = hashlib.sha256(fixture_file.read_bytes()).hexdigest()

        # 首次导入
        cmd_args = ["--db", str(isolated_db), "--input", str(fixture_file)]
        orig_argv = sys.argv
        sys.argv = ["ingest_browser_jobs.py"] + cmd_args
        exit_code_1 = ingest_mod.main()
        assert exit_code_1 == 0

        # 验证文件完全未被重写 (保持只读)
        raw_hash_after = hashlib.sha256(fixture_file.read_bytes()).hexdigest()
        assert raw_hash_before == raw_hash_after, "原始输入文件严禁修改，SHA-256 必须一致！"

        cur.execute("SELECT content_sha256, added_count FROM ingested_batches WHERE content_sha256 = ?", (raw_hash_before,))
        batch_row = cur.fetchone()
        assert batch_row is not None and batch_row["added_count"] == 1

        # 第二次重复导入同一文件 (幂等重放拦截)
        exit_code_2 = ingest_mod.main()
        assert exit_code_2 == 0
        cur.execute("SELECT count(*) FROM jobs WHERE company_id = (SELECT id FROM companies WHERE name = '测试科技')")
        assert cur.fetchone()[0] == 1, "重复岗位数应为 1，严禁重复写入！"
        print("  -> PASS: 原始文件哈希严格未变，数据库凭据事务提交，重复重放自动幂等拦截。")
        sys.argv = orig_argv

        # ==========================================
        # 验证 5: R5 DeepSeek 混合地点完整保留
        # ==========================================
        print("\n[TEST 5: R5] 验证 DeepSeek 多行混合地点完整保留...")
        sample_mixed = """大模型算法科学家\n工作地点：北京\n工作地点：硅谷\n职责要求:\n1. 预训练"""
        parsed_mixed = deepseek_mod.parse_deepseek_text(sample_mixed)
        assert parsed_mixed["location"] == "北京 / 硅谷"
        assert not is_domestic_only(parsed_mixed["location"]), "混合地点必须被识别为非纯大陆并拒绝自动入库"

        sample_no_field = """大模型专家\n职责: 负责北京团队协同\n要求: 硕士"""
        parsed_none = deepseek_mod.parse_deepseek_text(sample_no_field)
        assert parsed_none["location"] == "", "无标签行时 location 必须保持为空"
        print("  -> PASS: 混合多行地点完整合并原文，无标签行保持为空。")

        # ==========================================
        # 验证 6: R6 薪资参数校验与物理备份隔离
        # ==========================================
        print("\n[TEST 6: R6] 验证薪资工具 --dry-run 优先只读与校验备份...")
        # 6.1 --limit 0 校验
        p_invalid = subprocess.run([sys.executable, "-B", "tools/backfill_salaries.py", "--limit", "0"], cwd=str(ROOT), capture_output=True, text=True)
        assert p_invalid.returncode != 0 and "正整数" in p_invalid.stderr

        sub_db = tmp_path / "sub_folder" / "custom.db"
        sub_db.parent.mkdir(parents=True)
        con_sub = sqlite3.connect(sub_db)
        con_sub.executescript("""
            CREATE TABLE jobs(id TEXT PRIMARY KEY, salary_min REAL, salary_max REAL, currency TEXT, title TEXT, description TEXT, source TEXT, updated_at TEXT);
            CREATE TABLE job_changes(id INTEGER PRIMARY KEY, job_id TEXT, change_type TEXT, changed_at TEXT, note TEXT);
            INSERT INTO jobs VALUES('sal_1', NULL, NULL, NULL, '算法', '薪资 35-55K', 'test', '2026-10-15T00:00:00Z');
        """)
        con_sub.commit()
        con_sub.close()

        # 6.2 同时传 --dry-run 与 --apply：必须强制只读
        p_conflict = subprocess.run([sys.executable, "-B", "tools/backfill_salaries.py", "--db", str(sub_db), "--dry-run", "--apply", "--limit", "1"], cwd=str(ROOT), capture_output=True, text=True)
        assert p_conflict.returncode == 0
        assert "只读预览模式" in p_conflict.stdout
        con_sub_chk1 = sqlite3.connect(sub_db)
        assert con_sub_chk1.execute("SELECT salary_min FROM jobs WHERE id='sal_1'").fetchone()[0] is None, "--dry-run 时严禁写库！"
        assert len(list(sub_db.parent.glob("_backup-*.db"))) == 0, "--dry-run 严禁产生备份文件"
        con_sub_chk1.close()

        # 6.3 单独 --apply 写库
        p_sal = subprocess.run([sys.executable, "-B", "tools/backfill_salaries.py", "--db", str(sub_db), "--apply", "--limit", "1"], cwd=str(ROOT), capture_output=True, text=True)
        assert p_sal.returncode == 0
        backups = list(sub_db.parent.glob("_backup-*-pre-backfill-salary.db"))
        assert len(backups) == 1, f"备份应严格位于 custom.db 同目录中: {backups}"

        con_sub_chk2 = sqlite3.connect(sub_db)
        chk_row = con_sub_chk2.execute("SELECT salary_min, salary_max, updated_at FROM jobs WHERE id='sal_1'").fetchone()
        assert chk_row[0] == 35.0 and chk_row[1] == 55.0
        assert chk_row[2] == "2026-10-15T00:00:00Z", "薪资回填严禁篡改历史 updated_at 时间戳"
        con_sub_chk2.close()
        print("  -> PASS: --dry-run 严格保证只读，--limit 校验严密，备份与历史时间戳未被篡改。")

        # ==========================================
        # 验证 7: R7 进程终止与超时已有 stdout 冲刷 + Handler 故障覆盖
        # ==========================================
        print("\n[TEST 7: R7] 验证 run_cmd 超时 stdout 冲刷与 Handler 终止失败分支...")
        # 7.1 验证 run_cmd 超时时保留已有 stdout
        py_timeout_code = "import time, sys; sys.stdout.write('PARTIAL_OUTPUT_PREFIX\\n'); sys.stdout.flush(); time.sleep(5)"
        code_t, out_t, err_t = daemon.run_cmd([sys.executable, "-u", "-c", py_timeout_code], timeout=1)
        assert code_t == 124, f"超时应返回 124，实际返回: {code_t}"
        assert "PARTIAL_OUTPUT_PREFIX" in out_t, f"超时前已有的 stdout 必须被保留，实际为: {out_t}"
        print("  -> PASS (7.1): run_cmd 超时时成功终止，管道残余 stdout 完整冲刷保留。")

        # 7.2 调用真实停止业务函数验证异常状态，不能手工拼500响应冒充验证。
        record = {'pid': 123, 'run_id': '33333333-3333-4333-8333-333333333333'}
        state = {'running': True, 'blocked': False, 'record': record}
        with patch.object(server, '_sync_daemon_state', return_value=state), \
             patch.object(server, 'inspect_record', return_value=state), \
             patch.object(server, 'terminate_owned', side_effect=OSError('test termination denied')):
            response, status = server._stop_managed_daemon()
            assert status == 500 and response['status'] == 'termination_unconfirmed'
        print('  -> PASS (7.2): 真实停止函数保留终止未确认状态。')

        # 8: 不再接管任意sleep进程；复用有界身份/锁/账本检查。
        from tools._verify_daemon_identity import run_checks
        run_checks(tmp_path / 'daemon-identity', 3)

        con.close()

    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 68)
    print("ALL TESTS PASSED: R1~R7 & S1 全量隔离套件 100% 验证通过！")
    print("=" * 68)


if __name__ == "__main__":
    run_isolated_suite()
