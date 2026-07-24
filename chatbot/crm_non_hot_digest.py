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

def _window_due_at(started_at):
    """Return the fixed UTC expiry for a digest window."""
    window = max(int(getattr(Config, "CRM_NON_HOT_DIGEST_WINDOW_MINUTES", 15)), 1)
    return started_at + timedelta(minutes=window)


def _business_period_label(assigned_at):
    """Label for the digest business period based on the first assignment."""
    dt = coerce_utc_datetime(assigned_at) or utc_now()
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # Pre-cutover cycles are excluded from digest
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
    non_commercial_reasons = ("historical_reconciliation", "lead_processed", "lead_processed_repair", "startup", "backfill", "reconciliation", "cycle_repair")
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

    now = utc_now()
    window_minutes = max(int(getattr(Config, "CRM_NON_HOT_DIGEST_WINDOW_MINUTES", 15)), 1)

    identity = digest_identity(
        recipient_user_id=recipient,
        digest_type=DIGEST_TYPE,
        business_period=_business_period_label(now),
        content_version=CONTENT_VERSION,
    )

    # Try to find an existing open (pending) digest for this executive.
    existing = db[NOTIFICATION_COLLECTION].find_one({
        "digest_identity": identity,
        "schema_version": "crm_notification_v1",
        "state": {"$in": ["pending", "sending"]},
    })

    if existing:
        # Append this lead if not already present
        existing_lead_ids = set(existing.get("lead_ids") or [])
        str_lead_id = str(lead_id)
        if str_lead_id not in existing_lead_ids:
            new_ids = list(existing_lead_ids) + [str_lead_id]
            existing_cycles = list(existing.get("assignment_cycle_ids") or [])
            if str(cycle_id) not in existing_cycles:
                existing_cycles.append(str(cycle_id))
            new_count = len(new_ids)
            # Volume threshold: if we just reached the max, mark as ready now.
            max_before_send = int(getattr(Config, "CRM_NON_HOT_DIGEST_MAX_LEADS_BEFORE_SEND", "0"))
            if max_before_send > 0 and new_count >= max_before_send:
                db[NOTIFICATION_COLLECTION].update_one(
                    {"_id": existing["_id"]},
                    {"$set": {
                        "lead_ids": new_ids,
                        "assignment_cycle_ids": existing_cycles,
                        "updated_at": now,
                        "lead_count": new_count,
                        "send_after": now,
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
                    }},
                )
        else:
            new_count = existing.get("lead_count", len(existing.get("lead_ids") or []))
        return db[NOTIFICATION_COLLECTION].find_one({"_id": existing["_id"]})

    # No open window — create one.
    # The digest window is always 10 minutes from the first lead,
    # regardless of time of day.  After-hours handling (if any) is
    # configured separately and does NOT affect window_due_at.
    send_after = _window_due_at(now)
    now_iso = now.isoformat()
    send_after_iso = send_after.isoformat()
    payload = {
        "digest_type": DIGEST_TYPE,
        "recipient_user_id": recipient,
        "lead_ids": [str(lead_id)],
        "assignment_cycle_ids": [str(cycle_id)],
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
            "lead_ids": [str(lead_id)],
            "assignment_cycle_ids": [str(cycle_id)],
            "window_started_at": now_iso,
            "window_due_at": send_after_iso,
            "lead_count": 1,
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
    for digest in db[NOTIFICATION_COLLECTION].find({
        "lead_ids": str_lead_id,
        "state": {"$in": ["pending", "sending"]},
        "digest_type": DIGEST_TYPE,
    }):
        current_ids = list(digest.get("lead_ids") or [])
        if str_lead_id not in current_ids:
            continue
        new_ids = [lid for lid in current_ids if lid != str_lead_id]
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

def claim_due_digest(db, *, worker_id, now=None):
    """Atomically claim one due non-HOT digest for delivery.

    Returns the claimed notification or ``None``.
    """
    current = coerce_utc_datetime(now) or utc_now()
    return claim_next(
        db,
        worker_id=worker_id,
        now=current,
        extra_filter={
            "digest_type": DIGEST_TYPE,
            "send_after": {"$lte": current},
        },
    )


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

    Returns (content_text, lead_count) or (None, 0) if the digest is stale.
    """
    lead_ids = list(notification.get("lead_ids") or [])
    if not lead_ids:
        return None, 0

    # Re-validate each lead: fetch current state
    leads = list(db["leads"].find(
        {"_id": {"$in": lead_ids}},
        {
            "prospecto.nombre": 1, "prospecto.codigo": 1, "prospecto.comuna": 1,
            "prospecto.origen": 1, "origen": 1, "codigo": 1, "property_code": 1,
            "nombre": 1, "lead_temperature_effective": 1, "pipeline_stage": 1,
            "ejecutivo_asignado": 1, "stage": 1,
        },
    ))

    # Filter out leads that no longer belong
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
        # Compare canonical user IDs, not names.
        lead_exec_id = None
        lead_exec_name = lead.get("ejecutivo_asignado") or ""
        if recipient_norm and lead_exec_name:
            exec_user = db["usuarios"].find_one({"nombre": lead_exec_name}, {"_id": 1})
            if exec_user:
                lead_exec_id = str(exec_user.get("_id"))
        if recipient_norm and lead_exec_id and recipient_norm != lead_exec_id:
            logger.info("[NON_HOT_DIGEST] Lead %s executive_mismatch: recipient=%s != lead_exec=%s (name=%s)",
                        lead["_id"], recipient_norm[:12], lead_exec_id[:12], lead_exec_name[:20])
            continue
        if recipient_norm and not lead_exec_id and lead_exec_name:
            logger.info("[NON_HOT_DIGEST] Lead %s executive_not_found: name=%s", lead["_id"], lead_exec_name[:20])
            continue
        # Exclude leads with management registered in the current cycle
        cycle = db["crm_assignment_cycles"].find_one(
            {"lead_id": lead["_id"], "unassigned_at": None},
            sort=[("assigned_at", -1)],
        )
        if cycle and cycle.get("first_valid_management_at"):
            logger.info(
                "[NON_HOT_DIGEST] Lead %s gestionado (first_valid_management_at=%s). Excluido del digest.",
                lead["_id"], cycle["first_valid_management_at"],
            )
            continue
        valid.append(lead)

    if not valid:
        return None, 0

    max_preview = max(int(getattr(Config, "CRM_NON_HOT_DIGEST_MAX_PREVIEW_ITEMS", 3)), 1)
    count = len(valid)
    oldest = min(
        (coerce_utc_datetime(l.get("created_at")) or utc_now() for l in valid),
        default=utc_now(),
    )
    oldest_minutes = int((utc_now() - oldest).total_seconds() / 60)
    source_dist = _build_source_distribution(valid)
    preview_lines = _build_preview_lines(valid, max_preview)

    # Resolve executive name for message
    cycle = db["crm_assignment_cycles"].find_one(
        {"lead_id": valid[0]["_id"], "unassigned_at": None},
        sort=[("assigned_at", -1)],
    )
    exec_name = _executive_name_from_cycle(db, cycle) if cycle else "Ejecutivo"

    # Build the CRM link — opens all leads sorted by oldest unattended.
    # Internal temperature enum is never exposed in the visible message.
    base = str(getattr(Config, 'CRM_BASE_URL', 'https://procasa-chatbot-yr8d.onrender.com')).rstrip('/')
    crm_url = f"{base}/crm?orden=antiguos_sin_atender"

    if count == 1:
        lines = [
            f"📋 *Tienes 1 nuevo Lead*",
            "",
            f"Hola {exec_name}, tienes un nuevo lead pendiente de gestión.",
            "",
            *preview_lines,
        ]
        if source_dist:
            lines.extend(["", f"📊 *Origen*: {source_dist}"])
    else:
        lines = [
            f"📋 *Tienes {count} nuevos Leads*",
            "",
            f"Hola {exec_name}, tienes {count} nuevos leads pendientes de gestión.",
            f"El más antiguo lleva {oldest_minutes} min. sin gestionar.",
            "",
            *preview_lines,
        ]
        extra = count - len(preview_lines)
        if extra > 0:
            lines.extend(["", f"_{extra} lead{'s' if extra > 1 else ''} adicional{'es' if extra > 1 else ''} disponible{'s' if extra > 1 else ''} en el CRM._"])
        if source_dist:
            lines.extend(["", f"📊 *Distribución por origen*: {source_dist}"])

    lines.extend([
        "",
        f"🔗 *Revisar y gestionar en CRM*:",
        crm_url,
        "",
        "💡 _Toda gestión debe registrarse en el CRM para el control SLA._",
    ])
    return "\n".join(lines), count


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


# Canary allowlist — only these digest IDs can send live during incident resolution
CANARY_DIGEST_IDS: set[str] = set()

def send_digest(db, *, notification, worker_id, sender=None):
    """Deliver or shadow-deliver a due digest.  Fully synchronous.

    ``sender`` is a sync callable ``(phone, message) -> dict`` or None.
    In shadow mode the provider is never called.
    """
    # Hard block: ignore shadow mode entirely during incident
    shadow = True
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

    if not sender:
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="failed_retryable", error="no_sender_provided",
        )
        return {"status": "failed", "reason": "no_sender"}

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

def process_one_digest(db, *, worker_id, now=None, sender=None):
    """Claim and deliver/record one due digest.  Fully synchronous.

    Returns a status dict.  Designed to be called from a periodic worker
    via ``run_in_executor`` so all PyMongo operations run off the event loop.
    ``sender`` is an optional sync callable ``(phone, message) -> dict``.
    """
    notification = claim_due_digest(db, worker_id=worker_id, now=now)
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
