"""Centralized role and ownership rules for CRM lead administration."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping


CRM_ADMIN_ROLES = frozenset({"admin", "supervisor", "jefatura", "jefe"})

_REASSIGNMENT_KEYS = frozenset(
    {
        "asignacion",
        "assigned_to",
        "assignment",
        "ejecutivo",
        "ejecutivo_asignado",
        "executive",
        "prospecto_ejecutivo",
        "reassign",
        "reasignar",
    }
)


def _normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.casefold().split())


def can_administer_leads(role: object) -> bool:
    """Return whether a role may reassign, archive or inspect CRM audit data."""
    return _normalized_text(role) in CRM_ADMIN_ROLES


def lead_is_assigned_to_user(lead: Mapping | None, user: Mapping | None) -> bool:
    """Match an agent with the lead assignment, allowing legacy shortened surnames."""
    if not lead or not user:
        return False

    user_name = _normalized_text(user.get("nombre"))
    if not user_name:
        return False

    prospect = lead.get("prospecto") or {}
    assigned_names = (
        lead.get("ejecutivo_asignado"),
        prospect.get("ejecutivo") if isinstance(prospect, Mapping) else None,
    )
    for assigned in assigned_names:
        assigned_name = _normalized_text(assigned)
        if not assigned_name:
            continue
        if (
            assigned_name == user_name
            or assigned_name.startswith(user_name + " ")
            or user_name.startswith(assigned_name + " ")
        ):
            return True
    return False


def payload_attempts_reassignment(payload: Mapping | None) -> bool:
    """Detect assignment fields even when nested in a manually crafted request."""
    if not isinstance(payload, Mapping):
        return False
    for key, value in payload.items():
        normalized_key = re.sub(r"[^a-z0-9]+", "_", _normalized_text(key)).strip("_")
        if normalized_key in _REASSIGNMENT_KEYS:
            return True
        if isinstance(value, Mapping) and payload_attempts_reassignment(value):
            return True
    return False
