"""Durable, guarded processing of executive conversation intelligence.

The signed webhook atomically queues a lead by marking its canonical document.
A bounded worker claims pending leads, checks the current owner/cycle again,
then classifies the business-only messages. No WhatsApp sends, CRM actions,
SLA transitions, campaign sends, or personal chat reads occur in this module.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument

from .crm_metrics import active_assignment_cycle
from .executive_conversation_intelligence import VERSION, classify_hybrid
from .executive_opportunities import merge_opportunities
from .active_search import capture_active_search, find_matching_inventory
from .active_search_followup import ensure_followup_task, refresh_active_search_batch

logger = logging.getLogger(__name__)
WINDOW = 200


def _available(now):
    return {"commercial_intelligence.pending": True, "$or": [
        {"commercial_intelligence.lease_expires_at": None},
        {"commercial_intelligence.lease_expires_at": {"$lt": now}},
    ]}


def ensure_indexes(db):
    """Idempotent; called only when the independently gated worker is enabled."""
    db["leads"].create_index(
        [("commercial_intelligence.lease_expires_at", 1),
         ("commercial_intelligence.pending_at", 1)],
        name="executive_intelligence_pending_lease",
        partialFilterExpression={"commercial_intelligence.pending": True},
    )


def _is_authorized(db, doc: dict) -> bool:
    lifecycle = doc.get("lifecycle") or {}
    marker = doc.get("commercial_intelligence") or {}
    if marker.get("pending_source") != "executive_whatsapp":
        return False
    owner = str(lifecycle.get("assigned_to_user_id") or "")
    cycle_id = str(lifecycle.get("current_assignment_cycle_id") or "")
    if not owner or not cycle_id or cycle_id != str(marker.get("pending_cycle_id") or ""):
        return False
    cycle = active_assignment_cycle(db, doc["_id"])
    if not cycle or str(cycle.get("assignment_cycle_id") or "") != cycle_id:
        return False
    if str(cycle.get("assigned_to_user_id") or "") != owner:
        return False
    if cycle.get("cycle_status") not in (None, "active") or cycle.get("unassigned_at") is not None:
        return False
    from bson import ObjectId
    oid = ObjectId(owner) if ObjectId.is_valid(owner) else owner
    sid = marker.get("pending_session_id")
    if not sid:
        return False
    user = db["usuarios"].find_one({
        "_id": oid, "rol": "agente", "is_active": True,
        "wasender.session_id": {"$in": [int(sid), str(sid)] if str(sid).isdigit() else [sid]},
    }, {"_id": 1, "nombre": 1})
    if not user:
        return False
    from .executive_whatsapp_audit import _session
    session = _session(db, str(sid))
    return bool(session and session["user_id"] == owner and session["capture_enabled"])


def _claim(db, lead_id, *, now: datetime):
    # Multiple application instances may run; claim is an atomic lease.
    return db["leads"].find_one_and_update(
        {"_id": lead_id, **_available(now)},
        {"$set": {"commercial_intelligence.lease_expires_at": now + timedelta(seconds=90),
                  "commercial_intelligence.lease_token": str(uuid4())}},
        projection={"_id": 1, "lifecycle": 1, "commercial_intelligence": 1},
        return_document=ReturnDocument.AFTER,
    )


def _load_window(db, doc):
    """Filter in Mongo before transfer; only new authorized turns plus context.

    Canonical embedded history still needs a server-side scan. Transfer and
    Python/model work are bounded, without discarding pending messages.
    """
    marker, lifecycle = doc["commercial_intelligence"], doc["lifecycle"]
    cycle = str(lifecycle["current_assignment_cycle_id"])
    checkpoint = marker.get("last_processed_message_id") if (
        marker.get("last_processed_cycle_id") == cycle and marker.get("version") == VERSION) else None
    rows = list(db["leads"].aggregate([
        {"$match": {"_id": doc["_id"],
                    "commercial_intelligence.pending_message_id": marker["pending_message_id"],
                    "commercial_intelligence.lease_token": marker["lease_token"],
                    "lifecycle.current_assignment_cycle_id": cycle,
                    "lifecycle.assigned_to_user_id": lifecycle["assigned_to_user_id"]}},
        {"$project": {"messages": {"$filter": {
            "input": {"$ifNull": ["$messages", []]}, "as": "m", "cond": {"$and": [
                {"$eq": ["$$m.channel", "executive_whatsapp"]},
                {"$eq": ["$$m.assignment_cycle_id", cycle]},
                {"$eq": ["$$m.assigned_executive_id", str(lifecycle["assigned_to_user_id"])]},
                {"$eq": ["$$m.wasender_session_id", str(marker["pending_session_id"])]},
                {"$in": ["$$m.actor_type", ["customer", "human_agent"]]},
            ]}}}}},
        {"$project": {"messages": 1, "position": {"$indexOfArray": ["$messages.message_id", checkpoint]}}},
        {"$project": {
            "context": {"$cond": [{"$gte": ["$position", 0]},
                {"$slice": ["$messages", {"$max": [0, {"$subtract": ["$position", 1]}]}, {"$min": [2, {"$add": ["$position", 1]}]}]}, []]},
            "messages": {"$slice": ["$messages", {"$add": ["$position", 1]}, WINDOW + 1]},
            "checkpoint_missing": {"$and": [{"$literal": checkpoint is not None}, {"$lt": ["$position", 0]}]},
        }},
    ]))
    if not rows:
        return False
    row = rows[0]
    pending = row.get("messages", [])
    doc["_more_pending"] = len(pending) > WINDOW
    batch = pending[:WINDOW]
    doc["_checkpoint_missing"] = row.get("checkpoint_missing", False)
    doc["_processed_cursor"] = batch[-1]["message_id"] if batch else checkpoint
    doc["messages"] = [{**m, "_context_only": True} for m in row.get("context", [])] + batch
    return True


def _load_office_search_context(db, doc: dict) -> tuple[list[dict], dict]:
    """Only canonical office CUSTOMER turns in this exact conversation.

    Unlike executive turns, these messages are NEVER passed to DeepSeek or
    stored in the executive interpretation window. No personal chats/portals.
    """
    base = db["leads"].find_one({"_id": doc["_id"]}, {
        "conversation_id": 1, "codigo_propiedad": 1, "codigo": 1,
        "operacion": 1, "prospecto.operacion": 1,
    }) or {}
    conv_id = str(base.get("conversation_id") or "")
    if not conv_id:
        return [], base
    rows = list(db["leads"].aggregate([
        {"$match": {"_id": doc["_id"],
                    "conversation_id": conv_id}},
        {"$project": {"office_customer": {"$slice": [{"$filter": {
            "input": {"$ifNull": ["$messages", []]}, "as": "m",
            "cond": {"$and": [
                {"$eq": ["$$m.actor_type", "customer"]},
                {"$eq": ["$$m.role", "user"]},
                {"$eq": ["$$m.conversation_id", conv_id]},
                {"$in": [{"$ifNull": ["$$m.channel", ""]}, ["", "office_whatsapp", "whatsapp"]]},
                {"$in": [{"$ifNull": ["$$m.source", ""]},
                         ["", "office_whatsapp", "wasender_inbound", "whatsapp_inbound", "whatsapp"]]},
            ]}}}, -100]}}},
    ]))
    return (rows[0].get("office_customer") or []) if rows else [], base


def _merge_search_into_summary(db, doc: dict, turns: list[dict],
                               summary: dict) -> None:
    office, base = _load_office_search_context(db, doc)
    marker = doc.get("commercial_intelligence") or {}
    life = doc.get("lifecycle") or {}
    search = capture_active_search(
        office + turns,
        conversation_id=str(base.get("conversation_id") or ""),
        cycle_id=str(life.get("current_assignment_cycle_id") or ""),
        owner_id=str(life.get("assigned_to_user_id") or ""),
        session_id=str(marker.get("pending_session_id") or ""),
        original_property_code=str(base.get("codigo_propiedad") or base.get("codigo") or ""),
        original_operation=str(base.get("operacion") or (base.get("prospecto") or {}).get("operacion") or ""),
        prior=marker.get("active_search"),
    )
    if not search:
        return
    if search.get("status") != "ACTIVE":
        # Explicitly completed searches remain traceable, not a target for
        # matching or outbound communication.
        doc["_active_search"] = {**search, "match_status": "CLOSED", "matches": []}
        return
    search["matches"] = find_matching_inventory(db, search)
    search["match_status"] = "CANDIDATES_NEED_VERIFICATION" if search["matches"] else "WAITING_INVENTORY"
    search["last_matched_at"] = datetime.now(timezone.utc).isoformat()
    doc["_active_search"] = search
    if search.get("followup_required"):
        criteria = search["criteria"]
        canonical = {
            key: criteria[key] for key in
            ("operation", "comunas", "property_types", "max_price_clp")
            if key in criteria
        }
        summary["search_status"] = "ACTIVE"
        summary["search"] = {**summary.get("search", {}), **canonical}
        summary.setdefault("needs", {})["DEMAND"] = summary["search"]
        summary.setdefault("field_evidence", {})["DEMAND"] = search.get("field_evidence", {})
        summary["active_search_status"] = search["match_status"]
        # This is an outstanding opportunity, not a promise of a visit.
        summary["followup_search_required"] = True


def _finish(db, doc: dict, *, summary: dict | None, status: str) -> str:
    marker = doc.get("commercial_intelligence") or {}
    lifecycle = doc.get("lifecycle") or {}
    # Compare-and-swap prevents a slow model result from hiding a newer message
    # or writing into a new assignment cycle after reassignment.
    match = {
        "_id": doc["_id"],
        "commercial_intelligence.pending": True,
        "commercial_intelligence.pending_message_id": marker.get("pending_message_id"),
        "commercial_intelligence.pending_cycle_id": marker.get("pending_cycle_id"),
        "commercial_intelligence.pending_session_id": marker.get("pending_session_id"),
        "commercial_intelligence.lease_token": marker.get("lease_token"),
    }
    if status != "stale_assignment":
        match.update({
            "lifecycle.current_assignment_cycle_id": marker.get("pending_cycle_id"),
            "lifecycle.assigned_to_user_id": lifecycle.get("assigned_to_user_id"),
        })
    # A stale job may only be cleared, never interpreted or persisted as an
    # outcome. Its message/cycle CAS still prevents clearing newer work.
    values = {
        "commercial_intelligence.pending": bool(doc.get("_more_pending")) if summary is not None else False,
        "commercial_intelligence.status": status,
        "commercial_intelligence.last_processed_message_id": doc.get("_processed_cursor", marker.get("pending_message_id")),
        "commercial_intelligence.last_processed_cycle_id": marker.get("pending_cycle_id"),
        "commercial_intelligence.lease_expires_at": None,
        "commercial_intelligence.lease_token": None,
    }
    if summary is not None:
        values["commercial_intelligence.summary"] = summary
        values["commercial_intelligence.version"] = VERSION
        if "_active_search" in doc:
            values["commercial_intelligence.active_search"] = doc["_active_search"]
        if "_opportunities" in doc:
            # The ledger lives on the canonical lead, not in the versioned
            # summary. Preserve it across agent reassignment and greetings.
            previous_revision = marker.get("opportunities_revision")
            match["commercial_intelligence.opportunities_revision"] = (
                previous_revision if previous_revision is not None
                else {"$exists": False}
            )
            values["commercial_intelligence.opportunities"] = doc["_opportunities"]
            values["commercial_intelligence.opportunities_revision"] = (
                int(previous_revision or 0) + 1
            )
    changed = db["leads"].update_one(
        match,
        {"$set": values},
    )
    if changed.modified_count != 1:
        # Release only our own lease; never change a newer pending marker or
        # another worker's token. This avoids a 90-second wait after a new turn.
        db["leads"].update_one(
            {"_id": doc["_id"], "commercial_intelligence.lease_token": marker.get("lease_token")},
            {"$set": {"commercial_intelligence.lease_expires_at": None,
                      "commercial_intelligence.lease_token": None}},
        )
    return status if changed.modified_count == 1 else "superseded"


def process_claimed(db, doc: dict, *, llm_enabled: bool = True, propose=None) -> str:
    marker = doc.get("commercial_intelligence") or {}
    if not _is_authorized(db, doc):
        # On reassignment, do not interpret the previous owner's conversation.
        return _finish(db, doc, summary=None, status="stale_assignment")
    if "messages" not in doc and not _load_window(db, doc):
        return "superseded"
    owner = str(doc["lifecycle"]["assigned_to_user_id"])
    cycle_id = str(doc["lifecycle"]["current_assignment_cycle_id"])
    session_id = str(marker["pending_session_id"])
    turns = [
        m for m in (doc.get("messages") or [])
        if isinstance(m, dict)
        and m.get("channel") == "executive_whatsapp"
        and str(m.get("assignment_cycle_id") or "") == cycle_id
        and str(m.get("assigned_executive_id") or "") == owner
        and str(m.get("wasender_session_id") or "") == session_id
        and m.get("actor_type") in ("customer", "human_agent")
    ]
    if not turns:
        return _finish(db, doc, summary=None, status="no_eligible_turns")
    # No ad-hoc outreach, no automatic CRM results, no opt-out side effects.
    # Previous commercial evidence is valid only within this assignment cycle.
    prior = marker if (str(marker.get("last_processed_cycle_id") or "") == cycle_id
                       and marker.get("version") == VERSION) else {}
    if not prior and ((marker.get("summary") or {}).get("contact_permission") == "OPTED_OUT"
                      or (marker.get("summary") or {}).get("intent") == "OPT_OUT"):
        prior = {"summary": {"contact_permission": "OPTED_OUT"}}
    summary = classify_hybrid(
        turns, prior, llm_enabled=llm_enabled, propose=propose,
    )
    _merge_search_into_summary(db, doc, turns, summary)
    ledger, active, open_count = merge_opportunities(
        turns, marker.get("opportunities"), summary,
        cycle_id=cycle_id, session_id=session_id, owner_id=owner,
    )
    doc["_opportunities"] = ledger
    management = summary["executive_management"]
    management["open_opportunities_count"] = open_count
    management["active_opportunity_id"] = active["opportunity_id"] if active else None
    management["agent_reply_observed"] = bool(active and active.get("agent_reply_observed"))
    # A later greeting/no-signal does NOT erase an outstanding commercial
    # request. Keep current-message intent separately for traceability.
    management["last_message_case_type"] = management["case_type"]
    if active and summary["intent"] == "NO_SIGNAL":
        management["case_type"] = active["case_type"]
        management["next_step_hint"] = active["next_step_hint"]
    if active and active["status"] == "PENDIENTE_GESTION":
        management["pending_customer_request"] = True
    if summary.get("followup_search_required"):
        # Do not confuse "last message = gracias" with "buyer is done".
        management["case_type"] = "BUSQUEDA_ACTIVA_ALTERNATIVAS"
        management["next_step_hint"] = (
            "REVALIDAR_Y_PROPONER_ALTERNATIVAS"
            if doc["_active_search"].get("matches")
            else "BUSCAR_ALTERNATIVAS_Y_HACER_SEGUIMIENTO"
        )
        management["pending_customer_request"] = True
        summary["needs_review"] = bool(doc["_active_search"].get("matches"))

    if doc.get("_checkpoint_missing"):
        summary["needs_review"] = True
        summary["history_incomplete"] = True
    # Revoked capture, reassignment, or a disabled user during a model request
    # must also prevent publishing its result.
    if not _is_authorized(db, doc):
        return _finish(db, doc, summary=None, status="stale_assignment")
    outcome = _finish(db, doc, summary=summary, status="ready")
    if (outcome == "ready" and doc.get("_active_search")
            and doc["_active_search"].get("followup_required")
            and not doc["_active_search"].get("opt_out")):
        try:
            # Only after the CAS committed the current-owner classification.
            # This task prompts human verification/offer; it sends no WhatsApp.
            ensure_followup_task(
                db, lead_id=doc["_id"], cycle_id=cycle_id, owner_id=owner,
                search=doc["_active_search"],
            )
        except Exception as exc:
            logger.warning("ACTIVE_SEARCH followup_error=%s", type(exc).__name__)
    return outcome


def backfill_missing_active_searches(db, *, limit: int = 20) -> dict:
    """One-time bounded repair of recent already-classified active searches.

    This is required for existing cases where the executive summary contains
    SEARCH_UPDATED but the earlier office budget was never interpreted.
    No webhook replay, new message, private conversation or customer send.
    """
    cursor = db["leads"].find({
        "commercial_intelligence.summary.search_status": "ACTIVE",
        "commercial_intelligence.active_search": {"$exists": False},
        "commercial_intelligence.pending": False,
    }, {
        "_id": 1, "lifecycle": 1, "commercial_intelligence": 1,
    }).sort("commercial_intelligence.summary.last_customer_at", -1).limit(
        max(1, min(limit, 50)),
    )
    examined, repaired = 0, 0
    for doc in cursor:
        examined += 1
        summary = (doc.get("commercial_intelligence") or {}).get("summary") or {}
        if summary.get("contact_permission") == "OPTED_OUT":
            continue
        if not _is_authorized(db, doc):
            continue
        marker, life = doc["commercial_intelligence"], doc["lifecycle"]
        cycle_id = str(life.get("current_assignment_cycle_id") or "")
        owner = str(life.get("assigned_to_user_id") or "")
        sid = str(marker.get("pending_session_id") or "")
        office, base = _load_office_search_context(db, doc)
        if not base.get("conversation_id"):
            continue
        rows = list(db["leads"].aggregate([
            {"$match": {"_id": doc["_id"],
                        "lifecycle.current_assignment_cycle_id": cycle_id,
                        "lifecycle.assigned_to_user_id": owner}},
            {"$project": {"customer_exec": {"$slice": [{"$filter": {
                "input": {"$ifNull": ["$messages", []]}, "as": "m",
                "cond": {"$and": [
                    {"$eq": ["$$m.channel", "executive_whatsapp"]},
                    {"$eq": ["$$m.actor_type", "customer"]},
                    {"$eq": ["$$m.assignment_cycle_id", cycle_id]},
                    {"$eq": ["$$m.assigned_executive_id", owner]},
                    {"$eq": ["$$m.wasender_session_id", sid]},
                    {"$ne": [{"$ifNull": ["$$m.provider_message_id", None]}, None]},
                ]}}}, -100]}}},
        ]))
        executive = (rows[0].get("customer_exec") or []) if rows else []
        search = capture_active_search(
            office + executive, conversation_id=str(base["conversation_id"]),
            cycle_id=cycle_id, owner_id=owner, session_id=sid,
            original_property_code=str(base.get("codigo_propiedad") or base.get("codigo") or ""),
            original_operation=str(base.get("operacion") or (base.get("prospecto") or {}).get("operacion") or ""),
        )
        if not search or search.get("status") != "ACTIVE" or not search.get("followup_required"):
            continue
        matches = find_matching_inventory(db, search)
        search.update(
            matches=matches,
            match_status="CANDIDATES_NEED_VERIFICATION" if matches else "WAITING_INVENTORY",
            last_matched_at=datetime.now(timezone.utc).isoformat(),
        )
        criteria = search["criteria"]
        public = {key: criteria[key] for key in (
            "operation", "comunas", "property_types", "max_price_clp",
        ) if key in criteria}
        hint = ("REVALIDAR_Y_PROPONER_ALTERNATIVAS" if matches
                else "BUSCAR_ALTERNATIVAS_Y_HACER_SEGUIMIENTO")
        result = db["leads"].update_one({
            "_id": doc["_id"],
            "lifecycle.current_assignment_cycle_id": cycle_id,
            "lifecycle.assigned_to_user_id": owner,
            "commercial_intelligence.summary.search_status": "ACTIVE",
            "commercial_intelligence.pending": False,
            "commercial_intelligence.active_search": {"$exists": False},
        }, {"$set": {
            "commercial_intelligence.active_search": search,
            "commercial_intelligence.summary.search": public,
            "commercial_intelligence.summary.needs.DEMAND": public,
            "commercial_intelligence.summary.field_evidence.DEMAND": search["field_evidence"],
            "commercial_intelligence.summary.active_search_status": search["match_status"],
            "commercial_intelligence.summary.followup_search_required": True,
            "commercial_intelligence.summary.executive_management.case_type":
                "BUSQUEDA_ACTIVA_ALTERNATIVAS",
            "commercial_intelligence.summary.executive_management.next_step_hint": hint,
            "commercial_intelligence.summary.executive_management.pending_customer_request": True,
        }})
        if result.modified_count != 1:
            continue
        repaired += 1
        try:
            ensure_followup_task(
                db, lead_id=doc["_id"], cycle_id=cycle_id,
                owner_id=owner, search=search,
            )
        except Exception as exc:
            logger.warning("ACTIVE_SEARCH backfill_task_error=%s", type(exc).__name__)
    return {"examined": examined, "repaired": repaired}


def process_pending_batch(db, *, limit: int = 5, llm_enabled: bool = True, propose=None) -> dict:
    examined = 0
    processed = 0
    failures = 0
    now = datetime.now(timezone.utc)
    # Bounded scan; only pointer fields leave Mongo until a claim is obtained.
    cursor = db["leads"].find(
        _available(now),
        {"_id": 1},
    ).sort("commercial_intelligence.pending_at", 1).limit(max(1, min(limit * 3, 30)))
    for row in cursor:
        if processed >= limit:
            break
        examined += 1
        doc = _claim(db, row["_id"], now=now)
        if not doc:
            continue
        try:
            outcome = process_claimed(db, doc, llm_enabled=llm_enabled, propose=propose)
            if outcome in ("ready", "stale_assignment", "no_eligible_turns"):
                processed += 1
            if outcome == "ready" and os.getenv("PROCASA_EXEC_SILENCE_WORKER_ENABLED", "0") == "1":
                try:
                    from .executive_silence_worker import enqueue
                    enqueue(db, doc["_id"])
                except Exception as exc:
                    logger.warning("EXEC_SILENCE enqueue_error=%s", type(exc).__name__)
        except Exception as exc:
            failures += 1
            # Fail closed. Preserve pending flag for a later retry after lease.
            # Never log transcripts, phone numbers or provider IDs.
            logger.warning("EXEC_INTELLIGENCE processing_failure=%s", type(exc).__name__)
    return {"examined": examined, "processed": processed, "failures": failures}


async def executive_intelligence_worker_loop() -> None:
    indexed = False
    refresh_ticks = 0
    silence_indexed = False
    while True:
        try:
            if os.getenv("PROCASA_EXEC_INTELLIGENCE_ENABLED", "0") == "1":
                from .storage import get_db
                if not indexed:
                    await asyncio.to_thread(ensure_indexes, get_db())
                    indexed = True
                llm_enabled = os.getenv("PROCASA_EXEC_INTELLIGENCE_LLM_ENABLED", "0") == "1"
                result = await asyncio.to_thread(
                    process_pending_batch, get_db(),
                    limit=3, llm_enabled=llm_enabled,
                )
                if result["processed"] or result["failures"]:
                    logger.info(
                        "EXEC_INTELLIGENCE processed=%s failures=%s",
                        result["processed"], result["failures"],
                    )
                if os.getenv("PROCASA_EXEC_SILENCE_WORKER_ENABLED", "0") == "1":
                    from .executive_silence_worker import ensure_indexes as silence_indexes, process_due
                    if not silence_indexed:
                        await asyncio.to_thread(silence_indexes, get_db())
                        silence_indexed = True
                    await asyncio.to_thread(process_due, get_db(), limit=2)
                # Every ~15 min, re-evaluate unmatched active searches against
                # the current inventory after new listings / price reductions.
                # This creates executive follow-up tasks, never customer sends.
                refresh_ticks += 1
                if refresh_ticks == 1 or refresh_ticks >= 45:
                    if refresh_ticks >= 45:
                        refresh_ticks = 0
                    repaired = await asyncio.to_thread(
                        backfill_missing_active_searches, get_db(), limit=20,
                    )
                    if repaired["repaired"]:
                        logger.info("ACTIVE_SEARCH repaired=%s", repaired["repaired"])
                    refreshed = await asyncio.to_thread(
                        refresh_active_search_batch, get_db(), limit=20,
                    )
                    if refreshed["checked"]:
                        logger.info(
                            "ACTIVE_SEARCH checked=%s task_checked=%s",
                            refreshed["checked"], refreshed["task_checked"],
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("EXEC_INTELLIGENCE worker_error=%s", type(exc).__name__)
        await asyncio.sleep(20)
