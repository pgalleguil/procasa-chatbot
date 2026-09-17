from __future__ import annotations

from copy import deepcopy

from broker_registry import (
    BROKER_IDENTITIES_COLLECTION,
    BROKER_IDENTITY_EVENTS_COLLECTION,
    BROKER_IDENTITY_KEYS_COLLECTION,
    learn_broker_identity,
    learn_broker_identity_from_management,
    resolve_broker_identity,
)
from canonical_classification import canonicalize_classification
from captacion_assignment_eligibility import can_assign_property
from captacion_management import _record_contact_identity_feedback, reconcile_broker_assignment
from captacion_distribution import assign_captacion_candidate_atomically
from owner_probability import apply_owner_probability_to_document


class _Result:
    def __init__(self, modified_count=1, upserted_id=None):
        self.modified_count = modified_count
        self.matched_count = modified_count
        self.upserted_id = upserted_id


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = {}
        self.last_update = None
        for row in rows or []:
            self.rows[row.get("_id")] = deepcopy(row)
        self.update_calls = 0

    def create_index(self, *args, **kwargs):
        return "idx"

    def find_one(self, query, projection=None, **kwargs):
        for row in self.rows.values():
            if _matches(row, query):
                if projection:
                    return {key: row[key] for key, value in projection.items() if value and key in row}
                return deepcopy(row)
        return None

    def find(self, query=None, projection=None, **kwargs):
        query = query or {}
        rows = [row for row in self.rows.values() if _matches(row, query)]
        if projection:
            return [
                {key: row[key] for key, value in projection.items() if value and key in row}
                for row in rows
            ]
        return [deepcopy(row) for row in rows]

    def update_one(self, query, update, upsert=False, **kwargs):
        self.update_calls += 1
        self.last_update = deepcopy(update)
        row = self.find_one(query)
        inserted = False
        if row is None:
            if not upsert:
                return _Result(0)
            row = {key: value for key, value in query.items() if not key.startswith("$")}
            if "_id" not in row and "$setOnInsert" in update:
                row["_id"] = update["$setOnInsert"].get("_id")
            inserted = True
        if inserted:
            row.update(deepcopy(update.get("$setOnInsert") or {}))
        for field, value in (update.get("$set") or {}).items():
            _set_path(row, field, value)
        for field, value in (update.get("$addToSet") or {}).items():
            values = row.setdefault(field, [])
            if isinstance(value, dict) and "$each" in value:
                for item in value["$each"]:
                    if item not in values:
                        values.append(deepcopy(item))
            elif value not in values:
                values.append(deepcopy(value))
        for field, value in (update.get("$push") or {}).items():
            parts = field.split(".")
            target = row
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target.setdefault(parts[-1], []).append(deepcopy(value))
        self.rows[row.get("_id")] = row
        return _Result(1, row.get("_id") if inserted else None)

    def update_many(self, query, update, **kwargs):
        matched = 0
        for row in list(self.rows.values()):
            if _matches(row, query):
                matched += 1
                self.update_one({"_id": row.get("_id")}, update, upsert=False)
        return _Result(matched)

    def aggregate(self, pipeline):
        return []


class FakeDB:
    def __init__(self, rows=None):
        self.collections = {}
        for name, values in (rows or {}).items():
            self.collections[name] = FakeCollection(values)

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())

    def __setitem__(self, name, value):
        self.collections[name] = value


def _matches(row, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(row, item) for item in expected):
                return False
            continue
        if isinstance(expected, dict):
            if "$in" in expected and row.get(key) not in expected["$in"]:
                return False
            if "$ne" in expected and row.get(key) == expected["$ne"]:
                return False
            continue
        if row.get(key) != expected:
            return False
    return True


def _set_path(row, path, value):
    parts = path.split(".")
    target = row
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = deepcopy(value)


