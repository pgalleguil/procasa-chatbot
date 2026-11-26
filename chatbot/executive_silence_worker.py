"""Prospective, bounded phase-one follow-up worker.

No historical scan, no new worker loop, no personal chats, no SLA changes.
A candidate is not permission: verified inquiry evidence and capture continuity
are checked automatically. Production sending remains separately gated.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import logging
import time
from uuid import uuid4
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
import requests

from config import Config
from .executive_silence_policy import evaluate_silence, _time, next_followup_slot, WAIT_MINUTES, has_pending_commitment, sequence_due, _plain
from .business_calendar import add_business_minutes, as_chile, CHILE
from .executive_silence_llm import draft_followup, initial_recovery_draft, verified_initial_stage_fallback
from .executive_capture_continuity import continuity_since, record_status, recover_provider_continuity
from .crm_executive_outbound import (OUTBOX, eligible_manual_session,
    verify_provider_sender_identity, OutboundBlocked)
from .crm_executive_outbound_api import _session_status
from .phone_utils import normalize_phone_strict
from .whatsapp_client import _provider_message_id

QUEUE = "crm_exec_silence_queue"
MODE = "crm_exec_silence_api"
SESSION_GUARD = "crm_exec_silence_session_guard"
logger = logging.getLogger(__name__)
_last_sequence_reconcile = None
SESSION_STOP = {"rate_limited", "rejected", "delivery_unknown",
                "accepted_without_provider_id", "accepted_unrecorded", "session_unhealthy"}


def enabled():
    return os.getenv("PROCASA_EXEC_SILENCE_WORKER_ENABLED", "0") == "1"


def ensure_indexes(db):
    db[QUEUE].create_index([("state", 1), ("due_at", 1)])
    db[QUEUE].create_index([("state", 1), ("sequence_started", 1), ("claimed_at", 1)])
    db[QUEUE].create_index([("state", 1), ("sequence_reconciled", 1), ("claimed_at", 1)])
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
        {"commercial_intelligence": 1, "lifecycle": 1, "messages": 1})
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
    # An automation echo is not a new human anchor. Preserve the existing
    # sequence; its next stage revalidates the entire canonical history.
    if (contact.get("last_actor") == "automation" and not _has_executive_dialogue(doc)
            and any(isinstance(m, dict) and m.get("message_id") == anchor
                    and m.get("source") == MODE and m.get("actor_type") == "automation"
                    for m in doc.get("messages") or [])):
        return
    # A classified reply or a change in assignment immediately cancels any
    # obsolete pending job. In-flight sends still revalidate canonical history.
    if ci.get("pending") is False and ci.get("status") == "ready":
        invalid = {"lead_id": lead_id, "state": "pending"}
        if contact.get("awaiting") == "CUSTOMER" and contact.get("last_actor") == "human_agent" and all((anchor, cycle, owner, session)) and not _has_executive_dialogue(doc):
            invalid["$or"] = [{"anchor": {"$ne": anchor}}, {"cycle": {"$ne": str(cycle)}},
                              {"owner": {"$ne": str(owner)}}, {"session": {"$ne": str(session)}}]
        db[QUEUE].update_many(invalid, {"$set": {"state": "held", "reason": "superseded_or_customer_replied"}})
    if (_has_executive_dialogue(doc) or not cutoff or not sent or not cutoff <= sent <= now
            or ci.get("pending") is not False or ci.get("status") != "ready"
            or contact.get("awaiting") != "CUSTOMER"
            or contact.get("last_actor") != "human_agent"
            or not all((anchor, cycle, owner, session))):
        return
    key = sha256(f"{lead_id}|{cycle}|{anchor}".encode()).hexdigest()
    db[QUEUE].update_one({"_id": key}, {"$setOnInsert": {
        "lead_id": lead_id, "cycle": str(cycle), "owner": str(owner),
        "session": str(session), "anchor": anchor, "state": "pending",
        "attempt": 1, "created_at": now, "due_at": next_followup_slot(add_business_minutes(sent, WAIT_MINUTES)),
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


# User-authorized exception for the two technical failures on Nov 26.
# Ends before the original portal-contact evidence expires at 12:17 Chile.
_DELAYED_INITIAL_JOBS = {
    "debd6253759629f18c7b05a209fb382c5ef26b8c3d12fe4fadb491cf5767e2c4":
        ("6b06d75aac48b1d66c5308c4", "19190ba5-adc1-44ea-9d66-61bfcc458eb2", "75979349-bc2c-4812-b53e-af2655bd8e57"),
    "45420dc5c6c38986b563f7a451617a48488abc16a50993b87df223d177ec8f7e":
        ("6b06d9b2ac48b1d66c5308e0", "6c67c18e-fbef-4858-9368-e89d3f2b2145", "ea698a2c-90fc-4034-92be-eda4bca75c0e"),
}

def _delayed_initial_until(job):
    expected = _DELAYED_INITIAL_JOBS.get(job.get("_id"))
    if (expected == (str(job.get("lead_id")), job.get("cycle"), job.get("anchor"))
            and job.get("owner") == "6a681413140190dde11f26d1"
            and job.get("session") == "133517" and job.get("attempt", 1) == 1
            and job.get("incident_recovery_20261126") is True):
        return datetime(2026, 11, 26, 15, 15, tzinfo=timezone.utc)
    return None


def _reviewed_initial_recovery(db, job, doc, anchor, decision, now):
    """Only the individually reviewed, explicitly requeued initial contacts.
    
    The independent eligibility decision and current canonical timeline must
    still pass; a model HOLD on a dialogue is never overridden.
    """
    end = _recovery_until(job)
    if (end is None or now >= end
            or job.get("reviewed_initial_copy") is not True
            or job.get("draft_decision") != "HOLD"
            or decision.get("state") != "CANDIDATE"
            or decision.get("scenario") != "INITIAL_NO_REPLY"):
        return None
    messages = doc.get("messages") or []
    if (len(messages) != 1 or messages[0] != anchor
            or anchor.get("actor_type") != "human_agent"
            or anchor.get("source") != "crm_manual_api"
            or anchor.get("message_id") != job.get("anchor")
            or not anchor.get("provider_echo_message_id")
            or not anchor.get("provider_echo_at")):
        return None
    return initial_recovery_draft(db, doc, anchor)


def _has_executive_dialogue(doc):
    """Any recorded executive-channel customer turn excludes initial silence.

    Include previous assignment cycles; a reassignment must not turn an
    existing dialogue into a never-answered first contact. Unknown attribution
    in this channel is not proof that the customer never replied.
    """
    contact = ((doc.get("commercial_intelligence") or {}).get("summary") or {}).get("contact") or {}
    if contact.get("customer_responded") is True or contact.get("bilateral_observed") is True or contact.get("last_customer_at"):
        return True
    return any(
        isinstance(m, dict)
        and m.get("channel") == "executive_whatsapp"
        and (m.get("actor_type") == "customer"
             or (m.get("actor_type") is None and m.get("role") in {"user", "customer"}))
        for m in doc.get("messages") or []
    )


def _persisted_utc(value):
    """BSON datetime is UTC even with PyMongo's default tz_aware=False.

    Only use for persisted database dates; do not relax message timestamp
    validation for external or unverified strings.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return _time(value)


