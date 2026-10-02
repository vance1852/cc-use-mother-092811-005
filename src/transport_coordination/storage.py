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
CREATE TABLE IF NOT EXISTS representation_grants (
    passenger_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    agent_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (passenger_actor_id, agent_actor_id)
);
CREATE TABLE IF NOT EXISTS assistance_requests (
    assistance_id TEXT PRIMARY KEY,
    passenger_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    agent_actor_id TEXT REFERENCES actors(actor_id),
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS need_items (
    need_id TEXT PRIMARY KEY,
    assistance_id TEXT NOT NULL REFERENCES assistance_requests(assistance_id),
    need_key TEXT NOT NULL,
    category TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    visibility_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(assistance_id, need_key)
);
CREATE TABLE IF NOT EXISTS itinerary_versions (
    assistance_id TEXT NOT NULL REFERENCES assistance_requests(assistance_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (assistance_id, version)
);
CREATE TABLE IF NOT EXISTS legs (
    leg_id TEXT PRIMARY KEY,
    assistance_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    site_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    from_location TEXT NOT NULL,
    to_location TEXT NOT NULL,
    scheduled_start TEXT NOT NULL,
    scheduled_end TEXT NOT NULL,
    acceptance_deadline TEXT NOT NULL,
    required_kinds_json TEXT NOT NULL,
    state TEXT NOT NULL,
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
    source_version INTEGER,
    accepted_at TEXT,
    started_at TEXT,
    delivered_at TEXT,
    completed_at TEXT,
    no_show_reported_at TEXT,
    taken_over_by TEXT REFERENCES actors(actor_id),
    locked_resource_id TEXT,
    UNIQUE(assistance_id, version, ordinal)
);
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    assistance_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    from_ordinal INTEGER NOT NULL,
    to_ordinal INTEGER NOT NULL,
    location TEXT NOT NULL,
    deadline TEXT NOT NULL,
    state TEXT NOT NULL,
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
    outbound_confirmed_by TEXT,
    outbound_confirmed_at TEXT,
    inbound_confirmed_by TEXT,
    inbound_confirmed_at TEXT,
    arrival_reported_by TEXT,
    arrival_reported_at TEXT,
    passenger_present INTEGER CHECK(passenger_present IS NULL OR passenger_present IN (0, 1)),
    UNIQUE(assistance_id, version, from_ordinal, to_ordinal)
);
CREATE TABLE IF NOT EXISTS assist_resources (
    resource_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    kind TEXT NOT NULL,
    identifier TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL,
    locked_leg_id TEXT,
    UNIQUE(organization_id, site_id, kind, identifier)
);
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id TEXT PRIMARY KEY,
    assistance_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    reason TEXT NOT NULL,
    level INTEGER NOT NULL CHECK(level IN (1, 2, 3)),
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS access_log (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    assistance_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    revealed_keys_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
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
