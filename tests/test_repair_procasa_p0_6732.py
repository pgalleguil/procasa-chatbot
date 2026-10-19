from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from maintenance import repair_procasa_p0_6732 as repair


class _Cursor(list):
    def limit(self, count):
        return _Cursor(self[:count])


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find_one(self, _query, **_options):
        return deepcopy(self.rows[0]) if self.rows else None

    def find(self, _query, **_options):
        return _Cursor(deepcopy(self.rows))


class _Database(dict):
    def __getitem__(self, name):
        return super().__getitem__(name)


def _reviewed_alert():
    return {
        "_id": repair.MARIA_BREACH_ALERT_ID,
        "assignment_cycle_id": repair.MARIA_CYCLE_ID,
        "recipient_user_id": repair.MARIA_USER_ID,
        "notification_type": "SLA_BREACH_WARNING",
        "state": "sent",
        "delivery_outcome": "confirmed",
        "provider_message_id": repair.MARIA_BREACH_ALERT_PROVIDER_ID,
        "attempt_count": 1,
        "last_error": None,
        "created_at": repair.MARIA_BREACH_ALERT_CREATED_AT,
        "delivery_started_at": repair.MARIA_BREACH_ALERT_STARTED_AT,
        "delivery_completed_at": repair.MARIA_BREACH_ALERT_COMPLETED_AT,
        "sent_at": repair.MARIA_BREACH_ALERT_COMPLETED_AT,
        "updated_at": repair.MARIA_BREACH_ALERT_COMPLETED_AT,
    }


def _fake_db():
    lead = {
        "_id": ObjectId(repair.LEAD_OID),
        "phone": "+56911112222",
        "ejecutivo_asignado": repair.MARIA_NAME,
        "prospecto": {"codigo": "6732", "ejecutivo": repair.MARIA_NAME},
    }
    return _Database({
        "leads": _Collection([lead]),
        "crm_assignment_cycles": _Collection([
            {"assignment_cycle_id": repair.HERNAN_CYCLE_ID, "lead_id": lead["_id"]},
            {"assignment_cycle_id": repair.MARIA_CYCLE_ID, "lead_id": lead["_id"]},
        ]),
        "crm_management_results": _Collection([
            {"_id": key, "lead_id": lead["_id"]} for key in repair.MANAGEMENT_RECORDS
        ]),
        "crm_events": _Collection([{"_id": "event-1", "lead_id": lead["_id"]}]),
        "crm_notifications_v1": _Collection([{"_id": "notification-1", "lead_id": lead["_id"]}]),
        "lead_ingest_events": _Collection([{"_id": "ingest-1", "lead_id": lead["_id"], "property_code": "6732"}]),
        "crm_assignment_integrity_alerts": _Collection([{"_id": "alert-1", "lead_id": lead["_id"]}]),
        "crm_sla_reassignment_audit_v1": _Collection([{"_id": "audit-1", "lead_id": lead["_id"]}]),
        "crm_sla_alerts_v1": _Collection([_reviewed_alert()]),
        "pending_notifications": _Collection([{"_id": "pending-1", "phone": lead["phone"]}]),
    })


def test_capture_backup_records_only_reviewed_6732_collections():
    backup = repair._build_backup(_fake_db())

    assert backup["format"] == repair.BACKUP_FORMAT
    assert backup["database"] == "URLS"
    assert str(backup["lead_id"]) == repair.LEAD_OID
    assert set(backup["collections"]) == set(repair.BACKUP_COLLECTIONS)
    assert backup["collections"]["cycles"]["collection"] == "crm_assignment_cycles"
    assert len(backup["collections"]["cycles"]["documents"]) == 2
    assert len(backup["collections"]["management_results"]["documents"]) == 2
    assert backup["collections"]["sla_alerts"]["documents"] == [_reviewed_alert()]


def test_capture_backup_write_is_checksumming_and_refuses_overwrite(tmp_path):
    path = tmp_path / "6732-backup.json"
    backup = repair._build_backup(_fake_db())

    checksum = repair._write_backup(path, backup)

    assert len(checksum) == 64
    assert repair.load_backup(path)["format"] == repair.BACKUP_FORMAT
    with pytest.raises(repair.RepairSafetyError, match="already exists"):
        repair._write_backup(path, backup)


def test_capture_mode_requires_a_new_backup_output_path():
    with pytest.raises(SystemExit):
        repair.parse_args(["--capture-backup", "--actor", "Pablo Galleguillos"])

    args = repair.parse_args([
        "--capture-backup", "--backup-out", "/tmp/6732.json", "--actor", "Pablo Galleguillos",
    ])
    assert args.capture_backup is True
    assert args.backup_out.name == "6732.json"
    assert args.backup is None


def test_visit_preference_evidence_uses_the_original_inbound_event_id():
    assert repair.VISIT_ACCEPTANCE_EVENT_ID == "6ad613bce66c461dd69d69aa"
    assert repair.VISIT_ACCEPTANCE_AT.isoformat() == "2026-10-19T12:57:32.037000+00:00"


def test_reviewed_provider_accepted_false_breach_is_audited_without_rewriting_delivery():
    alert = _reviewed_alert()

    assert repair._validate_reviewed_sla_alerts([alert]) == alert
    update = repair._sla_alert_correction_fields(
        alert, actor="Pablo Galleguillos", now=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)
    )

    correction = update["administrative_correction"]
    assert update["administratively_corrected"] is True
    assert update["sla_performance_excluded"] is True
    assert correction["status"] == "corrected_false_sla_breach"
    assert correction["provider_state_preserved"] == "sent"
    assert correction["provider_outcome_preserved"] == "confirmed"
    assert correction["provider_message_id_preserved"] == "89720542"
    assert correction["provider_acceptance_confirmed"] is True
    assert correction["provider_delivery_receipt_confirmed"] is False
    assert correction["penalty_excluded"] is True
    assert correction["resend"] is False
    assert not {"state", "sent_at", "delivery_started_at", "delivery_completed_at", "delivery_outcome"} & set(update)


@pytest.mark.parametrize("change", [
    {"state": "delivery_unknown"},
    {"provider_message_id": "different-provider-id"},
    {"delivery_outcome": "unknown"},
    {"assignment_cycle_id": repair.HERNAN_CYCLE_ID},
    {"recipient_user_id": repair.HERNAN_USER_ID},
    {"delivered_at": datetime(2026, 10, 19, 18, 31, tzinfo=timezone.utc)},
])
def test_false_breach_alert_preflight_rejects_unreviewed_delivery_evidence(change):
    alert = {**_reviewed_alert(), **change}

    with pytest.raises(repair.RepairSafetyError):
        repair._validate_reviewed_sla_alerts([alert])


def test_false_breach_alert_preflight_rejects_additional_alerts():
    with pytest.raises(repair.RepairSafetyError, match="exactly one"):
        repair._validate_reviewed_sla_alerts([_reviewed_alert(), _reviewed_alert()])


def test_alert_concurrency_filter_pins_provider_state_and_no_existing_correction():
    selector = repair._sla_alert_concurrency_filter(_reviewed_alert())

    assert selector["_id"] == repair.MARIA_BREACH_ALERT_ID
    assert selector["state"] == "sent"
    assert selector["provider_message_id"] == "89720542"
    assert selector["delivery_outcome"] == "confirmed"
    assert selector["delivery_completed_at"] == repair.MARIA_BREACH_ALERT_COMPLETED_AT
    assert selector["administrative_correction"] == {"$exists": False}
