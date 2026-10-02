"""Immutable data emitted by the foundational engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class NetworkRecord:
    url: str
    method: str
    resource_type: str
    status: int | None = None
    content_type: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id: str
    requested_url: str
    final_url: str
    title: str
    html: str
    status: int | None
    elapsed_seconds: float
    network_idle_reached: bool
    network: tuple[NetworkRecord, ...] = field(default_factory=tuple)
    metadata: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )

