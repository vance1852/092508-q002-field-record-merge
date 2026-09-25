"""野外观察上报去重合并服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('reporter', 'curator', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_reports (
    report_id TEXT PRIMARY KEY,
    reporter_id TEXT NOT NULL REFERENCES users(user_id),
    taxon_name TEXT NOT NULL,
    latitude TEXT NOT NULL,
    longitude TEXT NOT NULL,
    coordinate_uncertainty_m TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('public', 'restricted', 'sensitive')),
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    media_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identifications (
    identification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL REFERENCES observation_reports(report_id),
    taxon_name TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    note TEXT NOT NULL DEFAULT '',
    identified_by TEXT NOT NULL REFERENCES users(user_id),
    identified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_report_id TEXT NOT NULL REFERENCES observation_reports(report_id),
    right_report_id TEXT NOT NULL REFERENCES observation_reports(report_id),
    status TEXT NOT NULL CHECK (status IN ('open', 'merged', 'rejected')),
    absorbed_by_decision_id INTEGER REFERENCES merge_decisions(decision_id),
    created_at TEXT NOT NULL,
    UNIQUE (left_report_id, right_report_id)
);

CREATE TABLE IF NOT EXISTS candidate_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES merge_candidates(candidate_id),
    algorithm_version TEXT NOT NULL,
    score TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    reasons_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES merge_candidates(candidate_id),
    action TEXT NOT NULL CHECK (action IN ('merge', 'reject', 'split')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    supersedes_decision_id INTEGER REFERENCES merge_decisions(decision_id),
    superseded_by_decision_id INTEGER REFERENCES merge_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS unified_observations (
    unified_id INTEGER PRIMARY KEY AUTOINCREMENT,
    visibility TEXT NOT NULL CHECK (visibility IN ('public', 'restricted', 'sensitive')),
    protected INTEGER NOT NULL CHECK (protected IN (0, 1)),
    representative_taxon TEXT NOT NULL,
    centroid_latitude TEXT NOT NULL,
    centroid_longitude TEXT NOT NULL,
    published_uncertainty_m TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'revoked')),
    created_by_decision_id INTEGER NOT NULL REFERENCES merge_decisions(decision_id),
    superseded_by_decision_id INTEGER REFERENCES merge_decisions(decision_id),
    revoked_by_decision_id INTEGER REFERENCES merge_decisions(decision_id),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS unified_members (
    unified_id INTEGER NOT NULL REFERENCES unified_observations(unified_id),
    report_id TEXT NOT NULL REFERENCES observation_reports(report_id),
    added_by_decision_id INTEGER NOT NULL REFERENCES merge_decisions(decision_id),
    removed_by_decision_id INTEGER REFERENCES merge_decisions(decision_id),
    PRIMARY KEY (unified_id, report_id)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "observation_reports", "identifications",
    "merge_candidates", "candidate_evaluations", "merge_decisions",
    "unified_observations", "unified_members", "idempotency_keys", "audit_events",
})


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=check_same_thread)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
