"""Transactional supervisor controls for one canonical CRM assignment cycle.

These operations deliberately do not enqueue or send notifications. They are
single-lead actions and fail closed when MongoDB transactions are unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pymongo.errors import DuplicateKeyError

from .crm_metrics import commercial_sla_start_at, coerce_utc_datetime, is_pre_visual_cutover
from .mongo_identity import mongo_id_variants
from .crm_admin_scope import office_access_error
from .crm_permissions import can_supervise_crm_assignments


ADMIN_EVENTS = "crm_assignment_admin_events"
MAX_SLA_EXTENSION_MINUTES = 1440


class AdminControlError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now(value: Any = None) -> datetime:
    parsed = coerce_utc_datetime(value) if value is not None else None
    return parsed or datetime.now(timezone.utc)


def _find_one(collection: Any, filter_: Mapping[str, Any], session: Any) -> Any:
    return collection.find_one(dict(filter_), session=session)


def _active_cycles(db: Any, lead_id: Any, session: Any) -> list[dict[str, Any]]:
    variants = list(mongo_id_variants(lead_id))
    if not variants:
        return []
    rows = list(db["crm_assignment_cycles"].find({
        "lead_id": {"$in": variants},
        "unassigned_at": None,
        "$or": [
            {"cycle_status": "active"},
            {"cycle_status": {"$exists": False}},
        ],
    }, session=session).limit(20))
    return rows


def _transaction(db: Any, callback):
    client = getattr(db, "client", None)
    if client is None or not callable(getattr(client, "start_session", None)):
        raise AdminControlError("transaction_unavailable")
    try:
        with client.start_session() as session:
            return session.with_transaction(callback)
    except AdminControlError:
        raise
    except Exception as exc:
        raise AdminControlError("transaction_failed") from exc


def _valid_action_id(value: Any) -> str:
    action_id = str(value or "").strip()
    if not action_id or len(action_id) > 120 or any(ch.isspace() for ch in action_id):
        raise AdminControlError("invalid_action_id")
    return action_id


def _lead_is_open(lead: Mapping[str, Any]) -> bool:
    if lead.get("closed_at") is not None or lead.get("archived_at") is not None:
        return False
    if lead.get("suppressed") is True:
        return False
    closed = {"CLOSED", "CLOSED_WON", "CLOSED_LOST", "ARCHIVED", "SUPPRESSED"}
    return not any(str(lead.get(key) or "").upper() in closed for key in ("stage", "pipeline_stage", "status"))


def _resolved_property_context(lead: Mapping[str, Any]) -> bool:
    prospect = lead.get("prospecto") if isinstance(lead.get("prospecto"), Mapping) else {}
    property_data = lead.get("datos_propiedad") if isinstance(lead.get("datos_propiedad"), Mapping) else {}
    nested_property = lead.get("propiedad") if isinstance(lead.get("propiedad"), Mapping) else {}
    return any(str(value or "").strip() for value in (
        prospect.get("codigo"), prospect.get("codigo_propiedad"),
        property_data.get("codigo"), property_data.get("codigo_propiedad"),
        nested_property.get("codigo"), lead.get("codigo_propiedad"),
    ))


def _check_property_identity(lead: Mapping[str, Any]) -> None:
    prospect = lead.get("prospecto") if isinstance(lead.get("prospecto"), Mapping) else {}
    if prospect.get("link_detectado") is True and not _resolved_property_context(lead):
        raise AdminControlError("property_identity_unresolved")


def _management_exists(db: Any, cycle: Mapping[str, Any], session: Any) -> bool:
    if any(cycle.get(key) not in (None, "") for key in (
        "first_valid_management_at", "first_contact_attempt_at", "reassignment_protection_at",
    )):
        return True
    lead_id = cycle.get("lead_id")
    cycle_id = cycle.get("assignment_cycle_id")
    for key in mongo_id_variants(lead_id):
        result = _find_one(db["crm_management_results"], {
            "lead_id": key, "assignment_cycle_id": cycle_id,
        }, session)
        if result:
            return True
        event = _find_one(db["crm_events"], {
            "lead_id": key,
            "assignment_cycle_id": cycle_id,
            "confirmed": True,
            "actor_type": {"$in": ["human", "human_agent", "agent", "administrator", "supervisor"]},
            "type": {"$in": ["CALL_COMPLETED_LEAD", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", "HUMAN_NOTE", "GESTION_LOG"]},
        }, session)
        if event:
            return True
    return False


def _actor_value(actor: Mapping[str, Any] | str) -> str:
    if isinstance(actor, Mapping):
        return str(actor.get("_id") or actor.get("id") or "")
    return str(actor or "")


def _actor_details(actor: Mapping[str, Any] | str) -> tuple[str, str]:
    if isinstance(actor, Mapping):
        display = str(actor.get("nombre") or actor.get("username") or "").strip()
        role = str(actor.get("rol") or "").strip()
        return display, role
    return str(actor or ""), ""


def admin_reassign_lead(
    db: Any,
    *,
    lead_id: Any,
    target_user_id: Any = None,
    return_to_previous: bool = False,
    actor: Mapping[str, Any] | str,
    reason: str,
    action_id: str,
    hold_hours: int = 0,
    now: Any = None,
    emergency_handoff: bool = False,
) -> dict[str, Any]:
    """Move exactly one open lead; emergency handoffs stay outside the SLA until activated."""
    action_id = _valid_action_id(action_id)
    reason = str(reason or "").strip()[:300]
    if not reason:
        raise AdminControlError("reason_required")
    if bool(target_user_id) == bool(return_to_previous):
        raise AdminControlError("target_or_previous_required")
    actor_id = _actor_value(actor)
    actor_display, actor_role = _actor_details(actor)
    if not can_supervise_crm_assignments(actor_role):
        raise AdminControlError("supervisor_only")
    if not actor_id:
        raise AdminControlError("actor_required")
    if isinstance(hold_hours, bool) or int(hold_hours) != hold_hours:
        raise AdminControlError("invalid_hold_hours")
    if emergency_handoff:
        if not 1 <= int(hold_hours) <= 168:
            raise AdminControlError("invalid_hold_hours")
    elif not 0 <= int(hold_hours) <= 168:
        raise AdminControlError("invalid_hold_hours")
    hold_hours = int(hold_hours)
    changed_at = _now(now)
    hold_until = changed_at + timedelta(hours=hold_hours) if hold_hours else None
    event_id = f"emergency-assignment:{action_id}" if emergency_handoff else f"assignment:{action_id}"
    cycle_id = f"admin-emergency:{action_id}" if emergency_handoff else f"admin:{action_id}"

    def apply(session):
        events = db[ADMIN_EVENTS]
        existing = _find_one(events, {"_id": event_id}, session)
        if existing:
            if (str(existing.get("lead_id")) != str(lead_id)
                    or bool(existing.get("emergency_handoff")) != bool(emergency_handoff)
                    or str(existing.get("reason") or "") != reason
                    or (target_user_id and str(existing.get("target_owner_user_id") or "") != str(target_user_id))
                    or (emergency_handoff and (
                        str(existing.get("actor_user_id") or "") != actor_id
                        or int(existing.get("hold_hours") or 0) != hold_hours
                        or existing.get("action") != "emergency_reassign"
                    ))):
                raise AdminControlError("idempotency_conflict")
            return {"status": "already_applied", "executive": existing.get("target_display_name"),
                    "cycle_id": existing.get("new_cycle_id"), "silent": True}

        lead = _find_one(db["leads"], {"_id": lead_id}, session)
        if not lead:
            raise AdminControlError("lead_not_found")
        if not _lead_is_open(lead):
            raise AdminControlError("lead_closed")
        _check_property_identity(lead)
        cycles = _active_cycles(db, lead_id, session)
        if len(cycles) > 1:
            raise AdminControlError("multiple_active_cycles")
        if emergency_handoff and len(cycles) != 1:
            raise AdminControlError("canonical_cycle_required")
        source = cycles[0] if cycles else None
        lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
        if source:
            source_cycle_id = str(source.get("assignment_cycle_id") or "")
            if not source_cycle_id or source.get("schema_version") != "crm_assignment_cycle_v1":
                raise AdminControlError("canonical_cycle_required")
            if str(lifecycle.get("current_assignment_cycle_id") or "") != source_cycle_id:
                raise AdminControlError("cycle_pointer_mismatch")
            if lifecycle.get("assignment_cycle_id") not in (None, "") and str(lifecycle.get("assignment_cycle_id")) != source_cycle_id:
                raise AdminControlError("cycle_pointer_mismatch")
            owner_id = str(source.get("assigned_to_user_id") or "")
            for mirror_owner_id in (
                lead.get("owner_user_id"),
                lead.get("assignment_mirror_owner_user_id"),
                lifecycle.get("assigned_to_user_id"),
            ):
                if mirror_owner_id not in (None, "") and str(mirror_owner_id) != owner_id:
                    raise AdminControlError("owner_mirror_mismatch")
            owner_display = str(source.get("assigned_to_display_name") or "")
            lead_owner = str(lead.get("ejecutivo_asignado") or (lead.get("prospecto") or {}).get("ejecutivo") or "")
            if owner_display and lead_owner and owner_display.casefold() != lead_owner.casefold():
                raise AdminControlError("owner_mirror_mismatch")
            if not emergency_handoff and _management_exists(db, source, session):
                raise AdminControlError("management_after_assignment")
        else:
            source_cycle_id = ""
            if lead.get("ejecutivo_asignado") or (lead.get("prospecto") or {}).get("ejecutivo"):
                raise AdminControlError("active_cycle_missing")

        if return_to_previous:
            history = source.get("previous_owner_user_ids") if source else None
            if not isinstance(history, (list, tuple)) or not history:
                raise AdminControlError("previous_owner_missing")
            target_user_id_local = str(history[0])
        else:
            target_user_id_local = str(target_user_id)

        target = None
        for key in mongo_id_variants(target_user_id_local):
            target = _find_one(db["usuarios"], {
                "_id": key, "rol": "agente", "is_active": {"$ne": False},
            }, session)
            if target:
                break
        if not target:
            raise AdminControlError("target_user_ineligible")
        scope_error = office_access_error(db, actor, lead, target=target, session=session)
        if scope_error:
            raise AdminControlError(scope_error)
        target_id = str(target.get("_id"))
        target_name = str(target.get("nombre") or "").strip()
        if not target_name:
            raise AdminControlError("target_user_ineligible")
        if source and target_id == str(source.get("assigned_to_user_id")):
            raise AdminControlError("already_assigned")

        if source:
            close_query = {
                "_id": source.get("_id"),
                "assignment_cycle_id": source_cycle_id,
                "lead_id": source.get("lead_id"),
                "unassigned_at": None,
            }
            if source.get("cycle_status") is None:
                close_query["cycle_status"] = {"$exists": False}
            else:
                close_query["cycle_status"] = source.get("cycle_status")
            if source.get("cycle_version") is not None:
                close_query["cycle_version"] = source.get("cycle_version")
            closed = db["crm_assignment_cycles"].update_one(close_query, {"$set": {
                "cycle_status": "closed", "unassigned_at": changed_at,
                "closed_at": changed_at,
                "closed_reason": "supervisor_emergency_handoff" if emergency_handoff else "supervisor_reassignment",
                "updated_at": changed_at,
            }}, session=session)
            if int(getattr(closed, "matched_count", 0)) != 1:
                raise AdminControlError("cycle_changed")

        prior_history = list(source.get("previous_owner_user_ids") or []) if source else []
        if source:
            previous_owner = str(source.get("assigned_to_user_id") or "")
            history = [previous_owner] + [str(value) for value in prior_history if str(value) != previous_owner]
        else:
            history = []
        new_cycle = {
            "_id": cycle_id,
            "assignment_cycle_id": cycle_id,
            "lead_id": lead.get("_id"),
            "assigned_to_user_id": target_id,
            "assigned_to_display_name": target_name,
            "assigned_at": changed_at,
            "cycle_started_at": changed_at,
            "sla_started_at": None if emergency_handoff else commercial_sla_start_at(changed_at),
            "sla_start_status": "pending_supervisor_activation" if emergency_handoff else "started",
            "notification_sent_at": None,
            "delivered_at": None,
            "read_at": None,
            "notification_eligible": False,
            "temperature_at_assignment": str(lead.get("lead_temperature_effective") or (source or {}).get("temperature_at_assignment") or "COLD").upper(),
            "unassigned_at": None,
            "assigned_by": actor_id,
            "assigned_by_role": actor_role,
            "assigned_by_display_name": actor_display,
            "reason": "crm_list_admin" if emergency_handoff else "admin_manual_reassignment",
            "reassignment_reason": reason,
            "cycle_origin": "admin_emergency_reassignment" if emergency_handoff else "admin_reassignment",
            "schema_version": "crm_assignment_cycle_v1",
            "metric_version": str((source or {}).get("metric_version") or "crm_metrics_v1"),
            "sla_policy_version": "legacy" if is_pre_visual_cutover(changed_at) else "sla_visual_v1_20260723",
            "cycle_status": "active",
            "cycle_version": 1,
            "previous_assignment_cycle_id": source_cycle_id or None,
            "reassignment_source_cycle_id": source_cycle_id or None,
            "previous_owner_user_ids": history,
            "administrative_assignment_id": action_id,
            "administrative_assignment_mode": "HOLD_UNTIL" if hold_until else ("RETURN_PREVIOUS" if return_to_previous else "NORMAL"),
            "administrative_hold_until": hold_until,
            "administrative_hold_reason": reason if emergency_handoff else None,
            "administrative_hold_actor_id": actor_id if emergency_handoff else None,
            "reassignment_state": "AWAITING_SUPERVISOR_HANDOFF" if emergency_handoff else None,
            "notification_mode": "NONE" if emergency_handoff else None,
            "sla_extension_minutes": 0,
            "first_valid_management_at": None,
            "first_contact_attempt_at": None,
            "sla_first_management_status": "pending",
        }
        if emergency_handoff:
            new_cycle["transfer_authorization"] = {
                "kind": "administrative",
                "actor": actor_id,
                "action_id": action_id,
                "scope": "emergency_handoff",
            }
            new_cycle["supervisor_handoff_state"] = "pending"
            new_cycle["supervisor_handoff_requested_at"] = changed_at
            new_cycle["supervisor_handoff_requested_by"] = actor_id
            new_cycle["supervisor_handoff_hold_expires_at"] = hold_until
            new_cycle["supervisor_handoff_activated_at"] = None
        db["crm_assignment_cycles"].insert_one(new_cycle, session=session)

        lead_filter = {"_id": lead.get("_id")}
        if source:
            lead_filter["lifecycle.current_assignment_cycle_id"] = source_cycle_id
        else:
            lead_filter["lifecycle.current_assignment_cycle_id"] = {"$in": [None, ""]}
        mirror_update = db["leads"].update_one(lead_filter, {
            "$set": {
                "ejecutivo_asignado": target_name,
                "prospecto.ejecutivo": target_name,
                "owner_user_id": target_id,
                "assignment_mirror_owner_user_id": target_id,
                "assignment_mirror_source": "crm_assignment_cycles",
                "lifecycle.assigned_at": changed_at,
                "lifecycle.cycle_started_at": changed_at,
                "lifecycle.assigned_to_user_id": target_id,
                "lifecycle.assigned_to_display_name": target_name,
                "lifecycle.assignment_cycle_id": cycle_id,
                "lifecycle.current_assignment_cycle_id": cycle_id,
                "lifecycle.notification_sent_at": None,
                "lifecycle.delivered_at": None,
                "lifecycle.read_at": None,
                "lifecycle.sla_started_at": new_cycle["sla_started_at"],
                "lifecycle.sla_start_status": new_cycle["sla_start_status"],
                "lifecycle.reassignment_state": new_cycle.get("reassignment_state"),
                "last_crm_update": changed_at,
            }
        }, session=session)
        if int(getattr(mirror_update, "matched_count", 0)) != 1:
            raise AdminControlError("lead_changed")

        events.insert_one({
            "_id": event_id,
            "action_id": action_id,
            "action": "emergency_reassign" if emergency_handoff else ("return_previous" if return_to_previous else "reassign"),
            "emergency_handoff": bool(emergency_handoff),
            "lead_id": lead.get("_id"),
            "source_cycle_id": source_cycle_id or None,
            "new_cycle_id": cycle_id,
            "source_owner_user_id": str((source or {}).get("assigned_to_user_id") or "") or None,
            "target_owner_user_id": target_id,
            "target_display_name": target_name,
            "actor_user_id": actor_id,
            "actor_display_name": actor_display,
            "actor_role": actor_role,
            "reason": reason,
            "silent": True,
            "notification_mode": "NONE" if emergency_handoff else None,
            "hold_hours": hold_hours,
            "hold_until": hold_until,
            "created_at": changed_at,
        }, session=session)
        return {
            "status": "ok", "executive": target_name, "cycle_id": cycle_id,
            "silent": True, "sla_start_status": new_cycle["sla_start_status"],
            "hold_until": hold_until.isoformat() if hold_until else None,
        }

    return _transaction(db, apply)


def extend_active_sla(
    db: Any,
    *,
    lead_id: Any,
    assignment_cycle_id: str,
    minutes: int,
    actor: Mapping[str, Any] | str,
    reason: str,
    action_id: str,
    now: Any = None,
) -> dict[str, Any]:
    """Extend the active cycle's SLA; prior breach evidence remains intact."""
    action_id = _valid_action_id(action_id)
    if isinstance(minutes, bool) or int(minutes) != minutes or not 1 <= int(minutes) <= 1440:
        raise AdminControlError("invalid_extension_minutes")
    minutes = int(minutes)
    reason = str(reason or "").strip()[:300]
    if not reason:
        raise AdminControlError("reason_required")
    actor_id = _actor_value(actor)
    actor_display, actor_role = _actor_details(actor)
    if not can_supervise_crm_assignments(actor_role):
        raise AdminControlError("supervisor_only")
    if not actor_id:
        raise AdminControlError("actor_required")
    changed_at = _now(now)
    event_id = f"sla-extension:{action_id}"

    def apply(session):
        events = db[ADMIN_EVENTS]
        existing = _find_one(events, {"_id": event_id}, session)
        if existing:
            if str(existing.get("lead_id")) != str(lead_id) or str(existing.get("assignment_cycle_id")) != str(assignment_cycle_id):
                raise AdminControlError("idempotency_conflict")
            return {"status": "already_applied", "total_extension_minutes": existing.get("new_extension_minutes")}

        lead = _find_one(db["leads"], {"_id": lead_id}, session)
        if not lead:
            raise AdminControlError("lead_not_found")
        if not _lead_is_open(lead):
            raise AdminControlError("lead_closed")
        scope_error = office_access_error(db, actor, lead, session=session)
        if scope_error:
            raise AdminControlError(scope_error)
        cycles = _active_cycles(db, lead_id, session)
        if len(cycles) != 1:
            raise AdminControlError("active_cycle_not_unique")
        cycle = cycles[0]
        if str(cycle.get("assignment_cycle_id") or "") != str(assignment_cycle_id):
            raise AdminControlError("cycle_changed")
        lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
        if str(lifecycle.get("current_assignment_cycle_id") or "") != str(assignment_cycle_id):
            raise AdminControlError("cycle_pointer_mismatch")
        if str(cycle.get("sla_start_status") or "").lower() in {
            "pending_notification", "not_started", "pending_supervisor_activation",
        }:
            raise AdminControlError("sla_not_started")
        if cycle.get("first_valid_management_at") not in (None, "") or _management_exists(db, cycle, session):
            raise AdminControlError("sla_already_completed")

        old_total = int(cycle.get("sla_extension_minutes") or 0)
        new_total = old_total + minutes
        if new_total > MAX_SLA_EXTENSION_MINUTES:
            raise AdminControlError("extension_limit_exceeded")
        extension = {
            "action_id": action_id,
            "minutes": minutes,
            "total_minutes": new_total,
            "actor_user_id": actor_id,
            "actor_display_name": actor_display,
            "actor_role": actor_role,
            "reason": reason,
            "created_at": changed_at,
            "historical_breach_at": cycle.get("sla_breached_at") or cycle.get("sla_expired_at"),
            "rescue_executed_at": cycle.get("reassigned_at") or cycle.get("rescued_at"),
        }
        update_filter = {
            "_id": cycle.get("_id"),
            "assignment_cycle_id": assignment_cycle_id,
            "cycle_status": "active",
            "unassigned_at": None,
        }
        if cycle.get("cycle_version") is not None:
            update_filter["cycle_version"] = cycle.get("cycle_version")
        updated = db["crm_assignment_cycles"].update_one(update_filter, {
            "$set": {
                "sla_extension_minutes": new_total,
                "sla_extension_last_reason": reason,
                "sla_extension_last_actor_user_id": actor_id,
                "sla_extension_last_at": changed_at,
                "updated_at": changed_at,
            },
            "$push": {"sla_extensions": extension},
            "$inc": {"cycle_version": 1},
        }, session=session)
        if int(getattr(updated, "matched_count", 0)) != 1:
            raise AdminControlError("cycle_changed")
        events.insert_one({
            "_id": event_id,
            "action_id": action_id,
            "action": "extend_sla",
            "lead_id": lead.get("_id"),
            "assignment_cycle_id": assignment_cycle_id,
            "minutes": minutes,
            "previous_extension_minutes": old_total,
            "new_extension_minutes": new_total,
            "actor_user_id": actor_id,
            "actor_display_name": actor_display,
            "actor_role": actor_role,
            "reason": reason,
            "historical_breach_at": extension["historical_breach_at"],
            "rescue_executed_at": extension["rescue_executed_at"],
            "created_at": changed_at,
            "silent": True,
        }, session=session)
        return {"status": "ok", "total_extension_minutes": new_total, "added_minutes": minutes, "silent": True}

    return _transaction(db, apply)


