from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from chatbot.crm_metrics import calculate_sla

from chatbot.crm_admin_controls import (
    AdminControlError,
    admin_reassign_lead,
    extend_active_sla,
    protect_active_assignment,
)


def _value(document, path):
    current = document
    for part in path.split("."):
        current = current.get(part) if isinstance(current, dict) else None
    return current


def _matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, branch) for branch in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(document, branch) for branch in expected):
                return False
            continue
        actual = _value(document, key)
        if isinstance(expected, dict):
            for op, value in expected.items():
                if op == "$in" and actual not in value:
                    return False
                if op == "$ne" and actual == value:
                    return False
                if op == "$exists" and ((actual is not None) != bool(value)):
                    return False
        elif actual != expected:
            return False
    return True


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class _Collection:
    def __init__(self, rows=()):
        self.rows = [deepcopy(row) for row in rows]

    def find_one(self, query, *args, **kwargs):
        return next((row for row in self.rows if _matches(row, query)), None)

    def find(self, query, *args, **kwargs):
        return _Cursor([row for row in self.rows if _matches(row, query)])

    def update_one(self, query, update, *args, **kwargs):
        row = next((row for row in self.rows if _matches(row, query)), None)
        if row is None:
            return SimpleNamespace(matched_count=0, modified_count=0)
        for key, value in update.get("$set", {}).items():
            target = row
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = deepcopy(value)
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        for key, value in update.get("$push", {}).items():
            row.setdefault(key, []).append(deepcopy(value))
        return SimpleNamespace(matched_count=1, modified_count=1)

    def insert_one(self, document, *args, **kwargs):
        if self.find_one({"_id": document.get("_id")}):
            raise ValueError("duplicate _id")
        self.rows.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document.get("_id"))


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def with_transaction(self, callback):
        return callback(self)


class _Client:
    def start_session(self):
        return _Session()


class _Db(dict):
    client = _Client()


def _db():
    return _Db({
        "leads": _Collection([{
            "_id": "lead-1",
            "oficina_id": 7,
            "ejecutivo_asignado": "Hernán Castro",
            "prospecto": {"ejecutivo": "Hernán Castro"},
            "lifecycle": {"current_assignment_cycle_id": "cycle-2"},
        }]),
        "crm_assignment_cycles": _Collection([{
            "_id": "cycle-doc-2",
            "lead_id": "lead-1",
            "assignment_cycle_id": "cycle-2",
            "schema_version": "crm_assignment_cycle_v1",
            "cycle_status": "active",
            "unassigned_at": None,
            "assigned_to_user_id": "hernan-id",
            "assigned_to_display_name": "Hernán Castro",
            "assigned_at": datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc),
            "cycle_version": 2,
            "previous_owner_user_ids": ["mariela-id"],
            "temperature_at_assignment": "HOT",
        }]),
        "usuarios": _Collection([{
            "_id": "mariela-id", "nombre": "Mariela Arriagada",
            "rol": "agente", "is_active": True, "office_id": 7,
        }]),
        "crm_management_results": _Collection(),
        "crm_events": _Collection(),
        "crm_assignment_admin_events": _Collection(),
    })


def test_return_previous_is_exact_lead_silent_and_idempotent():
    db = _db()
    kwargs = {
        "lead_id": "lead-1",
        "return_to_previous": True,
        "actor": {"_id": "supervisor-id", "rol": "supervisor", "office_id": 7},
        "reason": "Devolución autorizada",
        "action_id": "return-1",
        "now": datetime(2026, 10, 20, 14, 30, tzinfo=timezone.utc),
    }
    result = admin_reassign_lead(db, **kwargs)
    assert result["status"] == "ok"
    assert result["executive"] == "Mariela Arriagada"
    lead = db["leads"].rows[0]
    assert lead["lifecycle"]["current_assignment_cycle_id"] == "admin:return-1"
    assert lead["ejecutivo_asignado"] == "Mariela Arriagada"
    cycles = db["crm_assignment_cycles"].rows
    assert sum(row["cycle_status"] == "active" for row in cycles) == 1
    event = db["crm_assignment_admin_events"].rows[0]
    assert event["silent"] is True
    assert event["target_display_name"] == "Mariela Arriagada"
    replay = admin_reassign_lead(db, **kwargs)
    assert replay["status"] == "already_applied"
    assert len(db["crm_assignment_admin_events"].rows) == 1


