"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示交通运营机构下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class NeedItem:
    """旅客提交的一条协助需求及其可见范围。"""

    need_id: str
    assistance_id: str
    need_key: str
    category: str
    detail: dict[str, Any]
    visibility: dict[str, Any]


@dataclass(frozen=True)
class Leg:
    """服务链中的一段接续责任。"""

    leg_id: str
    assistance_id: str
    version: int
    ordinal: int
    site_id: str
    organization_id: str
    from_location: str
    to_location: str
    scheduled_start: str
    scheduled_end: str
    acceptance_deadline: str
    required_kinds: tuple[str, ...]
    state: str
    frozen: bool
    source_version: int | None
    resource_id: str | None


@dataclass(frozen=True)
class Handoff:
    """相邻两段之间在固定交接位置的交接单。"""

    handoff_id: str
    assistance_id: str
    version: int
    from_ordinal: int
    to_ordinal: int
    location: str
    deadline: str
    state: str
    frozen: bool


@dataclass(frozen=True)
class Escalation:
    """一次超时、失约或紧急接管的升级记录。"""

    escalation_id: str
    assistance_id: str
    version: int
    ordinal: int
    reason: str
    level: int
    status: str
    note: str
    opened_by: str
    opened_at: str
