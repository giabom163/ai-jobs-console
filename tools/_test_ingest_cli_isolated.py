"""Comprehensive isolated tests for:
1. ingest_signal_cli.py (mainland location gate, strict date, rollback, signal cleanup)
2. build_site.py Pre-Deploy Audit Gate (exact row-level link & reverse rejection test)
No modifications to production database.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from tools import ingest_signal_cli, build_site

def run_tests():
    temp_dir = tempfile.mkdtemp(prefix="test_cli_isolated_")
    temp_db = Path(temp_dir) / "isolated.db"
    prod_db = ROOT / "ai_jobs.db"

    print(f"[*] Setting up isolated DB: {temp_db}")
    con_prod = sqlite3.connect(prod_db)
    con_temp = sqlite3.connect(temp_db)
    con_prod.backup(con_temp)
    con_prod.close()
    con_temp.close()

    try:
        # =================================================================
        # 1. 地点门禁严格验证（基于 job_geography.classify_location 唯一入口）
        # =================================================================
        print("\n--- [Test 1] Location gate via job_geography.classify_location ---")
        
        # 1.1 混合地点必须拒绝 (深圳/首尔 -> mixed)
        try:
            args = ingest_signal_cli.parse_args([
                "--title", "测试岗位", "--url", "https://example.com/1",
                "--source", "招聘", "--published-at", "2026-10-15",
                "--summary", "摘要", "--location", "深圳/首尔",
                "--target-db", str(temp_db)
            ])
            ingest_signal_cli.validate_input(args)
            assert False, "Failed to reject mixed location '深圳/首尔'"
        except ValueError as e:
            assert "must be 'domestic', got 'mixed'" in str(e), f"Unexpected error: {e}"
            print("-> PASS: Correctly rejected mixed location '深圳/首尔'")

        # 1.2 模糊/无范围远程必须拒绝 (远程 -> unknown)
        try:
            args = ingest_signal_cli.parse_args([
                "--title", "测试岗位", "--url", "https://example.com/2",
                "--source", "招聘", "--published-at", "2026-10-15",
                "--summary", "摘要", "--location", "远程",
                "--target-db", str(temp_db)
            ])
            ingest_signal_cli.validate_input(args)
            assert False, "Failed to reject unscoped remote location"
        except ValueError as e:
            assert "must be 'domestic', got 'unknown'" in str(e), f"Unexpected error: {e}"
            print("-> PASS: Correctly rejected unscoped remote location '远程'")

        # 1.3 境外地点必须拒绝 (香港 -> foreign, 美国加州硅谷 -> foreign)
        for overseas in ["香港", "美国加州硅谷"]:
            try:
                args = ingest_signal_cli.parse_args([
                    "--title", "测试岗位", "--url", "https://example.com/3",
                    "--source", "招聘", "--published-at", "2026-10-15",
                    "--summary", "摘要", "--location", overseas,
                    "--target-db", str(temp_db)
                ])
                ingest_signal_cli.validate_input(args)
                assert False, f"Failed to reject overseas location '{overseas}'"
            except ValueError as e:
                assert "must be 'domestic', got 'foreign'" in str(e), f"Unexpected error: {e}"
                print(f"-> PASS: Correctly rejected overseas location '{overseas}'")

        # 1.4 国内城市必须放行（消除此前自建白名单对南昌等城市的误杀）
        for valid_loc in ["南昌", "北京", "上海市", "深圳市"]:
            args = ingest_signal_cli.parse_args([
                "--title", "测试岗位", "--url", "https://example.com/4",
                "--source", "招聘", "--published-at", "2026-10-15",
                "--summary", "摘要", "--location", valid_loc,
                "--target-db", str(temp_db)
            ])
            rec = ingest_signal_cli.validate_input(args)
            assert rec["location"] == valid_loc
            print(f"-> PASS: Correctly accepted domestic location '{valid_loc}'")

        # =================================================================
        # 2. 严格日期校验（拒绝非法日历日期）
        # =================================================================
        print("\n--- [Test 2] Strict ISO/calendar date validation ---")
        for bad_date in ["2026-99-99", "2026-02-30", "invalid-date", "2026-13-01"]:
            try:
                args = ingest_signal_cli.parse_args([
                    "--title", "测试岗位", "--url", "https://example.com/5",
                    "--source", "招聘", "--published-at", bad_date,
                    "--summary", "摘要", "--location", "北京",
                    "--target-db", str(temp_db)
                ])
                ingest_signal_cli.validate_input(args)
                assert False, f"Failed to reject invalid date: {bad_date}"
            except ValueError as e:
                print(f"-> PASS: Correctly rejected invalid date '{bad_date}'")

        # =================================================================
        # 3. 真实数据库写入与字段映射（非 dry-run，断言 summary 与 content）
        # =================================================================
        print("\n--- [Test 3] Real DB write & field verification (No dry-run) ---")
        args = ingest_signal_cli.parse_args([
            "--id", "sig-iso-test-01",
            "--title", "隔离真实写入测试岗位",
            "--url", "https://example.com/real-write",
            "--source", "测试官方招聘",
            "--published-at", "2026-10-15",
            "--summary", "这是一条测试线索正文与摘要",
            "--location", "北京",
            "--target-db", str(temp_db)
        ])
        record = ingest_signal_cli.validate_input(args)
        assert record["summary"] == "这是一条测试线索正文与摘要"
        assert record["content"] == "这是一条测试线索正文与摘要"
        ingest_signal_cli.ingest_signal(record, target_db=temp_db, dry_run=False)

        con = sqlite3.connect(temp_db)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM discovery_documents WHERE id='sig-iso-test-01'").fetchone()
        assert row is not None, "Failed to insert record into discovery_documents"
        assert row["content"] == "这是一条测试线索正文与摘要"
        assert row["review_status"] == "pending", f"Default must be pending, got {row['review_status']}"
        assert row["claim_status"] == "unverified"
        assert row["date_basis"] == "unverified"
        con.close()
        print("-> PASS: Real DB write successful; summary and content mapped without KeyError.")

        # =================================================================
        # 4. 非 approved 重新写入时清理旧 signals 行测试
        # =================================================================
        print("\n--- [Test 4] Verify cleanup of signals table when non-approved ---")
        con = sqlite3.connect(temp_db)
        # 手工模拟一条历史曾进入 signals 表的旧记录
        con.execute("INSERT OR REPLACE INTO signals (id, title, url, source, published_at, summary, tag) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("sig-iso-test-01", "旧标题", "https://example.com/old", "旧来源", "2026-10-01", "旧摘要", "招聘线索"))
        con.commit()
        assert con.execute("SELECT count(*) FROM signals WHERE id='sig-iso-test-01'").fetchone()[0] == 1
        con.close()

        # 重新以 pending 录入该 ID，断言 signals 中的旧行被物理清除
        ingest_signal_cli.ingest_signal(record, target_db=temp_db, dry_run=False)
        con = sqlite3.connect(temp_db)
        sig_count = con.execute("SELECT count(*) FROM signals WHERE id='sig-iso-test-01'").fetchone()[0]
        con.close()
        assert sig_count == 0, f"Expected 0 in signals table after pending rewrite, got {sig_count}"
        print("-> PASS: Stale signals row properly deleted upon non-approved rewrite.")

        # =================================================================
        # 5. 真实异常回滚测试（精确捕获 OperationalError）
        # =================================================================
        print("\n--- [Test 5] Real transaction rollback with exact OperationalError assertion ---")
        con = sqlite3.connect(temp_db)
        con.execute("DROP TABLE signals")  # 删表故意引发执行期异常
        con.commit()
        con.close()

        bad_rec = dict(record)
        bad_rec["id"] = "sig-bad-rollback-99"
        bad_rec["review_status"] = "approved"  # 试图触发写 signals 表
        try:
            ingest_signal_cli.ingest_signal(bad_rec, target_db=temp_db, dry_run=False)
            assert False, "Expected write failure on missing signals table"
        except sqlite3.OperationalError as e:
            assert "no such table: signals" in str(e), f"Unexpected operational error: {e}"
            print(f"-> Captured expected sqlite3.OperationalError: {e}")

        # 验证事务完全回滚，未在 discovery_documents 留下脏数据
        con = sqlite3.connect(temp_db)
        residual = con.execute("SELECT count(*) FROM discovery_documents WHERE id='sig-bad-rollback-99'").fetchone()[0]
        con.close()
        assert residual == 0, "Rollback failed! Residual record found in discovery_documents"
        print("-> PASS: Transaction cleanly rolled back with zero residual records.")

        # =================================================================
        # 6. Pre-Deploy 审计门禁反向测试（证明伪造 approved 必被物理阻断）
        # =================================================================
        print("\n--- [Test 6] Pre-Deploy Audit Gate reverse rejection tests ---")
        # 重建临时数据库测试 audit 门禁
        gate_db = Path(temp_dir) / "gate_test.db"
        con_prod = sqlite3.connect(prod_db)
        con_gate = sqlite3.connect(gate_db)
        con_prod.backup(con_gate)
        con_prod.close()

        # 6.1 场景一：approved > 0，但无任何 discovery_reviews 记录 -> 必须阻断
        con_gate.execute("""
            INSERT INTO discovery_documents (id, url, title, source, published_at, date_basis, first_seen, last_seen, content, content_sha256, query, category, ai_scope, claim_status, evidence_json, company_id, review_status, liveness, last_verified_at)
            VALUES ('fake-doc-01', 'https://example.com/fake', '伪造标题', '测试', '2026-10-15', 'source_verified', '2026-10-15', '2026-10-15', '伪造内容', 'fake_sha256_001', 'q', 'hiring', 'ai_role', 'official_announcement', '{}', 'c1', 'approved', 'active', '2026-10-15')
        """)
        con_gate.commit()

        try:
            build_site.verify_audit_evidence(con_gate)
            assert False, "Failed to block unreviewed approved document"
        except ValueError as e:
            assert "lack matching discovery_reviews audit evidence" in str(e)
            print(f"-> PASS: Correctly blocked approved document without review: {e}")

        # 6.2 场景二：discovery_reviews 存在一条不相关的记录，或 content_sha256 不匹配 -> 必须阻断
        con_gate.execute("""
            INSERT INTO discovery_reviews (id, document_id, batch_id, reviewer, reviewed_at, content_sha256, before_json, decision_json)
            VALUES ('rev-01', 'other-doc-99', 'batch-1', 'tester', '2026-10-15', 'other_sha256', '{}', '{"decision":"approved"}')
        """)
        con_gate.commit()

        try:
            build_site.verify_audit_evidence(con_gate)
            assert False, "Failed to block document with mismatched review linkage"
        except ValueError as e:
            assert "lack matching discovery_reviews audit evidence" in str(e)
            print("-> PASS: Correctly blocked document with mismatched/unrelated review record")

        # 6.3 场景三：discovery_reviews 拥有完全匹配的 document_id 与 content_sha256 -> 成功放行
        con_gate.execute("""
            INSERT INTO discovery_reviews (id, document_id, batch_id, reviewer, reviewed_at, content_sha256, before_json, decision_json)
            VALUES ('rev-02', 'fake-doc-01', 'batch-1', 'tester', '2026-10-15', 'fake_sha256_001', '{}', '{"decision":"approved"}')
        """)
        con_gate.commit()
        build_site.verify_audit_evidence(con_gate)
        print("-> PASS: Correctly passed document with exact matching discovery_reviews linkage")
        con_gate.close()

        print("\n" + "=" * 65)
        print("ALL COMPREHENSIVE ISOLATED TESTS PASSED (100% VERIFIED)")
        print("=" * 65)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    run_tests()
