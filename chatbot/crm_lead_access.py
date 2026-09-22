"""Canonical CRM lead authorization and server-side contact redaction.

This module is intentionally independent from the reassignment executor.  It
resolves access from the active assignment cycle and the lifecycle pointer,
while preserving the existing mirror/name fallback for legacy leads that were
never marked as SLA-reassigned.  It never writes to MongoDB.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from chatbot.crm_permissions import can_administer_leads, lead_is_assigned_to_user


CRM_ASSIGNMENT_CONTEXT_INVALID = "CRM_ASSIGNMENT_CONTEXT_INVALID"
LEAD_REASSIGNED_SLA_LOCKED = "LEAD_REASSIGNED_SLA_LOCKED"
SLA_EXPIRED_PENDING_REASSIGNMENT = "SLA_EXPIRED_PENDING_REASSIGNMENT"
LOCKED_LEAD_MESSAGE = (
    "Este lead fue reasignado por vencimiento SLA y ya no está disponible para tu gestión."
)
EXPIRED_PENDING_MESSAGE = (
    "El plazo SLA terminó y este lead está pendiente de reasignación."
)
ACCESS_POLICY_VERSION = "crm_sla_security_v1"


class ContactVisibility(str, Enum):
    FULL = "FULL"
    REDACTED = "REDACTED"
    NONE = "NONE"


class AccessMode(str, Enum):
    LEGACY = "LEGACY_ACCESS_MODE"
    CANONICAL = "CANONICAL_SLA_ACCESS_MODE"


_SENSITIVE_KEY_PARTS = frozenset({
    "phone", "telefono", "tel", "mobile", "movil", "celular",
    "whatsapp", "email", "correo", "mail", "contact", "contacto",
})
_SENSITIVE_URL_RE = re.compile(r"(?:wa\.me/|tel:|mailto:)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?56\s*)?(?:9\s*)?\d(?:[\s.-]*\d){7,}(?!\d)")
_MARKER_KEYS = frozenset({
    "reassignment_decision_id", "automatic_reassignment_number",
    "reassigned_from_owner_user_id", "reassignment_source_cycle_id",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _id_text(value: Any) -> str:
    return _text(value)


def _is_present(value: Any) -> bool:
    return value is not None and value != ""


def _has_sla_reassignment_marker(doc: Mapping[str, Any] | None) -> bool:
    if not isinstance(doc, Mapping):
        return False
    if _text(doc.get("assigned_by")).casefold() == "sla_reassignment":
        return True
    if bool(doc.get("reassigned_by_sla")):
        return True
    if _text(doc.get("reassignment_source")).casefold() in {
        "sla", "sla_reassignment", "crm_sla_reassignment_v1",
    }:
        return True
    for key in _MARKER_KEYS:
        value = doc.get(key)
        if key == "automatic_reassignment_number":
            try:
                if int(value or 0) >= 1:
                    return True
            except (TypeError, ValueError):
                pass
        elif _is_present(value):
            return True
    return False


def _candidate_id_values(value: Any) -> list[Any]:
    values = [value]
    string_value = _text(value)
    if string_value and string_value not in values:
        values.append(string_value)
    try:
        from bson import ObjectId
        if string_value and ObjectId.is_valid(string_value):
            object_id = ObjectId(string_value)
            if object_id not in values:
                values.append(object_id)
    except Exception:
        pass
    return values


def _find_lead(db: Any, lead: Mapping[str, Any] | None, lead_id: Any) -> Mapping[str, Any] | None:
    if lead:
        return lead
    if db is None or lead_id in (None, ""):
        return None
    collection = db["leads"]
    for candidate in _candidate_id_values(lead_id):
        found = collection.find_one({"_id": candidate})
        if found:
            return found
    return None


def _active_cycles(db: Any, lead_id: Any) -> list[Mapping[str, Any]]:
    if db is None:
        return []
    values = _candidate_id_values(lead_id)
    query = {
        "lead_id": {"$in": values},
        "cycle_status": "active",
        "unassigned_at": None,
    }
    try:
        cursor = db["crm_assignment_cycles"].find(query)
        return list(cursor)
    except Exception:
        # Test doubles and older adapters sometimes do not implement $in.
        result: list[Mapping[str, Any]] = []
        for value in values:
            result.extend(list(db["crm_assignment_cycles"].find({
                "lead_id": value, "cycle_status": "active", "unassigned_at": None,
            })))
        return result


def _find_user(db: Any, user: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if user:
        return user
    return None


def _base_permissions(*, allowed: bool, admin: bool) -> dict[str, bool]:
    return {
        "detail_read": bool(allowed),
        "contact_call": bool(allowed),
        "contact_whatsapp": bool(allowed),
        "contact_email": bool(allowed),
        "management_result": bool(allowed),
        "human_note": bool(allowed),
        "update": bool(allowed),
        "recommendations": bool(allowed),
        "send_recommendation": bool(allowed),
        "sensitive_log_action": bool(allowed),
        "admin_view": bool(admin and allowed),
    }


@dataclass(frozen=True)
class CrmLeadAccessContext:
    lead_id: str | None
    user_id: str | None
    user_role: str | None
    current_assignment_cycle_id: str | None
    current_owner_user_id: str | None
    current_owner_display_name: str | None
    is_current_owner: bool
    is_admin: bool
    access_mode: str
    access_allowed: bool
    contact_visibility: ContactVisibility
    action_permissions: dict[str, bool] = field(default_factory=dict)
    lock_reason: str | None = None
    assignment_number: int | None = None
    reassigned_by_sla: bool = False
    policy_version: str = ACCESS_POLICY_VERSION
    http_status: int = 403

    @property
    def is_locked(self) -> bool:
        return self.lock_reason in {
            LEAD_REASSIGNED_SLA_LOCKED,
            SLA_EXPIRED_PENDING_REASSIGNMENT,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return an audit-safe context without contact PII."""
        return {
            "lead_id": self.lead_id,
            "user_id": self.user_id,
            "user_role": self.user_role,
            "current_assignment_cycle_id": self.current_assignment_cycle_id,
            "current_owner_user_id": self.current_owner_user_id,
            "current_owner_display_name": self.current_owner_display_name,
            "is_current_owner": self.is_current_owner,
            "is_admin": self.is_admin,
            "access_mode": self.access_mode,
            "access_allowed": self.access_allowed,
            "contact_visibility": self.contact_visibility.value,
            "action_permissions": dict(self.action_permissions),
            "lock_reason": self.lock_reason,
            "assignment_number": self.assignment_number,
            "reassigned_by_sla": self.reassigned_by_sla,
            "policy_version": self.policy_version,
            "http_status": self.http_status,
        }


