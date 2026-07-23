from datetime import datetime, timezone

from chatbot.crm_metrics import (
    build_snapshot_document, calculate_sla, coerce_utc_datetime,
    create_assignment_cycle, event_evidence, normalize_result, pipeline_activity_in_period,
    resolve_canonical_lead, unique_managed_lead_ids, validate_list_parity,
)


class FakeCollection:
    def __init__(self, docs=None): self.docs = list(docs or [])
    def find_one(self, query, sort=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()): return doc
        return None
    def find(self, query, limit=0):
        digits = str(query.get("phone", {}).get("$regex", "")).replace("^\\+?", "").replace("$", "")
        found = [d for d in self.docs if str(d.get("phone", "")).lstrip("+") == digits]
        return found[:limit] if limit else found
    def insert_one(self, doc):
        doc.setdefault("_id", f"id-{len(self.docs)+1}"); self.docs.append(doc)
    def update_one(self, query, update):
        doc = self.find_one(query)
        if doc: doc.update(update.get("$set", {}))


class FakeDB(dict):
    pass


def event(lead_id="lead-1", event_type="CONTACT_RESULT", result="CONTACTADO",
          confirmed=True, actor="Ana"):
    return {"lead_id": lead_id, "type": event_type, "result": result,
            "confirmed": confirmed, "actor": actor, "actor_type": "human", "meta": {}}


def test_multiple_events_count_one_managed_lead():
    events = [event(event_type="HUMAN_NOTE", result=None, confirmed=False) for _ in range(3)]
    events += [event(event_type="HUMAN_NOTE", result="NO_RESPONDIO"), event(event_type="HUMAN_NOTE", result="CONTACTADO")]
    assert unique_managed_lead_ids(events) == {"lead-1"}


def test_opening_apps_does_not_credit_management():
    for kind in ("CLICK_WHATSAPP_LEAD", "CLICK_PHONE_LEAD", "CLICK_EMAIL_LEAD"):
        assert event_evidence(event(event_type=kind, result=None, confirmed=False))["management"] is False


def test_confirmed_result_credits_and_attempt_is_separate_from_effective_contact():
    failed = event_evidence(event(event_type="HUMAN_NOTE", result="NO_RESPONDIO"))
    contacted = event_evidence(event(event_type="HUMAN_NOTE", result="CONTACTADO"))
    assert failed["management"] and failed["contact_attempt"] and not failed["effective_contact"]
    assert contacted["management"] and contacted["contact_attempt"] and contacted["effective_contact"]


def test_unconfirmed_result_does_not_credit():
    assert not event_evidence(event(result="CONTACTADO", confirmed=False))["management"]


def test_event_without_canonical_lead_is_not_credited():
    assert not event_evidence(event(lead_id=None))["management"]


def test_ambiguous_duplicate_phone_is_not_resolved_or_credited():
    db = FakeDB(leads=FakeCollection([
        {"_id": "a", "phone": "+56911111111"}, {"_id": "b", "phone": "+56911111111"},
    ]))
    resolution = resolve_canonical_lead(db, phone="56911111111")
    assert resolution.status == "ambiguous_phone"
    assert resolution.lead is None


def test_reassignment_closes_cycle_without_retroactive_attribution():
    db = FakeDB(crm_assignment_cycles=FakeCollection())
    lead = {"_id": "lead-1"}
    first = create_assignment_cycle(db, lead=lead, assigned_to_user_id="u1",
                                    assigned_by="admin", reason="initial",
                                    assigned_at="2026-07-13T09:00:00-04:00")
    second = create_assignment_cycle(db, lead=lead, assigned_to_user_id="u2",
                                     assigned_by="admin", reason="reassign",
                                     assigned_at="2026-07-14T09:00:00-04:00")
    assert first["assignment_cycle_id"] != second["assignment_cycle_id"]
    assert first["assigned_to_user_id"] == "u1"
    assert first["unassigned_at"] is not None
    assert second["assigned_to_user_id"] == "u2"


def test_bson_and_iso_timestamps_are_compatible_and_new_values_are_utc():
    bson = datetime(2026, 7, 13, 13, tzinfo=timezone.utc)
    assert coerce_utc_datetime(bson) == bson
    assert coerce_utc_datetime("2026-07-13T09:00:00-04:00") == bson
    assert coerce_utc_datetime("not-a-date") is None


def test_result_aliases_are_normalized():
    assert normalize_result("intento_fallido") == "NO_RESPONDIO"
    assert normalize_result("requiere seguimiento") == "SOLICITA_SEGUIMIENTO"


def test_single_sla_function_uses_first_valid_management():
    start = "2026-07-13T09:00:00-04:00"
    managed = "2026-07-13T10:00:00-04:00"
    result = calculate_sla(assigned_at=start, first_valid_management_at=managed)
    assert result["fulfilled"] is True
    assert result["status"] == "fulfilled"


def test_pipeline_visit_may_belong_to_old_lead_and_closure_uses_event_period():
    visits = [{"_id": "v1", "lead_id": "old", "created_at": "2026-07-14T12:00:00Z"}]
    events = [
        {"lead_id": "old", "timestamp": "2026-07-15T12:00:00Z", "meta": {"to": "CLOSED_WON"}},
        {"lead_id": "new", "timestamp": "2026-07-20T12:00:00Z", "meta": {"to": "CLOSED_LOST"}},
    ]
    result = pipeline_activity_in_period(
        visits=visits, events=events, start="2026-07-13T00:00:00Z", end="2026-07-18T00:00:00Z"
    )
    assert result == {"visits": 1, "closed_won": 1, "closed_lost": 0}


def test_card_panel_list_and_paginated_count_parity():
    kpis = {"scope_total": 3, "nuevo": 2, "gestion": 1, "visita": 0, "cerrado": 0}
    assert validate_list_parity(kpis=kpis, listed_total=3)["validated"]
    assert validate_list_parity(kpis=kpis, listed_total=2, state_filter="NEW")["validated"]
    assert not validate_list_parity(kpis=kpis, listed_total=1, state_filter="NEW")["validated"]


def test_snapshot_separates_cohort_pipeline_and_current_priority():
    snapshot = build_snapshot_document(
        period_start="2026-07-13", period_end="2026-07-17",
        cohort={"received": 2, "managed": 1}, pipeline={"visits": 1},
        priorities={"hot_unattended": 3}, executives=[], data_quality={"complete": False},
    )
    assert set(("cohort", "pipeline_activity", "current_priorities")) <= snapshot.keys()
    assert snapshot["immutable"] is True
