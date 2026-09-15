"""Shared human-management protection gate for CRM assignment cycles.

The gate is deliberately one-document atomic.  It is disabled by default and
does nothing when ``CRM_SLA_TRANSACTION_GATE_ENABLED`` is false.  The future
reassignment transaction reads the same protection fields and therefore cannot
win over a human claim that has already been committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from pymongo import ReturnDocument

from config import Config


class HumanProtectionType(str, Enum):
    CALL = "CALL"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    CRM_MANAGEMENT_RESULT = "CRM_MANAGEMENT_RESULT"
    HUMAN_NOTE = "HUMAN_NOTE"
    HUMAN_OUTREACH = "HUMAN_OUTREACH"


class CycleGateStatus(str, Enum):
    CLAIMED = "CLAIMED"
    ALREADY_PROTECTED = "ALREADY_PROTECTED"
    LEAD_REASSIGNED_SLA_LOCKED = "LEAD_REASSIGNED_SLA_LOCKED"
    CYCLE_NOT_FOUND = "CYCLE_NOT_FOUND"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    GATE_DISABLED = "GATE_DISABLED"
    INVALID_HUMAN_EVIDENCE = "INVALID_HUMAN_EVIDENCE"


@dataclass(frozen=True)
class CycleGateResult:
    status: str
    lead_id: str
    assignment_cycle_id: str
    protection_type: str | None = None
    protection_at: datetime | None = None
    actor_user_id: str | None = None
    reason: str | None = None


_ACTOR_TYPES = frozenset({"human", "human_agent", "agent", "administrator", "supervisor"})
_SYSTEM_ACTORS = frozenset({"system", "bot", "sistema", "assistant", "none", ""})
_CLICK_TYPES = frozenset({
    "CLICK_WHATSAPP_LEAD", "CLICK_PHONE_LEAD", "CLICK_EMAIL_LEAD",
    "CLICK_WHATSAPP_OWNER", "CLICK_PHONE_OWNER", "CLICK_EMAIL_OWNER",
    "OPEN_DETAIL", "PAGE_VIEW", "NAVIGATION", "FILTER",
})
_INBOUND_TYPES = frozenset({"MSG_IN", "INBOUND_MESSAGE", "CLIENT_MESSAGE"})
_OUTREACH_TYPES = {
    "CALL_COMPLETED_LEAD": HumanProtectionType.CALL.value,
    "CALL_STARTED_LEAD": HumanProtectionType.CALL.value,
    "SEND_WA_LEAD": HumanProtectionType.WHATSAPP.value,
    "WHATSAPP_SENT_LEAD": HumanProtectionType.WHATSAPP.value,
    "SEND_EMAIL_LEAD": HumanProtectionType.EMAIL.value,
    "EMAIL_SENT_LEAD": HumanProtectionType.EMAIL.value,
}


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _human_actor(event: Mapping[str, Any]) -> bool:
    actor = str(event.get("actor_user_id") or event.get("actor") or "").strip().lower()
    meta = event.get("meta") if isinstance(event.get("meta"), Mapping) else {}
    actor_type = str(event.get("actor_type") or meta.get("actor_type") or "").strip().lower()
    if actor in _SYSTEM_ACTORS or actor_type in _SYSTEM_ACTORS:
        return False
    if actor_type:
        return actor_type in _ACTOR_TYPES
    return bool(actor)


def classify_human_protection(event: Mapping[str, Any]) -> str | None:
    """Return a closed protection type only for auditable human outreach.

    Clicks, page views, chatbot/system activity, inbound client messages and
    bare state changes intentionally return ``None``.
    """

    if not isinstance(event, Mapping) or not _human_actor(event):
        return None
    raw_type = event.get("type") or event.get("event_type") or ""
    event_type = str(getattr(raw_type, "value", raw_type)).upper()
    if event_type in _CLICK_TYPES or event_type in _INBOUND_TYPES:
        return None
    confirmed = event.get("confirmed", event.get("auditable", False))
    if confirmed is not True:
        return None

    if event_type in _OUTREACH_TYPES:
        return _OUTREACH_TYPES[event_type]
    result = str(event.get("result_type") or event.get("result") or "").upper()
    if result in {
        "MESSAGE_SENT_WAITING_RESPONSE", "CALL_NO_ANSWER", "EMAIL_SENT",
        "EFFECTIVE_CONTACT", "FOLLOW_UP_REQUESTED", "INVALID_NUMBER",
        "CONTACTADO", "SOLICITA_SEGUIMIENTO", "NO_INTERESADO", "OTRO",
        "NO_RESPONDIO", "OCUPADO", "NUMERO_INVALIDO", "MENSAJE_ENVIADO",
    }:
        if result in {"MESSAGE_SENT_WAITING_RESPONSE", "MENSAJE_ENVIADO"}:
            return HumanProtectionType.WHATSAPP.value
        if result in {"EMAIL_SENT"}:
            return HumanProtectionType.EMAIL.value
        if result in {"CALL_NO_ANSWER", "NO_RESPONDIO", "OCUPADO"}:
            return HumanProtectionType.CALL.value
        return HumanProtectionType.CRM_MANAGEMENT_RESULT.value
    if event_type in {"HUMAN_NOTE", "GESTION_LOG", "MANUAL_ENTRY"}:
        return HumanProtectionType.HUMAN_NOTE.value
    return None


def protection_type_for_management_result(result_type: Any) -> str | None:
    """Classify a canonical CRM result as human protection evidence."""

    from .crm_management import RESULT_RULES, canonical_result_type

    canonical = canonical_result_type(result_type)
    if canonical not in RESULT_RULES:
        return None
    # A canonical result submitted through the human management path is
    # itself auditable human activity.  This includes results that do not stop
    # SLA; protection and KPI compliance remain separate concepts.
    return HumanProtectionType.CRM_MANAGEMENT_RESULT.value


def _empty_protection_filter() -> dict[str, Any]:
    return {"$or": [{"reassignment_protection_at": {"$exists": False}}, {"reassignment_protection_at": None}]}


def _find_one(collection: Any, filter_: Mapping[str, Any], *, session: Any = None) -> Any:
    return collection.find_one(filter_, session=session)


def claim_cycle_for_human_management(
    db: Any,
    *,
    lead_id: Any,
    assignment_cycle_id: Any,
    actor_user_id: Any,
    protection_type: str,
    occurred_at: Any = None,
    session: Any = None,
    actor_can_manage_any_cycle: bool = False,
) -> CycleGateResult:
    """Atomically claim protection for the current cycle, if the gate is on."""

    lead_id = str(lead_id or "")
    cycle_id = str(assignment_cycle_id or "")
    actor_id = str(actor_user_id or "")
    if not getattr(Config, "CRM_SLA_TRANSACTION_GATE_ENABLED", False):
        return CycleGateResult(CycleGateStatus.GATE_DISABLED.value, lead_id, cycle_id)
    if protection_type not in {item.value for item in HumanProtectionType}:
        return CycleGateResult(
            CycleGateStatus.INVALID_HUMAN_EVIDENCE.value,
            lead_id,
            cycle_id,
            reason="invalid_protection_type",
        )
    protected_at = _utc(occurred_at) or datetime.now(timezone.utc)
    cycles = db["crm_assignment_cycles"]
    current = _find_one(
        cycles,
        {"lead_id": lead_id, "assignment_cycle_id": cycle_id},
        session=session,
    )
    if not current:
        active = _find_one(
            cycles,
            {"lead_id": lead_id, "cycle_status": "active", "unassigned_at": None},
            session=session,
        )
        if active and str(active.get("assignment_cycle_id")) != cycle_id:
            return CycleGateResult(
                CycleGateStatus.LEAD_REASSIGNED_SLA_LOCKED.value,
                lead_id,
                cycle_id,
                reason="current_cycle_changed",
            )
        return CycleGateResult(CycleGateStatus.CYCLE_NOT_FOUND.value, lead_id, cycle_id)
    if current.get("cycle_status") != "active" or current.get("unassigned_at") is not None:
        return CycleGateResult(
            CycleGateStatus.LEAD_REASSIGNED_SLA_LOCKED.value,
            lead_id,
            cycle_id,
            reason="cycle_not_active",
        )
    if not actor_can_manage_any_cycle and str(current.get("assigned_to_user_id")) != actor_id:
        return CycleGateResult(
            CycleGateStatus.OWNER_MISMATCH.value,
            lead_id,
            cycle_id,
            reason="actor_not_current_owner",
        )

    claim_filter = {
        "_id": current.get("_id"),
        "lead_id": lead_id,
        "assignment_cycle_id": cycle_id,
        "cycle_status": "active",
        "unassigned_at": None,
        **_empty_protection_filter(),
    }
    if not actor_can_manage_any_cycle:
        claim_filter["assigned_to_user_id"] = actor_id
    claimed = cycles.find_one_and_update(
        claim_filter,
        [
            {"$set": {
                "reassignment_protection_at": protected_at,
                "reassignment_protection_type": protection_type,
                "reassignment_protection_actor_user_id": actor_id,
                "cycle_version": {"$add": [{"$ifNull": ["$cycle_version", 0]}, 1]},
                "updated_at": protected_at,
            }}
        ],
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    if claimed:
        return CycleGateResult(
            CycleGateStatus.CLAIMED.value,
            lead_id,
            cycle_id,
            protection_type,
            protected_at,
            actor_id,
        )
    current_after = _find_one(cycles, {"_id": current.get("_id")}, session=session)
    if current_after and current_after.get("reassignment_protection_at") is not None:
        return CycleGateResult(
            CycleGateStatus.ALREADY_PROTECTED.value,
            lead_id,
            cycle_id,
            str(current_after.get("reassignment_protection_type") or protection_type),
            _utc(current_after.get("reassignment_protection_at")),
            str(current_after.get("reassignment_protection_actor_user_id") or ""),
        )
    if current_after and (
        current_after.get("cycle_status") != "active"
        or current_after.get("unassigned_at") is not None
    ):
        return CycleGateResult(
            CycleGateStatus.LEAD_REASSIGNED_SLA_LOCKED.value,
            lead_id,
            cycle_id,
            reason="cycle_changed_during_claim",
        )
    return CycleGateResult(
        CycleGateStatus.OWNER_MISMATCH.value,
        lead_id,
        cycle_id,
        reason="claim_cas_failed",
    )
