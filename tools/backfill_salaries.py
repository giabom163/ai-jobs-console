#!/usr/bin/env python
"""AI 岗位核查台 - 存量岗位薪资结构化提取与回溯补齐脚本。

安全与审计契约：
  1) 默认仅预览（--dry-run），必须显式传入 --apply 才会执行写库；
  2) 默认限制批次规模（--limit 3），禁止无界全量运行；
  3) 写入前调用 ensure_backup 创建物理一致性备份；
  4) 写入操作在单个事务内执行，并在 job_changes 记录变更审计；
  5) 实事求是输出匹配数，严禁推测或伪造薪资指标。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.backup_manager import ensure_backup
from tools.ingest_browser_jobs import parse_salary_range, parse_salary_evidence

DB = ROOT / "ai_jobs.db"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB), help="数据库路径")
    parser.add_argument("--limit", type=int, default=3, help="单次处理上限，默认 3 (必须为正整数)")
    parser.add_argument("--dry-run", action="store_true", default=False, help="显式指定只读预览模式 (默认即为预览)")
    parser.add_argument("--apply", action="store_true", help="显式确认写库操作（默认仅预览）")
    args = parser.parse_args()

    if args.limit <= 0:
        raise ValueError(f"--limit 参数必须为正整数 (传入了: {args.limit})")

    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    cur = con.cursor()

    cur.execute("""
        SELECT id, title, description, source
        FROM jobs
        WHERE (salary_min IS NULL AND salary_max IS NULL)
          AND (description IS NOT NULL AND length(description) > 0)
    """)
    rows = cur.fetchall()
    con.close()

    total_scanned = len(rows)
    matches = []

    for jid, title, desc, src in rows:
        head_text = desc[:500]
        s_min, s_max, curr = parse_salary_range(head_text)
        if s_min is not None or s_max is not None:
            matches.append({
                "id": jid,
                "title": title,
                "source": src,
                "salary_min": s_min,
                "salary_max": s_max,
                "currency": curr,
                "source_description": desc,
                "salary_evidence": parse_salary_evidence(head_text),
            })
            if len(matches) >= args.limit:
                break

    print(f"[*] 扫描到未填薪资存量岗位: {total_scanned} 条")
    print(f"[*] 符合薪资解析规则的样本: {len(matches)} 条 (限制上限: {args.limit})")

    for idx, m in enumerate(matches, 1):
        print(f"  [{idx}] {m['id']} | {m['title']} | {m['salary_min']}-{m['salary_max']} {m['currency']} ({m['source']})")

    if not matches:
        print("[*] 结果: 存量岗位描述头部未发现符合模式的薪资结构，实际匹配 0 条。保持数据库原样。")
        return 0

    if args.dry_run:
        print("[*] 提示: 显式指定了 --dry-run，强制以只读预览模式运行 (即使同时携带 --apply 也绝不写库)。")
        return 0

    if not args.apply:
        print("[*] 提示: 当前为默认预览模式。若需实际写入，请显式携带 --apply 参数。")
        return 0

    print("[*] 正在执行操作前数据库物理备份...")
    # 明确将实际数据库路径传给 ensure_backup，保证备份对象与修改对象严格一致 (R6)
    bp = ensure_backup("pre-backfill-salary", target_db=db_path)
    print(f"[OK] 一致性备份已生成: {bp.name}")

    con_w = sqlite3.connect(db_path)
    cur_w = con_w.cursor()
    cur_w.execute('BEGIN IMMEDIATE')
    if 'salary_evidence_json' not in {row[1] for row in cur_w.execute('PRAGMA table_info(jobs)')}:
        cur_w.execute('ALTER TABLE jobs ADD COLUMN salary_evidence_json TEXT')
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        # 严格保持来源 posted_at/updated_at 时间戳证据不动，不覆盖历史时间戳口径 (R6)
        actual_written = 0
        for m in matches:
            cur_w.execute("""
                UPDATE jobs
                SET salary_min = ?, salary_max = ?, currency = ?, salary_evidence_json = ?
                WHERE id = ? AND salary_min IS NULL AND salary_max IS NULL AND description = ?
            """, (m["salary_min"], m["salary_max"], m["currency"], json.dumps(m["salary_evidence"],ensure_ascii=False), m["id"], m["source_description"]))
            if cur_w.rowcount > 0:
                actual_written += 1
                cur_w.execute("""
                    INSERT INTO job_changes(job_id, change_type, changed_at, note)
                    VALUES(?, 'salary_backfill', ?, ?)
                """, (m["id"], now_iso, f"old=NULL; new={m['salary_min']}-{m['salary_max']}{m['currency']}"))

        con_w.commit()
        print(f"[OK] 成功实际写入 {actual_written} 条薪资数据 (并发冲突跳过: {len(matches) - actual_written} 条)！")
    finally:
        con_w.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
