from datetime import datetime, timedelta, timezone

import mongomock
from bson import ObjectId

from chatbot.crm_current_owner_mirror_repair import (
    inspect_current_owner_mirror_conflicts,
    run_current_owner_mirror_repair_once,
    validate_current_owner_access_integrity,
)
from chatbot.crm_lead_access import resolve_crm_lead_access_context


HERNAN_ID = "6a681413140190dde11f26d1"
MARIELA_ID = "6989c6309dd2ba54e478196c"
HERNAN_NAME = "Hernán Castro"
MARIELA_NAME = "Mariela Arriagada"
NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)


def _cycle(lead_id, cycle_id="cycle-a", owner_id=HERNAN_ID, owner_name=HERNAN_NAME, **extra):
    value = {
        "assignment_cycle_id": cycle_id,
        "lead_id": lead_id,
        "assigned_to_user_id": owner_id,
        "assigned_to_display_name": owner_name,
        "assigned_at": NOW - timedelta(hours=2),
        "sla_started_at": NOW - timedelta(hours=2),
        "cycle_status": "active",
        "unassigned_at": None,
        "reassignment_state": "active",
        "reassignment_decision_id": "previous-decision",
    }
    value.update(extra)
    return value


def _lead(lead_id, cycle_id="cycle-a", owner_id=HERNAN_ID, owner_name=HERNAN_NAME, **extra):
    value = {
        "_id": lead_id,
        "ejecutivo_asignado": owner_name,
        "prospecto": {"ejecutivo": owner_name},
        "lifecycle": {
            "current_assignment_cycle_id": cycle_id,
            "assigned_to_user_id": owner_id,
            "assigned_to_display_name": owner_name,
            "assigned_at": NOW - timedelta(hours=2),
            "sla_started_at": NOW - timedelta(hours=2),
        },
        "reassignment_decision_id": "previous-decision",
        "automatic_reassignment_number": 1,
        "last_crm_update": NOW - timedelta(days=1),
    }
    value.update(extra)
    return value


def _db():
    return mongomock.MongoClient()["crm"]


def _seed(db, lead, cycle, *, user=True):
    db["leads"].insert_one(lead)
    db["crm_assignment_cycles"].insert_one(cycle)
    if user:
        db["usuarios"].insert_one({"_id": cycle["assigned_to_user_id"], "nombre": cycle["assigned_to_display_name"], "rol": "agente"})


def test_correct_current_cycle_and_mirrors_are_noop():
    db = _db()
    lead_id = ObjectId()
    _seed(db, _lead(lead_id), _cycle(lead_id))

    result = run_current_owner_mirror_repair_once(db, now=NOW)

    assert result["writes"] == 0
    assert result["true_conflicts_before"] == 0


def test_stale_lifecycle_display_is_repaired_without_touching_sla_or_last_update():
    db = _db()
    lead_id = ObjectId()
    lead = _lead(lead_id)
    lead["lifecycle"]["assigned_to_display_name"] = MARIELA_NAME
    original = db["leads"].insert_one(lead).inserted_id
    db["crm_assignment_cycles"].insert_one(_cycle(lead_id))

    result = run_current_owner_mirror_repair_once(db, now=NOW)
    repaired = db["leads"].find_one({"_id": original})

    assert result["repaired"] == 1
    assert repaired["lifecycle"]["assigned_to_display_name"] == HERNAN_NAME
    assert repaired["lifecycle"]["assigned_at"].replace(tzinfo=timezone.utc) == lead["lifecycle"]["assigned_at"]
    assert repaired["lifecycle"]["sla_started_at"].replace(tzinfo=timezone.utc) == lead["lifecycle"]["sla_started_at"]
    assert repaired["last_crm_update"].replace(tzinfo=timezone.utc) == lead["last_crm_update"]
    assert repaired["assignment_mirror_source"] == "crm_assignment_cycles"


def test_stale_lifecycle_owner_id_and_display_are_repaired():
    db = _db()
    lead_id = ObjectId()
    lead = _lead(lead_id)
    lead["lifecycle"]["assigned_to_user_id"] = MARIELA_ID
    lead["lifecycle"]["assigned_to_display_name"] = MARIELA_NAME
    _seed(db, lead, _cycle(lead_id))

    result = run_current_owner_mirror_repair_once(db, now=NOW)
    repaired = db["leads"].find_one({"_id": lead_id})

    assert result["repaired"] == 1
    assert repaired["lifecycle"]["assigned_to_user_id"] == HERNAN_ID
    assert repaired["lifecycle"]["assigned_to_display_name"] == HERNAN_NAME
    assert repaired["ejecutivo_asignado"] == HERNAN_NAME
    assert repaired["prospecto"]["ejecutivo"] == HERNAN_NAME
    assert repaired["assignment_mirror_owner_user_id"] == HERNAN_ID