def protect_active_assignment(
    db: Any,
    *,
    lead_id: Any,
    assignment_cycle_id: str,
    hold_hours: int,
    actor: Mapping[str, Any] | str,
    reason: str,
    action_id: str,
    now: Any = None,
) -> dict[str, Any]:
    """Place a finite, silent supervisor hold on one canonical active cycle."""
    action_id = _valid_action_id(action_id)
    reason = str(reason or "").strip()[:300]
    if not reason:
        raise AdminControlError("reason_required")
    if isinstance(hold_hours, bool) or int(hold_hours) != hold_hours or not 1 <= int(hold_hours) <= 168:
        raise AdminControlError("invalid_hold_hours")
    actor_id = _actor_value(actor)
    actor_display, actor_role = _actor_details(actor)
    if not can_supervise_crm_assignments(actor_role):
        raise AdminControlError("supervisor_only")
    if not actor_id:
        raise AdminControlError("actor_required")
    changed_at = _now(now)
    requested_until = changed_at + timedelta(hours=int(hold_hours))
    event_id = f"protection:{action_id}"

    def apply(session):
        events = db[ADMIN_EVENTS]
        existing = _find_one(events, {"_id": event_id}, session)
        if existing:
            if str(existing.get("lead_id")) != str(lead_id):
                raise AdminControlError("idempotency_conflict")
            value = existing.get("hold_until")
            return {"status": "already_applied", "hold_until": value.isoformat() if hasattr(value, "isoformat") else str(value or ""), "silent": True}
        lead = _find_one(db["leads"], {"_id": lead_id}, session)
        if not lead:
            raise AdminControlError("lead_not_found")
        if not _lead_is_open(lead):
            raise AdminControlError("lead_closed")
        _check_property_identity(lead)
        scope_error = office_access_error(db, actor, lead, session=session)
        if scope_error:
            raise AdminControlError(scope_error)
        cycles = _active_cycles(db, lead_id, session)
        if len(cycles) != 1:
            raise AdminControlError("canonical_cycle_required")
        cycle = cycles[0]
        if (cycle.get("schema_version") != "crm_assignment_cycle_v1"
                or str(cycle.get("assignment_cycle_id") or "") != str(assignment_cycle_id or "")):
            raise AdminControlError("cycle_changed")
        if str((lead.get("lifecycle") or {}).get("current_assignment_cycle_id") or "") != str(assignment_cycle_id):
            raise AdminControlError("cycle_pointer_mismatch")
        current_until = coerce_utc_datetime(cycle.get("administrative_hold_until"))
        hold_until = max(current_until, requested_until) if current_until else requested_until
        query = {
            "_id": cycle.get("_id"), "lead_id": cycle.get("lead_id"),
            "assignment_cycle_id": assignment_cycle_id, "cycle_status": "active",
            "unassigned_at": None, "schema_version": "crm_assignment_cycle_v1",
        }
        if cycle.get("cycle_version") is not None:
            query["cycle_version"] = cycle.get("cycle_version")
        changed = db["crm_assignment_cycles"].update_one(query, {"$set": {
            "administrative_assignment_mode": "HOLD_UNTIL",
            "administrative_hold_until": hold_until,
            "administrative_hold_reason": reason,
            "administrative_hold_actor_id": actor_id,
            "administrative_hold_updated_at": changed_at,
            "updated_at": changed_at,
        }, "$inc": {"cycle_version": 1}}, session=session)
        if int(getattr(changed, "matched_count", 0)) != 1:
            raise AdminControlError("cycle_changed")
        events.insert_one({
            "_id": event_id, "action_id": action_id, "action": "protect",
            "lead_id": lead.get("_id"), "assignment_cycle_id": assignment_cycle_id,
            "actor_user_id": actor_id, "actor_display_name": actor_display,
            "actor_role": actor_role, "reason": reason,
            "hold_until": hold_until, "silent": True, "created_at": changed_at,
        }, session=session)
        return {"status": "ok", "hold_until": hold_until.isoformat(), "silent": True}

    return _transaction(db, apply)


