"""SQLite 持久化。

并发策略
--------
数据库使用 WAL 模式，网页/API 的读取只取已提交的快照，不会看到补证重算到
一半的状态。写入补证时先用 ``BEGIN IMMEDIATE`` 取得写锁，再读取当前已采信
的全部凭证并重算，最后把新凭证和结果放在同一个事务中提交；任何一步失败都
整体回滚。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "data/app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    minute_base INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    joins_json TEXT NOT NULL,
    boards_json TEXT NOT NULL,
    total_passengers INTEGER NOT NULL,
    threshold INTEGER NOT NULL,
    limit_count INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'csv',
    verified_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    lower INTEGER NOT NULL,
    upper INTEGER NOT NULL,
    status TEXT NOT NULL,
    pinned_overdue INTEGER NOT NULL,
    residual_lower INTEGER NOT NULL,
    residual_upper INTEGER NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    credential_no TEXT NOT NULL,
    join_minute INTEGER NOT NULL,
    board_minute INTEGER NOT NULL,
    count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, credential_no)
);

CREATE INDEX IF NOT EXISTS idx_credentials_session
    ON credentials (session_id, id);

-- 读头方向对照：最新一行即现行对照；新对照无解时不写入，原对照继续生效。
CREATE TABLE IF NOT EXISTS calibrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    minute_base INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    readers_json TEXT NOT NULL,
    assignments_json TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 读头表导入预览：确认时按落库当时的现行对照复核，通过才建场。
CREATE TABLE IF NOT EXISTS import_previews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    minute_base INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    readers_json TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    joins_json TEXT NOT NULL,
    boards_json TEXT NOT NULL,
    calibration_id INTEGER NOT NULL REFERENCES calibrations(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
"""

# 老库升级：sessions 增加来源与人工核对列。
_SESSION_COLUMNS = {
    "source": "ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'csv'",
    "verified_at": "ALTER TABLE sessions ADD COLUMN verified_at TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    for column, statement in _SESSION_COLUMNS.items():
        if column not in existing:
            conn.execute(statement)


def resolve_db_path(path: str | os.PathLike[str] | None = None) -> str:
    if path is None:
        path = os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    return str(path)


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    db_path = resolve_db_path(path)
    if db_path != ":memory:":
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(path: str | os.PathLike[str] | None = None) -> None:
    conn = connect(path)
    try:
        if resolve_db_path(path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()
