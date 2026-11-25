"""Prospective, bounded phase-one follow-up worker.

No historical scan, no new worker loop, no personal chats, no SLA changes.
A candidate is not permission: verified inquiry evidence and capture continuity
are checked automatically. Production sending remains separately gated.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from uuid import uuid4
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
import requests

from config import Config
from .executive_silence_policy import evaluate_silence, _time, next_followup_slot, WAIT_MINUTES, has_pending_commitment
from .business_calendar import add_business_minutes, as_chile, CHILE
from .executive_silence_llm import draft_followup, initial_recovery_draft
from .executive_capture_continuity import continuity_since, record_status, recover_provider_continuity
from .crm_executive_outbound import (OUTBOX, eligible_manual_session,
    verify_provider_sender_identity, OutboundBlocked)
from .crm_executive_outbound_api import _session_status
from .phone_utils import normalize_phone_strict
from .whatsapp_client import _provider_message_id

QUEUE = "crm_exec_silence_queue"
MODE = "crm_exec_silence_api"
SESSION_GUARD = "crm_exec_silence_session_guard"
SESSION_STOP = {"rate_limited", "rejected", "delivery_unknown",
                "accepted_without_provider_id", "accepted_unrecorded", "session_unhealthy"}


def enabled():
    return os.getenv("PROCASA_EXEC_SILENCE_WORKER_ENABLED", "0") == "1"


def ensure_indexes(db):
    db[QUEUE].create_index([("state", 1), ("due_at", 1)])
    db[OUTBOX].create_index([("mode", 1), ("phone", 1), ("attempted_at", 1)])


def _rollout_cutoff(now):
    cutoff = _time(os.getenv("PROCASA_EXEC_SILENCE_CUTOFF"))
    if not cutoff:
        return None
    # The start date is stable for ALL executives and all future contacts.
    # Do not reset at midnight or restrict recovery to a named pilot lead.
    local = as_chile(cutoff)
    return CHILE.localize(datetime(local.year, local.month, local.day)).astimezone(timezone.utc)


def enqueue(db, lead_id, *, now=None):
    """One new anchor per classified lead; do not overwrite existing jobs."""
    if not enabled():
        return
    now = now or datetime.now(timezone.utc)
    doc = db["leads"].find_one({"_id": lead_id},
        {"commercial_intelligence": 1, "lifecycle": 1})
    if not doc:
        return
    ci = doc.get("commercial_intelligence") or {}
    lc = doc.get("lifecycle") or {}
    contact = (ci.get("summary") or {}).get("contact") or {}
    anchor = ci.get("last_processed_message_id")
    cycle = lc.get("current_assignment_cycle_id")
    owner = lc.get("assigned_to_user_id")
    session = ci.get("pending_session_id")
    cutoff = _rollout_cutoff(now)
    sent = _time(contact.get("last_agent_at"))
    # A classified reply or a change in assignment immediately cancels any
    # obsolete pending job. In-flight sends still revalidate canonical history.
    if ci.get("pending") is False and ci.get("status") == "ready":
        invalid = {"lead_id": lead_id, "state": "pending"}
        if contact.get("awaiting") == "CUSTOMER" and contact.get("last_actor") == "human_agent" and all((anchor, cycle, owner, session)):
            invalid["$or"] = [{"anchor": {"$ne": anchor}}, {"cycle": {"$ne": str(cycle)}},
                              {"owner": {"$ne": str(owner)}}, {"session": {"$ne": str(session)}}]
        db[QUEUE].update_many(invalid, {"$set": {"state": "held", "reason": "superseded_or_customer_replied"}})
    if (not cutoff or not sent or not cutoff <= sent <= now
            or ci.get("pending") is not False or ci.get("status") != "ready"
            or contact.get("awaiting") != "CUSTOMER"
            or contact.get("last_actor") != "human_agent"
            or not all((anchor, cycle, owner, session))):
        return
    key = sha256(f"{lead_id}|{cycle}|{anchor}".encode()).hexdigest()
    db[QUEUE].update_one({"_id": key}, {"$setOnInsert": {
        "lead_id": lead_id, "cycle": str(cycle), "owner": str(owner),
        "session": str(session), "anchor": anchor, "state": "pending",
        "created_at": now, "due_at": next_followup_slot(add_business_minutes(sent, WAIT_MINUTES)),
    }}, upsert=True)


def _verified_portal_first_contact(db, doc, job, now):
    """Validate the CRM's existing inquiry and accepted first human contact.

    This is operational eligibility for service follow-up to a known inquiry,
    not evidence that the recipient gave explicit WhatsApp opt-in. Never
    create or alter a customer consent record from an outbound send.
    """
    if doc.get("source_type") != "manual" or not doc.get("_id"):
        return False
    prospect = doc.get("prospecto") or {}
    origin = str(prospect.get("canal_origen") or doc.get("origen") or "").strip().lower()
    if not any(portal in origin for portal in ("enlace inmobiliario", "enlaceinmobiliario",
                                               "portalinmobiliario", "portal inmobiliario",
                                               "yapo", "toctoc", "toc toc", "chilepropiedades")):
        return False
    if not str(prospect.get("codigo") or "").isdigit():
        return False
    for m in doc.get("messages") or []:
        if not isinstance(m, dict):
            continue
        sent = _time(m.get("timestamp"))
        if (m.get("message_id") != job["anchor"]
                or m.get("role") != "agent" or m.get("actor_type") != "human_agent"
                or m.get("source") != "crm_manual_api" or m.get("channel") != "executive_whatsapp"
                or str(m.get("assigned_executive_id")) != job["owner"]
                or str(m.get("assignment_cycle_id")) != job["cycle"]
                or str(m.get("wasender_session_id")) != job["session"]
                or not m.get("provider_message_id") or not sent
                or not now - timedelta(hours=24) <= sent <= now):
            continue
        return bool(db[OUTBOX].find_one({
            "lead_id": doc["_id"], "mode": "manual_crm", "state": "accepted",
            "canonical_recorded": True, "provider_send_attempted": True,
            "executive_id": job["owner"], "assignment_cycle_id": job["cycle"],
            "session_id": job["session"], "provider_message_id": str(m["provider_message_id"]),
        }, {"_id": 1}))
    return False


def _contact_evidence(db, doc, job, phone, now):
    """Automatically reuse real inquiry evidence; no per-lead pilot approval.

    A documented portal inquiry with a provider-verified human first contact
    is also eligible under the CRM service-follow-up rule. This does not
    establish explicit opt-in; opt-out and continuity checks still apply.
    """
    from .crm_hot_linked_auto import _verified_whatsapp_permission, _verified_recent_office_job
    if (_verified_whatsapp_permission(doc, phone=phone, now=now)
            or _verified_recent_office_job(db, lead=doc, phone=phone, now=now)):
        return True
    if _verified_portal_first_contact(db, doc, job, now):
        return True
    for msg in doc.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        timestamp = _time(msg.get("timestamp"))
        if (msg.get("actor_type") == "customer"
                and msg.get("role") in {"user", "customer"}
                and msg.get("channel") == "executive_whatsapp"
                and str(msg.get("assigned_executive_id")) == job["owner"]
                and str(msg.get("assignment_cycle_id")) == job["cycle"]
                and str(msg.get("wasender_session_id")) == job["session"]
                and msg.get("provider_message_id")
                and timestamp and now - timedelta(hours=24) <= timestamp <= now):
            return True
    return False

# One-off user-authorized recovery for the exact six previously held anchors.
# Expires at 21:00 Chile on 2026-11-25; no other lead or date is affected.
_RECOVERY_JOB_IDS = frozenset((
    "debd6253759629f18c7b05a209fb382c5ef26b8c3d12fe4fadb491cf5767e2c4",
    "2a073ccec4b7590cb0d4eb2e7d5479a8c86da2ab25346118eff0e50fb327ff6e",
    "45420dc5c6c38986b563f7a451617a48488abc16a50993b87df223d177ec8f7e",
    "2858c55b7ea9dc133ffd52b9d3b64300914a786793b591f1139989387ec306b5",
    "484fbf4e53aebecc974a2423181e26773adb93560c2ca1d32ab486105a1e5931",
    "74277b4027fd4237f76d02a00183f46e2a12bef6e186e3e27560166de64e0a07",
))
_RECOVERY_UNTIL = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)


def _recovery_until(job):
    if (job.get("_id") in _RECOVERY_JOB_IDS
            and job.get("session") == "133517"
            and job.get("owner") == "6a681413140190dde11f26d1"):
        return _RECOVERY_UNTIL
    return None


def _decision(db, job, *, now):
    doc = db["leads"].find_one({"_id": job["lead_id"]})
    if not doc:
        return None, {"state": "HOLD", "reason": "lead_missing"}
    ci = doc.get("commercial_intelligence") or {}
    lc = doc.get("lifecycle") or {}
    if (ci.get("last_processed_message_id") != job["anchor"]
            or str(lc.get("current_assignment_cycle_id")) != job["cycle"]
            or str(lc.get("assigned_to_user_id")) != job["owner"]
            or str(ci.get("pending_session_id")) != job["session"]):
        return doc, {"state": "HOLD", "reason": "superseded"}
    phone = normalize_phone_strict(doc.get("phone"))
    if not phone:
        return doc, {"state": "HOLD", "reason": "invalid_phone"}
    # Existing pending tasks always win. Query by both canonical ID forms.
    if db["crm_tasks"].find_one({"$or": [
            {"lead_id": {"$in": [doc["_id"], str(doc["_id"])]}},
            {"phone": {"$in": [phone, phone.lstrip("+"), phone.lstrip("+")[2:]]}}],
            "status": {"$nin": ["completed", "cancelled", "canceled", "closed", "done", "sent", "error"]}}, {"_id": 1}):
        return doc, {"state": "HOLD", "reason": "crm_task_pending"}
    authorized = _contact_evidence(db, doc, job, phone, now)
    prior = db[OUTBOX].find_one({"mode": MODE, "phone": phone}, {"_id": 1})
    connected = continuity_since(db, session_id=job["session"], owner_id=job["owner"])
    if has_pending_commitment(doc):
        return doc, {"state": "HOLD", "reason": "crm_followup_pending"}
    if not authorized:
        return doc, {"state": "HOLD", "reason": "inquiry_contact_evidence_missing"}
    if connected is None:
        # A signed webhook echo proves the commercial anchor was captured.
        # Consult actual provider history only for an authorized due job,
        # never for unrelated chats or on every idle worker poll.
        anchor = next((m for m in doc.get("messages", []) if isinstance(m, dict)
                       and m.get("message_id") == job["anchor"]
                       and m.get("provider_echo_message_id")
                       and m.get("provider_echo_at")
                       and m.get("source") == "crm_manual_api"
                       and str(m.get("assigned_executive_id")) == job["owner"]
                       and str(m.get("wasender_session_id")) == job["session"]
                       and str(m.get("assignment_cycle_id")) == job["cycle"]), None)
        anchor_at = _time(anchor.get("timestamp")) if anchor else None
        if anchor_at is not None and _time(anchor.get("provider_echo_at")) is not None:
            connected = recover_provider_continuity(
                db, session_id=job["session"], owner_id=job["owner"], anchor=anchor_at, now=now)
        if connected is None:
            return doc, {"state": "HOLD", "reason": "capture_continuity_unverified"}
    return doc, evaluate_silence(doc, now=now,
        enabled_since=_rollout_cutoff(now),
        contact_authorized=authorized, inquiry_verified=authorized,
        history_complete=connected is not None, session_verified=connected is not None,
        followup_attempts=int(bool(prior)), outstanding_own_action=False,
        continuity_since=connected, scheduled_contact_clear=True,
        recovery_until=_recovery_until(job))


def _finish(db, job, *, state, reason, **extra):
    db[QUEUE].update_one({"_id": job["_id"], "lease": job["lease"]},
        {"$set": {"state": state, "reason": reason, **extra}, "$unset": {"lease": ""}})


def _live_allowed(job):
    return (os.getenv("PROCASA_EXEC_SILENCE_SEND_ENABLED", "0") == "1"
            and os.getenv("PROCASA_EXEC_SILENCE_CONTINUITY_ENABLED", "0") == "1"
            and job["session"] in {s.strip() for s in
                os.getenv("PROCASA_EXEC_SILENCE_SESSION_IDS", "").split(",") if s.strip()})


def dispatch(db, job, doc, decision, *, now, post=None):
    """Serialize automatic sends per provider session across workers.

    A failed or uncertain provider outcome pauses that session; do not
    automatically retry or continue contacting other leads on that session.
    An abandoned lock fails closed rather than racing a possible send.
    """
    if not _live_allowed(job):
        return "shadow_candidate"
    key, token = str(job["session"]), str(uuid4())
    guard = db[SESSION_GUARD]
    try:
        guard.insert_one({"_id": key, "token": token, "state": "sending", "at": now})
    except DuplicateKeyError:
        existing = guard.find_one({"_id": key}, {"state": 1})
        return "session_paused" if (existing or {}).get("state") == "paused" else "session_busy"
    try:
        result = _dispatch_locked(db, job, doc, decision, now=now, post=post)
    except OutboundBlocked:
        guard.update_one({"_id": key, "token": token}, {"$set": {"state": "paused", "reason": "provider_or_identity_guard"}})
        raise
    except Exception:
        guard.update_one({"_id": key, "token": token}, {"$set": {"state": "paused", "reason": "uncertain_dispatch_error"}})
        raise
    if result in SESSION_STOP:
        guard.update_one({"_id": key, "token": token}, {"$set": {"state": "paused", "reason": result}})
    else:
        guard.delete_one({"_id": key, "token": token})
    return result


def _dispatch_locked(db, job, doc, decision, *, now, post=None):
    """Single reserved provider attempt. Never retry uncertain delivery."""
    if not _live_allowed(job):
        return "shadow_candidate"
    info = eligible_manual_session(db, lead=doc, actor_user_id=job["owner"])
    session = info["session"]
    if str(session["session_id"]) != job["session"]:
        return "session_changed"
    owner = db["usuarios"].find_one({"_id": session["user_oid"]}, {"telefono": 1})
    verify_provider_sender_identity(session, expected_phone=(owner or {}).get("telefono"), force=True)
    status = _session_status(session)
    if status != "connected":
        record_status(db, session_id=job["session"], owner_id=job["owner"], status=status)
        return "session_unhealthy"
    # Recheck after all slow provider status calls, including the local window.
    now = datetime.now(timezone.utc)
    doc, decision = _decision(db, job, now=now)
    if decision["state"] != "CANDIDATE" or not _live_allowed(job):
        return "superseded"
    anchor = next((m for m in doc.get("messages", []) if m.get("message_id") == job["anchor"]), None)
    if not anchor:
        return "anchor_missing"
    # Generate exactly once per claimed eligible anchor. Never invoke the
    # model on polling, an ineligible lead or a scheduled WAIT.
    try:
        text, usage = draft_followup(db, doc, job, anchor,
                                     scenario=decision["scenario"], attempt=1)
    except ValueError as exc:
        # This branch is strictly before any outbox reservation or provider
        # call. A formatting failure cannot create uncertain delivery.
        reason = str(exc)
        known = {"draft_schema", "draft_decision", "draft_format",
                 "draft_unsafe_claim", "draft_incomplete", "draft_invalid",
                 "context_unavailable"}
        reason = reason if reason in known else type(exc).__name__
        db[QUEUE].update_one({"_id": job["_id"], "lease": job["lease"]},
                             {"$set": {"draft_error": reason}})
        recovery_end = _recovery_until(job)
        if (recovery_end is None or now >= recovery_end
                or decision["scenario"] != "INITIAL_NO_REPLY"
                or reason == "context_unavailable"):
            return "model_invalid"
        try:
            text = initial_recovery_draft(db, doc, anchor)
        except ValueError:
            return "model_invalid"
        usage = {"version": "verified_initial_recovery_v1", "fallback_reason": reason}
        if not text:
            return "model_invalid"
    db[QUEUE].update_one({"_id": job["_id"], "lease": job["lease"]},
                         {"$set": {"draft_usage": usage,
                                   "draft_decision": "DRAFT" if text else "HOLD"}})
    if not text:
        return "model_hold"
    # Model latency must not allow a newly received message, CRM task,
    # permission revocation or expired send window to slip past the gate.
    now = datetime.now(timezone.utc)
    fresh, rechecked = _decision(db, job, now=now)
    if fresh != doc or rechecked["state"] != "CANDIDATE" or not _live_allowed(job):
        return "superseded_after_draft"
    phone = normalize_phone_strict(doc.get("phone"))
    # One permanent reservation per normalized customer phone (pilot).
    key = sha256(f"{MODE}|{phone}".encode()).hexdigest()
    row = {"_id": key, "mode": MODE, "phone": phone, "lead_id": doc["_id"],
           "executive_id": job["owner"], "assignment_cycle_id": job["cycle"],
           "session_id": job["session"], "anchor_message_id": job["anchor"],
           "state": "provider_attempt", "attempted_at": now,
           "content_sha256": sha256(text.encode()).hexdigest(),
           "scenario": decision["scenario"], "draft_usage": usage}
    try:
        db[OUTBOX].insert_one(row)
    except DuplicateKeyError:
        return "duplicate"
    # Own reservation would now fail the attempt count; compare exact anchor,
    # consent, connection epoch and timeline before crossing provider boundary.
    fresh = db["leads"].find_one({"_id": doc["_id"]})
    epoch = continuity_since(db, session_id=job["session"], owner_id=job["owner"])
    if (not fresh or fresh != doc or not epoch or epoch > _time(anchor["timestamp"])
            or not _live_allowed(job)):
        db[OUTBOX].update_one({"_id": key}, {"$set": {"state": "blocked_before_provider"}})
        return "superseded"
    state, pid = "delivery_unknown", None
    try:
        response = (post or requests.post)(
            Config.WASENDER_BASE_URL.rstrip("/") + "/send-message",
            headers={"Authorization": "Bearer " + session["token"],
                     "Content-Type": "application/json"},
            json={"to": info["recipient"], "text": text}, timeout=15)
        body = response.json() if response.content else {}
        if response.status_code in (200, 201) and isinstance(body, dict) and body.get("success") is True:
            pid = _provider_message_id(body)
            state = "accepted" if pid else "accepted_without_provider_id"
        elif response.status_code == 429:
            state = "rate_limited"
        elif 400 <= response.status_code < 500 and response.status_code not in (408, 425):
            state = "rejected"
    except Exception:
        pass
    at = datetime.now(timezone.utc)
    db[OUTBOX].update_one({"_id": key}, {"$set": {
        "state": state, "provider_message_id": pid, "provider_result_at": at}})
    if state != "accepted":
        return state
    entry = {"message_id": str(uuid4()), "lead_id": str(doc["_id"]),
             "conversation_id": doc.get("conversation_id"), "role": "assistant",
             "actor_type": "automation", "actor_id": job["owner"],
             "assigned_executive_id": job["owner"], "assignment_cycle_id": job["cycle"],
             "channel": "executive_whatsapp", "source": MODE,
             "wasender_session_id": job["session"], "provider_message_id": pid,
             "content": text, "timestamp": at.isoformat(), "automatic": True,
             "timestamp_source": "crm_api_accepted"}
    base = {"_id": doc["_id"], "lifecycle.current_assignment_cycle_id": job["cycle"],
            "lifecycle.assigned_to_user_id": job["owner"]}
    # Webhook may have written the automation first; reconcile by reservation ID.
    result = db["leads"].update_one({**base,
        "messages.silence_dispatch_id": {"$ne": key},
        "messages.provider_message_id": {"$ne": pid}},
        {"$push": {"messages": {**entry, "silence_dispatch_id": key}}})
    if not result.modified_count:
        result = db["leads"].update_one({**base,
            "messages": {"$elemMatch": {"silence_dispatch_id": key}}},
            {"$set": {"messages.$.crm_api_provider_message_id": pid}})
    db[OUTBOX].update_one({"_id": key}, {"$set": {
        "canonical_recorded": bool(result.matched_count)}})
    return "accepted" if result.matched_count else "accepted_unrecorded"


def process_due(db, *, limit=3, now=None):
    if not enabled():
        return
    now = now or datetime.now(timezone.utc)
    # Do not retry a job leased during a crash: its dispatch may have succeeded.
    for _ in range(min(max(limit, 1), 5)):
        job = db[QUEUE].find_one_and_update({"state": "pending", "due_at": {"$lte": now}},
            {"$set": {"state": "evaluating", "lease": str(uuid4()), "claimed_at": now}},
            sort=[("due_at", 1)], return_document=ReturnDocument.AFTER)
        if not job:
            break
        try:
            doc, decision = _decision(db, job, now=now)
            if decision["state"] == "WAIT":
                _finish(db, job, state="pending", reason=decision["reason"], due_at=decision["due_at"])
            elif decision["state"] == "CANDIDATE":
                state = dispatch(db, job, doc, decision, now=now)
                if state == "session_busy":
                    _finish(db, job, state="pending", reason=state, due_at=now + timedelta(minutes=1))
                else:
                    _finish(db, job, state=state, reason=state if state in SESSION_STOP or state == "session_paused" else decision["reason"])
            else:
                _finish(db, job, state="held", reason=decision["reason"])
        except OutboundBlocked as exc:
            _finish(db, job, state="held", reason=exc.code)
        except Exception as exc:
            _finish(db, job, state="error", reason=type(exc).__name__)