def _context_invalid(*, lead_id: Any, user: Mapping[str, Any] | None,
                     access_mode: str = AccessMode.CANONICAL.value,
                     reassigned: bool = True,
                     reason: str = CRM_ASSIGNMENT_CONTEXT_INVALID) -> CrmLeadAccessContext:
    user_id = _id_text((user or {}).get("_id")) or None
    role = _text((user or {}).get("rol")) or None
    return CrmLeadAccessContext(
        lead_id=_id_text(lead_id) or None,
        user_id=user_id,
        user_role=role,
        current_assignment_cycle_id=None,
        current_owner_user_id=None,
        current_owner_display_name=None,
        is_current_owner=False,
        is_admin=can_administer_leads(role),
        access_mode=access_mode,
        access_allowed=False,
        contact_visibility=ContactVisibility.NONE,
        action_permissions=_base_permissions(allowed=False, admin=False),
        lock_reason=reason,
        assignment_number=None,
        reassigned_by_sla=reassigned,
        http_status=409 if reason in {
            CRM_ASSIGNMENT_CONTEXT_INVALID,
            LEAD_REASSIGNED_SLA_LOCKED,
            SLA_EXPIRED_PENDING_REASSIGNMENT,
        } else 403,
    )


def _is_sla_expired(cycle: Mapping[str, Any] | None, lead: Mapping[str, Any] | None) -> bool:
    if not cycle or not lead:
        return False
    try:
        from .crm_metrics import utc_now
        from .crm_sla_reassignment_worker import canonical_expiration_recheck
        access_check_now = utc_now()
        expiration = canonical_expiration_recheck(cycle, lead, now=access_check_now)
        return bool(expiration.breach_at and access_check_now >= expiration.breach_at)
    except Exception:
        # Access enforcement fails closed if the canonical expiry cannot be
        # reconstructed, while ordinary non-SLA CRM access remains unchanged.
        return False


