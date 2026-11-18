"""Prospective HOT-only WhatsApp greeting from the assigned executive's own session.

No office token, chatbot response, executive impersonation, automatic retries,
or SLA exemption for unconfirmed/unknown/provider-rejected messages.
Disabled unless both a deployment flag and a future cutover are configured.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
import os
import re
from uuid import uuid4

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import requests

from config import Config
from .crm_metrics import (
    active_assignment_cycle, coerce_utc_datetime,
    resolve_cycle_sla_temperature,
)
from .crm_executive_outbound import OUTBOX, OutboundBlocked
from .executive_whatsapp_audit import _session, _authorized_lead
from .phone_utils import normalize_phone_strict
from .price_drop_campaign_classifier import lead_must_not_be_contacted
from .whatsapp_client import normalize_whatsapp_recipient, _provider_message_id

logger = logging.getLogger(__name__)
POLICY = "hot_exec_auto_v1"
KIND = "HOT_EXEC_API_CONFIRMED"
MAX_ASSIGNMENT_AGE_MINUTES = 60
SESSION_ID = re.compile(r"^[1-9][0-9]{0,17}$")
CONTACT_WINDOW = timedelta(hours=24)


def _cutover() -> datetime | None:
    raw = os.getenv("PROCASA_EXEC_HOT_AUTO_CUTOVER_AT", "")
    if not raw or len(raw) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else None


def _session_allowlist() -> set[str]:
    raw = os.getenv("PROCASA_EXEC_HOT_AUTO_SESSION_IDS", "")
    if not raw or len(raw) > 256:
        return set()
    ids = [x.strip() for x in raw.split(",")]
    if len(ids) > 10 or len(ids) != len(set(ids)) or any(not SESSION_ID.fullmatch(x) for x in ids):
        return set()
    return set(ids)


def enabled() -> bool:
    return (
        os.getenv("PROCASA_EXEC_HOT_AUTO_ENABLED", "0") == "1"
        and _cutover() is not None
        and bool(_session_allowlist())
    )


def sla_exemption(cycle: dict | None, lead: dict | None = None) -> dict | None:
    """A canonical cycle's durable accepted-send evidence, independent of live flags.

    A feature-flag shutdown cannot retroactively give an exempt cycle an SLA.
    Counterfeit lead-level or unassigned historical records are not sufficient.
    """
    cycle = cycle if isinstance(cycle, dict) else {}
    row = cycle.get("hot_auto_sla_exemption") or {}
    if not isinstance(row, dict):
        return None
    accepted = coerce_utc_datetime(row.get("accepted_at"))
    assigned = coerce_utc_datetime(cycle.get("assigned_at"))
    if (
        row.get("status") != "active"
        or row.get("kind") != KIND
        or row.get("policy_version") != POLICY
        or not row.get("provider_message_id")
        or str(row.get("assignment_cycle_id") or "") != str(cycle.get("assignment_cycle_id") or "")
        or str(row.get("executive_id") or "") != str(cycle.get("assigned_to_user_id") or "")
        or not str(row.get("session_id") or "").isdigit()
        or accepted is None
        or assigned is None
        or accepted < assigned
        or not str(row.get("outbox_id") or "").strip()
        or str(cycle.get("temperature_at_assignment") or "").upper() != "HOT"
        or cycle.get("cycle_status") != "active"
        or cycle.get("unassigned_at") is not None
    ):
        return None
    # Never apply past-cycle exemption to a new or unrelated lead.
    if lead is not None:
        lifecycle = (lead or {}).get("lifecycle") or {}
        if (
            str(lifecycle.get("current_assignment_cycle_id") or "") != str(cycle.get("assignment_cycle_id") or "")
            or str(lifecycle.get("assigned_to_user_id") or "") != str(cycle.get("assigned_to_user_id") or "")
        ):
            return None
    return row


def _verified_whatsapp_permission(lead: dict, *, phone: str, now: datetime) -> bool:
    """Business inquiry reply, not a marketing blast.

    Require either a source-identifiable recent inbound WhatsApp from the
    customer or an explicit, unrevoked sales-inquiry authorization.
    """
    auth = ((lead.get("crm_hot_auto") or {}).get("whatsapp_contact_authorization") or {})
    if isinstance(auth, dict):
        reviewed = coerce_utc_datetime(auth.get("reviewed_at"))
        expires = coerce_utc_datetime(auth.get("expires_at"))
        if (
            auth.get("status") == "ACTIVE"
            and auth.get("channel") == "WHATSAPP"
            and auth.get("scope") == "SALE_INQUIRY"
            and normalize_phone_strict(auth.get("phone_normalized")) == phone
            and len(str(auth.get("evidence_reference") or "")) >= 8
            and str(auth.get("reviewed_by") or "").strip()
            and reviewed is not None and reviewed <= now
            and auth.get("revoked_at") is None
            and (expires is None or expires > now)
        ):
            return True
    for msg in reversed(lead.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        if (msg.get("actor_type") != "customer"
                or msg.get("role") not in ("user", "customer")
                or not msg.get("provider_message_id")
                or msg.get("channel") == "executive_whatsapp"):
            continue
        source = str(msg.get("source") or "").lower()
        channel = str(msg.get("channel") or "").lower()
        if source not in {"office_whatsapp", "wasender_inbound", "whatsapp_inbound", "whatsapp"} and channel not in {"office_whatsapp", "whatsapp"}:
            continue
        timestamp = msg.get("timestamp")
        # Unknown-zone historical timestamps are not positive evidence.
        if isinstance(timestamp, str) and not re.search(r"(Z|[+-]\d{2}:\d{2})$", timestamp):
            continue
        received = coerce_utc_datetime(timestamp)
        if received and now - CONTACT_WINDOW <= received <= now:
            return True
    return False


def _verified_recent_office_job(db, *, lead: dict, phone: str, now: datetime) -> bool:
    """Use the canonical, idempotent office inbound queue as inquiry evidence.

    Office webhooks persist a signed/provider-ID inbound in chatbot_inbound_jobs
    before the chatbot writes its timeline. Never infer consent from generic
    lead creation, portal scraping, lead-level last_message_at or campaigns.
    """
    if not lead.get("_id"):
        return False
    row = db["chatbot_inbound_jobs"].find_one({
        "kind": "inbound_job",
        "message_type": "inbound_message",
        "message_domain": "chatbot",
        "is_from_me": False,
        "lead_id": str(lead["_id"]),
        "phone": phone,
        "received_at": {"$gte": now - CONTACT_WINDOW, "$lte": now},
        "inbound_provider_message_id": {"$type": "string", "$ne": ""},
    }, {
        "_id": 1,
        "inbound_provider_message_id": 1,
    })
    return bool(row and row.get("inbound_provider_message_id"))


def _message_for(lead: dict, executive_name: str) -> str:
    first = str((lead.get("prospecto") or {}).get("nombre") or "").strip().split(" ")[0]
    executive = str(executive_name or "").strip().split(" ")[0]
    greeting = f"Hola {first}, " if first else "Hola, "
    return (
        greeting + f"soy {executive} de PROCASA. "
        "Me asignaron tu consulta inmobiliaria y desde este número puedo ayudarte "
        "con información o coordinar una visita. ¿Cómo te gustaría continuar?"
    )


def eligible_hot_auto(db, *, lead: dict, cycle: dict, now: datetime) -> dict:
    if not enabled():
        raise OutboundBlocked("hot_auto_disabled")
    cutover = _cutover()
    assigned = coerce_utc_datetime(cycle.get("assigned_at"))
    if not cutover or not assigned or assigned < cutover or assigned > now:
        raise OutboundBlocked("before_cutover")
    if now - assigned > timedelta(minutes=MAX_ASSIGNMENT_AGE_MINUTES):
        raise OutboundBlocked("assignment_not_recent")
    if (
        cycle.get("cycle_status") != "active"
        or cycle.get("unassigned_at") is not None
        or not cycle.get("assignment_cycle_id")
        or str(cycle.get("temperature_at_assignment") or "").upper() != "HOT"
        or resolve_cycle_sla_temperature(cycle=cycle, lead=lead) != "HOT"
        or cycle.get("sla_start_status") == "pending_notification"
        or str(cycle.get("reassignment_state") or "") == "AWAITING_OWNER_NOTIFICATION"
    ):
        raise OutboundBlocked("not_active_hot_cycle")
    if sla_exemption(cycle, lead) is not None:
        raise OutboundBlocked("already_exempt")
    if not isinstance(lead, dict) or lead.get("_id") is None:
        raise OutboundBlocked("lead_missing")
    if lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
        raise OutboundBlocked("invalid_contact")
    lc = lead.get("lifecycle") or {}
    owner = str(cycle.get("assigned_to_user_id") or "")
    cycle_id = str(cycle["assignment_cycle_id"])
    if (str(lc.get("current_assignment_cycle_id") or "") != cycle_id
            or str(lc.get("assigned_to_user_id") or "") != owner):
        raise OutboundBlocked("assignment_changed")
    if lead_must_not_be_contacted(lead):
        raise OutboundBlocked("customer_blocked")
    if str(lead.get("lead_temperature_effective") or "").upper() != "HOT":
        raise OutboundBlocked("lead_not_hot")
    # Avoid a second greeting if the assigned executive has already
    # contacted this lead via WhatsApp during the very same cycle.
    if any(
        isinstance(m, dict)
        and str(m.get("assignment_cycle_id") or "") == cycle_id
        and m.get("channel") == "executive_whatsapp"
        and m.get("actor_type") in ("human_agent", "automation")
        for m in (lead.get("messages") or [])
    ):
        raise OutboundBlocked("executive_already_contacted")
    try:
        phone = normalize_whatsapp_recipient(str(lead.get("phone") or ""))
    except ValueError:
        raise OutboundBlocked("invalid_phone") from None
    if "@" in phone:
        raise OutboundBlocked("individual_only")
    canonical = normalize_phone_strict(phone)
    if not (
        _verified_whatsapp_permission(lead, phone=canonical, now=now)
        or _verified_recent_office_job(db, lead=lead, phone=canonical, now=now)
    ):
        raise OutboundBlocked("whatsapp_permission_missing")
    authorized = _authorized_lead(db, phone=canonical, user_id=owner)
    if not authorized or str(authorized[0]["_id"]) != str(lead["_id"]):
        raise OutboundBlocked("ambiguous_or_wrong_owner")
    if not ObjectId.is_valid(owner):
        raise OutboundBlocked("owner_invalid")
    user = db["usuarios"].find_one(
        {"_id": ObjectId(owner), "is_active": True, "rol": "agente"},
        {"_id": 1, "nombre": 1, "wasender.session_id": 1},
    )
    if not user:
        raise OutboundBlocked("executive_not_active")
    sid = str((user.get("wasender") or {}).get("session_id") or "")
    if sid not in _session_allowlist():
        raise OutboundBlocked("session_not_enabled")
    session = _session(db, sid)
    if not session or session.get("user_id") != owner or not session.get("capture_enabled"):
        raise OutboundBlocked("session_not_ready")
    return {
        "owner": owner, "cycle_id": cycle_id, "session": session,
        "recipient": phone, "message": _message_for(lead, user.get("nombre")),
    }


def send_hot_auto(db, *, lead_id, post=None, now=None) -> dict:
    """Exactly one outbound attempt per lead/cycle/owner. No retries on timeout."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise OutboundBlocked("aware_now_required")
    lead = db["leads"].find_one({"_id": lead_id})
    if not lead:
        raise OutboundBlocked("lead_not_found")
    cycle = active_assignment_cycle(db, lead_id)
    if not cycle:
        raise OutboundBlocked("cycle_not_found")
    info = eligible_hot_auto(db, lead=lead, cycle=cycle, now=now)
    owner, cycle_id = info["owner"], info["cycle_id"]
    sid = str(info["session"]["session_id"])
    key = sha256(("\x1f".join((POLICY, str(lead_id), cycle_id, owner))).encode()).hexdigest()
    row = {
        "_id": key, "mode": POLICY, "lead_id": lead_id,
        "assignment_cycle_id": cycle_id, "executive_id": owner,
        "session_id": sid, "state": "provider_attempt",
        "attempted_at": now,
        "content_sha256": sha256(info["message"].encode()).hexdigest(),
    }
    try:
        db[OUTBOX].insert_one(row)
    except DuplicateKeyError:
        return {"status": "duplicate"}
    # Check ownership, consent and temperature again immediately before POST.
    try:
        fresh = db["leads"].find_one({"_id": lead_id})
        fresh_cycle = active_assignment_cycle(db, lead_id)
        checked = eligible_hot_auto(db, lead=fresh, cycle=fresh_cycle, now=now)
        if checked["cycle_id"] != cycle_id or checked["owner"] != owner or str(checked["session"]["session_id"]) != sid:
            raise OutboundBlocked("assignment_or_session_changed")
    except OutboundBlocked as exc:
        db[OUTBOX].update_one({"_id": key}, {"$set": {"state": "blocked", "reason": exc.code}})
        return {"status": "blocked", "reason": exc.code}

    url = Config.WASENDER_BASE_URL.rstrip("/") + "/send-message"
    headers = {"Authorization": "Bearer " + info["session"]["token"], "Content-Type": "application/json"}
    state, pid = "delivery_unknown", None
    try:
        response = (post or requests.post)(
            url, json={"to": info["recipient"], "text": info["message"]},
            headers=headers, timeout=15,
        )
        body = response.json() if response.content else {}
        ok = (response.status_code in (200, 201)
              and isinstance(body, dict) and body.get("success") is True)
        pid = _provider_message_id(body) if ok else None
        state = "accepted" if ok and pid else "accepted_without_provider_id" if ok else "rejected"
    except Exception:
        pass
    accepted_at = datetime.now(timezone.utc)
    db[OUTBOX].update_one({"_id": key}, {"$set": {
        "state": state, "provider_message_id": pid, "provider_result_at": accepted_at,
    }})
    if state != "accepted":
        return {"status": state}
    # Keep a structured automatic actor: not a human CRM management event.
    msg = {
        "message_id": str(uuid4()), "lead_id": str(lead_id),
        "conversation_id": lead.get("conversation_id"), "role": "assistant",
        "actor_type": "automation", "actor_id": owner,
        "assigned_executive_id": owner, "assignment_cycle_id": cycle_id,
        "channel": "executive_whatsapp", "source": "crm_hot_auto_api",
        "wasender_session_id": sid, "provider_message_id": pid,
        "content": info["message"], "timestamp": accepted_at.isoformat(),
        "timestamp_source": "crm_api_accepted",
        "delivery_status": "accepted", "automatic": True,
    }
    base_filter = {
        "_id": lead_id, "phone": lead.get("phone"),
        "lifecycle.current_assignment_cycle_id": cycle_id,
        "lifecycle.assigned_to_user_id": owner,
    }
    result = db["leads"].update_one({
        **base_filter,
        "$nor": [{"messages": {"$elemMatch": {
            "wasender_session_id": sid, "provider_message_id": pid,
        }}}],
    }, {
        "$push": {"messages": msg},
        "$set": {
            "crm_hot_auto.latest_cycle_id": cycle_id,
            "crm_hot_auto.last_provider_message_id": pid,
            "crm_hot_auto.last_accepted_at": accepted_at,
        },
    })
    canonical = result.modified_count == 1
    if not canonical:
        # A webhook echo may have won the race. Attribute that exact message
        # to the automation before considering an SLA exemption.
        corrected = db["leads"].update_one({
            **base_filter,
            "messages": {"$elemMatch": {
                "wasender_session_id": sid, "provider_message_id": pid,
                "assignment_cycle_id": cycle_id,
            }},
        }, {"$set": {
            "messages.$.actor_type": "automation",
            "messages.$.role": "assistant",
            "messages.$.source": "crm_hot_auto_api",
            "messages.$.automatic": True,
        }})
        canonical = corrected.modified_count == 1
    if not canonical:
        db[OUTBOX].update_one({"_id": key}, {"$set": {"canonical_recorded": False}})
        return {"status": "accepted_unrecorded"}
    # No CRM management marker is created. The cycle exemption is separate and
    # only written after durable accepted-send evidence has been recorded.
    exemption = {
        "status": "active", "kind": KIND, "policy_version": POLICY,
        "assignment_cycle_id": cycle_id, "executive_id": owner,
        "session_id": sid, "provider_message_id": pid,
        "accepted_at": accepted_at, "outbox_id": key,
    }
    wrote = db["crm_assignment_cycles"].update_one({
        "assignment_cycle_id": cycle_id, "lead_id": cycle.get("lead_id"),
        "assigned_to_user_id": owner, "cycle_status": "active",
        "unassigned_at": None, "temperature_at_assignment": "HOT",
        "hot_auto_sla_exemption.status": {"$ne": "active"},
    }, {"$set": {"hot_auto_sla_exemption": exemption}})
    recorded = wrote.modified_count == 1
    db[OUTBOX].update_one({"_id": key}, {"$set": {
        "canonical_recorded": True, "sla_exemption_recorded": recorded,
    }})
    return {"status": "accepted_exempt" if recorded else "accepted_no_exemption",
            "provider_message_id": pid}


