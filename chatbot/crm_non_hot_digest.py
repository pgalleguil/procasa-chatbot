"""10-minute windowed digest for non-HOT (LEAD) assignments.

Each executive receives at most one open digest window.  The first non-HOT
assignment starts a fixed 10-minute clock.  Subsequent non-HOT assignments
accumulate into the same digest.  When the window expires the digest is
claimed, delivered (or shadow-logged) and closed.

The module reuses ``crm_notifications_v1`` for persistence so that the
existing claim / finalize / dedup machinery applies.

Commercial labels use ``Lead`` / ``Leads`` exclusively.  The internal
temperature enum ``COLD`` is never exposed in visible content.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import timedelta
import hashlib
import uuid
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from config import Config
from .crm_metrics import coerce_utc_datetime, utc_now
from .crm_notifications import (
    COLLECTION as NOTIFICATION_COLLECTION,
    content_hash,
    create_pending,
    claim_next,
    finalize_attempt,
    digest_identity,
)
from .lead_router import get_active_executive_phone, build_crm_lead_url
from .lead_temperature import HOT

DIGEST_TYPE = "non_hot_digest_v1"
CONTENT_VERSION = "non_hot_digest_v1"
PAYLOAD_VERSION = "crm_notification_v1"

DIGEST_IDENTITY_FIELD = "digest_identity"
INDIVIDUAL_IDENTITY_FIELD = "individual_identity"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window_due_at(started_at, *, window_minutes=None):
    """Return a fixed in-hours window or the next business opening."""
    from .lead_router import get_next_business_slot, is_business_hours
    if not is_business_hours(started_at):
        return get_next_business_slot(started_at)
    configured_window = (
        window_minutes
        if window_minutes is not None
        else getattr(Config, "CRM_NON_HOT_DIGEST_WINDOW_MINUTES", 10)
    )
    window = max(int(configured_window), 1)
    return started_at + timedelta(minutes=window)


def _business_period_label(assigned_at):
    """Label for the digest business period based on the first assignment."""
    dt = coerce_utc_datetime(assigned_at) or utc_now()
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _immediate_send() -> bool:
    """Temporary override: send the non-HOT digest immediately (no window wait)."""
    return bool(getattr(Config, "CRM_NON_HOT_DIGEST_IMMEDIATE_SEND", False))


def _reference_id(lead_id):
    """Short public reference for a lead (used in message previews)."""
    raw = str(lead_id).encode("utf-8")
    return "CRM-" + hashlib.sha256(raw).hexdigest()[:8].upper()


def _executive_name_from_cycle(db, cycle):
    """Resolve a display name for the executive from the cycle."""
    display = cycle.get("assigned_to_display_name") or ""
    if not display:
        user = db["usuarios"].find_one({"_id": cycle.get("assigned_to_user_id")}, {"nombre": 1})
        if user:
            display = user.get("nombre", "")
    return display or str(cycle.get("assigned_to_user_id", "Ejecutivo"))


def _build_grouped_digest_message(*, executive_name, lead_count):
    """Build the concise CRM-list message for a multi-lead digest.

    Individual deferred notifications intentionally reuse the normal individual
    template.  A group, however, must not become a long WhatsApp list: the
    executive receives the total and opens their own new non-HOT leads in CRM.
    ``scope=mine`` is resolved from the authenticated CRM user, not from a
    display name embedded in the URL.
    """
    base_url = str(getattr(Config, "CRM_BASE_URL", "")).rstrip("/")
    query = urlencode({
        "scope": "mine",
        "temperatura": "COLD",
        "estado": "NEW",
        "orden": "recent_assigned",
    })
    crm_url = f"{base_url}/crm?{query}"
    return "\n".join([
        f"\U0001F4E5 *{lead_count} NUEVOS LEADS ASIGNADOS*",
        "",
        f"Hola {executive_name}, tienes *{lead_count} nuevos leads* para revisar.",
        "",
        f"\U0001F517 *Ver mis leads nuevos en CRM:*\n{crm_url}",
        "",
        "\u26A0\uFE0F Registra el resultado de la gesti\u00F3n en el CRM. Abrir WhatsApp o llamar no cuenta como gesti\u00F3n.",
    ])


# ---------------------------------------------------------------------------
# Accumulate: add a lead to the digest window
# ---------------------------------------------------------------------------

def accumulate_non_hot_lead(db, *, lead, cycle):
    """Add a non-HOT lead to the executive's digest window.

    If no open window exists, one is created with a 15-minute deadline.
    If a window already exists the lead is appended (window is NOT extended).

    Returns the digest notification document or ``None`` if the lead should
    be skipped (e.g. already delivered, already in a sent digest).
    """
    temperature = str(lead.get("lead_temperature_effective") or "").upper()
    if temperature == HOT:
        return None

    # Resolve the actual cycle from DB (the passed `cycle` may be a minimal dict)
    lead_id = lead.get("_id")
    cycle_id = cycle.get("assignment_cycle_id")
    if not lead_id or not cycle_id:
        return None

    db_cycle = None
    try:
        from bson import ObjectId
        db_cycle = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle_id})
    except Exception:
        pass

    if db_cycle is None:
        db_cycle = cycle  # fallback to passed dict
    from .crm_notifications import verified_commercial_source
    if not verified_commercial_source(db, db_cycle):
        logger.info(
            "[NON_HOT_DIGEST] unverified commercial source cycle=%s", cycle_id
        )
        return None
    # Canary: skip all eligibility checks for authorized leads
    str_lead_id = str(lead.get("_id"))
    if str_lead_id in CANARY_LEAD_IDS:
        logger.info("[NON_HOT_DIGEST] Canary lead %s bypassing eligibility checks", str_lead_id[:12])

    # Pre-cutover cycles are excluded from digest
    if str_lead_id not in CANARY_LEAD_IDS:
        from .crm_metrics import is_pre_cutover_cycle
        if is_pre_cutover_cycle(db_cycle.get("assigned_at")):
            logger.debug("[NON_HOT_DIGEST] Skipping pre-cutover cycle %s", cycle_id)
            return None

        # Exclude cycles with non-notifiable origins or missing notification_eligible
        non_notifiable = ("historical_reconciliation", "startup_repair", "cycle_repair", "backfill")
        co = str(db_cycle.get("cycle_origin") or "")
        if co in non_notifiable:
            logger.debug("[NON_HOT_DIGEST] Skipping non-notifiable cycle_origin=%s for %s", co, cycle_id)
            return None
        # Also skip if notification_eligible is explicitly False OR if the reason
        # is a non-commercial processing reason (no notification_eligible field).
        eligible = db_cycle.get("notification_eligible")
        reason = str(db_cycle.get("reason") or "")
        non_commercial_reasons = ("historical_reconciliation", "lead_processed", "lead_processed_repair",
                                  "startup", "startup_repair", "backfill", "reconciliation", "cycle_repair",
                                  "deploy_reprocessing")
        if eligible is False:
            logger.debug("[NON_HOT_DIGEST] Skipping notification_eligible=false for %s", cycle_id)
            return None
        if eligible is None and reason in non_commercial_reasons:
            logger.debug("[NON_HOT_DIGEST] Skipping non-commercial reason=%s for %s", reason, cycle_id)
            return None
    recipient = str(cycle.get("assigned_to_user_id") or "")
    if not lead_id or not cycle_id or not recipient:
        return None

    if not getattr(Config, "CRM_NON_HOT_DIGEST_ENABLED", False):
        return None

    # The fixed window belongs to the commercial assignment event, never to
    # a retry, restart or reconciliation time.
    now = coerce_utc_datetime(db_cycle.get("assigned_at")) or utc_now()
    window_minutes = max(int(getattr(Config, "CRM_NON_HOT_DIGEST_WINDOW_MINUTES", 10)), 1)

    identity = digest_identity(
        recipient_user_id=recipient,
        digest_type=DIGEST_TYPE,
        business_period=_business_period_label(now),
        content_version=CONTENT_VERSION,
    )

    # Try to find an existing open (pending) digest for this executive.
    existing = db[NOTIFICATION_COLLECTION].find_one({
        "recipient_user_id": recipient,
        "digest_type": DIGEST_TYPE,
        "schema_version": "crm_notification_v1",
        "state": {"$in": ["pending", "sending"]},
    }, sort=[("created_at", 1)])

    if existing:
        # Append this lead if not already present.
        # Compare canonical string representations for dedup, but store ObjectId.
        existing_lead_ids = [str(lid) for lid in (existing.get("lead_ids") or [])]
        str_lead_id = str(lead_id)
        if str_lead_id not in existing_lead_ids:
            new_ids = list(existing.get("lead_ids") or []) + [lead_id]
            existing_cycles = list(existing.get("assignment_cycle_ids") or [])
            if str(cycle_id) not in existing_cycles:
                existing_cycles.append(str(cycle_id))
            cycle_reasons = list(existing.get("cycle_reasons") or [])
            cycle_origins = list(existing.get("cycle_origins") or [])
            if db_cycle.get("reason") not in cycle_reasons:
                cycle_reasons.append(db_cycle.get("reason"))
            origin = db_cycle.get("cycle_origin") or db_cycle.get("reason")
            if origin not in cycle_origins:
                cycle_origins.append(origin)
            new_count = len(new_ids)
            # Volume threshold: if we just reached the max, mark as ready now.
            # Temporary immediate-send override also marks it ready now.
            max_before_send = int(getattr(Config, "CRM_NON_HOT_DIGEST_MAX_LEADS_BEFORE_SEND", "0"))
            if _immediate_send() or (max_before_send > 0 and new_count >= max_before_send):
                db[NOTIFICATION_COLLECTION].update_one(
                    {"_id": existing["_id"]},
                    {"$set": {
                        "lead_ids": new_ids,
                        "assignment_cycle_ids": existing_cycles,
                        "updated_at": now,
                        "lead_count": new_count,
                        "send_after": now,
                        "notification_eligible": True,
                        "cycle_reasons": cycle_reasons,
                        "cycle_origins": cycle_origins,
                    }},
                )
            else:
                db[NOTIFICATION_COLLECTION].update_one(
                    {"_id": existing["_id"]},
                    {"$set": {
                        "lead_ids": new_ids,
                        "assignment_cycle_ids": existing_cycles,
                        "updated_at": now,
                        "lead_count": new_count,
                        "notification_eligible": True,
                        "cycle_reasons": cycle_reasons,
                        "cycle_origins": cycle_origins,
                    }},
                )
        else:
            new_count = existing.get("lead_count", len(existing.get("lead_ids") or []))
        return db[NOTIFICATION_COLLECTION].find_one({"_id": existing["_id"]})

    # No open window — create one.
    # The digest window is 10 minutes from the first lead (unless temporary
    # immediate-send is active, in which case it is due right away).
    send_after = now if _immediate_send() else _window_due_at(now, window_minutes=window_minutes)
    now_iso = now.isoformat()
    send_after_iso = send_after.isoformat()
    payload = {
        "digest_type": DIGEST_TYPE,
        "recipient_user_id": recipient,
        "lead_ids": [str(lead_id)],
        "assignment_cycle_ids": [cycle_id],
        "window_started_at": now_iso,
        "window_due_at": send_after_iso,
        "lead_count": 1,
    }

    notification = create_pending(
        db,
        identity_field=DIGEST_IDENTITY_FIELD,
        identity=identity,
        payload=payload,
        payload_version=PAYLOAD_VERSION,
        canonical_fields={
            "recipient_user_id": recipient,
            "digest_type": DIGEST_TYPE,
            "business_period": _business_period_label(now),
            "content_version": CONTENT_VERSION,
            "lead_ids": [lead_id],
            "assignment_cycle_ids": [cycle_id],
            "window_started_at": now_iso,
            "window_due_at": send_after_iso,
            "lead_count": 1,
            "notification_eligible": db_cycle.get("notification_eligible") is True,
            "cycle_reasons": [db_cycle.get("reason")],
            "cycle_origins": [db_cycle.get("cycle_origin") or db_cycle.get("reason")],
        },
        metadata={
            "digest_type": DIGEST_TYPE,
            "recipient_user_id": recipient,
        },
        send_after=send_after,
    )
    return notification


# ---------------------------------------------------------------------------
# Exclude: remove a lead from an open digest (e.g. became HOT, reassigned)
# ---------------------------------------------------------------------------

def exclude_from_open_digest(db, *, lead_id, assignment_cycle_id=None):
    """Remove a lead from any open digest window.

    Called when a lead transitions to HOT, is reassigned, archived, or closed.
    The lead is removed from the pending digest's lead_ids list.

    Returns the digest document(s) that were modified, or an empty list.
    """
    str_lead_id = str(lead_id)
    modified = []
    # Match both string and ObjectId stored lead_ids
    for digest in db[NOTIFICATION_COLLECTION].find({
        "$or": [{"lead_ids": lead_id}, {"lead_ids": str_lead_id}],
        "state": {"$in": ["pending", "sending"]},
        "digest_type": DIGEST_TYPE,
    }):
        current_ids = list(digest.get("lead_ids") or [])
        # Compare canonical string representations
        current_strs = [str(cid) for cid in current_ids]
        if str_lead_id not in current_strs:
            continue
        new_ids = [cid for i, cid in enumerate(current_ids) if current_strs[i] != str_lead_id]
        current_cycles = list(digest.get("assignment_cycle_ids") or [])
        new_cycles = current_cycles
        if assignment_cycle_id:
            new_cycles = [cid for cid in current_cycles if str(cid) != str(assignment_cycle_id)]
        updates = {
            "lead_ids": new_ids,
            "assignment_cycle_ids": new_cycles,
            "lead_count": len(new_ids),
            "updated_at": utc_now(),
        }
        if not new_ids:
            updates["state"] = "suppressed"
            updates["suppressed_reason"] = "all_leads_removed"
        db[NOTIFICATION_COLLECTION].update_one(
            {"_id": digest["_id"]},
            {"$set": updates},
        )
        modified.append(digest["_id"])
    return modified


# ---------------------------------------------------------------------------
# Claim due digest windows
# ---------------------------------------------------------------------------

def _recover_stuck_digests(db, *, now):
    """Reclaim non-HOT digests stuck in 'sending' with an expired lease.

    A digest can be left in ``sending`` forever if the process restarts during
    the provider call or the provider call hangs without a recorded outcome.
    ``claim_next`` only picks up ``pending``/``failed_retryable``, so without
    this step the executive would never be notified (notification lost).
    Only digests with no accepted provider message id are recovered; anything
    with delivery evidence is left untouched.
    """
    current = coerce_utc_datetime(now) or utc_now()
    db[NOTIFICATION_COLLECTION].update_many(
        {"digest_type": DIGEST_TYPE,
         "message_domain": "commercial_notification",
         "state": "sending",
         "lease_expires_at": {"$lte": current},
         "provider_message_id": {"$in": [None]},
         "actually_delivered": {"$ne": True}},
        {"$set": {"state": "failed_retryable", "lease_owner": None,
                  "lease_expires_at": None, "delivery_token": None,
                  "provider_call_started_at": None,
                  "next_attempt_at": current, "updated_at": current}},
    )


def claim_due_digest(db, *, worker_id, now=None, notification_id=None):
    """Atomically claim one due non-HOT digest for delivery.

    Returns the claimed notification or ``None``.
    """
    current = coerce_utc_datetime(now) or utc_now()
    _recover_stuck_digests(db, now=current)
    extra_filter = {
            "digest_type": DIGEST_TYPE,
            "message_domain": "commercial_notification",
            "send_after": {"$lte": current},
            "notification_eligible": True,
            "cycle_reasons": {"$not": {"$elemMatch": {"$nin": [
                "lead_created", "inbound_message", "manual_lead_created",
            ]}}},
            "cycle_origins": {"$not": {"$elemMatch": {"$nin": [
                "inbound_message", "manual_lead",
            ]}}},
            # ``finalize_attempt`` stores a failed provider ID as null; both
            # missing and null mean no provider accepted this delivery.
            "provider_message_id": {"$in": [None]},
            "actually_delivered": {"$ne": True},
    }
    # Recovery uses the same durable claim path, narrowed to the exact
    # notification. Normal workers never pass this selector.
    if notification_id is not None:
        extra_filter["_id"] = notification_id
    return claim_next(db, worker_id=worker_id, now=current, extra_filter=extra_filter)


# ---------------------------------------------------------------------------
# Build content
# ---------------------------------------------------------------------------

def _build_source_distribution(leads):
    """Return a short source-breakdown string."""
    sources = Counter()
    for lead in leads:
        src = str(lead.get("prospecto", {}).get("origen") or lead.get("origen") or "Directo")
        sources[src] += 1
    parts = [f"{n} {src}" for src, n in sources.most_common(3)]
    return ", ".join(parts) if parts else ""


def _build_preview_lines(leads, max_items):
    """Build up to max_items preview lines from lead data."""
    lines = []
    for lead in leads[:max_items]:
        name = (
            lead.get("prospecto", {}).get("nombre")
            or lead.get("nombre")
            or _reference_id(lead.get("_id"))
        )
        prop = (
            lead.get("prospecto", {}).get("codigo")
            or lead.get("codigo")
            or lead.get("property_code")
            or "S/N"
        )
        comuna = lead.get("prospecto", {}).get("comuna") or ""
        location = f" - {comuna}" if comuna else ""
        lines.append(f"• {name} — Prop. {prop}{location}")
    return lines


def build_digest_message_content(db, notification):
    """Build the WhatsApp message text for the digest.
    
    Uses the canonical notification context for each lead.
    Returns (content_text, lead_count) or (None, 0) if stale.
    """
    lead_ids = list(notification.get("lead_ids") or [])
    if not lead_ids:
        return None, 0

    _normalized_ids = []
    for _lid in lead_ids:
        if isinstance(_lid, str) and len(_lid) == 24 and not _lid.startswith("$"):
            try:
                from bson import ObjectId
                _normalized_ids.append(ObjectId(_lid))
            except Exception:
                _normalized_ids.append(_lid)
        else:
            _normalized_ids.append(_lid)

    leads = list(db["leads"].find(
        {"_id": {"$in": _normalized_ids}},
        {
            "prospecto.nombre": 1, "prospecto.codigo": 1, "prospecto.comuna": 1,
            "codigo": 1, "property_code": 1,
            "nombre": 1, "lead_temperature_effective": 1, "pipeline_stage": 1,
            "ejecutivo_asignado": 1, "stage": 1,
            "created_at": 1, "lifecycle": 1,
        },
    ))

    recipient = str(notification.get("recipient_user_id") or "")
    recipient_norm = None
    if recipient:
        resolved = resolve_recipient_user(db, recipient)
        if resolved:
            recipient_norm = str(resolved.get("_id"))

    valid = []
    for lead in leads:
        if str(lead.get("lead_temperature_effective") or "").upper() == HOT:
            _notify_hot_outside_digest(db, lead)
            continue
        if str(lead.get("pipeline_stage") or lead.get("stage") or "").upper() in {
            "ARCHIVED", "CLOSED_WON", "CLOSED_LOST",
        }:
            continue
        if lead.get("is_duplicate"):
            continue
        lead_exec_id = None
        lead_exec_name = lead.get("ejecutivo_asignado") or ""
        if recipient_norm and lead_exec_name:
            exec_user = db["usuarios"].find_one({"nombre": lead_exec_name}, {"_id": 1})
            if exec_user:
                lead_exec_id = str(exec_user.get("_id"))
        if recipient_norm and lead_exec_id and recipient_norm != lead_exec_id:
            continue
        expected_cycle_ids = {
            str(value) for value in (notification.get("assignment_cycle_ids") or [])
        }
        cycle = db["crm_assignment_cycles"].find_one(
            {
                "lead_id": lead["_id"],
                "unassigned_at": None,
                "assignment_cycle_id": {"$in": list(expected_cycle_ids)},
            },
            sort=[("assigned_at", -1)],
        )
        from .crm_notifications import verified_commercial_source
        if not verified_commercial_source(db, cycle):
            continue
        if cycle and cycle.get("first_valid_management_at"):
            continue
        valid.append(lead)

    if not valid:
        return None, 0

    from .crm_message_context import build_lead_notification_context
    from .lead_router import format_whatsapp_template

    contexts = [build_lead_notification_context(db, ld["_id"]) for ld in valid]
    exec_name = contexts[0].get("exec_name") or "Ejecutivo"

    # A one-lead digest is merely a deferred delivery (for example, a lead
    # received outside business hours).  Reuse the normal individual template
    # verbatim; otherwise the recipient sees a different message depending on
    # the delivery time rather than on the event itself.
    if len(valid) == 1:
        lead_data = dict(valid[0])
        context = contexts[0]
        lead_data.setdefault("nombre", context.get("nombre_cliente"))
        lead_data.setdefault("property_code", context.get("property_code"))
        lead_data.setdefault("operacion", context.get("operacion"))
        lead_data.setdefault("comuna", context.get("comuna"))
        content = format_whatsapp_template(
            lead_data,
            exec_name,
            context.get("property_code") or "S/N",
            is_new_assignment=True,
        )
        return content, 1

    content = _build_grouped_digest_message(
        executive_name=exec_name,
        lead_count=len(valid),
    )
    return content, len(valid)


def _notify_hot_outside_digest(db, lead):
    """Send immediate HOT notification for a lead found HOT during digest validation."""
    from .crm_hot_delivery import assign_and_enqueue_hot
    from .crm_metrics import active_assignment_cycle
    cycle = active_assignment_cycle(db, lead["_id"])
    if not cycle:
        return
    exec_user = db["usuarios"].find_one({"_id": cycle.get("assigned_to_user_id")}, {"telefono": 1})
    if not exec_user:
        return
    exec_phone = exec_user.get("telefono") or ""
    prospect = lead.get("prospecto") or {}
    property_code = (
        prospect.get("codigo") or prospect.get("codigo_interno") or lead.get("codigo")
    )
    payload = {
        "lead_phone": lead.get("phone"),
        "property_code": property_code,
        "nombre": prospect.get("nombre") or "Cliente",
        "comuna": prospect.get("comuna"),
        "operacion": prospect.get("operacion"),
        "last_message": lead.get("last_message_preview") or "",
        "lead_type": "LeadHotWhatsapp",
        "hot_reason": "Clasificado HOT durante ventana de digest",
    }
    assign_and_enqueue_hot(
        db, lead=lead,
        recipient_user_id=str(cycle.get("assigned_to_user_id", "")),
        recipient_phone=exec_phone,
        payload=payload,
        assigned_by="digest_validator",
        reason="HOT_during_digest_window",
    )


# ---------------------------------------------------------------------------
# Send (or shadow-send)
# ---------------------------------------------------------------------------


def resolve_recipient_user(db, recipient: str) -> dict | None:
    """Resolve a recipient user from various identifier formats.

    Priority:
    1. ObjectId (from string hex)
    2. Raw string _id (legacy users with string _id)
    3. nombre (exact match, logged as fallback)
    Returns the user dict or None.
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    # 1. Try as ObjectId hex string
    if isinstance(recipient, str) and len(recipient) == 24:
        try:
            oid = ObjectId(recipient)
            user = db["usuarios"].find_one({"_id": oid})
            if user:
                return user
        except InvalidId:
            pass

    # 2. Try as raw string _id (legacy)
    user = db["usuarios"].find_one({"_id": recipient})
    if user:
        return user

    # 3. Fallback: exact nombre match (logged)
    user = db["usuarios"].find_one({"nombre": recipient})
    if user:
        logger.info("[DIGEST] recipient_resolution_mode=name_fallback for %s", recipient[:20])
        return user

    logger.warning("[DIGEST] recipient not resolved: %s (type=%s)", str(recipient)[:24], type(recipient).__name__)
    return None


