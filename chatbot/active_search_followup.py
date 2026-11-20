"""Idempotent human follow-up tasks for buyer-search matches.

Never sends WhatsApp; a price inferred from UF must be freshly verified
before offering it. No SLA transitions or contact/marketing permissions change.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from .active_search import find_matching_inventory

TASK_TYPE = "SEARCH_ALTERNATIVES"
ACTIVE = "ACTIVE"


def _task_id(lead_id, cycle_id, matches: list[dict]) -> str:
    group = "waiting" if not matches else "matches:" + sha256(
        "|".join(sorted(x["property_code"] for x in matches)).encode()
    ).hexdigest()[:16]
    digest = sha256(f"{lead_id}|{cycle_id}|{group}".encode()).hexdigest()
    return "active_search:" + digest[:32]


def _criteria_note(search: dict, matches: list[dict]) -> str:
    c = search.get("criteria") or {}
    kinds = c.get("property_types") or []
    if kinds == ["CASA"]:
        kind = "casa"
    elif kinds == ["DEPARTAMENTO"]:
        kind = "departamento"
    else:
        kind = "casa o departamento"
    amount = c.get("max_price_clp")
    budget = ("hasta $" + f"{amount:,}".replace(",", ".")
              if isinstance(amount, int) and amount > 0 else "presupuesto por confirmar")
    area = " o ".join(c.get("comunas") or []) or "comuna por confirmar"
    landmarks = c.get("location_targets") or []
    reference = (" Referencia geográfica (sin validar): " +
                 ", ".join(landmarks[:3]) + ". Requiere verificación humana."
                 if landmarks else "")
    if not matches:
        return (f"Búsqueda activa: {kind}, {budget}, en {area}." + reference +
                " Sin coincidencias verificadas; revisar búsqueda histórica y "
                "confirmar datos faltantes antes de ofrecer.")
    ids = ", ".join(x["property_code"] for x in matches[:5])
    return (f"Búsqueda activa: {kind}, {budget}, en {area}." + reference +
            f" Revisar opciones internas {ids} y ofrecer únicamente después "
            "de confirmar disponibilidad, precio y autorización de contacto.")


def ensure_followup_task(
    db, *, lead_id, cycle_id: str, owner_id: str,
    search: dict, now: datetime | None = None,
) -> str | None:
    """One task per search revision/match set, never blindly resurrect closed.

    The assigned executive manually approves and delivers an offer. An actual
    customer send must go through the existing office/CRM authorization flow.
    """
    if (search.get("status") != ACTIVE
            or not search.get("followup_required")
            or search.get("opt_out")):
        return None
    now = now or datetime.now(timezone.utc)
    lead = db["leads"].find_one({
        "_id": lead_id,
        "lifecycle.current_assignment_cycle_id": cycle_id,
        "lifecycle.assigned_to_user_id": owner_id,
        "commercial_intelligence.active_search.status": ACTIVE,
    }, {"phone": 1, "lifecycle": 1, "ejecutivo_asignado": 1})
    if not lead:
        return None
    matches = search.get("matches") or []
    identity = _task_id(lead_id, cycle_id, matches)
    record = {
        "_id": identity, "task_id": identity,
        "lead_id": lead_id, "assignment_cycle_id": cycle_id,
        "assigned_to_user_id": owner_id,
        "phone": str(lead.get("phone") or "").replace("+", "").replace(" ", ""),
        "type": TASK_TYPE, "status": "pending",
        "execute_at": now, "created_at": now,
        "agent": str(lead.get("ejecutivo_asignado") or ""),
        "note": _criteria_note(search, matches),
        "property_codes_to_review": [x["property_code"] for x in matches],
        "source": "verified_active_search",
    }
    db["crm_tasks"].update_one(
        {"_id": identity},
        {"$setOnInsert": record}, upsert=True,
    )
    # Permanent case/index integration remains opt-in until the CRM admin UI
    # and the separate worker are configured. It creates no customer/owner
    # messages, does not change assignment, and only enqueues Mongo jobs.
    import os
    if os.getenv("PROCASA_DEMAND_SOURCING_ENABLED", "0") == "1":
        try:
            from .demand_sourcing_engine import ensure_demand_case
            ensure_demand_case(
                db, lead_id=lead_id, cycle_id=cycle_id,
                owner_id=owner_id, search=search, now=now,
            )
        except Exception as exc:
            # The buyer follow-up is already persisted. A sourcing subsystem
            # error cannot block or roll back normal CRM work.
            import logging
            logging.getLogger(__name__).warning(
                "DEMAND_SOURCING case_upsert_error=%s", type(exc).__name__,
            )
        if not matches:
            from .demand_sourcing import ensure_admin_demand_review
            ensure_admin_demand_review(
                db, lead_id=lead_id, cycle_id=cycle_id,
                owner_id=owner_id, search=search, now=now,
            )
    if matches:
        # The waiting task is superseded by a match-specific action. A human
        # already-completed task is never reopened or rewritten.
        waiting_id = _task_id(lead_id, cycle_id, [])
        db["crm_tasks"].update_one(
            {"_id": waiting_id, "status": "pending"},
            {"$set": {"status": "completed", "resolved_at": now,
                      "resolution": "inventory_candidates_found"}},
        )
    return identity


def refresh_active_search_batch(
    db, *, limit: int = 20, now: datetime | None = None,
) -> dict:
    """Bounded re-evaluation so new verified inventory can unlock a follow-up.

    Inbound messages create the original active search; this loop only reads
    that structured profile and the business inventory, not conversations.
    """
    now = now or datetime.now(timezone.utc)
    from datetime import timedelta
    threshold = (now - timedelta(minutes=15)).isoformat()
    candidates = db["leads"].find({
        "commercial_intelligence.active_search.status": ACTIVE,
        "commercial_intelligence.active_search.followup_required": True,
        "$or": [
            {"commercial_intelligence.active_search.last_matched_at": {"$lt": threshold}},
            {"commercial_intelligence.active_search.last_matched_at": {"$exists": False}},
        ],
    }, {"_id": 1, "lifecycle": 1, "commercial_intelligence.active_search": 1}).sort(
        "commercial_intelligence.active_search.last_matched_at", 1,
    ).limit(max(1, min(limit, 50)))
    examined, changed, tasks = 0, 0, 0
    for lead in candidates:
        examined += 1
        search = (lead.get("commercial_intelligence") or {}).get("active_search") or {}
        lifecycle = lead.get("lifecycle") or {}
        owner = str(lifecycle.get("assigned_to_user_id") or "")
        cycle_id = str(lifecycle.get("current_assignment_cycle_id") or "")
        if not owner or not cycle_id or search.get("opt_out"):
            continue
        from .crm_metrics import active_assignment_cycle
        cycle = active_assignment_cycle(db, lead["_id"])
        if (not cycle or str(cycle.get("assignment_cycle_id")) != cycle_id
                or str(cycle.get("assigned_to_user_id")) != owner
                or cycle.get("cycle_status") != "active"):
            continue
        matches = find_matching_inventory(db, search)
        updated = {**search, "matches": matches,
                   "match_status": ("CANDIDATES_NEED_VERIFICATION"
                                    if matches else "WAITING_INVENTORY"),
                   "last_matched_at": now.isoformat()}
        previous_at = search.get("last_matched_at")
        result = db["leads"].update_one({
            "_id": lead["_id"],
            "lifecycle.current_assignment_cycle_id": cycle_id,
            "lifecycle.assigned_to_user_id": owner,
            "commercial_intelligence.active_search.status": ACTIVE,
            "commercial_intelligence.active_search.last_matched_at":
                previous_at if previous_at is not None else {"$exists": False},
        }, {"$set": {"commercial_intelligence.active_search": updated}})
        if result.modified_count != 1:
            continue
        changed += 1
        if ensure_followup_task(
            db, lead_id=lead["_id"], owner_id=owner, cycle_id=cycle_id,
            search=updated, now=now,
        ):
            tasks += 1
    return {"examined": examined, "checked": changed, "task_checked": tasks}