def activate_emergency_handoff(
    db: Any,
    *,
    lead_id: Any,
    assignment_cycle_id: str,
    actor: Mapping[str, Any] | str,
    action_id: str,
    handoff_evidence: str,
    now: Any = None,
) -> dict[str, Any]:
    """Start SLA only after a supervisor records evidence of the agent handoff."""
    action_id = _valid_action_id(action_id)
    evidence = str(handoff_evidence or "").strip()[:1000]
    if len(evidence) < 8:
        raise AdminControlError("handoff_evidence_required")
    actor_id = _actor_value(actor)
    actor_display, actor_role = _actor_details(actor)
    if not can_supervise_crm_assignments(actor_role):
        raise AdminControlError("supervisor_only")
    if not actor_id:
        raise AdminControlError("actor_required")
    changed_at = _now(now)
    event_id = f"handoff-activation:{action_id}"

    def apply(session):
        events = db[ADMIN_EVENTS]
        existing = _find_one(events, {"_id": event_id}, session)
        if existing:
            if (str(existing.get("lead_id")) != str(lead_id)
                    or str(existing.get("assignment_cycle_id")) != str(assignment_cycle_id)
                    or str(existing.get("handoff_evidence") or "") != evidence):
                raise AdminControlError("idempotency_conflict")
            return {"status": "already_applied", "cycle_id": assignment_cycle_id,
                    "sla_started_at": existing.get("sla_started_at"), "silent": True}

        lead = _find_one(db["leads"], {"_id": lead_id}, session)
        if not lead:
            raise AdminControlError("lead_not_found")
        if not _lead_is_open(lead):
            raise AdminControlError("lead_closed")
        scope_error = office_access_error(db, actor, lead, session=session)
        if scope_error:
            raise AdminControlError(scope_error)
        cycles = _active_cycles(db, lead_id, session)
        if len(cycles) != 1:
            raise AdminControlError("active_cycle_not_unique")
        cycle = cycles[0]
        if str(cycle.get("assignment_cycle_id") or "") != str(assignment_cycle_id or ""):
            raise AdminControlError("cycle_changed")
        lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
        if str(lifecycle.get("current_assignment_cycle_id") or "") != str(assignment_cycle_id):
            raise AdminControlError("cycle_pointer_mismatch")
        if (str(cycle.get("sla_start_status") or "").lower() != "pending_supervisor_activation"
                or str(cycle.get("reassignment_state") or "").upper() != "AWAITING_SUPERVISOR_HANDOFF"):
            raise AdminControlError("handoff_not_pending")

        sla_started_at = commercial_sla_start_at(changed_at)
        if not sla_started_at:
            raise AdminControlError("sla_start_unavailable")
        changed = db["crm_assignment_cycles"].update_one({
            "_id": cycle.get("_id"),
            "assignment_cycle_id": assignment_cycle_id,
            "cycle_status": cycle.get("cycle_status", "active"),
            "unassigned_at": None,
            "sla_start_status": "pending_supervisor_activation",
        }, {"$set": {
            "sla_started_at": sla_started_at,
            "sla_start_status": "started",
            "reassignment_state": "SUPERVISOR_HANDOFF_COMPLETED",
            "supervisor_handoff_state": "confirmed",
            "supervisor_handoff_activated_at": changed_at,
            "supervisor_handoff_activated_by": actor_id,
            "supervisor_handoff_activated_by_display_name": actor_display,
            "supervisor_handoff_evidence": evidence,
            "administrative_hold_released_at": changed_at,
            "administrative_assignment_mode": "NORMAL",
            "updated_at": changed_at,
        }, "$inc": {"cycle_version": 1}}, session=session)
        if int(getattr(changed, "matched_count", 0)) != 1:
            raise AdminControlError("cycle_changed")
        mirror = db["leads"].update_one({
            "_id": lead.get("_id"),
            "lifecycle.current_assignment_cycle_id": assignment_cycle_id,
        }, {"$set": {
            "lifecycle.sla_started_at": sla_started_at,
            "lifecycle.sla_start_status": "started",
            "lifecycle.reassignment_state": "SUPERVISOR_HANDOFF_COMPLETED",
            "last_crm_update": changed_at,
        }}, session=session)
        if int(getattr(mirror, "matched_count", 0)) != 1:
            raise AdminControlError("lead_changed")

        events.insert_one({
            "_id": event_id,
            "action_id": action_id,
            "action": "activate_supervisor_handoff",
            "lead_id": lead.get("_id"),
            "assignment_cycle_id": assignment_cycle_id,
            "actor_user_id": actor_id,
            "actor_display_name": actor_display,
            "actor_role": actor_role,
            "handoff_evidence": evidence,
            "sla_started_at": sla_started_at,
            "silent": True,
            "notification_mode": "NONE",
            "created_at": changed_at,
        }, session=session)
        return {"status": "ok", "cycle_id": assignment_cycle_id,
                "sla_started_at": sla_started_at.isoformat(), "silent": True}

    return _transaction(db, apply)