def _attempt_key(phone, attempt):
    # Preserve existing stage-one reservation keys; never replay an old send.
    return sha256((f"{MODE}|{phone}" if attempt == 1 else
                   f"{MODE}|{phone}|stage|{attempt}").encode()).hexdigest()


def _sequence_evidence(db, doc, job, phone):
    """Only exact, accepted, canonically recorded, echoed prior stages qualify."""
    attempt = job.get("attempt", 1)
    if type(attempt) is not int or attempt not in (1, 2, 3):
        return None
    if attempt == 1:
        return None if db[OUTBOX].find_one({"mode": MODE, "phone": phone}, {"_id": 1}) else ()
    rows = list(db[OUTBOX].find({"mode": MODE, "phone": phone}).limit(4))
    if len(rows) != attempt - 1:
        return None
    by_id = {row["_id"]: row for row in rows}
    verified = []
    for n in range(1, attempt):
        key = _attempt_key(phone, n)
        row = by_id.get(key) or {}
        if (row.get("state") != "accepted" or row.get("canonical_recorded") is not True
                or row.get("lead_id") != doc["_id"]
                or row.get("executive_id") != job["owner"]
                or row.get("assignment_cycle_id") != job["cycle"]
                or row.get("session_id") != job["session"]
                or row.get("anchor_message_id") != job["anchor"]
                or row.get("scenario") != "INITIAL_NO_REPLY"
                or row.get("attempt", 1) != n or not row.get("provider_message_id")):
            return None
        accepted = _persisted_utc(row.get("provider_result_at") or row.get("attempted_at"))
        matches = [m for m in doc.get("messages") or [] if isinstance(m, dict)
                   and m.get("silence_dispatch_id") == key
                   and m.get("source") == MODE and m.get("actor_type") == "automation"
                   and m.get("automatic") is True
                   and str(m.get("assigned_executive_id")) == job["owner"]
                   and str(m.get("assignment_cycle_id")) == job["cycle"]
                   and str(m.get("wasender_session_id")) == job["session"]
                   and (str(m.get("provider_message_id")) == str(row["provider_message_id"])
                        or str(m.get("crm_api_provider_message_id")) == str(row["provider_message_id"]))
                   and (m.get("provider_echo_message_id") or m.get("timestamp_source") == "provider")
                   and sha256(str(m.get("content") or "").encode()).hexdigest() == row.get("content_sha256")]
        if len(matches) != 1 or not accepted or not matches[0].get("message_id"):
            return None
        verified.append({"message_id": matches[0]["message_id"], "accepted_at": accepted})
    return tuple(verified)