def process_hot_auto_batch(db, *, now=None, limit=30, post=None) -> dict:
    """Bounded prospective scan. No scans/writes if deployment flag is OFF."""
    if not enabled():
        return {"status": "disabled", "examined": 0, "sent": 0}
    now = now or datetime.now(timezone.utc)
    cutoff = max(_cutover(), now - timedelta(minutes=MAX_ASSIGNMENT_AGE_MINUTES))
    cursor = db["crm_assignment_cycles"].find({
        "cycle_status": "active", "unassigned_at": None,
        "temperature_at_assignment": "HOT",
        "assigned_at": {"$gte": cutoff, "$lte": now},
        "hot_auto_sla_exemption.status": {"$ne": "active"},
    }, {"lead_id": 1, "assignment_cycle_id": 1}).sort("assigned_at", 1).limit(min(max(limit, 1), 100))
    examined = sent = 0
    skipped: dict[str, int] = {}
    for cycle in cursor:
        examined += 1
        try:
            lid = cycle.get("lead_id")
            if not isinstance(lid, ObjectId):
                lid = ObjectId(str(lid))
            outcome = send_hot_auto(db, lead_id=lid, now=now, post=post)
            status = outcome["status"]
            if status == "accepted_exempt":
                sent += 1
            else:
                skipped[status] = skipped.get(status, 0) + 1
        except (OutboundBlocked, ValueError, TypeError) as exc:
            reason = getattr(exc, "code", type(exc).__name__)
            skipped[reason] = skipped.get(reason, 0) + 1
        except Exception as exc:
            logger.exception("[HOT_EXEC_AUTO] processing_error error_type=%s", type(exc).__name__)
            skipped["error"] = skipped.get("error", 0) + 1
    return {"status": "evaluated", "examined": examined, "sent": sent, "skipped": skipped}


async def hot_auto_worker_loop(*, stop_event=None):
    """Separate opt-in loop; calls synchronous MongoDB/network from a thread."""
    from .storage import get_db
    while stop_event is None or not stop_event.is_set():
        if enabled():
            try:
                result = await asyncio.to_thread(lambda: process_hot_auto_batch(get_db()))
                logger.info("[HOT_EXEC_AUTO] status=%s examined=%s accepted_exempt=%s skipped=%s",
                            result["status"], result["examined"], result["sent"], result.get("skipped"))
            except Exception:
                logger.exception("[HOT_EXEC_AUTO] batch_failed")
        if stop_event is None:
            await asyncio.sleep(30)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
