"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistance_chains (
    chain_id TEXT PRIMARY KEY,
    passenger_ref TEXT NOT NULL,
    passenger_token_hash TEXT NOT NULL,
    agent_actor_id TEXT,
    status TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    segment_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS need_items (
    item_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL REFERENCES assistance_chains(chain_id),
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    visible_scope TEXT NOT NULL CHECK(visible_scope IN ('all', 'legs')),
    visible_segments_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(chain_id, code)
);
CREATE TABLE IF NOT EXISTS itinerary_versions (
    chain_id TEXT NOT NULL REFERENCES assistance_chains(chain_id),
    version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    trigger_actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(chain_id, version)
);
CREATE TABLE IF NOT EXISTS chain_segments (
    segment_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    service_kind TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    scheduled_start TEXT NOT NULL,
    scheduled_end TEXT NOT NULL,
    board_location TEXT NOT NULL,
    handover_location TEXT,
    equipment_required_json TEXT NOT NULL,
    state TEXT NOT NULL,
    candidate_board_cap TEXT,
    candidate_handover_cap TEXT,
    assigned_actor_id TEXT,
    accepted_at TEXT,
    completed_at TEXT,
    immutable INTEGER NOT NULL DEFAULT 0 CHECK(immutable IN (0, 1)),
    source_segment_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chain_id, version, seq)
);
CREATE TABLE IF NOT EXISTS handovers (
    handover_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    boundary_seq INTEGER NOT NULL,
    location TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    state TEXT NOT NULL,
    outgoing_actor_id TEXT,
    incoming_actor_id TEXT,
    outgoing_confirmed_at TEXT,
    incoming_confirmed_at TEXT,
    completed_at TEXT,
    immutable INTEGER NOT NULL DEFAULT 0 CHECK(immutable IN (0, 1)),
    source_handover_id TEXT,
    UNIQUE(chain_id, version, boundary_seq)
);
CREATE TABLE IF NOT EXISTS capability_declarations (
    capability_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    site_id TEXT,
    service_kind TEXT NOT NULL,
    handover_point TEXT NOT NULL,
    equipment_json TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, service_kind, handover_point, window_start, equipment_json)
);
CREATE TABLE IF NOT EXISTS resource_locks (
    lock_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    resource_code TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    locked_at TEXT,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_lock ON resource_locks(segment_id, resource_code)
    WHERE state='locked';
CREATE TABLE IF NOT EXISTS equipment_incidents (
    incident_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    handover_point TEXT NOT NULL,
    resource_code TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS need_disclosures (
    disclosure_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_seq INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    item_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escalation_events (
    escalation_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_seq INTEGER,
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    owner_actor_id TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
