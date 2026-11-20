"""Restore the canonical current-owner mirror for newly created manual leads.

An assignment cycle is authoritative, but the executive WhatsApp audit, CRM
access checks and SLA views require matching owner/cycle pointers on the lead.
The manual-entry path historically created cycles without these pointers.

This helper fails closed if any existing owner, cycle or user disagrees. It
does not send WhatsApp, change assignments, move SLA dates, or read messages.
"""
from __future__ import annotations

from typing import Any, Mapping

from bson import ObjectId

from .crm_assignment_integrity import active_assignment_cycles


def persist_manual_cycle_mirror(
    db: Any, *, lead_id: ObjectId, cycle: Mapping[str, Any], owner_name: str,
    dry_run: bool = False,
) -> bool:
    owner_id = str(cycle.get("assigned_to_user_id") or "").strip()
    cycle_id = str(cycle.get("assignment_cycle_id") or "").strip()
    canonical_name = str(cycle.get("assigned_to_display_name") or "").strip()
    if (
        not isinstance(lead_id, ObjectId)
        or not ObjectId.is_valid(owner_id)
        or not cycle_id
        or not canonical_name
        or canonical_name != str(owner_name or "").strip()
        or cycle.get("cycle_status") not in (None, "active")
        or cycle.get("unassigned_at") is not None
        or cycle.get("assigned_at") is None
    ):
        return False

    owner = db["usuarios"].find_one({
        "_id": ObjectId(owner_id), "rol": "agente", "is_active": True,
        "nombre": canonical_name,
    }, {"_id": 1})
    if owner is None:
        return False

    cycles = active_assignment_cycles(db, lead_id)
    if (
        len(cycles) != 1
        or str(cycles[0].get("assignment_cycle_id") or "") != cycle_id
        or str(cycles[0].get("assigned_to_user_id") or "") != owner_id
        or str(cycles[0].get("assigned_to_display_name") or "") != canonical_name
    ):
        return False

    lead = db["leads"].find_one({"_id": lead_id}, {
        "_id": 1, "ejecutivo_asignado": 1, "prospecto.ejecutivo": 1,
        "lifecycle": 1, "owner_user_id": 1, "assigned_to_user_id": 1,
        "assignment_mirror_owner_user_id": 1,
    })
    if not lead:
        return False
    lifecycle = lead.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        return False
    if (
        lead.get("ejecutivo_asignado") != canonical_name
        or (lead.get("prospecto") or {}).get("ejecutivo") != canonical_name
    ):
        return False

    expected = {
        "lifecycle.assigned_to_user_id": owner_id,
        "lifecycle.assigned_to_display_name": canonical_name,
        "lifecycle.assignment_cycle_id": cycle_id,
        "lifecycle.current_assignment_cycle_id": cycle_id,
        "assignment_mirror_owner_user_id": owner_id,
    }
    for key in ("owner_user_id", "assigned_to_user_id"):
        value = lead.get(key)
        if value not in (None, "") and str(value) != owner_id:
            return False
    for dotted, value in expected.items():
        existing = (lifecycle.get(dotted.split(".", 1)[1])
                    if dotted.startswith("lifecycle.") else lead.get(dotted))
        if existing not in (None, "") and str(existing) != str(value):
            return False

    # CAS: a concurrent owner/cycle mutation cannot be overwritten by a
    # stale manual-entry process.
    constraints = [
        {"$or": [
            {field: {"$exists": False}}, {field: None}, {field: ""},
            {field: target},
        ]}
        for field, target in expected.items()
    ]
    update = {
        **expected,
        "lifecycle.cycle_started_at": (
            cycle.get("cycle_started_at") or cycle["assigned_at"]
        ),
        "assignment_mirror_source": "crm_assignment_cycles",
    }
    if cycle.get("sla_started_at") is not None:
        update["lifecycle.sla_started_at"] = cycle["sla_started_at"]

    if dry_run:
        return True

    result = db["leads"].update_one({
        "_id": lead_id,
        "ejecutivo_asignado": canonical_name,
        "prospecto.ejecutivo": canonical_name,
        "$and": constraints,
    }, {"$set": update})
    return result.matched_count == 1