def test_two_active_cycles_and_pointer_mismatch_are_skipped():
    db = _db()
    lead_id = ObjectId()
    _seed(db, _lead(lead_id, cycle_id="cycle-a"), _cycle(lead_id, cycle_id="cycle-a"))
    db["crm_assignment_cycles"].insert_one(_cycle(lead_id, cycle_id="cycle-b"))
    result = run_current_owner_mirror_repair_once(db, now=NOW)
    assert result["repaired"] == 0
    assert result["skipped_ambiguous"] == 1

    db = _db()
    lead_id = ObjectId()
    _seed(db, _lead(lead_id, cycle_id="different"), _cycle(lead_id, cycle_id="cycle-a"))
    result = run_current_owner_mirror_repair_once(db, now=NOW)
    assert result["repaired"] == 0
    assert result["skipped_ambiguous"] == 1


def test_explicit_canonical_owner_conflict_is_skipped():
    db = _db()
    lead_id = ObjectId()
    lead = _lead(lead_id, owner_id=MARIELA_ID, owner_name=MARIELA_NAME)
    lead["owner_user_id"] = MARIELA_ID
    _seed(db, lead, _cycle(lead_id))

    result = run_current_owner_mirror_repair_once(db, now=NOW)

    assert result["repaired"] == 0
    assert result["skipped_ambiguous"] == 1


def test_concurrent_pointer_change_is_cas_lost(monkeypatch):
    db = _db()
    lead_id = ObjectId()
    lead = _lead(lead_id)
    lead["lifecycle"]["assigned_to_display_name"] = MARIELA_NAME
    _seed(db, lead, _cycle(lead_id))

    import chatbot.crm_current_owner_mirror_repair as repair
    monkeypatch.setattr(repair, "_active_cycles_for_lead", lambda *_args: [])
    result = repair.run_current_owner_mirror_repair_once(db, now=NOW)

    assert result["repaired"] == 0
    assert result["cas_lost"] == 1
    assert db["leads"].find_one({"_id": lead_id})["lifecycle"]["assigned_to_display_name"] == MARIELA_NAME


def test_repair_is_idempotent_on_second_run():
    db = _db()
    lead_id = ObjectId()
    lead = _lead(lead_id)
    lead["lifecycle"]["assigned_to_display_name"] = MARIELA_NAME
    _seed(db, lead, _cycle(lead_id))

    first = run_current_owner_mirror_repair_once(db, now=NOW)
    second = run_current_owner_mirror_repair_once(db, now=NOW)

    assert first["writes"] == 1
    assert second["writes"] == 0
    assert second["true_conflicts_before"] == 0


def test_5695_shape_repairs_and_current_owner_access_is_allowed():
    db = _db()
    lead_id = ObjectId("6aaa8562da2187c900af1db9")
    cycle_id = "policy-repair-cycle:sla-reassignment:e60133249e935f8523f56cf7d4969f76e94f929095cf670700a2b061d84d5b9b"
    lead = _lead(lead_id, cycle_id=cycle_id)
    lead["lifecycle"]["assigned_to_display_name"] = MARIELA_NAME
    cycle = _cycle(lead_id, cycle_id=cycle_id)
    _seed(db, lead, cycle)

    result = run_current_owner_mirror_repair_once(db, now=NOW)
    access = validate_current_owner_access_integrity(db)
    user = db["usuarios"].find_one({"_id": HERNAN_ID})
    context = resolve_crm_lead_access_context(db, user=user, lead=db["leads"].find_one({"_id": lead_id}), security_enabled=True)

    assert result["repaired"] == 1
    assert access["evaluated_current_owners"] == 1
    assert access["access_allowed"] == 1
    assert access["access_denied"] == 0
    assert context.access_allowed is True
    assert context.http_status == 200
    assert context.lock_reason is None
    assert context.is_current_owner is True


def test_current_owner_after_expiry_and_late_management_remains_allowed():
    db = _db()
    lead_id = ObjectId()
    lead = _lead(lead_id)
    lead["lifecycle"]["first_valid_management_at"] = NOW
    cycle = _cycle(lead_id, sla_started_at=NOW - timedelta(days=1))
    _seed(db, lead, cycle)

    user = db["usuarios"].find_one({"_id": HERNAN_ID})
    context = resolve_crm_lead_access_context(db, user=user, lead=lead, security_enabled=True)

    assert context.access_allowed is True
    assert context.http_status == 200
    assert context.lock_reason is None


def test_old_owner_after_committed_reassignment_is_locked():
    db = _db()
    lead_id = ObjectId()
    cycle = _cycle(lead_id)
    lead = _lead(lead_id)
    _seed(db, lead, cycle)
    db["usuarios"].insert_one({"_id": MARIELA_ID, "nombre": MARIELA_NAME, "rol": "agente"})

    old_owner = db["usuarios"].find_one({"_id": MARIELA_ID})
    context = resolve_crm_lead_access_context(db, user=old_owner, lead=lead, security_enabled=True)

    assert context.access_allowed is False
    assert context.http_status == 409
    assert context.lock_reason == "LEAD_REASSIGNED_SLA_LOCKED"


def test_inspection_counts_only_real_existing_mirror_conflicts():
    db = _db()
    lead_id = ObjectId()
    lead = _lead(lead_id)
    lead["lifecycle"].pop("assigned_to_display_name")
    lead.pop("assignment_mirror_owner_user_id", None)
    _seed(db, lead, _cycle(lead_id))

    result = inspect_current_owner_mirror_conflicts(db)

    assert result["true_conflicts_before"] == 0
    assert result["conflict_lead_ids"] == []