# Canary allowlists — emptied after successful validation
CANARY_DIGEST_IDS: set[str] = set()
CANARY_LEAD_IDS: set[str] = set()

def send_digest(db, *, notification, worker_id, sender=None):
    """Deliver or shadow-deliver a due digest.  Fully synchronous.

    ``sender`` is a sync callable ``(phone, message) -> dict`` or None.
    In shadow mode the provider is never called.
    """
    if notification.get("message_domain") != "commercial_notification":
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="suppressed", error="wrong_message_domain",
        )
        return {"status": "suppressed", "reason": "wrong_message_domain"}

    # Shadow mode: controlled by config. Canary IDs can bypass during incident.
    is_canary = str(notification.get("_id")) in CANARY_DIGEST_IDS
    shadow = Config.CRM_NON_HOT_DIGEST_SHADOW_MODE and not is_canary
    content, lead_count = build_digest_message_content(db, notification)
    if content is None:
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="suppressed", error="no_valid_leads",
        )
        return {"status": "suppressed", "reason": "no_valid_leads"}

    if shadow:
        # Shadow mode: mark as sent with distinguishing fields.
        # ``state="sent"`` keeps the notification lifecycle compatible with the
        # existing worker contract.  ``delivery_mode`` and
        # ``actually_delivered`` allow analytics and reports to exclude shadow
        # deliveries from real metrics.
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="sent", provider_message_id=None,
            error=None,
        )
        db[NOTIFICATION_COLLECTION].update_one(
            {"_id": notification["_id"]},
            {"$set": {
                "delivery_mode": "shadow",
                "actually_delivered": False,
                "provider_message_id": None,
            }},
        )
        return {"status": "shadow_sent", "lead_count": lead_count, "suppressed": False,
                "delivery_mode": "shadow", "actually_delivered": False}

    # Live delivery: use internal sender when none provided
    _effective_sender = sender
    if not _effective_sender:
        recipient = str(notification.get("recipient_user_id") or "")
        from .crm_delivery import resolve_executive_user, get_executive_phone
        exec_user = resolve_executive_user(db, recipient)
        if not exec_user:
            finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                             state="failed_recipient", error="executive_not_found")
            return {"status": "failed_recipient", "reason": "executive_not_found"}
        phone = get_executive_phone(exec_user)
        if not phone:
            finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                             state="failed_recipient", error="executive_phone_missing")
            return {"status": "failed_recipient", "reason": "no_phone"}

        from .crm_notifications import reserve_for_delivery, refresh_lease, record_delivery_attempt
        from .crm_notifications import (
            reserve_cycle_delivery, finalize_cycle_delivery, is_cycle_delivered,
            release_cycle_delivery,
        )
        from .whatsapp_client import send_whatsapp_message_detailed
        import asyncio, uuid, threading
        import hashlib

        # --- 1. Pre-provider: reserve delivery slot atomically ---
        delivery_token = str(uuid.uuid4())
        reserved = reserve_for_delivery(db, notification_id=notification["_id"],
                                        worker_id=worker_id, delivery_token=delivery_token)
        if not reserved:
            logger.warning("[DIGEST_DUP] notif=%s already reserved — skipping", str(notification["_id"])[:12])
            return {"status": "already_reserved", "reason": "delivery_in_progress"}

        # --- 2. Cycle-level barrier: one delivery per cycle ---
        cycle_ids = list(notification.get("assignment_cycle_ids") or [])
        reserved_cycles = []
        for cid in cycle_ids:
            if is_cycle_delivered(db, assignment_cycle_id=cid):
                logger.warning("[DIGEST_DUP] cycle=%s already delivered — skipping", str(cid)[:12])
                finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                                 state="failed_retryable", error="cycle_already_delivered")
                return {"status": "failed_retryable", "reason": "cycle_already_delivered"}
            rc = reserve_cycle_delivery(db, assignment_cycle_id=cid,
                                        digest_id=str(notification["_id"]),
                                        delivery_token=delivery_token)
            if rc:
                reserved_cycles.append(cid)

        # --- 3. Refresh lease before calling provider ---
        refresh_lease(db, notification_id=notification["_id"], worker_id=worker_id, lease_seconds=120)

        # --- 4. Call provider ---
        content_hash = hashlib.sha256((content or "").encode()).hexdigest()
        phone_display = phone[-4:] if phone else "?"
        logger.info("[DIGEST_SEND] notif=%s exec=%s phone_end=%s len=%d token=%s",
                    str(notification["_id"])[:12], str(recipient)[:16],
                    phone_display, len(content or ""), delivery_token[:8])
        call_started = utc_now()
        try:
            receipt = asyncio.run(send_whatsapp_message_detailed(phone, content))
        except Exception as exc:
            record_delivery_attempt(db, notification_id=notification["_id"],
                                    delivery_token=delivery_token,
                                    attempt_data={"started_at": call_started, "worker_id": worker_id,
                                                  "http_status": None, "provider_message_id": None,
                                                  "content_hash": content_hash, "result": "exception"})
            finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                             state="failed_retryable", error=type(exc).__name__)
            return {"status": "failed_retryable", "error": type(exc).__name__}

        success = bool(receipt.get("success"))
        provider_id = receipt.get("provider_message_id")
        http_status = receipt.get("http_status")
        logger.info("[DIGEST_RESULT] notif=%s success=%s provider=%s http=%s token=%s",
                    str(notification["_id"])[:12], success, provider_id, http_status, delivery_token[:8])

        # --- 5. Append-only delivery record ---
        result = "sent" if (success and provider_id) else ("failed_validation" if http_status == 422 else "failed")
        record_delivery_attempt(db, notification_id=notification["_id"],
                                delivery_token=delivery_token,
                                attempt_data={"started_at": call_started, "worker_id": worker_id,
                                              "http_status": http_status, "provider_message_id": provider_id,
                                              "content_hash": content_hash, "result": result})

        if success and provider_id:
            finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                             state="sent", provider_message_id=provider_id)
            db[NOTIFICATION_COLLECTION].update_one(
                {"_id": notification["_id"]},
                {"$set": {"delivery_mode": "live", "actually_delivered": True, "updated_at": utc_now()}},
            )
            # Mark cycles as delivered
            for cid in reserved_cycles:
                finalize_cycle_delivery(db, assignment_cycle_id=cid,
                                        provider_message_id=provider_id)
            return {"status": "sent", "lead_count": lead_count, "provider_message_id": provider_id,
                    "delivery_mode": "live", "actually_delivered": True}
        else:
            state = "failed_retryable" if http_status not in (422,) else "failed_validation"
            failure_reason = f"http_{http_status}" if http_status else receipt.get("error")
            finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                             state=state, error=failure_reason)
            # The provider explicitly rejected this request without an ID, so a
            # future retry needs a fresh delivery token and pre-call reservation.
            db[NOTIFICATION_COLLECTION].update_one(
                {"_id": notification["_id"], "state": state, "provider_message_id": None},
                {"$unset": {"provider_call_started_at": "", "delivery_token": ""},
                 "$set": {"updated_at": utc_now()}},
            )
            # A provider response without an accepted provider ID is a confirmed
            # non-delivery, so this exact reservation can safely be retried.
            for cid in reserved_cycles:
                release_cycle_delivery(
                    db, assignment_cycle_id=cid, digest_id=notification["_id"],
                    delivery_token=delivery_token, reason=failure_reason,
                )
            retry_after = None
            if http_status == 429:
                try:
                    retry_after = max(int(receipt.get("retry_after", 60)), 30)
                except (TypeError, ValueError):
                    retry_after = 60
                db[NOTIFICATION_COLLECTION].update_one(
                    {"_id": notification["_id"], "state": "failed_retryable"},
                    {"$set": {"next_attempt_at": utc_now() + timedelta(seconds=retry_after),
                              "updated_at": utc_now()}},
                )
            return {"status": state, "lead_count": lead_count, "retry_after": retry_after}

    recipient = str(notification.get("recipient_user_id") or "")
    exec_user = resolve_recipient_user(db, recipient)
    if not exec_user:
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="failed_final", error="recipient_not_found",
        )
        return {"status": "failed", "reason": "recipient_not_found"}

    phone = str(exec_user.get("telefono") or "").strip()
    if not phone:
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="failed_final", error="no_phone",
        )
        return {"status": "failed", "reason": "recipient_not_found"}

    # Instrumented send
    logger.info("[WASEND] sending digest=%s recipient=%s phone_end=%s len=%d",
                notification["_id"], recipient[:16], phone[-4:], len(content or ""))
    try:
        receipt = sender(phone, content)
        success = bool(receipt.get("success"))
        provider_id = receipt.get("provider_message_id")
        http_status = receipt.get("http_status", receipt.get("status_code"))
        logger.info("[WASEND] result digest=%s success=%s provider=%s http=%s",
                    notification["_id"], success, provider_id, http_status)

        if not success:
            if http_status == 422:
                # Validation error — never retry
                logger.warning("[WASEND] provider_422 digest=%s body=%s",
                               notification["_id"],
                               str(receipt.get("response_body", receipt.get("error", "")))[:200])
                finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                                 state="failed_validation", error=f"http_422",
                                 provider_message_id=None)
                return {"status": "failed_validation", "lead_count": lead_count,
                        "delivery_mode": "live", "actually_delivered": False}
            elif http_status == 429:
                # Rate limit — respect Retry-After
                retry_after = int(receipt.get("retry_after", 60))
                logger.warning("[WASEND] provider_429 digest=%s retry_after=%s",
                               notification["_id"], retry_after)
                next_attempt = utc_now() + timedelta(seconds=max(retry_after, 30))
                db[NOTIFICATION_COLLECTION].update_one(
                    {"_id": notification["_id"]},
                    {"$set": {"next_attempt_at": next_attempt, "updated_at": utc_now()},
                     "$push": {"attempts": {"rate_limited_at": utc_now(), "retry_after": retry_after}}},
                )
                return {"status": "rate_limited", "lead_count": lead_count,
                        "delivery_mode": "live", "actually_delivered": False}

        state = "sent" if success and provider_id else "quarantined" if success else "failed_retryable"
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state=state, provider_message_id=provider_id,
            error=None if success else receipt.get("error"),
        )
        db[NOTIFICATION_COLLECTION].update_one(
            {"_id": notification["_id"]},
            {"$set": {
                "delivery_mode": "live",
                "actually_delivered": bool(success and provider_id),
            }},
        )
        return {"status": state, "lead_count": lead_count,
                "delivery_mode": "live", "actually_delivered": bool(success and provider_id)}
    except Exception as exc:
        logger.error("[WASEND] exception digest=%s error=%s", notification["_id"], exc)
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="failed_retryable", error=type(exc).__name__,
        )
        return {"status": "failed_retryable", "error": type(exc).__name__}


