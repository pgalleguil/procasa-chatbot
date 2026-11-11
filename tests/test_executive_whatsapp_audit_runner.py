"""Local-only audit runner tests. No DeepSeek or customer data."""
import json
import pytest

from chatbot.executive_whatsapp_audit_runner import (
    build_private_model_request, evaluate_with_injected_model,
)


def fake_lead():
    return {
        "_id": "synthetic-lead",
        "messages": [
            {"role": "assistant", "content": "Hola, ¿en qué te ayudo?",
             "timestamp": "2026-11-11T08:00:00Z"},
        ],
        "executive_whatsapp_messages": [
            {
                "actor_type": "executive",
                "content": "Te puedo mostrar el departamento en https://example.com",
                "timestamp": "2026-11-11T10:01:00Z",
                "assignment_cycle_id": "cycle-id-secret",
                "executive_user_id": "person-1",
                "provider_key": "wasender:evidence-1",
            },
            {
                "actor_type": "client",
                "content": "Sí, mi teléfono +56912345678 y email cliente@ejemplo.cl",
                "timestamp": "2026-11-11T10:02:00Z",
                "assignment_cycle_id": "cycle-id-secret",
                "executive_user_id": "person-1",
                "provider_key": "wasender:evidence-2",
            },
        ],
    }


def model_result(**overrides):
    obj = {
        "commercial_status": "ACTIVE_INTEREST",
        "contact_effective": True,
        "follow_up_required": True,
        "opt_out": False,
        "next_action": "Revisar propuesta",
        "preferences": {},
        "evidence_provider_keys": ["wasender:evidence-1", "wasender:evidence-2"],
        "uncertainty": "No está demostrada la cobertura histórica",
        "coverage_complete": False,
    }
    obj.update(overrides)
    return obj


def test_scrubs_chilean_phone_url_and_email_and_obscures_lead_identity():
    req, ctx = build_private_model_request(fake_lead())
    blob = json.dumps(req, ensure_ascii=False)
    assert "synthetic-lead" not in blob
    assert "cycle-id-secret" not in blob
    assert "person-1" not in blob
    assert "+56912345678" not in blob
    assert "cliente@ejemplo.cl" not in blob
    assert "https://example.com" not in blob
    assert "[PHONE]" in blob
    assert "[EMAIL]" in blob
    assert "[URL]" in blob
    assert req["coverage_complete"] is False
    assert ctx["coverage_complete"] is False


def test_injected_model_never_writes_and_requires_human_review():
    calls = []

    def fake_transport(payload):
        calls.append(payload)
        return json.dumps(model_result(), ensure_ascii=False)

    output = evaluate_with_injected_model(fake_lead(), model_call=fake_transport)
    assert len(calls) == 1
    assert output["candidate"]["contact_effective"] is True
    assert output["review_status"] == "PENDING_HUMAN_REVIEW"
    assert output["writes_performed"] == 0
    assert output["sends_performed"] == 0
    assert output["customer_campaign_eligible"] is False


def test_model_cannot_invent_contact_without_bidirectional_evidence():
    with pytest.raises(ValueError, match="bidirectional"):
        evaluate_with_injected_model(
            fake_lead(),
            model_call=lambda _: model_result(
                evidence_provider_keys=["wasender:evidence-2"],
            ),
        )


@pytest.mark.parametrize("output", [
    "not a json document",
    '{"commercial_status": "INVALID"}',
    [],
    None,
])
def test_non_json_or_incompatible_model_outputs_rejected(output):
    with pytest.raises(ValueError):
        evaluate_with_injected_model(fake_lead(), model_call=lambda _: output)


def test_truncation_forces_incomplete_coverage():
    req, prepared = build_private_model_request(
        fake_lead(), coverage_complete=True, max_messages=2,
    )
    assert req["coverage_complete"] is False
    assert prepared["coverage_complete"] is False


def test_invalid_message_limit_or_unprovided_transport_rejected():
    with pytest.raises(ValueError, match="invalid_message_limit"):
        build_private_model_request(fake_lead(), max_messages=0)
    with pytest.raises(ValueError, match="explicit_model_transport"):
        evaluate_with_injected_model(fake_lead(), model_call=None)


def test_large_conversation_stops_instead_of_sending_unbounded_data():
    lead = fake_lead()
    lead["executive_whatsapp_messages"][1]["content"] = "A" * 50000
    with pytest.raises(ValueError, match="audit_prompt_exceeds_privacy_budget"):
        build_private_model_request(lead)
