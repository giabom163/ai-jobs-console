#!/usr/bin/env python
"""AI 岗位核查台 - 物理数据库备份管理器与生命周期轮转系统。

功能：
  1) 物理备份创建 (ensure_backup)
  2) 备份生命周期轮转清理 (prune_backups)，防止磁盘无限膨胀
  3) 备份健康指标查询 (get_backup_stats)
"""
from __future__ import annotations

import os
import hashlib
import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "ai_jobs.db"


def get_backup_stats() -> dict:
    backups = [b for b in ROOT.glob("_backup-*.db") if b.is_file() and not b.is_symlink() and b.resolve().parent == ROOT.resolve()]
    total_size = sum(b.stat().st_size for b in backups)
    sorted_backups = sorted(backups, key=lambda x: x.stat().st_mtime, reverse=True)
    latest_name = sorted_backups[0].name if sorted_backups else "无"
    return {
        "count": len(backups),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "latest_backup": latest_name
    }


def prune_backups(keep_count: int = 10, max_age_days: int = 3, dry_run: bool = True, confirmed_manifest: str | None = None) -> dict:
    backups = [b for b in ROOT.glob("_backup-*.db") if b.is_file() and not b.is_symlink() and b.resolve().parent == ROOT.resolve()]

    sorted_backups = sorted(backups, key=lambda x: x.stat().st_mtime, reverse=True)
    now_ts = time.time()
    cutoff_ts = now_ts - (max_age_days * 86400)

    # 仅删除超过保留数量 且 修改时间超过 max_age_days 的备份
    to_delete = [b for b in sorted_backups[keep_count:] if b.stat().st_mtime < cutoff_ts]

    candidates = [{"name": b.name, "size_bytes": b.stat().st_size,
                   "sha256": hashlib.sha256(b.read_bytes()).hexdigest()} for b in to_delete]
    manifest = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
    total_freed_bytes = sum(row["size_bytes"] for row in candidates)
    if dry_run:
        return {"dry_run": True, "manifest_id": manifest, "pruned_count": len(candidates),
                "freed_mb": round(total_freed_bytes / (1024 * 1024), 2),
                "remaining_count": len(backups) - len(candidates), "candidates": candidates}
    if not confirmed_manifest or confirmed_manifest != manifest:
        raise ValueError("deletion requires the exact current manifest_id; preview again after any change")
    # File deletion is deliberately not exposed through the admin API or CLI.
    raise RuntimeError("backup deletion is disabled; preserve backups for manual review")

    pruned = 0
    freed_bytes = 0

    for b in to_delete:
        try:
            sz = b.stat().st_size
            b.unlink()
            pruned += 1
            freed_bytes += sz
        except Exception as exc:
            print(f"[backup_manager] 删除备份失败 {b.name}: {exc}")

    freed_mb = round(freed_bytes / (1024 * 1024), 2)
    print(f"[backup_manager] 轮转清理完毕: 删除了 {pruned} 个超期备份，释放空间 {freed_mb} MB，当前保留 {len(backups) - pruned} 个备份")
    return {
        "pruned_count": pruned,
        "freed_mb": freed_mb,
        "remaining_count": len(backups) - pruned
    }


def ensure_backup(tag: str = "backup", target_db: Path | str | None = None) -> Path:
    actual_db = Path(target_db).resolve() if target_db else DB
    if not actual_db.exists():
        raise FileNotFoundError(f"目标数据库不存在: {actual_db}")

    backup_dir = actual_db.parent
    backup_name = f"_backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}-{os.urandom(6).hex()}.db"
    backup_path = backup_dir / backup_name

    # 使用 SQLite 官方在线备份 API，确保事务一致性与物理快照完整
    src_uri = f"file:{actual_db.as_posix()}?mode=ro"
    src_con = sqlite3.connect(src_uri, uri=True)
    dst_con = sqlite3.connect(backup_path.resolve().as_posix())
    try:
        with dst_con:
            src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()

    # 物理完整性核验
    check_con = sqlite3.connect(f"file:{backup_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        cur = check_con.cursor()
        cur.execute("PRAGMA integrity_check;")
        res = cur.fetchone()
        if not res or res[0] != "ok":
            raise RuntimeError(f"备份文件完整性校验失败: {res}")
    finally:
        check_con.close()

    return backup_path


if __name__ == "__main__":
    stats_before = get_backup_stats()
    print("清理前备份状态:", stats_before)
    prune_res = prune_backups(keep_count=10, max_age_days=3, dry_run=True)
    print("清理结果:", prune_res)
    stats_after = get_backup_stats()
    print("清理后备份状态:", stats_after)