def resolve_crm_lead_access_context(
    db: Any,
    *,
    user: Mapping[str, Any] | None,
    lead: Mapping[str, Any] | None = None,
    lead_id: Any = None,
    security_enabled: bool | None = None,
) -> CrmLeadAccessContext:
    """Resolve the effective server-side access context without writing data."""
    if security_enabled is None:
        from config import Config
        security_enabled = bool(getattr(Config, "CRM_SLA_SECURITY_LAYER_ENABLED", False))

    resolved_lead = _find_lead(db, lead, lead_id)
    resolved_id = (resolved_lead or {}).get("_id", lead_id)
    if not resolved_lead:
        return _context_invalid(lead_id=resolved_id, user=user, reason=CRM_ASSIGNMENT_CONTEXT_INVALID)

    user_id = _id_text((user or {}).get("_id")) or None
    role = _text((user or {}).get("rol")) or None
    is_admin = can_administer_leads(role)
    cycles = _active_cycles(db, resolved_id)
    marker_docs = [resolved_lead, *cycles]
    reassigned = any(_has_sla_reassignment_marker(doc) for doc in marker_docs)
    lifecycle = resolved_lead.get("lifecycle") if isinstance(resolved_lead.get("lifecycle"), Mapping) else {}
    pointer = _text(lifecycle.get("current_assignment_cycle_id"))

    # The flag controls enforcement in callers.  The resolver still returns a
    # useful legacy context for diagnostics when disabled.
    canonical = bool(security_enabled and reassigned)
    if canonical:
        if not pointer:
            return _context_invalid(lead_id=resolved_id, user=user, reassigned=True)
        matching = [c for c in cycles if _text(c.get("assignment_cycle_id")) == pointer]
        if len(matching) != 1 or len(cycles) != 1:
            return _context_invalid(lead_id=resolved_id, user=user, reassigned=True)
        current_cycle = matching[0]
        owner_id = _id_text(current_cycle.get("assigned_to_user_id")) or None
        if not owner_id:
            return _context_invalid(lead_id=resolved_id, user=user, reassigned=True)
        is_owner = bool(user_id and user_id == owner_id)
        # Deadline expiry is a KPI/compliance fact, not an ownership change.
        # The current owner keeps operational access while this exact active
        # cycle remains current.  The executor closes the cycle atomically;
        # only that committed state produces LEAD_REASSIGNED_SLA_LOCKED.
        allowed = bool(is_admin or is_owner)
        if not allowed and user_id:
            return CrmLeadAccessContext(
                lead_id=_id_text(resolved_id) or None,
                user_id=user_id,
                user_role=role or None,
                current_assignment_cycle_id=pointer,
                current_owner_user_id=owner_id,
                current_owner_display_name=_text(current_cycle.get("assigned_to_display_name")) or None,
                is_current_owner=False,
                is_admin=False,
                access_mode=AccessMode.CANONICAL.value,
                access_allowed=False,
                contact_visibility=ContactVisibility.NONE,
                action_permissions=_base_permissions(allowed=False, admin=False),
                lock_reason=LEAD_REASSIGNED_SLA_LOCKED,
                assignment_number=_assignment_number(resolved_lead, current_cycle),
                reassigned_by_sla=True,
                http_status=409,
            )
        return CrmLeadAccessContext(
            lead_id=_id_text(resolved_id) or None,
            user_id=user_id,
            user_role=role or None,
            current_assignment_cycle_id=pointer,
            current_owner_user_id=owner_id,
            current_owner_display_name=_text(current_cycle.get("assigned_to_display_name")) or None,
            is_current_owner=is_owner,
            is_admin=is_admin,
            access_mode=AccessMode.CANONICAL.value,
            access_allowed=allowed,
            contact_visibility=ContactVisibility.FULL if allowed else ContactVisibility.NONE,
            action_permissions=_base_permissions(allowed=allowed, admin=is_admin),
            lock_reason=None,
            assignment_number=_assignment_number(resolved_lead, current_cycle),
            reassigned_by_sla=True,
            http_status=200 if allowed else 409,
        )

    # Legacy compatibility is deliberately scoped to records with no SLA
    # reassignment marker. Existing CRM RBAC remains the source of truth.
    legacy_detail = resolved_lead
    is_owner = bool(user and lead_is_assigned_to_user(legacy_detail, user))
    allowed = bool(is_admin or is_owner)
    current_cycle = None
    if pointer:
        matches = [c for c in cycles if _text(c.get("assignment_cycle_id")) == pointer]
        if len(matches) == 1:
            current_cycle = matches[0]
    if current_cycle is None and len(cycles) == 1:
        current_cycle = cycles[0]
    # Legacy records follow the same temporal rule when they still resolve to
    # the authenticated current owner.  Expiry alone never revokes access.
    return CrmLeadAccessContext(
        lead_id=_id_text(resolved_id) or None,
        user_id=user_id,
        user_role=role or None,
        current_assignment_cycle_id=_text((current_cycle or {}).get("assignment_cycle_id")) or pointer or None,
        current_owner_user_id=_id_text((current_cycle or {}).get("assigned_to_user_id")) or None,
        current_owner_display_name=_text((current_cycle or {}).get("assigned_to_display_name")) or None,
        is_current_owner=is_owner,
        is_admin=is_admin,
        access_mode=AccessMode.LEGACY.value,
        access_allowed=allowed,
        contact_visibility=ContactVisibility.FULL if allowed else ContactVisibility.NONE,
        action_permissions=_base_permissions(allowed=allowed, admin=is_admin),
        lock_reason=None if allowed else "LEAD_NOT_ASSIGNED",
        assignment_number=None,
        reassigned_by_sla=False,
        http_status=200 if allowed else 403,
    )