def broker_listing(**overrides):
    result = {
        "_id": "p-1",
        "origen": "toctoc",
        "source_portal": "toctoc",
        "listing_id": "4351894",
        "comuna": "Maipu",
        "title": "Casa de prueba",
        "description": "Descripción suficiente para una prueba determinista.",
        "seller_profile_id": "profile-1",
        "seller_client_id": "client-1",
        "publicador_visible": "XYZ Gestión SpA",
        "seller_type": "EMPRESA",
        "seller_type_evidence": "https://www.toctoc.com/corredora/xyz",
        "classification": {
            "state": "DUEÑO_PROBABLE",
            "owner_probability": 0.85,
            "assignment_ready": True,
            "decision_source": "structural_rules",
            "reason": "fixture",
        },
        "pipeline_complete": True,
    }
    result.update(overrides)
    return result


def test_structural_and_operation_signals_are_canonical_broker_vetoes():
    for extra in (
        {"seller_type_evidence": "https://www.toctoc.com/corredora/xyz"},
        {"operation_label_raw": "Venta Usado Corredor"},
    ):
        document = broker_listing(**extra)
        classification = canonicalize_classification(document)
        document["classification"] = classification
        decision = can_assign_property(document)
        assert classification["final"] == "BROKER_CONFIRMED"
        assert decision["assignment_ready"] is False


def test_owner_probability_cannot_revert_structural_broker_veto():
    document = broker_listing(
        title="Casa del propietario",
        description="Soy el dueño y no pago comisión.",
        classification={
            "state": "DUEÑO_PROBABLE",
            "owner_probability": 0.85,
            "decision_source": "structural_rules",
        },
    )
    apply_owner_probability_to_document(document)
    assert document["classification"]["final"] == "BROKER_CONFIRMED"
    assert document["classification"]["state"] == "CORREDOR_SEGURO"
    assert document["classification"]["hard_veto"] == "PROFESSIONAL"


def test_known_registry_match_cannot_become_owner_after_local_scoring():
    db = FakeDB()
    source = broker_listing()
    learn_broker_identity(
        db,
        document=source,
        source="PORTAL_STRUCTURE",
        evidence_type="STRUCTURAL_BROKER",
    )
    candidate = {
        **source,
        "_id": "p-known-again",
        "publicador_visible": "Otro nombre personal",
        "seller_type": "PARTICULAR",
        "seller_type_evidence": "",
        "description": "",
        "classification": {
            "state": "DUEÑO_PROBABLE",
            "owner_probability": 0.95,
            "decision_source": "rules",
        },
    }
    match = resolve_broker_identity(db, candidate)
    apply_owner_probability_to_document(candidate, registry_match=match)
    assert candidate["classification"]["final"] == "BROKER_CONFIRMED"
    assert candidate["classification"]["state"] == "CORREDOR_SEGURO"
    assert candidate["classification"]["assignment_ready"] is False
    assert candidate["classification"]["pipeline_complete"] is True


def test_incomplete_pipeline_is_blocked_before_assignment():
    document = broker_listing(
        classification={
            "state": "DUEÑO_PROBABLE",
            "final": "OWNER_PROBABLE",
            "assignment_ready": True,
        },
        pipeline_state="UNCERTAIN_PENDING_AI",
        pipeline_complete=False,
    )
    decision = can_assign_property(document)
    assert decision["assignment_ready"] is False
    assert "pipeline_incomplete" in decision["assignment_block_reasons"]


def test_exact_registry_keys_match_cross_portal_without_fuzzy_matching():
    db = FakeDB()
    source = broker_listing()
    learned = learn_broker_identity(
        db,
        document=source,
        source="PORTAL_STRUCTURE",
        evidence_type="STRUCTURAL_BROKER",
    )
    assert learned["status"] == "CREATED"
    other_portal = {
        **source,
        "_id": "y-1",
        "origen": "yapo",
        "source_portal": "yapo",
        "seller_profile_id": "different-profile",
        "seller_client_id": "different-client",
    }
    match = resolve_broker_identity(db, other_portal)
    assert match["matched"] is True
    assert match["match_type"] in {"EXACT_PHONE", "EXACT_EMAIL", "EXACT_DOMAIN", "EXACT_CONFIRMED_ALIAS"}


