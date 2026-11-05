from __future__ import annotations

import json

import pytest

from chatbot.price_drop_pilot_conversation_v14 import (
    CandidateContextError,
    VERSION,
    generate_reply,
    prepare_initial_message,
)


def verified_apartment(**overrides):
    data = {
        "property_code": "6436",
        "property_type": "Departamento",
        "comuna": "San Miguel",
        "old_price_uf": 2790,
        "current_price_uf": 2690,
        "new_price_uf": 2690,
        "current_price_verified": True,
        "available_for_sale": True,
        "prior_inquiry_verified": True,
        "prior_inquiry_evidence": "fixture: verified inquiry for code 6436",
    }
    data.update(overrides)
    return data


def conversation_context(verified=None, facts=None):
    preview = prepare_initial_message(verified or verified_apartment())
    return {
        **preview,
        "outbound_message": preview["message"],
        "property_type": preview["property_type"],
        "property_facts": facts or {
            "comuna": preview["comuna"],
            "dormitorios": 2,
            "banos": 2,
            "superficie_total": 65,
            "gastos_comunes_clp": 72000,
        },
    }


class SequenceGenerator:
    def __init__(self, answers):
        self.answers = list(answers)
        self.messages = []

    def __call__(self, messages, context, *, write_telemetry, diagnostics, retry_invalid_json):
        self.messages.append(messages)
        assert write_telemetry is False
        answer = self.answers.pop(0)
        diagnostics.update({
            "provider": "fake",
            "model": "fixture-model",
            "provider_calls": 1,
            "format_retries": 0,
            "raw_responses": [json.dumps({"respuesta_bot": answer}, ensure_ascii=False)],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "cost": None,
        })
        return {"respuesta_bot": answer}


def test_initial_preview_uses_verified_property_fields_and_prior_inquiry():
    preview = prepare_initial_message(verified_apartment())
    assert preview["preview_only"] is True
    assert preview["message"] == (
        "¡Hola! Te escribimos de PROCASA por el departamento de San Miguel que "
        "consultaste hace un tiempo. Bajó 100 UF: antes estaba en UF 2.790 y "
        "ahora está en UF 2.690.\n\n"
        "¿Te sigue interesando ese departamento con el nuevo precio?\n\n"
        "Ver el departamento: https://www.procasa.cl/6436"
    )


def test_initial_preview_omits_unverified_previous_inquiry():
    verified = verified_apartment(
        property_code="92817",
        property_type="Casa",
        comuna="La Florida",
        old_price_uf=5600,
        current_price_uf=5350,
        new_price_uf=5350,
        prior_inquiry_verified=True,
        prior_inquiry_evidence="",
    )
    preview = prepare_initial_message(verified)
    assert preview["prior_inquiry_verified"] is False
    assert "consultaste" not in preview["message"]
    assert "la casa de La Florida" in preview["message"]
    assert "¿Te interesa la casa" in preview["message"]
    assert "Bajó 250 UF" in preview["message"]
    assert "UF 5.600" in preview["message"]
    assert "UF 5.350" in preview["message"]
    assert "https://www.procasa.cl/92817" in preview["message"]


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"current_price_verified": False}, "current_price_not_verified"),
        ({"current_price_uf": 2700}, "current_price_mismatch"),
        ({"available_for_sale": False}, "property_not_verified_for_sale"),
        ({"publication_url": "https://example.com/6436"}, "publication_url_invalid"),
    ],
)
def test_initial_preview_blocks_unverified_or_mismatched_data(overrides, reason):
    with pytest.raises(CandidateContextError, match=reason):
        prepare_initial_message(verified_apartment(**overrides))


def test_candidate_sends_exact_outreach_and_pending_question_to_commercial_generator():
    ctx = conversation_context()
    history = [{
        "customer_text": "hola",
        "bot_text": "¡Hola! Te escribimos por la rebaja. ¿Alcanzaste a ver la publicación?",
    }]
    generator = SequenceGenerator(["Sí, el valor vigente es UF 2.690."])
    result = generate_reply("sí", ctx, history, generator=generator)

    sent = generator.messages[0]
    assert sent[2] == {"role": "assistant", "content": ctx["outbound_message"]}
    system = sent[1]["content"]
    assert "Motivo de contacto verificado" in system
    assert "Última pregunta pendiente" in system
    assert "¿Alcanzaste a ver la publicación?" in system
    assert "gastos_comunes_clp=72000" in system
    assert sent[-1] == {"role": "user", "content": "sí"}
    assert result["answer"] == "Sí, el valor vigente es UF 2.690."
    assert result["audit"]["model"] == "fixture-model"
    assert result["audit"]["context_sent"] == sent


