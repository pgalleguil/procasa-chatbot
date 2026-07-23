"""SLA v2 alert aggregation for non-HOT (grouped) and HOT (individual) leads.

Two-level identity:
- Member level: prevents the same lead from being alerted twice for the same threshold.
- Group/notification level: unique identity for the aggregated message document.

All alerts remain in shadow mode until CRM_SLA_ALERTS_SHADOW_MODE=false.
No automatic reassignment occurs.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from config import Config
from .crm_metrics import coerce_utc_datetime, utc_now
from .crm_notifications import COLLECTION as NOTIFICATION_COLLECTION
from .templates import (
    sla_non_hot_precritical_150,
    sla_non_hot_critical_180,
    sla_non_hot_critical_180_supervisor,
    sla_hot_precritical_45,
    sla_hot_critical_60,
    sla_hot_critical_60_supervisor,
    display_hot_reason,
    _preview_lines,
    _crm_filtered_url,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Non-HOT aggregation windows (minutes)
PRECRITICAL_WINDOW = 10
CRITICAL_WINDOW = 5

# SLA policies and threshold identifiers
POLICY_NON_HOT = "NON_HOT"
POLICY_HOT = "HOT"
POLICY_HOT_FOLLOW_UP = "HOT_FOLLOW_UP"

THRESHOLD_PRECRITICAL_150 = "NON_HOT_PRECRITICAL_150"
THRESHOLD_CRITICAL_180 = "NON_HOT_CRITICAL_180"
THRESHOLD_HOT_45 = "HOT_PRECRITICAL_45"
THRESHOLD_HOT_60 = "HOT_CRITICAL_60"

GROUP_SCOPE_CONTENT_VERSION = "v1"
GROUP_SCOPE_IDENTITY_FMT = "{recipient}|{policy}|{threshold}|{recipient_type}|{content_version}"


def _shadow() -> bool:
    """Return True if SLA alerts are in shadow mode."""
    return str(getattr(Config, "CRM_SLA_ALERTS_SHADOW_MODE", "true")).lower() == "true"


# ---------------------------------------------------------------------------
# Member dedup: mark a lead as notified for a threshold
# ---------------------------------------------------------------------------

MEMBER_COLLECTION = "crm_sla_notified_members"


def _member_identity(cycle_id: str, policy: str, threshold: str, recipient: str) -> str:
    return f"{cycle_id}|{policy}|{threshold}|{recipient}"


def ensure_member_indexes(db):
    """Create unique index on member identity to prevent double-notification."""
    collection = db[MEMBER_COLLECTION]
    existing = {idx.name for idx in collection.list_indexes()}
    if "uq_sla_member_identity" not in existing:
        try:
            collection.create_index(
                [("member_identity", 1)],
                unique=True,
                name="uq_sla_member_identity",
            )
        except Exception as exc:
            logger.warning("[SLA_ALERTS] Member index error: %s", exc)
    return {"created": []}


def ensure_sla_group_index(db):
    """Create unique index on sla_group_identity to prevent duplicate groups."""
    collection = db[NOTIFICATION_COLLECTION]
    existing = {idx.name for idx in collection.list_indexes()}
    if "uq_crm_sla_group_identity" in existing:
        pass  # already exists
    else:
        dups = list(collection.aggregate([
            {"$match": {"sla_group_identity": {"$exists": True}}},
            {"$group": {"_id": "$sla_group_identity", "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ]))
        if not dups:
            try:
                collection.create_index(
                    [("sla_group_identity", 1)],
                    unique=True,
                    partialFilterExpression={
                        "schema_version": "crm_notification_v1",
                        "sla_group_identity": {"$exists": True},
                    },
                    name="uq_crm_sla_group_identity",
                )
            except Exception as exc:
                logger.warning("[SLA_ALERTS] Group index error: %s", exc)

    # Create/verify open group scope index
    if "uq_crm_sla_open_group_scope" not in existing:
        try:
            collection.create_index(
                [
                    ("recipient_user_id", 1),
                    ("sla_policy", 1),
                    ("sla_threshold", 1),
                    ("recipient_type", 1),
                    ("content_version", 1),
                ],
                unique=True,
                partialFilterExpression={
                    "group_open": True,
                    "schema_version": "crm_notification_v1",
                },
                name="uq_crm_sla_open_group_scope",
            )
        except Exception as exc:
            logger.warning("[SLA_ALERTS] Open group index error: %s", exc)

    return {"status": "ok"}


MEMBER_STATE_RESERVED = "reserved"
MEMBER_STATE_SHADOW = "shadow_completed"
MEMBER_STATE_LIVE = "live_sent"
MEMBER_STATE_SUPPRESSED = "suppressed"
MEMBER_STATE_RELEASED = "released"
MEMBER_TERMINAL_STATES = {MEMBER_STATE_SHADOW, MEMBER_STATE_LIVE}
MEMBER_RELEASABLE_STATES = {MEMBER_STATE_SUPPRESSED, MEMBER_STATE_RELEASED}


def _member_identity_full(cycle_id, policy, threshold, recipient_user_id,
                          recipient_type, delivery_mode):
    return (f"{cycle_id}|{policy}|{threshold}|{recipient_user_id}|"
            f"{recipient_type}|{delivery_mode}")


def member_already_notified(db, *, cycle_id, policy, threshold, recipient_user_id,
                            recipient_type="executive", delivery_mode="shadow") -> bool:
    """Check if a lead-cycle has a terminal member record for this threshold."""
    mid = _member_identity_full(cycle_id, policy, threshold, recipient_user_id,
                                recipient_type, delivery_mode)
    existing = db[MEMBER_COLLECTION].find_one({"member_identity": mid})
    if existing and existing.get("state") in MEMBER_TERMINAL_STATES:
        return True
    return False


def reserve_member(db, *, lead_id, cycle_id, policy, threshold, recipient_user_id,
                   recipient_type="executive", delivery_mode="shadow"):
    """Atomically reserve a member slot for a threshold.

    Returns True if the reservation was accepted, False if already reserved.
    """
    mid = _member_identity_full(cycle_id, policy, threshold, recipient_user_id,
                                recipient_type, delivery_mode)
    doc = {
        "member_identity": mid,
        "lead_id": lead_id,
        "assignment_cycle_id": cycle_id,
        "sla_policy": policy,
        "threshold": threshold,
        "recipient_user_id": recipient_user_id,
        "recipient_type": recipient_type,
        "delivery_mode": delivery_mode,
        "state": MEMBER_STATE_RESERVED,
        "created_at": utc_now(),
    }
    try:
        db[MEMBER_COLLECTION].insert_one(doc)
        return True
    except DuplicateKeyError:
        # Already reserved — check if releasable
        existing = db[MEMBER_COLLECTION].find_one({"member_identity": mid})
        if existing and existing.get("state") in MEMBER_RELEASABLE_STATES:
            db[MEMBER_COLLECTION].update_one(
                {"member_identity": mid},
                {"$set": {"state": MEMBER_STATE_RESERVED, "updated_at": utc_now()}},
            )
            return True
        return False


def mark_member_state(db, *, cycle_id, policy, threshold, recipient_user_id,
                      recipient_type="executive", delivery_mode="shadow", state=None,
                      notification_id=None):
    """Update the state of a member reservation."""
    mid = _member_identity_full(cycle_id, policy, threshold, recipient_user_id,
                                recipient_type, delivery_mode)
    update = {"state": state or MEMBER_STATE_SUPPRESSED, "updated_at": utc_now()}
    if notification_id:
        if delivery_mode == "shadow":
            update["shadow_notification_id"] = notification_id
        else:
            update["notification_id"] = notification_id
    db[MEMBER_COLLECTION].update_one({"member_identity": mid}, {"$set": update})


# ---------------------------------------------------------------------------
# Group scope identity — stable across the open window
# ---------------------------------------------------------------------------

def _group_scope_identity(recipient_user_id, sla_policy, threshold,
                          recipient_type="executive"):
    """Identity for the open-group unique index. Does NOT include time,
    so concurrent workers find the same open group."""
    return GROUP_SCOPE_IDENTITY_FMT.format(
        recipient=recipient_user_id, policy=sla_policy,
        threshold=threshold, recipient_type=recipient_type,
        content_version=GROUP_SCOPE_CONTENT_VERSION,
    )


# ---------------------------------------------------------------------------
# Validate a single lead-cycle against current state
# ---------------------------------------------------------------------------

def _validate_member(db, lead_id, cycle_id, recipient_user_id,
                     policy="NON_HOT", recipient_type="executive") -> dict:
    """Check if a lead-cycle is still eligible. Returns status dict."""
    lead = db["leads"].find_one({"_id": lead_id}, {
        "lead_temperature_effective": 1, "pipeline_stage": 1, "stage": 1,
        "ejecutivo_asignado": 1,
    })
    if not lead:
        return {"valid": False, "reason": "lead_not_found"}

    stage = str(lead.get("pipeline_stage") or lead.get("stage") or "").upper()
    if stage in {"ARCHIVED", "CLOSED_WON", "CLOSED_LOST"}:
        return {"valid": False, "reason": "lead_closed"}

    lead_is_hot = str(lead.get("lead_temperature_effective") or "").upper() == "HOT"
    if policy == "NON_HOT" and lead_is_hot:
        return {"valid": False, "reason": "lead_became_hot"}
    if policy == "HOT" and not lead_is_hot:
        return {"valid": False, "reason": "lead_not_hot"}

    cycle = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle_id})
    if not cycle:
        return {"valid": False, "reason": "cycle_not_found"}
    if cycle.get("cycle_status") != "active":
        return {"valid": False, "reason": "cycle_not_active"}
    # For supervisor alerts, skip the cycle assignment check — supervisors
    # receive notifications about leads assigned to other executives.
    if recipient_type != "supervisor":
        if str(cycle.get("assigned_to_user_id") or "") != str(recipient_user_id):
            return {"valid": False, "reason": "reassigned"}
    if cycle.get("first_valid_management_at"):
        return {"valid": False, "reason": "already_managed"}

    return {"valid": True, "lead": lead, "cycle": cycle}


# ---------------------------------------------------------------------------
# Accumulate leads for a grouped SLA alert
# ---------------------------------------------------------------------------

def accumulate_sla_alert(db, *, lead, cycle, policy, threshold, recipient_user_id,
                         recipient_type="executive", aggregation_window_minutes=10):
    """Add a lead-cycle to a grouped SLA alert notification.

    If no open aggregation window exists, one is created.
    Returns the notification document or None if the lead should be skipped.
    """
    if _shadow():
        shadow = True
    else:
        shadow = False

    lead_id = lead.get("_id")
    cycle_id = cycle.get("assignment_cycle_id")
    if not lead_id or not cycle_id:
        return None

    # Check member dedup (using delivery_mode from shadow setting)
    dm = "shadow" if shadow else "live"
    if member_already_notified(
        db, cycle_id=cycle_id, policy=policy, threshold=threshold,
        recipient_user_id=recipient_user_id, recipient_type=recipient_type,
        delivery_mode=dm,
    ):
        return None

    # Reserve the member slot atomically (prevents concurrent duplicates)
    if not reserve_member(
        db, lead_id=lead_id, cycle_id=cycle_id, policy=policy, threshold=threshold,
        recipient_user_id=recipient_user_id, recipient_type=recipient_type,
        delivery_mode=dm,
    ):
        return None  # Another worker already reserved this member

    # Validate current state
    validation = _validate_member(db, lead_id, cycle_id, recipient_user_id,
                                   policy=policy, recipient_type=recipient_type)
    if not validation["valid"]:
        logger.info("[SLA_ALERTS] Lead %s invalido: %s", lead_id, validation["reason"])
        return None

    now = utc_now()
    window_due = now + timedelta(minutes=aggregation_window_minutes)

    # Check for existing open group via scope identity (stable across window)
    scope = _group_scope_identity(recipient_user_id, policy, threshold, recipient_type)
    existing = db[NOTIFICATION_COLLECTION].find_one({
        "group_scope_identity": scope,
        "group_open": True,
    })
    if existing:
        existing_items = list(existing.get("items", []))
        existing_ids = {str(it.get("lead_id")) for it in existing_items}
        if str(lead_id) not in existing_ids:
            existing_items.append({
                "lead_id": lead_id,
                "assignment_cycle_id": cycle_id,
                "threshold": threshold,
                "eligible_at": now,
            })
            new_count = len(existing_items)
            db[NOTIFICATION_COLLECTION].update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "items": existing_items,
                    "lead_count": new_count,
                    "updated_at": now,
                }},
            )
        return db[NOTIFICATION_COLLECTION].find_one({"_id": existing["_id"]})

    # Create new group with scope identity
    threshold_key = threshold.lower()
    notification_type = f"sla_{threshold_key}"
    if recipient_type == "supervisor":
        notification_type += "_supervisor"

    sla_group_id = str(uuid.uuid4())
    doc = {
        "sla_group_id": sla_group_id,
        "group_scope_identity": scope,
        "group_open": True,
        "notification_type": notification_type,
        "recipient_user_id": recipient_user_id,
        "recipient_type": recipient_type,
        "sla_policy": policy,
        "sla_threshold": threshold,
        "content_version": GROUP_SCOPE_CONTENT_VERSION,
        "aggregation_window_started_at": now,
        "aggregation_window_due_at": window_due,
        "items": [{
            "lead_id": lead_id,
            "assignment_cycle_id": cycle_id,
            "threshold": threshold,
            "eligible_at": now,
        }],
        "lead_count": 1,
        "state": "pending",
        "delivery_mode": "shadow" if shadow else "pending_live",
        "payload_version": "crm_sla_alert_v1",
        "schema_version": "crm_notification_v1",
        "created_at": now,
        "updated_at": now,
    }
    try:
        db[NOTIFICATION_COLLECTION].insert_one(doc)
        return db[NOTIFICATION_COLLECTION].find_one({
            "group_scope_identity": scope,
            "group_open": True,
        }) or doc
    except DuplicateKeyError:
        existing = db[NOTIFICATION_COLLECTION].find_one({
            "group_scope_identity": scope,
            "group_open": True,
        })
        if existing:
            # Add this lead to the existing group
            existing_items = list(existing.get("items", []))
            existing_ids = {str(it.get("lead_id")) for it in existing_items}
            if str(lead_id) not in existing_ids:
                existing_items.append({
                    "lead_id": lead_id,
                    "assignment_cycle_id": cycle_id,
                    "threshold": threshold,
                    "eligible_at": now,
                })
                db[NOTIFICATION_COLLECTION].update_one(
                    {"_id": existing["_id"]},
                    {"$set": {
                        "items": existing_items,
                        "lead_count": len(existing_items),
                        "updated_at": now,
                    }},
                )
            return db[NOTIFICATION_COLLECTION].find_one({"_id": existing["_id"]})
        raise


# ---------------------------------------------------------------------------
# Build message content for a grouped SLA alert
# ---------------------------------------------------------------------------

def build_sla_alert_content(db, notification) -> str | None:
    """Build the WhatsApp message text for a grouped SLA alert.

    Re-validates each member before building.
    Returns None if all members are invalid (suppressed).
    """
    items = list(notification.get("items", []))
    if not items:
        return None

    threshold = notification.get("sla_threshold", "")
    recipient = str(notification.get("recipient_user_id", ""))
    recipient_type = notification.get("recipient_type", "executive")
    items_are_hot = notification.get("sla_policy") == "HOT"

    if items_are_hot or recipient_type == "supervisor":
        # HOT alerts are individual, supervisor alerts are individual
        return _build_individual_content(db, notification)

    # Re-validate and filter members
    valid_items = []
    for item in items:
        lid = item.get("lead_id")
        cid = item.get("assignment_cycle_id")
        if not lid or not cid:
            continue
        # Check member dedup (might have been marked by another group)
        dm = notification.get("delivery_mode", "shadow")
        if member_already_notified(
            db, cycle_id=cid, policy=notification.get("sla_policy", ""),
            threshold=threshold, recipient_user_id=recipient,
            recipient_type=recipient_type, delivery_mode=dm,
        ):
            continue
        validation = _validate_member(db, lid, cid, recipient,
                                       policy=notification.get("sla_policy", "NON_HOT"),
                                       recipient_type=recipient_type)
        if validation["valid"]:
            valid_items.append(item)

    if not valid_items:
        return None

    # Fetch lead data for previews
    lead_ids = [it["lead_id"] for it in valid_items]
    leads = list(db["leads"].find(
        {"_id": {"$in": lead_ids}},
        {"prospecto.nombre": 1, "prospecto.codigo": 1, "prospecto.comuna": 1},
    ))
    lead_map = {l["_id"]: l for l in leads}

    previews = []
    for item in valid_items:
        l = lead_map.get(item["lead_id"])
        if l:
            p = l.get("prospecto", {}) or {}
            name = p.get("nombre", "Cliente")
            prop = p.get("codigo", "S/N")
            comuna = p.get("comuna", "")
            loc = f" \u2014 {comuna}" if comuna else ""
            previews.append(f"\u2022 {name} \u2014 {prop}{loc}")
        else:
            previews.append(f"\u2022 Cliente \u2014 S/N")

    count = len(valid_items)
    exec_name = _resolve_executive_name(db, recipient)

    if threshold == THRESHOLD_PRECRITICAL_150:
        return sla_non_hot_precritical_150(exec_name, count, previews)
    elif threshold == THRESHOLD_CRITICAL_180:
        return sla_non_hot_critical_180(exec_name, count, previews)
    return None


def _build_individual_content(db, notification) -> str | None:
    """Build an individual alert for HOT or supervisor."""
    items = list(notification.get("items", []))
    if not items:
        return None
    item = items[0]
    lid = item.get("lead_id")
    if not lid:
        return None

    lead = db["leads"].find_one({"_id": lid}, {
        "prospecto.nombre": 1, "prospecto.codigo": 1, "prospecto.comuna": 1,
        "lead_temperature_effective": 1, "ejecutivo_asignado": 1,
    })
    if not lead:
        return None

    p = lead.get("prospecto", {}) or {}
    name = p.get("nombre", "Cliente")
    prop = p.get("codigo", "S/N")
    hot_reason = display_hot_reason(None)
    threshold = notification.get("sla_threshold", "")
    recipient = str(notification.get("recipient_user_id", ""))
    recipient_type = notification.get("recipient_type", "executive")
    exec_name = _resolve_executive_name(db, recipient)

    if threshold == THRESHOLD_HOT_45:
        return sla_hot_precritical_45(exec_name, name, prop, hot_reason)
    elif threshold == THRESHOLD_HOT_60:
        if recipient_type == "supervisor":
            return sla_hot_critical_60_supervisor(exec_name, name, prop, hot_reason, 60)
        return sla_hot_critical_60(exec_name, name, prop, hot_reason, 60)
    elif threshold == THRESHOLD_CRITICAL_180 and recipient_type == "supervisor":
        count = notification.get("lead_count", len(items))
        url = _crm_filtered_url(exec_name)
        return sla_non_hot_critical_180_supervisor(exec_name, count, url)

    return None


# ---------------------------------------------------------------------------
# Send (or shadow-send) a SLA alert
# ---------------------------------------------------------------------------

def send_sla_alert(db, *, notification, worker_id, sender=None):
    """Deliver or shadow-deliver a SLA alert."""
    shadow = _shadow()
    content = build_sla_alert_content(db, notification)
    if content is None:
        _finalize_sla_alert(db, notification["_id"], worker_id, "suppressed",
                            error="no_valid_members")
        return {"status": "suppressed", "reason": "no_valid_members"}

    if shadow:
        _finalize_sla_alert(db, notification["_id"], worker_id, "sent",
                            provider_message_id=None, error=None, shadow=True)
        _mark_members_notified(db, notification, shadow=True)
        return {"status": "shadow_sent", "delivery_mode": "shadow"}

    if not sender:
        _finalize_sla_alert(db, notification["_id"], worker_id, "failed_retryable",
                            error="no_sender")
        return {"status": "failed", "reason": "no_sender"}

    try:
        receipt = sender(notification.get("recipient_phone", ""), content)
        success = bool(receipt.get("success"))
        provider_id = receipt.get("provider_message_id")
        state = "sent" if success and provider_id else "quarantined" if success else "failed_retryable"
        _finalize_sla_alert(db, notification["_id"], worker_id, state,
                            provider_message_id=provider_id)
        if state == "sent":
            _mark_members_notified(db, notification, shadow=False)
        return {"status": state}
    except Exception as exc:
        _finalize_sla_alert(db, notification["_id"], worker_id, "failed_retryable",
                            error=type(exc).__name__)
        return {"status": "failed_retryable"}


def _finalize_sla_alert(db, notification_id, worker_id, state,
                        provider_message_id=None, error=None, shadow=False):
    """Update notification document and mark delivery."""
    if not notification_id:
        logger.warning("[SLA_ALERTS] No notification_id to finalize")
        return
    now = utc_now()
    update = {
        "state": state,
        "group_open": False,
        "lease_owner": None,
        "lease_expires_at": None,
        "updated_at": now,
    }
    if shadow:
        update["delivery_mode"] = "shadow"
        update["actually_delivered"] = False
        update["provider_message_id"] = None
    else:
        update["delivery_mode"] = "live"
        update["actually_delivered"] = state == "sent"
        if provider_message_id is not None:
            update["provider_message_id"] = provider_message_id
        if error:
            update["error"] = error
    db[NOTIFICATION_COLLECTION].update_one(
        {"_id": notification_id, "state": {"$in": ["pending", "sending"]}},
        {"$set": update},
    )


def _mark_members_notified(db, notification, shadow=False):
    """Mark all members of a group as notified for their threshold."""
    items = notification.get("items", [])
    nid = str(notification.get("_id", "unknown"))
    dm = "shadow" if shadow else "live"
    state = MEMBER_STATE_SHADOW if shadow else MEMBER_STATE_LIVE
    for item in items:
        mark_member_state(
            db,
            cycle_id=item.get("assignment_cycle_id"),
            policy=notification.get("sla_policy", ""),
            threshold=notification.get("sla_threshold", ""),
            recipient_user_id=str(notification.get("recipient_user_id", "")),
            recipient_type=notification.get("recipient_type", "executive"),
            delivery_mode=dm,
            state=state,
            notification_id=nid,
        )


# ---------------------------------------------------------------------------
# Claim due SLA alert
# ---------------------------------------------------------------------------

def claim_due_sla_alert(db, *, worker_id, now=None, extra_filter=None):
    """Atomically claim one due SLA alert for delivery."""
    current = coerce_utc_datetime(now) or utc_now()
    query = {"state": {"$in": ["pending", "failed_retryable"]}}
    if extra_filter:
        query.update(extra_filter)
    query["aggregation_window_due_at"] = {"$lte": current}
    return db[NOTIFICATION_COLLECTION].find_one_and_update(
        query,
        {"$set": {
            "state": "sending",
            "lease_owner": worker_id,
            "lease_expires_at": current + timedelta(seconds=120),
            "updated_at": current,
        }},
        sort=[("created_at", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_executive_name(db, user_id):
    """Resolve a display name from user_id."""
    user = db["usuarios"].find_one({"_id": user_id}, {"nombre": 1})
    return user.get("nombre", "Ejecutivo") if user else "Ejecutivo"