# ---------------------------------------------------------------------------
# Process one due digest (async-friendly, designed for worker loop)
# ---------------------------------------------------------------------------

def process_one_digest(db, *, worker_id, now=None, sender=None, notification_id=None):
    """Claim and deliver/record one due digest.  Fully synchronous.

    Returns a status dict.  Designed to be called from a periodic worker
    via ``run_in_executor`` so all PyMongo operations run off the event loop.
    ``sender`` is an optional sync callable ``(phone, message) -> dict``.
    """
    notification = claim_due_digest(db, worker_id=worker_id, now=now, notification_id=notification_id)
    if not notification:
        return {"status": "idle"}
    # send_digest must not be async when called from here
    if hasattr(send_digest, '__code__') and send_digest.__code__.co_flags & 0x80:
        raise TypeError("send_digest must be sync when called from process_one_digest")
    result = send_digest(db, notification=notification, worker_id=worker_id, sender=sender)
    return result


# ---------------------------------------------------------------------------
# Ensure indexes
# ---------------------------------------------------------------------------

def ensure_digest_indexes(db):
    """Create the partial unique index for non-HOT digests."""
    collection = db[NOTIFICATION_COLLECTION]
    existing_indexes = {idx["name"] for idx in collection.list_indexes()}
    index_name = "uq_crm_notification_non_hot_digest_v1"
    if index_name in existing_indexes:
        return {"created": []}
    try:
        collection.create_index(
            [("recipient_user_id", 1), ("digest_type", 1), ("business_period", 1), ("content_version", 1)],
            unique=True,
            partialFilterExpression={
                "schema_version": "crm_notification_v1",
                "digest_type": DIGEST_TYPE,
                "recipient_user_id": {"$exists": True},
            },
            name=index_name,
        )
        return {"created": [index_name]}
    except Exception as exc:
        return {"created": [], "error": str(exc)}
