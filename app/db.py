"""SQLite 连接、建库与事务。

为什么每请求一个连接：SQLite 的 WAL 模式允许多读单写，村内并发只有
"一个经办人在点按钮"，连接开销可以忽略；反而共享连接会在多线程下踩
`check_same_thread` 与隐式事务的坑。简单可靠优先。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config
from .clock import now_iso

__all__ = [
    "connect",
    "init_db",
    "transaction",
    "next_counter",
    "peek_counter",
    "get_meta",
    "set_meta",
    "SCHEMA_PATH",
]

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.ensure_dirs()
    conn = sqlite3.connect(cfg.db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")  # 财务数据宁可慢一点，也不能断电丢账
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(cfg: Config) -> None:
    """建库或补齐表结构。schema.sql 全部 IF NOT EXISTS，可重复执行。"""
    cfg.ensure_dirs()
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(cfg)
    try:
        conn.executescript(schema)
        conn.execute("PRAGMA user_version=1")
        set_meta(conn, "schema_version", "1")
        set_meta(conn, "created_at", now_iso())
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务。BEGIN IMMEDIATE 先拿写锁，避免"读后写"竞争。

    isolation_level=None 时 sqlite3 不会自动开事务，必须自己 BEGIN；
    这也是把「事件落库 + 计数 + 审计追加」做成原子的前提。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_counter(conn: sqlite3.Connection, name: str) -> int:
    """自增序列，调用方须在事务内使用。"""
    conn.execute(
        "INSERT INTO counter(name, value) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1",
        (name,),
    )
    row = conn.execute("SELECT value FROM counter WHERE name = ?", (name,)).fetchone()
    assert row is not None
    return int(row["value"])


def peek_counter(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT value FROM counter WHERE name = ?", (name,)).fetchone()
    return int(row["value"]) if row else 0


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
