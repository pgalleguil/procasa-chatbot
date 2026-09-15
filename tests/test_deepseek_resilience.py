import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import mongomock
import pytest

from chatbot import chatbot_queue as queue
from chatbot import grok_client
from config import Config


def _response(content, *, finish_reason="stop", reasoning_content=None):
    message = SimpleNamespace(content=content, reasoning_content=reasoning_content)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    return SimpleNamespace(choices=[choice], usage=usage)


def _install_provider(monkeypatch, provider):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return provider(**kwargs) if callable(provider) else provider

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(grok_client, "client", fake_client)
    monkeypatch.setattr(grok_client, "_record_llm_telemetry", lambda **kwargs: None)
    return calls


@pytest.fixture(autouse=True)
def reset_breaker(monkeypatch):
    grok_client._reset_deepseek_circuit_for_tests()
    yield
    grok_client._reset_deepseek_circuit_for_tests()


def _structured(monkeypatch, provider, context=None):
    calls = _install_provider(monkeypatch, provider)
    result = grok_client.generar_respuesta_estructurada(
        [{"role": "user", "content": "Hola"}],
        telemetry_context=context,
    )
    return result, calls


def test_a_normal_response_is_parsed_once(monkeypatch):
    result, calls = _structured(
        monkeypatch,
        _response('{"intencion":"consulta_general","datos_extraidos":{},"respuesta_bot":"OK"}'),
    )
    assert result["respuesta_bot"] == "OK"
    assert result["fallback_used"] is False
    assert len(calls) == 1


def test_b_empty_content_is_terminal_and_fallbacks(monkeypatch):
    result, calls = _structured(monkeypatch, _response(None, reasoning_content="pensamiento"))
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "empty_response"
    assert len(calls) == 1


def test_c_length_exhaustion_is_terminal_and_fallbacks(monkeypatch):
    result, calls = _structured(monkeypatch, _response(None, finish_reason="length"))
    assert result["fallback_reason"] == "length_exhausted"
    assert len(calls) == 1


def test_d_timeout_uses_one_provider_call(monkeypatch):
    def timeout(**kwargs):
        raise TimeoutError("provider timeout")

    result, calls = _structured(monkeypatch, timeout)
    assert result["fallback_reason"] == "timeout"
    assert len(calls) == 1


def test_e_http_500_is_classified_and_fallbacks(monkeypatch):
    class Http500(Exception):
        status_code = 500

    def failure(**kwargs):
        raise Http500("server error")

    result, calls = _structured(monkeypatch, failure)
    assert result["fallback_reason"] == "http_5xx"
    assert len(calls) == 1


def test_f_connection_error_is_classified(monkeypatch):
    class ConnectionFailure(Exception):
        pass

    def failure(**kwargs):
        raise ConnectionFailure("connection reset")

    result, calls = _structured(monkeypatch, failure)
    assert result["fallback_reason"] == "connection_error"
    assert len(calls) == 1


def test_g_invalid_json_does_not_trigger_fast_second_call(monkeypatch):
    result, calls = _structured(monkeypatch, _response("not-json"))
    assert result["fallback_reason"] == "parse_error"
    assert len(calls) == 1


def test_h_payload_has_disabled_thinking_and_no_stream(monkeypatch):
    result, calls = _structured(monkeypatch, _response('{"respuesta_bot":"OK"}'))
    assert result["response_source"] == "deepseek"
    assert calls[0]["model"] == Config.DEEPSEEK_MODEL_REASONER == "deepseek-v4-flash"
    assert calls[0]["stream"] is False
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[0]["timeout"] <= 25


def test_i_fast_path_also_uses_disabled_thinking(monkeypatch):
    calls = _install_provider(monkeypatch, _response("OK"))
    assert grok_client.generar_respuesta([{"role": "user", "content": "Hola"}]) == "OK"
    assert calls[0]["model"] == Config.DEEPSEEK_MODEL_FAST == "deepseek-v4-flash"
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_j_three_provider_failures_open_the_circuit(monkeypatch):
    calls = _install_provider(monkeypatch, lambda **kwargs: (_ for _ in ()).throw(TimeoutError()))
    for _ in range(3):
        assert grok_client.generar_respuesta([{"role": "user", "content": "Hola"}]) == grok_client.LOCAL_FALLBACK_TEXT
    assert grok_client.get_deepseek_circuit_snapshot()["state"] == "OPEN"
    assert grok_client.generar_respuesta([{"role": "user", "content": "Hola"}]) == grok_client.LOCAL_FALLBACK_TEXT
    assert len(calls) == 3


def test_k_circuit_half_open_probe_can_close(monkeypatch):
    circuit = grok_client._deepseek_circuit
    circuit.state = "OPEN"
    circuit.opened_at = time.monotonic() - circuit.cooldown_seconds - 1
    calls = _install_provider(monkeypatch, _response("OK"))
    assert grok_client.generar_respuesta([{"role": "user", "content": "Hola"}]) == "OK"
    assert grok_client.get_deepseek_circuit_snapshot()["state"] == "CLOSED"
    assert len(calls) == 1


def test_l_local_fallback_has_no_provider_dependency(monkeypatch):
    calls = _install_provider(monkeypatch, _response("should not run"))
    result = grok_client._local_fallback({"trace_id": "trace-test", "phone": "+56900000000"}, "circuit_open")
    assert result["response_source"] == "local_fallback"
    assert result["fallback_used"] is True
    assert not calls


def test_m_metrics_capture_inflight_failures_and_empty_response(monkeypatch):
    metrics = {}
    context = {"trace_id": "trace-m", "_runtime_metrics": metrics}
    result, calls = _structured(monkeypatch, _response(None), context=context)
    assert result["fallback_used"] is True
    assert metrics["deepseek_failures"] == 1
    assert metrics["deepseek_empty_responses"] == 1
    assert metrics.get("deepseek_inflight", 0) == 0
    assert len(calls) == 1


def test_n_queue_health_exposes_depth_processing_and_age():
    client = mongomock.MongoClient()
    db = client["test"]
    now = datetime.now(timezone.utc)
    db[queue.JOB_COLLECTION].insert_one({
        "_id": "batch-health",
        "kind": queue.KIND_BATCH,
        "state": queue.ST_PENDING,
        "window_end_at": now - timedelta(seconds=9),
        "job_ids": ["job-health"],
    })
    health = queue.get_queue_health(
        db,
        heartbeat={"last_heartbeat": now.isoformat()},
        now=now,
    )
    assert health["queue_depth"] == 1
    assert health["processing_count"] == 0
    assert health["oldest_job_age_seconds"] >= 9


def test_o_same_conversation_key_is_unique_in_batch_creation():
    client = mongomock.MongoClient()
    db = client["test"]
    queue.ensure_queue_indexes(db)
    first = queue.create_inbound_job(
        db, phone="+56911111111", conversation_id="conversation-o", text="uno", inbound_provider_message_id="msg-o-1",
        received_at=datetime.now(timezone.utc), is_from_me=False,
    )
    second = queue.create_inbound_job(
        db, phone="+56911111111", conversation_id="conversation-o", text="dos", inbound_provider_message_id="msg-o-2",
        received_at=datetime.now(timezone.utc), is_from_me=False,
    )
    assert first and second
    active = list(db[queue.JOB_COLLECTION].find({
        "kind": queue.KIND_BATCH,
        "active_conversation_key": "conversation:conversation-o",
    }))
    assert len(active) == 1
