"""观察记录候选合并登记的 SQLite 模式与事务辅助。"""

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
    role TEXT NOT NULL CHECK (role IN ('ranger', 'volunteer', 'school', 'curator', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 原始观察记录：只追加，不更新、不删除；修订以新证据版本体现。
CREATE TABLE IF NOT EXISTS observation_records (
    record_id TEXT PRIMARY KEY,
    contributor_id TEXT NOT NULL REFERENCES users(user_id),
    observer_group TEXT NOT NULL,
    latitude TEXT NOT NULL,
    longitude TEXT NOT NULL,
    accuracy_m TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    privacy_level TEXT NOT NULL CHECK (privacy_level IN ('public', 'observers', 'curators')),
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    materials_json TEXT NOT NULL,
    note TEXT,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS taxonomy_opinions (
    opinion_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES observation_records(record_id),
    taxon_id TEXT NOT NULL,
    taxon_name TEXT NOT NULL,
    confidence TEXT NOT NULL,
    reviewer_id TEXT NOT NULL REFERENCES users(user_id),
    tags_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (record_id, opinion_id)
);

-- 记录后续补充的观察材料（新证据），只追加。
CREATE TABLE IF NOT EXISTS record_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES observation_records(record_id),
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    contributed_by TEXT NOT NULL REFERENCES users(user_id),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (record_id, kind, ref)
);

-- 馆员建立的统一观察记录。
CREATE TABLE IF NOT EXISTS canonical_observations (
    canonical_id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'dissolved')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    dissolved_at TEXT
);

-- 机器生成的候选对：同一证据指纹只保留一行，新证据产生新指纹版本。
CREATE TABLE IF NOT EXISTS merge_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_a TEXT NOT NULL REFERENCES observation_records(record_id),
    record_b TEXT NOT NULL REFERENCES observation_records(record_id),
    evidence_fingerprint TEXT NOT NULL CHECK (length(evidence_fingerprint) = 64),
    algorithm_version TEXT NOT NULL,
    confidence TEXT NOT NULL,
    recommendation TEXT NOT NULL CHECK (recommendation IN ('merge', 'review', 'no_match')),
    explanation_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'merged', 'rejected', 'superseded')),
    created_at TEXT NOT NULL,
    decided_by TEXT REFERENCES users(user_id),
    decided_at TEXT,
    decision_note TEXT,
    supersedes_candidate_id INTEGER REFERENCES merge_candidates(candidate_id),
    UNIQUE (record_a, record_b, evidence_fingerprint)
);

-- 成员关系带完整时间线；合并被撤销时只终结关系，不删除历史。
CREATE TABLE IF NOT EXISTS canonical_memberships (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id TEXT NOT NULL REFERENCES canonical_observations(canonical_id),
    record_id TEXT NOT NULL REFERENCES observation_records(record_id),
    candidate_id INTEGER REFERENCES merge_candidates(candidate_id),
    added_by TEXT NOT NULL REFERENCES users(user_id),
    added_at TEXT NOT NULL,
    ended_by TEXT REFERENCES users(user_id),
    ended_at TEXT,
    end_reason TEXT,
    UNIQUE (canonical_id, record_id, added_at)
);

-- 一条原始记录在任一时刻最多属于一个生效的统一记录。
CREATE UNIQUE INDEX IF NOT EXISTS one_active_membership_per_record
ON canonical_memberships(record_id)
WHERE ended_at IS NULL;

-- 已被拒绝或已合并（后被撤销）的旧候选对，新证据出现时要重新判断。
CREATE TABLE IF NOT EXISTS candidate_revivals (
    revival_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_a TEXT NOT NULL,
    record_b TEXT NOT NULL,
    old_candidate_id INTEGER NOT NULL,
    new_candidate_id INTEGER,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

-- 决策幂等：相同决定内容回放原结果。
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
    "schema_meta", "users", "observation_records", "taxonomy_opinions", "record_evidence",
    "canonical_observations", "merge_candidates", "canonical_memberships",
    "candidate_revivals", "idempotency_keys", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False
    )
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
