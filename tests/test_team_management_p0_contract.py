from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import types

# Los contratos importan submódulos puros de chatbot, pero el paquete raíz
# ejecuta chatbot.core y crea el cliente externo al importarse. Aislamos ese
# efecto únicamente para esta suite; no modificamos la inicialización de
# producción ni simulamos Mongo/DeepSeek dentro de las pruebas.
_chatbot_package = types.ModuleType("chatbot")
_chatbot_package.__path__ = [str(Path(__file__).resolve().parents[1] / "chatbot")]
sys.modules.setdefault("chatbot", _chatbot_package)

from analytics.leads_queries import (_ops_comparable_eligibility, _ops_exec_bucket,
                                     _ops_finalize_execs, build_operational_contract)
import analytics.leads_queries as operational_queries
from chatbot.crm_metrics import event_evidence, registered_outreach_evidence


def _lead(lead_id, executive, assigned_at, temperature="NORMAL"):
    return {
        "_id": lead_id,
        "ejecutivo_asignado": executive,
        "lead_temperature_effective": temperature,
        "lifecycle": {"assigned_at": assigned_at, "first_valid_management_at": None},
        "_created_normalized": assigned_at,
    }


def test_aging_uses_calendar_time_and_reconciles_to_pending():
    now = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)
    docs = [
        _lead("a", "Hernán Castro", datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)),
        _lead("b", "Hernán Castro", datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)),
        _lead("c", "Hernán Castro", datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)),
        _lead("d", "Hernán Castro", datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)),
    ]
    result = build_operational_contract(
        docs, [], "2026-08-01", "2026-08-18", now=now,
        team_executives={"Hernán Castro"},
    )
    aging = result["current"]["aging"]
    assert aging == {"lt_24h": 1, "d_1_3": 1, "d_4_7": 1, "gt_7d": 1}
    assert result["aging_reconciliation"] == {
        "pending_total": 4, "aging_bucket_total": 4, "ok": True
    }


def test_executive_bucket_exposes_stats_objects():
    bucket = _ops_exec_bucket("Hernán Castro")
    bucket["_hot_times"] = [26, 60, 42, 35]
    bucket["_normal_times"] = [61, 113, 70]
    bucket["_hot_managed"] = 4
    bucket["_hot_within"] = 3
    bucket["_normal_managed"] = 3
    bucket["_normal_within"] = 3
    result = _ops_finalize_execs({"Hernán Castro": bucket}, 1)[0]["period"]
    assert result["hot_stats"]["n"] == 4
    assert result["normal_stats"]["n"] == 3
    assert result["p50_hot"] == result["hot_stats"]["p50"]


def test_outreach_is_not_a_valid_result_or_sla_stop():
    outreach = {"type": "SEND_WA_LEAD", "lead_id": "a", "timestamp": datetime.now(timezone.utc)}
    assert registered_outreach_evidence(outreach)["recognized"] is True
    assert event_evidence(outreach)["management"] is False
    assert event_evidence(outreach)["contact_attempt"] is False

    result = {
        "type": "HUMAN_NOTE", "lead_id": "a", "actor": "agent",
        "confirmed": True, "result": "NO_RESPONDIO",
    }
    assert registered_outreach_evidence(result)["recognized"] is False
    assert event_evidence(result)["management"] is True
    assert event_evidence(result)["effective_contact"] is False


def test_sla_boundaries_use_real_minutes_and_are_inclusive(monkeypatch):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    for temperature, threshold, near, inside, outside in [
        ("HOT", 60, 59.999, 60.0, 60.0001),
        ("NORMAL", 180, 179.999, 180.0, 180.0001),
    ]:
        monkeypatch.setattr(operational_queries, "calculate_business_minutes", lambda *_args, value=near: value)
        assert operational_queries._ops_state(_lead("x", "Hernán Castro", now, temperature), now, None)["priority_code"] not in {"hot_open_overdue", "normal_open_overdue"}
        monkeypatch.setattr(operational_queries, "calculate_business_minutes", lambda *_args, value=inside: value)
        assert operational_queries._ops_state(_lead("x", "Hernán Castro", now, temperature), now, None)["priority_code"] not in {"hot_open_overdue", "normal_open_overdue"}
        monkeypatch.setattr(operational_queries, "calculate_business_minutes", lambda *_args, value=outside: value)
        assert operational_queries._ops_state(_lead("x", "Hernán Castro", now, temperature), now, None)["priority_code"] == temperature.lower() + "_open_overdue"


def test_comparable_eligibility_is_metric_level_and_conservative():
    july_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    july_end = datetime(2026, 7, 18, tzinfo=timezone.utc)
    july = _ops_comparable_eligibility(july_start, july_end)
    assert july["assigned"]["valid"] is True
    assert july["activity_attempts"]["valid"] is False
    assert july["managed"]["valid"] is False
    assert july["hot_sla_pct"]["valid"] is False

    crossing = _ops_comparable_eligibility(
        datetime(2026, 7, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert crossing["activity_attempts"]["valid"] is False
    assert crossing["managed"]["valid"] is False

    post = _ops_comparable_eligibility(
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 18, tzinfo=timezone.utc),
    )
    assert all(item["valid"] for item in post.values())


def test_activity_without_result_is_set_difference():
    activity_ids = {"a", "b", "c", "d"}
    result_ids = {"c", "d", "e"}
    assert activity_ids & result_ids == {"c", "d"}
    assert activity_ids - result_ids == {"a", "b"}
    assert result_ids - activity_ids == {"e"}


def test_executive_activity_gap_uses_lead_sets():
    assigned = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
    managed = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
    docs = []
    signals = {}
    for lead_id, has_activity, has_result in (("a", True, True), ("b", True, False), ("c", False, True)):
        doc = _lead(lead_id, "Hernán Castro", assigned)
        doc["lifecycle"]["first_valid_management_at"] = managed
        docs.append(doc)
        signals[lead_id] = {
            "period_activity": has_activity,
            "period_activity_events": ["SEND_WA_LEAD"] if has_activity else [],
            "period_result": has_result,
            "result": "NO_RESPONDIO" if has_result else None,
            "result_events": [{"timestamp": managed, "result": "NO_RESPONDIO"}] if has_result else [],
            "result_event_count": 1 if has_result else 0,
        }
    period = build_operational_contract(
        docs, docs, "2026-08-01", "2026-08-02",
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        team_executives={"Hernán Castro"}, activity_signals=signals,
    )["period"]
    assert period["activity_attempts"] == 2
    assert period["result_leads"] == 2
    assert period["activity_without_result"] == 1


def test_percentiles_keep_decimal_precision():
    bucket = _ops_exec_bucket("Hernán Castro")
    bucket["_hot_times"] = [26.2, 60.3, 42.1, 35.4]
    bucket["_hot_managed"] = 4
    bucket["_hot_within"] = 3
    result = _ops_finalize_execs({"Hernán Castro": bucket}, 1)[0]["period"]
    assert result["p90_hot"] == 60.3
    assert result["hot_stats"]["p90"] == 60.3
