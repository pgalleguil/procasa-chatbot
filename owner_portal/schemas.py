"""Small, PII-free contracts for the owner portal prototype."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OwnerPortalEventContract:
    """Future event contract. It is deliberately not persisted in this phase."""

    event_name: str
    property_code: str | None
    event_at: str | None
    session_id: str | None
    portal_token_id: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class OwnerPortalViewContract:
    """Document the public shape without carrying CRM or owner PII."""

    office_scope: str
    property_code: str
    machine_learning_status: str
    data: dict[str, Any]