def _assignment_number(lead: Mapping[str, Any], cycle: Mapping[str, Any]) -> int | None:
    for source in (cycle, lead, lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}):
        value = source.get("automatic_reassignment_number")
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _redact_text(value: str) -> str:
    value = _SENSITIVE_URL_RE.sub("[REDACTED_URL]", value)
    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    return _PHONE_RE.sub("[REDACTED_PHONE]", value)


def _sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", _text(key).casefold()).strip("_")
    tokens = set(normalized.split("_"))
    return bool(tokens & _SENSITIVE_KEY_PARTS) or normalized in {
        "phone_number", "telephone", "mobile_number", "email_address",
        "contact_details", "contact_data", "owner_contact",
    }


def sanitize_lead_for_access(payload: Any, access_context: CrmLeadAccessContext) -> Any:
    """Return a detached payload safe for the resolved contact visibility."""
    if access_context.contact_visibility == ContactVisibility.FULL:
        return copy.deepcopy(payload)

    def walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if _sensitive_key(key):
                    continue
                result[key] = walk(item)
            return result
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if isinstance(value, str):
            return _redact_text(value)
        return copy.deepcopy(value)

    return walk(payload)


def safe_access_error(context: CrmLeadAccessContext) -> dict[str, Any]:
    """Build an error body that never includes contact PII or transcripts."""
    body = {"error": context.lock_reason or CRM_ASSIGNMENT_CONTEXT_INVALID,
            "lead_id": context.lead_id, "locked": context.is_locked}
    if context.is_locked:
        body["message"] = (
            EXPIRED_PENDING_MESSAGE
            if context.lock_reason == SLA_EXPIRED_PENDING_REASSIGNMENT
            else LOCKED_LEAD_MESSAGE
        )
    return body


def _security_enabled() -> bool:
    from config import Config
    return bool(getattr(Config, "CRM_SLA_SECURITY_LAYER_ENABLED", False))


__all__ = [
    "ACCESS_POLICY_VERSION", "AccessMode", "ContactVisibility",
    "CRM_ASSIGNMENT_CONTEXT_INVALID", "LEAD_REASSIGNED_SLA_LOCKED",
    "SLA_EXPIRED_PENDING_REASSIGNMENT", "LOCKED_LEAD_MESSAGE",
    "EXPIRED_PENDING_MESSAGE", "CrmLeadAccessContext",
    "resolve_crm_lead_access_context", "sanitize_lead_for_access",
    "safe_access_error", "_has_sla_reassignment_marker", "_security_enabled",
]
