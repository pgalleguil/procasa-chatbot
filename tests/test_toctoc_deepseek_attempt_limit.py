from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TOCTOC_PATH = ROOT / "scrapers" / "scraper_toctoc"
root_config_module = sys.modules.pop("config", None)
sys.path.insert(0, str(TOCTOC_PATH))
import config as toctoc_config
import deepseek_classifier
sys.path.remove(str(TOCTOC_PATH))
sys.modules.pop("config", None)
if root_config_module is not None:
    sys.modules["config"] = root_config_module

from ai_entrypoint import classification_service_token
from deepseek_classifier import classify_with_deepseek
from classification_service import classify_capture


def test_deepseek_max_attempts_is_configurable_and_never_zero(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MAX_ATTEMPTS", "1")
    assert toctoc_config.AppConfig().deepseek_max_attempts == 1
    monkeypatch.setenv("DEEPSEEK_MAX_ATTEMPTS", "0")
    assert toctoc_config.AppConfig().deepseek_max_attempts == 1


def test_single_attempt_uses_original_state_classifier_and_full_context(monkeypatch):
    requests = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {"content": '{"state":"DUEÑO_SEGURO","confidence":0.94,"reason":"Declara que es su propiedad.","signals":["vendo mi casa"]}'},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 320, "completion_tokens": 42},
            }

    def fake_post(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    monkeypatch.setattr(deepseek_classifier.requests, "post", fake_post)
    config = SimpleNamespace(
        deepseek_enabled=True,
        deepseek_api_key="test-key",
        deepseek_model="deepseek-v4-flash",
        deepseek_base_url="https://api.example.invalid",
        deepseek_timeout_seconds=5,
        deepseek_max_tokens=500,
        deepseek_max_attempts=1,
        deepseek_description_max_chars=6000,
        deepseek_description_head_chars=2500,
        deepseek_description_tail_chars=2500,
        deepseek_description_snippet_radius=350,
        max_ai_calls_per_run=74,
        max_input_tokens_per_run=250000,
        max_output_tokens_per_run=66600,
        max_estimated_cost_per_run=5.0,
        estimated_input_cost_per_million=0.14,
        estimated_output_cost_per_million=0.28,
    )
    result = classify_capture(
        {
            "portal": "TOCTOC",
            "listing_id": "test-1",
            "seller_id_type_raw": "1",
            "title": "Casa usada, venta directa",
            "description": "Vendo mi casa.",
            "publicador_visible": "Particular",
        },
        rule_context={"owner_signal_evidence": ["primera persona posesiva"]},
        config=config,
        health={"healthy": True, "degraded": False, "sample_size": 1},
        deepseek_callable=classify_with_deepseek,
        allow_real_ai=True,
    )
    assert len(requests) == 1
    sent = requests[0][1]["json"]["messages"][1]["content"]
    assert "Casa usada, venta directa" in sent
    assert "Vendo mi casa." in sent
    assert "Particular" in sent
    assert "primera persona posesiva" in sent
    assert result["ai_called"] is True
    assert result["classification"]["final"] == "OWNER_CONFIRMED"
    assert result["classification"]["assignment_ready"] is True


def test_single_attempt_does_not_retry_provider_error(monkeypatch):
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        raise deepseek_classifier.requests.exceptions.ConnectionError("unavailable")

    monkeypatch.setattr(deepseek_classifier.requests, "post", fail_once)
    config = SimpleNamespace(
        deepseek_enabled=True,
        deepseek_api_key="test-key",
        deepseek_model="deepseek-v4-flash",
        deepseek_base_url="https://api.example.invalid",
        deepseek_timeout_seconds=5,
        deepseek_max_tokens=500,
        deepseek_max_attempts=1,
        deepseek_description_max_chars=6000,
    )
    result = classify_with_deepseek(
        {"title": "Casa usada", "description": "Texto ambiguo."},
        {},
        config,
        authorization_token=classification_service_token(),
    )
    assert calls == [1]
    assert result is not None
    assert result.state == "INCIERTO"
