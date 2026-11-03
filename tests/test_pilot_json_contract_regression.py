"""Regression for malformed DeepSeek text in the isolated price-drop campaign.

No network, provider, customer lead or CRM access occurs here.
"""
import json
from types import SimpleNamespace

def test_outbound_pilot_forces_json_and_repairs_bad_first_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-credential")
    from chatbot import grok_client

    calls = []
    bad = "Te escribimos por la rebaja, no es necesario reenviar el enlace."
    good = json.dumps({
        "intencion": "consulta_general",
        "respuesta_bot": "Sí, fuimos nosotros quienes te avisamos de la rebaja.",
        "datos_extraidos": {},
    }, ensure_ascii=False)

    def fake_create(**kwargs):
        calls.append(kwargs)
        raw = bad if len(calls) == 1 else good
        choice = SimpleNamespace(
            message=SimpleNamespace(content=raw), finish_reason="stop",
        )
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(
        grok_client, "client",
        SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_create))),
    )
    campaign = (
        "[CONTEXTO COMERCIAL DEL MENSAJE SALIENTE - PRIORIDAD] "
        "PROCASA inició la conversación. Avisamos una rebaja de precio."
    )
    messages = [
        {"role": "system", "content": campaign},
        {"role": "assistant", "content": "Te avisamos de la rebaja."},
        {"role": "user", "content": "Pero ustedes me escribieron primero"},
    ]
    result = grok_client.generar_respuesta_estructurada(
        messages, {"operacion": "Venta"}, write_telemetry=False,
    )
    assert result["respuesta_bot"].startswith("Sí, fuimos nosotros")
    assert len(calls) == 2
    assert all(
        call["response_format"] == {"type": "json_object"} for call in calls
    )
    assert all(call["messages"][-1]["role"] == "user" for call in calls)
    assert all(call["messages"][-1]["content"] ==
               "Pero ustedes me escribieron primero" for call in calls)


def test_other_conversations_keep_original_output_mode(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-credential")
    from chatbot import grok_client
    from config import Config
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        reply = json.dumps({
            "intencion": "consulta_general",
            "respuesta_bot": "Hola, cuéntame qué necesitas.",
            "datos_extraidos": {},
        })
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=reply), finish_reason="stop"
            )],
            usage=None,
        )

    monkeypatch.setattr(
        grok_client, "client",
        SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=fake_create))),
    )
    grok_client.generar_respuesta_estructurada(
        [{"role": "user", "content": "Hola"}],
        {"operacion": "Venta"}, write_telemetry=False,
    )
    assert len(calls) == 1
    if Config.DEEPSEEK_RESPONSE_FORMAT != "json_object":
        assert "response_format" not in calls[0]