def _decision(db, job, *, now):
    doc = db["leads"].find_one({"_id": job["lead_id"]})
    if not doc:
        return None, {"state": "HOLD", "reason": "lead_missing"}
    ci = doc.get("commercial_intelligence") or {}
    lc = doc.get("lifecycle") or {}
    if (str(lc.get("current_assignment_cycle_id")) != job["cycle"]
            or str(lc.get("assigned_to_user_id")) != job["owner"]
            or str(ci.get("pending_session_id")) != job["session"]):
        return doc, {"state": "HOLD", "reason": "superseded"}
    if _has_executive_dialogue(doc):
        return doc, {"state": "HOLD", "reason": "dialogue_policy_pending",
                     "scenario": "DIALOGUE_INTERRUPTED"}
    phone = normalize_phone_strict(doc.get("phone"))
    if not phone:
        return doc, {"state": "HOLD", "reason": "invalid_phone"}
    prior = _sequence_evidence(db, doc, job, phone)
    if prior is None:
        return doc, {"state": "HOLD", "reason": "attempt_history_unverified_or_duplicate"}
    if ci.get("last_processed_message_id") not in ({job["anchor"]} | {x["message_id"] for x in prior}):
        return doc, {"state": "HOLD", "reason": "superseded"}
    # Existing pending tasks always win. Query by both canonical ID forms.
    if db["crm_tasks"].find_one({"$or": [
            {"lead_id": {"$in": [doc["_id"], str(doc["_id"])]}},
            {"phone": {"$in": [phone, phone.lstrip("+"), phone.lstrip("+")[2:]]}}],
            "status": {"$nin": ["completed", "cancelled", "canceled", "closed", "done", "sent", "error"]}}, {"_id": 1}):
        return doc, {"state": "HOLD", "reason": "crm_task_pending"}
    auth = ((doc.get("crm_hot_auto") or {}).get("whatsapp_contact_authorization") or {})
    if isinstance(auth, dict) and auth:
        if auth.get("revoked_at") is not None or str(auth.get("status") or "").upper() in {"REVOKED", "DENIED", "OPTED_OUT"}:
            return doc, {"state": "HOLD", "reason": "contact_authorization_revoked"}
        if auth.get("expires_at") is not None:
            expiry = _time(auth["expires_at"])
            if expiry is None or expiry <= now:
                return doc, {"state": "HOLD", "reason": "contact_authorization_expired_or_invalid"}
    authorized = _contact_evidence(db, doc, job, phone, now)
    # For later stages, verified first-stage portal evidence remains tied to
    # this original inquiry, not a new consent or an unrelated campaign.
    # Current opt-out, scope and age still must pass the policy below.
    if not authorized and prior and now - prior[0]["accepted_at"] <= timedelta(days=7):
        authorized = _verified_portal_first_contact(db, doc, job, prior[0]["accepted_at"])
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
        followup_attempts=len(prior), verified_followups=prior, outstanding_own_action=False,
        continuity_since=connected, scheduled_contact_clear=True,
        recovery_until=_recovery_until(job),
        delayed_initial_until=_delayed_initial_until(job))


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
    if decision.get("scenario") == "DIALOGUE_INTERRUPTED" or _has_executive_dialogue(doc):
        return "dialogue_policy_pending"
    if not _live_allowed(job):
        return "shadow_candidate"
    key, token = str(job["session"]), str(uuid4())
    guard = db[SESSION_GUARD]
    try:
        guard.insert_one({"_id": key, "token": token, "state": "sending", "at": now,
                          "job_id": job.get("_id"), "lease": job.get("lease")})
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
    if decision.get("scenario") == "DIALOGUE_INTERRUPTED" or _has_executive_dialogue(doc or {}):
        return "dialogue_policy_pending"
    if decision["state"] == "WAIT" and _live_allowed(job):
        return "retry_window_before_provider"
    if decision["state"] != "CANDIDATE" or not _live_allowed(job):
        return "superseded"
    anchor = next((m for m in doc.get("messages", []) if m.get("message_id") == job["anchor"]), None)
    if not anchor:
        return "anchor_missing"
    # Generate exactly once per claimed eligible anchor. Never invoke the
    # model on polling, an ineligible lead or a scheduled WAIT.
    try:
        if job.get("reviewed_initial_copy") is True:
            text = _reviewed_initial_recovery(db, job, doc, anchor, decision, now)
            usage = {"version": "reviewed_initial_recovery_v1"}
        else:
            text, usage = draft_followup(db, doc, job, anchor,
                                         scenario=decision["scenario"], attempt=job.get("attempt", 1))
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
        # Recover only technical failures, never an explicit model HOLD or
        # a safety/context rejection. The original same-day recovery remains
        # compatible with the previously reviewed pilot.
        retryable = reason in {"draft_incomplete", "draft_invalid", "draft_schema", "draft_format"}
        exceptional = (recovery_end is not None and now < recovery_end
                       and decision["scenario"] == "INITIAL_NO_REPLY"
                       and reason != "context_unavailable")
        if not exceptional and retryable and not job.get("draft_retry_count"):
            return "retry_draft_before_provider"
        if decision["scenario"] != "INITIAL_NO_REPLY" or not (exceptional or retryable):
            return "model_invalid"
        try:
            text = (initial_recovery_draft(db, doc, anchor) if exceptional else
                    verified_initial_stage_fallback(db, doc, anchor, attempt=job.get("attempt", 1)))
        except ValueError:
            return "model_invalid"
        usage = {"version": "verified_initial_recovery_v1" if exceptional else "verified_stage_fallback_v1",
                 "fallback_reason": reason}
        if not text:
            return "model_invalid"
    except Exception as exc:
        # No provider call or reservation has occurred. Retry a transport
        # failure once; then use verified contextual copy, if possible.
        db[QUEUE].update_one({"_id": job["_id"], "lease": job["lease"]},
                             {"$set": {"draft_error": type(exc).__name__}})
        if not job.get("draft_retry_count"):
            return "retry_draft_before_provider"
        if decision["scenario"] != "INITIAL_NO_REPLY":
            return "model_invalid"
        try:
            text = verified_initial_stage_fallback(db, doc, anchor, attempt=job.get("attempt", 1))
        except Exception:
            return "model_invalid"
        if not text:
            return "model_invalid"
        usage = {"version": "verified_stage_fallback_v1", "fallback_reason": type(exc).__name__}
    if text and any(isinstance(m, dict) and m.get("channel") == "executive_whatsapp"
                    and _plain(m.get("content")) == _plain(text)
                    for m in doc.get("messages") or []):
        db[QUEUE].update_one({"_id": job["_id"], "lease": job["lease"]},
                             {"$set": {"draft_error": "draft_repeat"}})
        return "model_invalid" if job.get("draft_retry_count") else "retry_draft_before_provider"
    db[QUEUE].update_one({"_id": job["_id"], "lease": job["lease"]},
                         {"$set": {"draft_usage": usage,
                                   "draft_decision": "DRAFT" if text else "HOLD"}})
    if not text:
        return "model_hold"
    # Model latency must not allow a newly received message, CRM task,
    # permission revocation or expired send window to slip past the gate.
    now = datetime.now(timezone.utc)
    fresh, rechecked = _decision(db, job, now=now)
    if rechecked.get("scenario") == "DIALOGUE_INTERRUPTED" or _has_executive_dialogue(fresh or {}):
        return "dialogue_policy_pending"
    if rechecked["state"] == "WAIT" and _live_allowed(job):
        return "retry_window_before_provider"
    if fresh != doc or rechecked["state"] != "CANDIDATE" or not _live_allowed(job):
        return "superseded_after_draft"
    phone = normalize_phone_strict(doc.get("phone"))
    # Durable boundary: a stale worker whose pre-provider lease was revoked
    # cannot reserve or send. Anything after this checkpoint is not replayed.
    checkpoint = db[QUEUE].update_one({"_id": job["_id"], "lease": job["lease"],
                                        "state": "evaluating", "phase": "pre_provider"},
                                       {"$set": {"phase": "reservation_pending"}})
    if checkpoint.matched_count != 1:
        return "lease_lost_before_provider"
    # Per-stage reservation, still global across cycles for this phone.
    attempt = job.get("attempt", 1)
    key = _attempt_key(phone, attempt)
    row = {"_id": key, "mode": MODE, "phone": phone, "lead_id": doc["_id"],
           "executive_id": job["owner"], "assignment_cycle_id": job["cycle"],
           "session_id": job["session"], "anchor_message_id": job["anchor"],
           "state": "provider_attempt", "attempted_at": now,
           "content_sha256": sha256(text.encode()).hexdigest(),
           "scenario": decision["scenario"], "attempt": attempt, "draft_usage": usage}
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


