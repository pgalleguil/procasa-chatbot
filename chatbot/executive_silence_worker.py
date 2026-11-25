"""Prospective, bounded phase-one follow-up worker.

No historical scan, no new worker loop, no personal chats, no SLA changes.
A candidate is not permission: pilot approval is bound to the lead, cycle,
anchor and session. Production sending is separately gated and off by default.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from uuid import uuid4
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
import requests

from config import Config
from .executive_silence_policy import evaluate_silence, _time, next_followup_slot, WAIT_MINUTES
from .business_calendar import add_business_minutes
from .executive_silence_copy import followup_copy
from .executive_capture_continuity import continuity_since, record_status
from .crm_executive_outbound import (OUTBOX, eligible_manual_session,
    verify_provider_sender_identity, OutboundBlocked)
from .crm_executive_outbound_api import _session_status
from .phone_utils import normalize_phone_strict
from .whatsapp_client import _provider_message_id

QUEUE = "crm_exec_silence_queue"
MODE = "crm_exec_silence_api"


def enabled():
    return os.getenv("PROCASA_EXEC_SILENCE_WORKER_ENABLED", "0") == "1"


def ensure_indexes(db):
    db[QUEUE].create_index([("state", 1), ("due_at", 1)])
    db[OUTBOX].create_index([("mode", 1), ("phone", 1), ("attempted_at", 1)])


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
    cutoff = _time(os.getenv("PROCASA_EXEC_SILENCE_CUTOFF"))
    sent = _time(contact.get("last_agent_at"))
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


def _review(doc, job, phone, now):
    """Explicit pilot authorization, never infer it from a portal source."""
    a = (doc.get("commercial_intelligence") or {}).get("silence_pilot_review") or {}
    reviewed, expiry = _time(a.get("reviewed_at")), _time(a.get("expires_at"))
    return bool(a.get("status") == "APPROVED" and a.get("reviewed_by")
                and a.get("evidence_reference") and a.get("revoked_at") is None
                and reviewed and expiry and reviewed <= now < expiry
                and a.get("phone") == phone and a.get("cycle") == job["cycle"]
                and a.get("session") == job["session"] and a.get("anchor") == job["anchor"]
                and a.get("contact_authorized") is True
                and a.get("history_verified") is True
                and a.get("no_own_pending_action") is True
                and a.get("scheduled_contact_clear") is True)


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
    if db["crm_tasks"].find_one({"lead_id": {"$in": [doc["_id"], str(doc["_id"])]},
            "status": {"$nin": ["completed", "cancelled", "canceled", "closed"]}}, {"_id": 1}):
        return doc, {"state": "HOLD", "reason": "crm_task_pending"}
    reviewed = _review(doc, job, phone, now)
    prior = db[OUTBOX].find_one({"mode": MODE, "phone": phone}, {"_id": 1})
    connected = continuity_since(db, session_id=job["session"], owner_id=job["owner"])
    return doc, evaluate_silence(doc, now=now,
        enabled_since=_time(os.getenv("PROCASA_EXEC_SILENCE_CUTOFF")),
        contact_authorized=reviewed, inquiry_verified=reviewed,
        history_complete=reviewed, session_verified=connected is not None,
        followup_attempts=int(bool(prior)), outstanding_own_action=False if reviewed else None,
        continuity_since=connected, scheduled_contact_clear=reviewed)


def _finish(db, job, *, state, reason, **extra):
    db[QUEUE].update_one({"_id": job["_id"], "lease": job["lease"]},
        {"$set": {"state": state, "reason": reason, **extra}, "$unset": {"lease": ""}})


def _live_allowed(job):
    return (os.getenv("PROCASA_EXEC_SILENCE_SEND_ENABLED", "0") == "1"
            and os.getenv("PROCASA_EXEC_SILENCE_CONTINUITY_ENABLED", "0") == "1"
            and job["session"] in {s.strip() for s in
                os.getenv("PROCASA_EXEC_SILENCE_SESSION_IDS", "").split(",") if s.strip()})


def dispatch(db, job, doc, decision, *, now, post=None):
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
    # Only structured customer-evidenced operation, never an inferred price,
    # address, financing approval or inventory availability.
    summary = (doc.get("commercial_intelligence") or {}).get("summary") or {}
    demand = (summary.get("needs") or {}).get("DEMAND") or {}
    evidence = (summary.get("field_evidence") or {}).get("DEMAND") or {}
    operation = demand.get("operation") if evidence.get("operation") else ""
    text = followup_copy(scenario=decision["scenario"], attempt=1,
                         last_agent=anchor.get("content") or "",
                         verified_operation=operation)
    phone = normalize_phone_strict(doc.get("phone"))
    # One permanent reservation per normalized customer phone (pilot).
    key = sha256(f"{MODE}|{phone}".encode()).hexdigest()
    row = {"_id": key, "mode": MODE, "phone": phone, "lead_id": doc["_id"],
           "executive_id": job["owner"], "assignment_cycle_id": job["cycle"],
           "session_id": job["session"], "anchor_message_id": job["anchor"],
           "state": "provider_attempt", "attempted_at": now,
           "content_sha256": sha256(text.encode()).hexdigest(),
           "scenario": decision["scenario"]}
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
                _finish(db, job, state=state, reason=decision["reason"])
            else:
                _finish(db, job, state="held", reason=decision["reason"])
        except OutboundBlocked as exc:
            _finish(db, job, state="held", reason=exc.code)
        except Exception as exc:
            _finish(db, job, state="error", reason=type(exc).__name__)
