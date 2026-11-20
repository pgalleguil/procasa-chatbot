"""Local owner mirror recovery: defaults read-only and is safe to repeat."""
from datetime import datetime, timezone

from bson import ObjectId
import mongomock

from scripts.repair_manual_cycle_mirrors import _since_utc, audit_and_repair

OWNER = ObjectId("6989c6309dd2ba54e478196c")
NAME = "Mariela Arriagada"
SINCE = datetime(2026, 11, 19, tzinfo=timezone.utc)


def _make_db(n=5):
    db = mongomock.MongoClient()["URLS"]
    db["usuarios"].insert_one({
        "_id": OWNER, "nombre": NAME, "rol": "agente", "is_active": True,
    })
    for i in range(n):
        lid = ObjectId(f"6aff00000000000000000{i+1:03d}")
        phone = "+5691234" + str(1000 + i)
        cycle_id = f"cycle-manual-{i}"
        db["leads"].insert_one({
            "_id": lid, "phone": phone, "source_type": "manual",
            "ejecutivo_asignado": NAME,
            "prospecto": {"ejecutivo": NAME, "codigo": "17252"},
            "lifecycle": {"created_at": "2026-11-19T12:00:00-03:00"},
            "messages": [{"role": "system", "content": "Ingreso manual"}],
            "contact_restrictions": {"marketing_opt_out": False},
        })
        db["crm_assignment_cycles"].insert_one({
            "lead_id": lid, "assignment_cycle_id": cycle_id,
            "assigned_to_user_id": str(OWNER),
            "assigned_to_display_name": NAME,
            "assigned_at": datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc),
            "cycle_status": "active", "unassigned_at": None,
            "reason": "manual_lead_created", "property_code": "17252",
        })
    return db


def test_safe_dry_run_does_not_mutate_anything():
    db = _make_db(5)
    result = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE,
    )
    assert result["status"] == "DRY_RUN"
    assert result["ready"] == 5
    assert result["modified"] == 0
    assert result["review_required"] == 0
    for lead in db["leads"].find():
        assert "current_assignment_cycle_id" not in lead["lifecycle"]
        assert len(lead["messages"]) == 1


def test_execute_requires_exact_expected_count_and_verifies_auditable_lead():
    db = _make_db(5)
    blocked = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE,
        execute=True, expected_ready=4,
    )
    assert blocked["status"] == "BLOCKED"
    assert blocked["modified"] == 0
    ok = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE,
        execute=True, expected_ready=5,
    )
    assert ok["status"] == "COMPLETED"
    assert ok["modified"] == 5
    assert all(i["status"] == "repaired_and_verified" for i in ok["leads"])
    assert db["crm_assignment_cycles"].count_documents({}) == 5
    for lead in db["leads"].find():
        assert lead["lifecycle"]["assigned_to_user_id"] == str(OWNER)
        assert lead["lifecycle"]["current_assignment_cycle_id"].startswith("cycle-manual-")
        assert lead["messages"] == [{"role": "system", "content": "Ingreso manual"}]
        assert lead["contact_restrictions"]["marketing_opt_out"] is False
    repeated = audit_and_repair(db, owner_id=str(OWNER), since=SINCE)
    assert repeated["status"] == "DRY_RUN"
    assert repeated["ready"] == 0
    assert repeated["already_correct"] == 5


def test_ambiguous_lead_blocks_entire_batch_instead_of_lifting_privacy_fence():
    db = _make_db(5)
    lead = db["leads"].find_one()
    db["leads"].insert_one({
        "_id": ObjectId(), "phone": lead["phone"], "messages": [],
    })
    result = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE,
        execute=True, expected_ready=5,
    )
    assert result["status"] == "BLOCKED"
    assert result["review_required"] == 1
    assert result["modified"] == 0


