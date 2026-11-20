"""Regression: the manually created CRM lead must mirror its active cycle.

No external calls and no WhatsApp sends.
"""
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
import mongomock

from chatbot.crm_manual_cycle_mirror import persist_manual_cycle_mirror
from chatbot.executive_whatsapp_audit import _authorized_lead

OWNER = ObjectId("6989c6309dd2ba54e478196c")
PHONE = "+56912345678"
NAME = "Mariela Arriagada"
WHEN = datetime(2026, 11, 19, 19, 0, tzinfo=timezone.utc)


def _rig():
    db = mongomock.MongoClient()["URLS"]
    lead_id = ObjectId("6aff687e7213f38293f3c63d")
    cycle = {
        "lead_id": lead_id, "assignment_cycle_id": "cycle-manual-mariela",
        "assigned_to_user_id": str(OWNER), "assigned_to_display_name": NAME,
        "assigned_at": WHEN, "sla_started_at": WHEN,
        "cycle_status": "active", "unassigned_at": None,
        "reason": "manual_lead_created",
    }
    db["usuarios"].insert_one({
        "_id": OWNER, "nombre": NAME, "rol": "agente", "is_active": True,
        "wasender": {"session_id": 133526},
    })
    db["crm_assignment_cycles"].insert_one(cycle)
    db["leads"].insert_one({
        "_id": lead_id, "phone": PHONE, "ejecutivo_asignado": NAME,
        "prospecto": {"ejecutivo": NAME, "codigo": "17252"},
        "lifecycle": {"created_at": "2026-11-19T16:00:00-03:00",
                      "assigned_at": WHEN.isoformat()},
        "messages": [{"role": "system", "content": "Ingreso manual"}],
        "contact_restrictions": {"marketing_opt_out": False},
        "last_crm_update": WHEN.isoformat(),
    })
    return db, lead_id, cycle


def test_manual_cycle_mirror_makes_existing_lead_eligible_for_executive_audit():
    db, lid, cycle = _rig()
    assert _authorized_lead(db, phone=PHONE, user_id=str(OWNER)) is None

    assert persist_manual_cycle_mirror(
        db, lead_id=lid, cycle=cycle, owner_name=NAME,
    ) is True

    lead = db["leads"].find_one({"_id": lid})
    assert lead["lifecycle"]["assigned_to_user_id"] == str(OWNER)
    assert lead["lifecycle"]["assigned_to_display_name"] == NAME
    assert lead["lifecycle"]["current_assignment_cycle_id"] == cycle["assignment_cycle_id"]
    assert lead["lifecycle"]["assignment_cycle_id"] == cycle["assignment_cycle_id"]
    assert lead["lifecycle"]["created_at"] == "2026-11-19T16:00:00-03:00"
    assert lead["lifecycle"]["assigned_at"] == WHEN.isoformat()
    assert lead["messages"] == [{"role": "system", "content": "Ingreso manual"}]
    assert lead["contact_restrictions"]["marketing_opt_out"] is False
    assert db["crm_assignment_cycles"].count_documents({}) == 1

    match = _authorized_lead(db, phone=PHONE, user_id=str(OWNER))
    assert match is not None
    assert match[0]["_id"] == lid
    assert match[1]["assignment_cycle_id"] == "cycle-manual-mariela"
    assert persist_manual_cycle_mirror(
        db, lead_id=lid, cycle=cycle, owner_name=NAME,
    ) is True


def test_conflicting_owner_or_cycle_does_not_grant_access():
    db, lid, cycle = _rig()
    db["leads"].update_one(
        {"_id": lid}, {"$set": {"lifecycle.assigned_to_user_id": "another-owner"}}
    )
    assert persist_manual_cycle_mirror(db, lead_id=lid, cycle=cycle, owner_name=NAME) is False
    assert db["leads"].find_one({"_id": lid})["lifecycle"].get("current_assignment_cycle_id") is None

    db, lid, cycle = _rig()
    db["leads"].update_one(
        {"_id": lid}, {"$set": {"lifecycle.current_assignment_cycle_id": "other-cycle"}}
    )
    assert persist_manual_cycle_mirror(db, lead_id=lid, cycle=cycle, owner_name=NAME) is False
    assert db["leads"].find_one({"_id": lid})["lifecycle"]["current_assignment_cycle_id"] == "other-cycle"


def test_duplicate_active_cycles_or_inactive_owner_fail_closed():
    db, lid, cycle = _rig()
    db["crm_assignment_cycles"].insert_one({
        **cycle, "_id": ObjectId(), "assignment_cycle_id": "cycle-two",
    })
    assert persist_manual_cycle_mirror(db, lead_id=lid, cycle=cycle, owner_name=NAME) is False

    db, lid, cycle = _rig()
    db["usuarios"].update_one({"_id": OWNER}, {"$set": {"is_active": False}})
    assert persist_manual_cycle_mirror(db, lead_id=lid, cycle=cycle, owner_name=NAME) is False


def test_ambiguous_phone_does_not_allow_unauthorized_chat_persistence():
    db, lid, cycle = _rig()
    assert persist_manual_cycle_mirror(db, lead_id=lid, cycle=cycle, owner_name=NAME)
    other = ObjectId()
    db["leads"].insert_one({"_id": other, "phone": PHONE, "messages": []})
    assert _authorized_lead(db, phone=PHONE, user_id=str(OWNER)) is None


def test_manual_entry_mirrors_before_digest_and_never_replaces_lifecycle():
    source = Path("chatbot/manual_entry.py").read_text(encoding="utf-8")
    mirror = source.index("mirrored = persist_manual_cycle_mirror(")
    digest = source.index("notification = accumulate_non_hot_lead(")
    assert mirror < digest
    assert "owner_cycle_mirror_missing" in source
    assert '"status": "review_required"' in source