def test_sla_extension_keeps_old_breach_and_is_idempotent():
    db = _db()
    old_breach = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    cycle = db["crm_assignment_cycles"].rows[0]
    cycle["sla_breached_at"] = old_breach
    kwargs = {
        "lead_id": "lead-1",
        "assignment_cycle_id": "cycle-2",
        "minutes": 120,
        "actor": {"_id": "supervisor-id", "rol": "supervisor", "office_id": 7},
        "reason": "Extensión solicitada por supervisión",
        "action_id": "extend-1",
        "now": datetime(2026, 10, 20, 15, 1, tzinfo=timezone.utc),
    }
    result = extend_active_sla(db, **kwargs)
    assert result == {"status": "ok", "total_extension_minutes": 120, "added_minutes": 120, "silent": True}
    cycle = db["crm_assignment_cycles"].rows[0]
    assert cycle["sla_extension_minutes"] == 120
    assert cycle["sla_breached_at"] == old_breach
    replay = extend_active_sla(db, **kwargs)
    assert replay["status"] == "already_applied"
    assert cycle["sla_extension_minutes"] == 120


def test_sla_extension_rejects_stale_assignment_cycle():
    db = _db()
    with pytest.raises(AdminControlError, match="cycle_changed"):
        extend_active_sla(
            db, lead_id="lead-1", assignment_cycle_id="old-cycle", minutes=120,
            actor={"_id": "supervisor-id", "rol": "supervisor", "office_id": 7},
            reason="Motivo", action_id="stale-1",
        )


def test_non_supervisor_cannot_call_transaction_service():
    db = _db()
    with pytest.raises(AdminControlError, match="supervisor_only"):
        admin_reassign_lead(
            db, lead_id="lead-1", target_user_id="mariela-id",
            actor={"_id": "admin-id", "rol": "admin", "office_id": 7},
            reason="Cambio", action_id="admin-denied",
        )


def test_supervisor_cannot_act_outside_office_scope():
    db = _db()
    with pytest.raises(AdminControlError, match="office_scope_forbidden"):
        extend_active_sla(
            db, lead_id="lead-1", assignment_cycle_id="cycle-2", minutes=120,
            actor={"_id": "supervisor-id", "rol": "supervisor", "office_id": 8},
            reason="Ampliación", action_id="wrong-office",
        )


def test_protection_is_finite_silent_and_idempotent():
    db = _db()
    args = {
        "lead_id": "lead-1", "assignment_cycle_id": "cycle-2", "hold_hours": 24,
        "actor": {"_id": "supervisor-id", "rol": "supervisor", "office_id": 7},
        "reason": "Emergencia del ejecutivo", "action_id": "protect-1",
        "now": datetime(2026, 10, 20, 14, 30, tzinfo=timezone.utc),
    }
    result = protect_active_assignment(db, **args)
    assert result["status"] == "ok" and result["silent"] is True
    cycle = db["crm_assignment_cycles"].rows[0]
    assert cycle["administrative_hold_until"] == datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)
    assert cycle["administrative_assignment_mode"] == "HOLD_UNTIL"
    assert protect_active_assignment(db, **args)["status"] == "already_applied"
    assert len(db["crm_assignment_admin_events"].rows) == 1


def test_supervisor_without_office_scope_is_denied():
    db = _db()
    with pytest.raises(AdminControlError, match="supervisor_office_scope_missing"):
        extend_active_sla(
            db, lead_id="lead-1", assignment_cycle_id="cycle-2", minutes=120,
            actor={"_id": "supervisor-id", "rol": "supervisor"},
            reason="Ampliación", action_id="scope-missing",
        )


def test_sla_extension_rejects_a_lead_with_management_evidence():
    db = _db()
    db["crm_management_results"].rows.append({
        "lead_id": "lead-1", "assignment_cycle_id": "cycle-2",
    })
    with pytest.raises(AdminControlError, match="sla_already_completed"):
        extend_active_sla(
            db, lead_id="lead-1", assignment_cycle_id="cycle-2", minutes=120,
            actor={"_id": "supervisor-id", "rol": "supervisor", "office_id": 7},
            reason="Ampliación", action_id="managed-lead",
        )


def test_sla_extension_moves_deadline_without_replacing_base_threshold():
    start = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
    result = calculate_sla(
        assigned_at=start,
        now=datetime(2026, 10, 20, 17, 20, tzinfo=timezone.utc),
        temperature="NORMAL",
        extension_minutes=120,
    )
    assert result["status"] == "good"
    assert result["threshold_minutes"] == 300
    assert result["base_threshold_minutes"] == 180
    assert result["sla_extension_minutes"] == 120


def test_sla_hot_extension_shifts_hot_thresholds():
    start = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
    result = calculate_sla(
        assigned_at=start,
        now=datetime(2026, 10, 20, 16, 50, tzinfo=timezone.utc),
        temperature="HOT",
        hot_started_at=start,
        extension_minutes=120,
    )
    assert result["threshold_minutes"] == 180
    assert result["base_threshold_minutes"] == 60
    assert result["status"] == "near_critical"