def test_profile_and_client_ids_are_exact_and_portal_scoped():
    db = FakeDB()
    source = broker_listing()
    learn_broker_identity(
        db,
        document=source,
        source="PORTAL_STRUCTURE",
        evidence_type="STRUCTURAL_BROKER",
    )
    same_profile = {**source, "_id": "p-2", "publicador_visible": "Otro nombre"}
    assert resolve_broker_identity(db, same_profile)["matched"] is True
    other_portal = {**same_profile, "origen": "yapo", "source_portal": "yapo"}
    other_portal.pop("phone_normalized", None)
    other_portal.pop("phone", None)
    other_portal.pop("email", None)
    assert resolve_broker_identity(db, other_portal)["matched"] is False


def test_executive_feedback_updates_persistent_registry():
    db = FakeDB()
    result = learn_broker_identity_from_management(
        db,
        property_doc=broker_listing(),
        event={"event_id": "event-1", "actor_user_id": "exec-1", "actor_name_snapshot": "Ejecutivo"},
    )
    assert result["matched"] is True
    assert db[BROKER_IDENTITIES_COLLECTION].rows
    assert db[BROKER_IDENTITY_KEYS_COLLECTION].rows
    assert db[BROKER_IDENTITY_EVENTS_COLLECTION].rows


def test_management_corredor_outcome_automatically_bridges_registry():
    db = FakeDB()
    _record_contact_identity_feedback(
        db,
        broker_listing(),
        {"event_id": "management-1", "actor_user_id": "exec-1", "actor_name_snapshot": "Ejecutivo"},
        "broker_identified",
    )
    assert db[BROKER_IDENTITIES_COLLECTION].rows


def test_person_name_without_evidence_is_not_a_hard_veto():
    document = broker_listing(
        publicador_visible="Juan Pérez",
        seller_type="PARTICULAR",
        seller_type_evidence="",
        classification={
            "state": "DUEÑO_PROBABLE",
            "owner_probability": 0.85,
            "assignment_ready": True,
            "decision_source": "structural_rules",
            "reason": "personal seller",
        },
    )
    document["classification"] = canonicalize_classification(document)
    assert document["classification"]["final"] == "OWNER_PROBABLE"
    assert can_assign_property(document)["assignment_ready"] is True


def test_capacity_override_cannot_bypass_broker_gate():
    document = broker_listing()
    document["classification"] = canonicalize_classification(document)
    decision = can_assign_property(
        document,
        {"capacity_override": True, "max_open_override": True},
    )
    assert decision["assignment_ready"] is False


def test_writer_rejects_broker_before_update():
    document = broker_listing()
    document["classification"] = canonicalize_classification(document)
    coll = FakeCollection([document])
    db = FakeDB()
    db["propiedades_captacion"] = coll
    db["usuarios"] = FakeCollection([{"_id": "agent-1", "is_active": True, "rol": "agente"}])
    events = FakeCollection()
    result = assign_captacion_candidate_atomically(
        db,
        coll,
        events,
        {"_id": "p-1"},
        {"id": "agent-1", "name": "Agente", "comunas_interes_norm": ["maipu"]},
        max_per_agent=2,
    )
    assert result in {"quality", "phone", "identity_conflict"}
    assert coll.update_calls == 0


def test_broker_reclassification_retires_assignment_and_cycle():
    document = broker_listing(
        gestion={
            "ejecutivo_id": "agent-1",
            "ejecutivo_asignado": "Agente",
            "assignment_cycle_id": "cycle-1",
            "estado": "NUEVO",
        }
    )
    properties = FakeCollection([document])
    db = FakeDB()
    db["propiedades_captacion"] = properties
    db["captacion_assignment_cycles"] = FakeCollection([
        {"_id": "cycle-1", "property_id": "p-1", "status": "active"}
    ])
    result = reconcile_broker_assignment(db, document, reason="canonical_broker_veto")
    assert result["assigned_removed"] is True
    assert properties.last_update["$set"]["gestion.ejecutivo_id"] is None
    assert properties.last_update["$set"]["gestion.estado"] == "Corredor"
    second = reconcile_broker_assignment(db, document, reason="canonical_broker_veto")
    assert second["status"] == "already_reconciled"
    assert second["modified_count"] == 0
