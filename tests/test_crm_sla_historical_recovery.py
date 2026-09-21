import json

from chatbot.crm_sla_historical_recovery import (
    HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
    run_sla_historical_recovery_once,
)


def _payload():
    return json.dumps([
        {
            "notification_id": "notification-a",
            "provider_message_id": "81396652",
            "first_confirmed_observed_at": "2026-09-21T02:04:29.157477913Z",
            "evidence_source": HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
            "evidence_reference": "evidence-a",
        },
        {
            "notification_id": "notification-b",
            "provider_message_id": "81396778",
            "first_confirmed_observed_at": "2026-09-21T02:04:34.319779408Z",
            "evidence_source": HISTORICAL_DELIVERY_EVIDENCE_SOURCE,
            "evidence_reference": "evidence-b",
        },
    ])


def test_disabled_recovery_invokes_nothing_and_writes_nothing():
    calls = []
    result = run_sla_historical_recovery_once(
        object(), enabled=False, payload_json=_payload(),
        recovery_fn=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert result == {"status": "disabled", "writes": 0, "recovery_count": 0}
    assert calls == []


def test_invalid_json_fails_closed_without_invocation():
    calls = []
    result = run_sla_historical_recovery_once(
        object(), enabled=True, payload_json="not-json",
        recovery_fn=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "invalid_json"
    assert result["writes"] == 0
    assert calls == []


def test_invalid_evidence_fails_closed_without_partial_writes():
    payload = json.loads(_payload())
    payload[1]["evidence_source"] = "UNAUTHENTICATED_LOG"
    calls = []
    result = run_sla_historical_recovery_once(
        object(), enabled=True, payload_json=json.dumps(payload),
        recovery_fn=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "record_1_unsupported_evidence_source"
    assert result["writes"] == 0
    assert calls == []


def test_exact_two_valid_records_call_only_existing_recovery_primitive():
    calls = []

    def fake_recovery(db, **kwargs):
        calls.append(kwargs)
        return {"status": "recovered", "activation": {"status": "activated"}}

    result = run_sla_historical_recovery_once(
        object(), enabled=True, payload_json=_payload(), recovery_fn=fake_recovery,
    )
    assert result["status"] == "completed"
    assert result["writes"] == 2
    assert len(calls) == 2
    assert {call["provider_message_id"] for call in calls} == {"81396652", "81396778"}
    assert all("send" not in call and "owner" not in call and "cycle" not in call for call in calls)


def test_repeat_startup_is_idempotent_at_primitive_boundary():
    seen = set()

    def idempotent_recovery(_db, **kwargs):
        key = kwargs["notification_id"]
        if key in seen:
            return {"status": "already_recovered"}
        seen.add(key)
        return {"status": "recovered"}

    first = run_sla_historical_recovery_once(
        object(), enabled=True, payload_json=_payload(), recovery_fn=idempotent_recovery,
    )
    second = run_sla_historical_recovery_once(
        object(), enabled=True, payload_json=_payload(), recovery_fn=idempotent_recovery,
    )
    assert first["writes"] == 2
    assert second["writes"] == 0
    assert [item["status"] for item in second["outcomes"]] == ["already_recovered", "already_recovered"]
