from datetime import datetime, timezone

from analytics import leads_queries as q


class FakeCollection:
    def __init__(self, docs):
        self.docs = list(docs)
        self.last_pipeline = None

    def aggregate(self, pipeline):
        self.last_pipeline = pipeline
        return list(self.docs)

    def find(self, query):
        lead_ids = set(query.get("lead_id", {}).get("$in", []))
        return [doc for doc in self.docs if doc.get("lead_id") in lead_ids]


class FakeDB(dict):
    pass


def _lead(lead_id, executive, assigned_at, **extra):
    doc = {
        "_id": lead_id,
        "ejecutivo_asignado": executive,
        "lifecycle": {"assigned_at": assigned_at},
        "lead_temperature_effective": "COLD",
        "temperature_history": [],
    }
    doc.update(extra)
    return doc


def test_sla_accountability_reconciles_deduplicated_leads_and_activity(monkeypatch):
    assigned = datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc)
    leads = [
        _lead("1", "Ana", assigned),
        _lead("1", "Ana", assigned),
        _lead("2", "Ana", assigned),
        _lead("3", "Bruno", assigned),
    ]
    events = [{
        "lead_id": "1", "type": "GESTION_LOG", "actor": "Ana",
        "actor_type": "human", "confirmed": True,
        "result": "MESSAGE_SENT_WAITING_RESPONSE",
        "timestamp": datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc),
    }]
    db = FakeDB(leads=FakeCollection(leads), crm_events=FakeCollection(events))
    monkeypatch.setattr(q, "get_db", lambda: db)
    captured = {}
    monkeypatch.setattr(q, "_build_commercial_cohort_match", lambda start, end, filters: captured.setdefault("filters", filters) or {})

    result = q.query_sla_accountability("2026-07-01", "2026-07-31", {"source": "Portal"})

    assert captured["filters"] == {"source": "Portal"}
    assert result["summary"]["open_breached"] == 3
    assert result["summary"]["breached_with_activity_without_result"] == 1
    assert result["summary"]["breached_without_activity"] == 2
    assert result["summary"]["registration_gap_rate"] == 33.3
    assert result["reconciliation"]["open_breached_delta"] == 0
    assert {row["executive_name"] for row in result["by_executive"]} == {"Ana", "Bruno"}
    ana = next(row for row in result["by_executive"] if row["executive_name"] == "Ana")
    assert ana["lead"]["open_breached"] == 2
    assert ana["lead"]["breached_with_activity_without_result"] == 1


def test_sla_accountability_returns_null_rates_when_no_managed_cases(monkeypatch):
    assigned = datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc)
    db = FakeDB(leads=FakeCollection([_lead("1", None, assigned)]), crm_events=FakeCollection([]))
    monkeypatch.setattr(q, "get_db", lambda: db)
    monkeypatch.setattr(q, "_build_commercial_cohort_match", lambda start, end, filters: {})

    result = q.query_sla_accountability("2026-07-01", "2026-07-31")

    row = result["by_executive"][0]
    assert row["executive_name"] == "Sin asignar"
    assert row["lead"]["within_rate"] is None
    assert result["summary"]["registration_gap_rate"] is not None


def test_sla_accountability_keeps_lead_hot_separate_and_uses_canonical_thresholds(monkeypatch):
    assigned = datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc)
    managed = datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc)
    leads = [
        _lead("hot-open", "Ana", assigned, lead_temperature_effective="HOT",
              temperature_history=[{"value": "HOT", "timestamp": assigned.isoformat()}]),
        _lead("hot-managed", "Ana", assigned, lead_temperature_effective="HOT",
              temperature_history=[{"value": "HOT", "timestamp": assigned.isoformat()}],
              lifecycle={"assigned_at": assigned, "first_valid_management_at": managed}),
    ]
    db = FakeDB(leads=FakeCollection(leads), crm_events=FakeCollection([]))
    monkeypatch.setattr(q, "get_db", lambda: db)
    monkeypatch.setattr(q, "_build_commercial_cohort_match", lambda start, end, filters: {})

    result = q.query_sla_accountability("2026-07-01", "2026-07-31")

    hot = result["by_executive"][0]["lead_hot"]
    assert hot["open_breached"] == 1
    assert hot["managed_within"] == 1
    assert hot["within_rate"] == 100.0