def test_existing_owner_conflict_or_closed_cycle_fails_closed():
    db = _make_db(1)
    lid = db["leads"].find_one()["_id"]
    db["leads"].update_one(
        {"_id": lid}, {"$set": {"lifecycle.current_assignment_cycle_id": "wrong"}}
    )
    assert audit_and_repair(db, owner_id=str(OWNER), since=SINCE)["review_required"] == 1

    db = _make_db(1)
    db["crm_assignment_cycles"].update_one({}, {"$set": {"cycle_status": "closed"}})
    result = audit_and_repair(db, owner_id=str(OWNER), since=SINCE)
    assert result["inspected"] == 0
    assert result["ready"] == 0


def test_chile_local_date_is_converted_to_utc_correctly():
    assert _since_utc("2026-11-19").isoformat() == "2026-11-19T03:00:00+00:00"


def test_skipping_reviewed_requires_exact_safe_counts_and_preserves_conflict():
    db = _make_db(5)
    victim = db["leads"].find_one()
    # Deliberately duplicate one lead's number: its history must stay excluded.
    db["leads"].insert_one({
        "_id": ObjectId(), "phone": victim["phone"],
        "messages": [{"role": "user", "content": "private - must remain untouched"}],
    })
    before = {str(x["_id"]): x for x in db["leads"].find()}
    wrong = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE,
        execute=True, expected_ready=4, skip_reviewed=True, expected_review=2,
    )
    assert wrong["status"] == "BLOCKED"
    assert wrong["modified"] == 0
    assert {str(x["_id"]): x for x in db["leads"].find()} == before

    # The existing default gate must STILL block if any review is required.
    blocked = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE,
        execute=True, expected_ready=4,
    )
    assert blocked["status"] == "BLOCKED"

    ok = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE,
        execute=True, expected_ready=4, skip_reviewed=True, expected_review=1,
    )
    assert ok["status"] == "COMPLETED"
    assert ok["modified"] == 4
    assert ok["review_required"] == 1
    assert ok["review_skipped"] == 1
    after = {str(x["_id"]): x for x in db["leads"].find()}
    assert after[str(victim["_id"])] == before[str(victim["_id"])]
    dup = next(x for x in after.values() if x.get("messages") == [
        {"role": "user", "content": "private - must remain untouched"}
    ])
    assert dup.get("lifecycle") is None
    assert db["crm_assignment_cycles"].count_documents({}) == 5
    assert sum(
        1 for lead in after.values()
        if (lead.get("lifecycle") or {}).get("assigned_to_user_id") == str(OWNER)
    ) == 4


def test_skip_reviewed_does_not_turn_unsafe_leads_into_eligible():
    db = _make_db(3)
    docs = list(db["leads"].find())
    # A conflicting explicit owner is not authorized even if the names match.
    db["leads"].update_one({"_id": docs[0]["_id"]}, {
        "$set": {"lifecycle.assigned_to_user_id": "other-user"}
    })
    # A nonmanual source cannot be repaired by this tool.
    db["leads"].update_one({"_id": docs[1]["_id"]}, {
        "$set": {"source_type": "inbound_message"}
    })
    a = audit_and_repair(db, owner_id=str(OWNER), since=SINCE)
    assert (a["ready"], a["review_required"]) == (1, 2)

    r = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE, execute=True,
        expected_ready=1, skip_reviewed=True, expected_review=2,
    )
    assert r["status"] == "COMPLETED"
    assert r["modified"] == 1
    assert db["leads"].find_one({"_id": docs[0]["_id"]})[
        "lifecycle"]["assigned_to_user_id"] == "other-user"
    assert "assigned_to_user_id" not in db["leads"].find_one({
        "_id": docs[1]["_id"]
    })["lifecycle"]


def test_skip_reviewed_requires_explicit_expected_review_even_when_zero():
    db = _make_db(1)
    out = audit_and_repair(
        db, owner_id=str(OWNER), since=SINCE,
        execute=True, expected_ready=1, skip_reviewed=True,
    )
    assert out["status"] == "BLOCKED"
    assert out["modified"] == 0
