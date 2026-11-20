"""Historical executive WhatsApp mirror recovery: no real Mongo or WhatsApp calls."""
from datetime import datetime, timezone

from bson import ObjectId
import mongomock

from scripts.repair_legacy_executive_mirrors import (
    _utc_cutoff, audit_and_repair,
)

OWNER = ObjectId("6989c6309dd2ba54e478196c")
NAME = "Mariela Arriagada"
BEFORE = datetime(2026, 10, 20, tzinfo=timezone.utc)


def _db():
    db = mongomock.MongoClient()["URLS"]
    db["usuarios"].insert_one({
        "_id": OWNER, "nombre": NAME, "rol": "agente", "is_active": True,
    })
    return db


def _add(db, ix, *, assigned_at, source="prop360",
         owner_name=NAME, lifecycle=None, property_code="16479"):
    lid = ObjectId(f"6aff0000000000000000{ix:04d}")
    phone = "+5691234" + str(1000 + ix)
    cid = f"historical-cycle-{ix}"
    db["leads"].insert_one({
        "_id": lid, "phone": phone, "source_type": source,
        "ejecutivo_asignado": owner_name,
        "prospecto": {"ejecutivo": owner_name, "codigo": property_code},
        "lifecycle": lifecycle or {"created_at": "2026-08-01T12:00:00-04:00"},
        "messages": [{"role": "user", "content": "Historical chat must remain unchanged"}],
        "contact_restrictions": {"marketing_opt_out": True},
    })
    db["crm_assignment_cycles"].insert_one({
        "lead_id": lid, "assignment_cycle_id": cid,
        "assigned_to_user_id": str(OWNER),
        "assigned_to_display_name": NAME, "assigned_at": assigned_at,
        "cycle_status": "active", "unassigned_at": None,
        "reason": "inbound_message", "property_code": property_code,
    })
    return lid


def test_mixed_string_and_bson_dates_are_checked_and_never_reassigned():
    db = _db()
    older = datetime(2026, 9, 12, tzinfo=timezone.utc)
    ids = [
        _add(db, 1, assigned_at="2026-02-12T14:20:00-03:00", source="prop360"),
        _add(db, 2, assigned_at=older, source="manual"),
        _add(db, 3, assigned_at="2026-10-01T10:22:00Z", source="chatbot"),
    ]
    recent = _add(db, 4, assigned_at=datetime(2026, 11, 15, tzinfo=timezone.utc))
    cycles_before = list(db["crm_assignment_cycles"].find())
    report = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE)
    assert (report["status"], report["ready"], report["review_required"]) == ("DRY_RUN", 3, 0)
    assert report["inspected"] == 3
    assert report["modified"] == 0
    assert all("assigned_to_user_id" not in lead.get("lifecycle", {})
               for lead in db["leads"].find())

    denied = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE,
                             execute=True, expected_ready=3,
                             expected_review=0, expected_digest="0" * 64)
    assert denied["status"] == "BLOCKED"
    assert denied["modified"] == 0

    ok = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE,
                          execute=True, expected_ready=3, expected_review=0,
                          expected_digest=report["digest"])
    assert (ok["status"], ok["modified"]) == ("COMPLETED", 3)
    assert list(db["crm_assignment_cycles"].find()) == cycles_before
    for lid in ids:
        lead = db["leads"].find_one({"_id": lid})
        assert lead["lifecycle"]["assigned_to_user_id"] == str(OWNER)
        assert lead["lifecycle"]["current_assignment_cycle_id"].startswith("historical-cycle-")
        assert lead["messages"] == [
            {"role": "user", "content": "Historical chat must remain unchanged"}
        ]
        assert lead["contact_restrictions"]["marketing_opt_out"] is True
    assert "assigned_to_user_id" not in db["leads"].find_one({"_id": recent})["lifecycle"]

    again = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE)
    assert (again["ready"], again["already_correct"], again["review_required"]) == (0, 3, 0)


def test_reviewed_duplicate_owner_and_property_conflicts_are_left_untouched():
    db = _db()
    old = "2026-06-05T16:30:00-04:00"
    safe = _add(db, 5, assigned_at=old)
    duplicate = _add(db, 6, assigned_at=old)
    wrong_owner = _add(db, 7, assigned_at=old)
    wrong_property = _add(db, 8, assigned_at=old)
    original_duplicate = db["leads"].find_one({"_id": duplicate})
    db["leads"].insert_one({
        "_id": ObjectId(), "phone": original_duplicate["phone"],
        "messages": [{"role": "user", "content": "Do not copy"}],
    })
    db["leads"].update_one({"_id": wrong_owner}, {
        "$set": {"lifecycle.assigned_to_user_id": str(ObjectId())}
    })
    db["leads"].update_one({"_id": wrong_property}, {
        "$set": {"prospecto.codigo": "DIFFERENT_PROPERTY"}
    })
    before = {str(x["_id"]): x for x in db["leads"].find()}
    scan = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE)
    assert scan["status"] == "DRY_RUN"
    assert (scan["ready"], scan["review_required"]) == (1, 3)
    assert {x.get("reason") for x in scan["leads"] if x["status"] == "review_required"} == {
        "ambiguous_phone_identity", "owner_or_cycle_conflict",
        "assignment_integrity_conflict",
    }

    denied = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE,
                              execute=True, expected_ready=1, expected_review=3,
                              expected_digest=scan["digest"])
    assert denied["status"] == "BLOCKED"
    assert {str(x["_id"]): x for x in db["leads"].find()} == before

    allowed = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE,
                               execute=True, expected_ready=1, expected_review=3,
                               expected_digest=scan["digest"], skip_reviewed=True)
    assert (allowed["status"], allowed["modified"], allowed["review_skipped"]) == (
        "COMPLETED", 1, 3,
    )
    after = {str(x["_id"]): x for x in db["leads"].find()}
    for lid in (duplicate, wrong_owner, wrong_property):
        assert after[str(lid)] == before[str(lid)]
    assert after[str(safe)]["lifecycle"]["assigned_to_user_id"] == str(OWNER)


def test_duplicate_active_cycle_and_inactive_owner_block_modifications():
    db = _db()
    lid = _add(db, 9, assigned_at="2026-09-15T18:00:00Z")
    db["crm_assignment_cycles"].insert_one({
        "lead_id": lid, "assignment_cycle_id": "conflicting-active-cycle",
        "assigned_to_user_id": str(OWNER),
        "assigned_to_display_name": NAME, "assigned_at": "2026-09-16T18:00:00Z",
        "cycle_status": "active", "unassigned_at": None,
        "reason": "inbound_message", "property_code": "16479",
    })
    scan = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE)
    assert scan["ready"] == 0
    assert scan["review_required"] == 2
    db["usuarios"].update_one({"_id": OWNER}, {"$set": {"is_active": False}})
    blocked = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE)
    assert blocked["status"] == "BLOCKED"
    assert blocked["reason"] == "owner_inactive_or_missing"


def test_cutoff_and_missing_date_fail_closed_without_phone_content():
    assert _utc_cutoff("2026-10-20T00:00:00Z") == BEFORE
    for bad in ("2026-10-20", "invalid"):
        try:
            _utc_cutoff(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("naive or malformed cutoff should fail")

    db = _db()
    _add(db, 10, assigned_at=None)
    report = audit_and_repair(db, owner_id=str(OWNER), before=BEFORE)
    assert report["ready"] == 0
    assert report["review_required"] == 1
    assert report["leads"][0]["reason"] == "unparseable_assignment_date"
    assert "Historical chat" not in str(report)