def test_missing_context_audit_keeps_safe_candidate_error_code():
    ctx = conversation_context()
    ctx["property_type"] = ""
    generator = SequenceGenerator(["No debe invocarse"])

    result = generate_reply("hola", ctx, generator=generator)

    assert result["answer"] == (
        "Disculpa, tuve un problema al preparar la respuesta. "
        "¿Me puedes repetir tu consulta?"
    )
    assert result["audit"]["errors"] == ["property_type_invalid"]
    assert not generator.messages


def test_partial_unsafe_answer_gets_one_repair_and_keeps_verified_answer():
    ctx = conversation_context()
    generator = SequenceGenerator([
        "El precio vigente es UF 2.690. Tu visita ya quedó confirmada.",
        "El precio vigente es UF 2.690.",
    ])
    result = generate_reply("¿cuál es el precio?", ctx, generator=generator)

    assert len(generator.messages) == 2
    assert result["answer"] == "El precio vigente es UF 2.690."
    assert result["audit"]["repair_count"] == 1
    assert result["audit"]["provider_calls"] == 2
    repair_context = generator.messages[1]
    assert repair_context[-2] == {
        "role": "assistant",
        "content": "El precio vigente es UF 2.690. Tu visita ya quedó confirmada.",
    }
    assert "unconfirmed_operational_action" in repair_context[-1]["content"]
    assert "unconfirmed_operational_action" in result["audit"]["rule_interventions"]
    assert "bounded_model_repair" in result["audit"]["rule_interventions"]


def test_if_repair_stays_unsafe_preserve_verified_sentence():
    ctx = conversation_context()
    generator = SequenceGenerator([
        "El precio vigente es UF 2.690. Tu visita ya quedó confirmada.",
        "El precio vigente es UF 2.690. Tu visita ya quedó confirmada.",
    ])
    result = generate_reply("¿cuál es el precio?", ctx, generator=generator)

    assert len(generator.messages) == 2
    assert result["answer"] == "El precio vigente es UF 2.690."
    assert result["audit"]["repair_count"] == 1
    assert "unsafe_sentence_removed" in result["audit"]["rule_interventions"]


def test_unverified_owner_negotiation_claim_is_repaired():
    ctx = conversation_context()
    generator = SequenceGenerator([
        "El propietario está dispuesto a negociar el valor.",
        "No tengo confirmada la disposición del propietario para negociar.",
    ])
    result = generate_reply("¿es negociable?", ctx, generator=generator)

    assert len(generator.messages) == 2
    assert result["answer"] == "No tengo confirmada la disposición del propietario para negociar."
    assert "unverified_owner_negotiation_position" in result["audit"]["rule_interventions"]


def test_empty_generation_returns_safe_fallback_without_extra_repair():
    generator = SequenceGenerator([""])
    result = generate_reply("¿cuánto mide?", conversation_context(), generator=generator)

    assert len(generator.messages) == 1
    assert result["audit"]["repair_count"] == 0
    assert result["answer"] == (
        "Disculpa, tuve un problema al preparar la respuesta. "
        "¿Me puedes repetir tu consulta?"
    )


def test_history_budget_preserves_latest_correction_and_pending_question():
    ctx = conversation_context()
    history = [{"customer_text": str(i) + "x" * 695,
                "bot_text": "y" * 895} for i in range(24)]
    history[-1] = {"customer_text": "Corrección: no quiero visita",
                   "bot_text": "¿Quieres saber los gastos comunes?"}
    generator = SequenceGenerator(["Los gastos comunes son $72.000."])
    generate_reply("sí", ctx, history, generator=generator)
    sent = generator.messages[0]
    assert sent[-3]["content"] == history[-1]["customer_text"]
    assert sent[-2]["content"] == history[-1]["bot_text"]
    assert history[-1]["bot_text"] in sent[1]["content"]


def test_timeout_returns_safe_fallback():
    def timeout(*args, **kwargs):
        raise TimeoutError("test timeout")

    result = generate_reply("hola", conversation_context(), generator=timeout)
    assert result["audit"]["errors"] == ["TimeoutError"]
    assert result["audit"]["repair_count"] == 0
    assert "problema al preparar la respuesta" in result["answer"]


def test_structured_generator_invalid_json_uses_one_attempt_and_safe_fallback(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-no-network")
    from chatbot import grok_client

    responses = ["not json"]
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        content = responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
            ),
        )

    monkeypatch.setattr(grok_client.client.chat.completions, "create", create)
    result = generate_reply(
        "¿cuál es el precio?",
        conversation_context(),
        generator=grok_client.generar_respuesta_estructurada,
    )

    assert len(calls) == 1
    assert result["answer"] == (
        "Disculpa, tuve un problema al preparar la respuesta. "
        "¿Me puedes repetir tu consulta?"
    )
    assert result["audit"]["provider_calls"] == 1
    assert result["audit"]["format_retries"] == 0
    assert result["audit"]["errors"] == ["JSONDecodeError"]
    assert result["audit"]["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "reasoning_tokens": 3,
    }
    assert result["audit"]["cost"] is None
    assert result["audit"]["original_response"] == "not json"