def _schedule_successor(db, job, doc):
    attempt = job.get("attempt", 1)
    if attempt >= 3 or _has_executive_dialogue(doc):
        return True
    phone = normalize_phone_strict(doc.get("phone"))
    if not phone:
        return False
    row = db[OUTBOX].find_one({"_id": _attempt_key(phone, attempt), "state": "accepted",
                              "canonical_recorded": True, "lead_id": doc["_id"],
                              "executive_id": job["owner"], "assignment_cycle_id": job["cycle"],
                              "session_id": job["session"], "anchor_message_id": job["anchor"]})
    at = _persisted_utc((row or {}).get("provider_result_at"))
    if not at:
        return False
    root = job.get("sequence_root", job["_id"])
    key = sha256(f"{root}|stage|{attempt + 1}".encode()).hexdigest()
    db[QUEUE].update_one({"_id": key}, {"$setOnInsert": {
        "lead_id": job["lead_id"], "cycle": job["cycle"], "owner": job["owner"],
        "session": job["session"], "anchor": job["anchor"], "state": "pending",
        "sequence_root": root, "attempt": attempt + 1,
        "created_at": at, "due_at": sequence_due(at, attempt + 1),
    }}, upsert=True)
    return True


def _reconcile_accepted_sequences(db, now):
    """Recover only acknowledged sends interrupted before successor insertion.

    Bounded indexed scan, at most once every five minutes per process.
    Never resend, reset a reservation, unlock a session, or infer acceptance.
    """
    global _last_sequence_reconcile
    tick = time.monotonic()
    if _last_sequence_reconcile is not None and tick - _last_sequence_reconcile < 300:
        return
    _last_sequence_reconcile = tick
    try:
        rows = db[QUEUE].find({"state": {"$in": ["evaluating", "error"]},
                              "sequence_started": True,
                              "claimed_at": {"$lte": now - timedelta(minutes=10)}}).limit(5)
        # Repair accepted stages that lacked a successor (including old BSON
        # date conversion failures). Scan only this rollout's recent accepts.
        # Never reset a sent job or enqueue an already-existing stage.
        accepted_rows = db[QUEUE].find({"state": "accepted",
                                       "sequence_started": True,
                                       "sequence_reconciled": {"$ne": True},
                                       "claimed_at": {"$gte": now - timedelta(days=7)}}).limit(5)
        for accepted_job in accepted_rows:
            accepted_doc = db["leads"].find_one({"_id": accepted_job["lead_id"]})
            if accepted_doc and _schedule_successor(db, accepted_job, accepted_doc):
                db[QUEUE].update_one({"_id": accepted_job["_id"], "state": "accepted"},
                                     {"$set": {"sequence_reconciled": True}})
            else:
                _record_incident(db, accepted_job, "successor_evidence_missing")
        for job in rows:
            attempt = job.get("attempt", 1)
            doc = db["leads"].find_one({"_id": job["lead_id"]})
            if not doc:
                continue
            phone = normalize_phone_strict(doc.get("phone"))
            if not phone:
                continue
            row = db[OUTBOX].find_one({"_id": _attempt_key(phone, attempt),
                                      "state": "accepted", "canonical_recorded": True,
                                      "lead_id": job["lead_id"], "executive_id": job["owner"],
                                      "assignment_cycle_id": job["cycle"], "session_id": job["session"],
                                      "anchor_message_id": job["anchor"]})
            if not row or not row.get("provider_message_id") or not _persisted_utc(row.get("provider_result_at")):
                # The checkpoint is set before ANY reservation or provider call.
                # Revoke the stale lease atomically before releasing its guard.
                # Never recover a reservation_pending or uncertain-provider job.
                if (job.get("phase") == "pre_provider" and job.get("lease")
                        and not job.get("pre_provider_recoveries")
                        and not db[OUTBOX].find_one({"_id": _attempt_key(phone, attempt)}, {"_id": 1})):
                    recovered = db[QUEUE].update_one({"_id": job["_id"], "state": job["state"],
                        "lease": job["lease"], "claimed_at": job["claimed_at"], "phase": "pre_provider"},
                        {"$set": {"state": "pending", "reason": "recovered_before_provider",
                                  "due_at": now, "pre_provider_recoveries": 1}, "$unset": {"lease": ""}})
                    if recovered.modified_count == 1:
                        db[SESSION_GUARD].delete_one({"_id": job["session"], "job_id": job["_id"],
                                                     "lease": job["lease"], "state": "sending"})
                    continue
                if job.get("phase"):
                    _record_incident(db, job, "stale_dispatch_requires_reconciliation")
                continue
            # Acceptance is already durable. A new customer reply cancels
            # continuation; even if the reply raced scheduling, the next
            # stage will independently check the whole timeline.
            if not _has_executive_dialogue(doc):
                _schedule_successor(db, job, doc)
            if job.get("lease"):
                # Exact stale guard can be released ONLY after durable acceptance.
                # Do not touch a paused session or another job\'s lock.
                db[SESSION_GUARD].delete_one({"_id": job["session"], "job_id": job["_id"],
                                             "lease": job["lease"], "state": "sending"})
            db[QUEUE].update_one({"_id": job["_id"], "state": job["state"],
                                  "claimed_at": job["claimed_at"]},
                                 {"$set": {"state": "accepted", "reason": "accepted_reconciled"},
                                  "$unset": {"lease": ""}})
    except Exception as exc:
        logger.warning("EXEC_SILENCE reconcile_failure=%s", type(exc).__name__)


