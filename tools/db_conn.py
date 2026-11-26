"""Unified SQLite connection factory and read-snapshot context for AI Employment Monitor."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from .job_fields import register_read_view
    from .job_geography import is_domestic_only, is_mixed_location, is_unknown_location
else:
    from job_fields import register_read_view
    from job_geography import is_domestic_only, is_mixed_location, is_unknown_location


def _resolve_existing_db(db_path: Path | str) -> Path:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    return path


def _apply_common_pragmas(con: sqlite3.Connection, timeout: float, row_factory: bool, foreign_keys: bool) -> None:
    if row_factory:
        con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)};")
    if foreign_keys:
        con.execute("PRAGMA foreign_keys=ON;")


def connect_ro(
    db_path: Path | str,
    *,
    timeout: float = 5.0,
    row_factory: bool = True,
    with_udf: bool = False,
    with_read_view: bool = False,
    query_only: bool = False,
) -> sqlite3.Connection:
    """Open an existing SQLite database in strict read-only mode."""
    path = _resolve_existing_db(db_path)
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=timeout)
    try:
        _apply_common_pragmas(con, timeout, row_factory, foreign_keys=False)
        if with_udf:
            con.create_function("is_domestic_only", 1, is_domestic_only, deterministic=True)
            con.create_function("is_mixed_location", 1, is_mixed_location, deterministic=True)
            con.create_function("is_unknown_location", 1, is_unknown_location, deterministic=True)
        if with_read_view:
            register_read_view(con)
        if query_only:
            con.execute("PRAGMA query_only=ON;")
    except BaseException:
        con.close()
        raise
    return con


def connect_rw(
    db_path: Path | str,
    *,
    timeout: float = 5.0,
    row_factory: bool = True,
    foreign_keys: bool = False,
    with_udf: bool = False,
) -> sqlite3.Connection:
    """Open an existing SQLite database in read-write mode (never creates an empty DB)."""
    path = _resolve_existing_db(db_path)
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True, timeout=timeout)
    try:
        _apply_common_pragmas(con, timeout, row_factory, foreign_keys=foreign_keys)
        if with_udf:
            con.create_function("is_domestic_only", 1, is_domestic_only, deterministic=True)
            con.create_function("is_mixed_location", 1, is_mixed_location, deterministic=True)
            con.create_function("is_unknown_location", 1, is_unknown_location, deterministic=True)
    except BaseException:
        con.close()
        raise
    return con


@contextmanager
def read_snapshot(con: sqlite3.Connection):
    """Run multiple SELECTs inside a single consistent SQLite read transaction."""
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN;")
    try:
        yield con
    finally:
        con.rollback()