def _record_incident(db, job, reason):
    """Persist a deduplicated operational incident, without another provider call."""
    try:
        key = sha256(f"{job['_id']}|{reason}".encode()).hexdigest()
        db["crm_exec_silence_incidents"].update_one({"_id": key}, {"$setOnInsert": {
            "job_id": job["_id"], "lead_id": job.get("lead_id"),
            "owner": job.get("owner"), "session": job.get("session"),
            "attempt": job.get("attempt", 1), "reason": reason,
            "status": "open", "created_at": datetime.now(timezone.utc),
        }}, upsert=True)
        logger.warning("EXEC_SILENCE_INCIDENT job=%s reason=%s", job["_id"], reason)
    except Exception as exc:
        logger.error("EXEC_SILENCE incident_write_failed=%s", type(exc).__name__)


def process_due(db, *, limit=3, now=None):
    if not enabled():
        return
    now = now or datetime.now(timezone.utc)
    _reconcile_accepted_sequences(db, now)
    # Do not retry a job leased during a crash: its dispatch may have succeeded.
    for _ in range(min(max(limit, 1), 5)):
        job = db[QUEUE].find_one_and_update({"state": "pending", "due_at": {"$lte": now}},
            {"$set": {"state": "evaluating", "lease": str(uuid4()), "claimed_at": now, "sequence_started": True, "phase": "pre_provider"}},
            sort=[("due_at", 1)], return_document=ReturnDocument.AFTER)
        if not job:
            break
        try:
            doc, decision = _decision(db, job, now=now)
            if decision["state"] == "WAIT":
                _finish(db, job, state="pending", reason=decision["reason"], due_at=decision["due_at"])
            elif decision["state"] == "CANDIDATE":
                state = dispatch(db, job, doc, decision, now=now)
                if state == "accepted":
                    successor_ok = _schedule_successor(db, job, doc)
                    if not successor_ok:
                        _record_incident(db, job, "successor_evidence_missing")
                if state == "dialogue_policy_pending":
                    _finish(db, job, state="held", reason=state)
                elif state in {"session_busy", "retry_window_before_provider", "retry_draft_before_provider"}:
                    extra = {"draft_retry_count": 1} if state == "retry_draft_before_provider" else {}
                    _finish(db, job, state="pending", reason=state, due_at=datetime.now(timezone.utc) + timedelta(minutes=1), **extra)
                else:
                    if state in SESSION_STOP | {"session_paused", "model_invalid", "model_hold", "session_changed", "shadow_candidate"}:
                        _record_incident(db, job, state)
                    _finish(db, job, state=state, reason=state if state in SESSION_STOP | {"session_paused", "model_invalid", "model_hold", "session_changed", "shadow_candidate"} else decision["reason"], **({"sequence_reconciled": bool(successor_ok)} if state == "accepted" else {}))
            else:
                reason = decision["reason"]
                # Classification may still be finishing after a provider echo.
                # Re-evaluate once before holding; never bypass canonical checks.
                if reason == "classification_not_ready" and not job.get("classification_retry_count"):
                    _finish(db, job, state="pending", reason=reason,
                            due_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                            classification_retry_count=1)
                else:
                    if reason in {"classification_not_ready", "capture_continuity_unverified",
                                  "attempt_history_unverified_or_duplicate", "unverified_prerequisite",
                                  "incomplete_message_history", "inquiry_contact_evidence_missing"}:
                        _record_incident(db, job, reason)
                    _finish(db, job, state="held", reason=reason)
        except OutboundBlocked as exc:
            _record_incident(db, job, exc.code)
            _finish(db, job, state="held", reason=exc.code)
        except Exception as exc:
            _record_incident(db, job, type(exc).__name__)
            _finish(db, job, state="error", reason=type(exc).__name__)
