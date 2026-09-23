from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

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
import classification_service as classification_service_module
from classification_cache import ClassificationCache
from scrapers.scraper_toctoc.deepseek_call_ledger import (
    DeepSeekCallLedger,
    DeepSeekLedgerError,
)
from scrapers.scraper_toctoc.retry_failed_deepseek import (
    build_retry_records,
    select_retryable_items,
)


class _MemoryResult:
    matched_count = 1


class _MemoryCollection:
    def __init__(self):
        self.rows = {}
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))
        return kwargs.get("name", "idx")

    def find_one(self, query, projection=None, sort=None):
        rows = [
            row for row in self.rows.values()
            if all(row.get(key) == value for key, value in query.items())
        ]
        if sort:
            key, direction = sort[0]
            rows.sort(key=lambda row: row.get(key, 0), reverse=direction < 0)
        if not rows:
            return None
        row = dict(rows[0])
        if projection:
            row = {key: row[key] for key, value in projection.items() if value and key in row}
        return row

    def insert_one(self, row):
        if row["_id"] in self.rows:
            raise type("DuplicateKeyError", (Exception,), {})("duplicate")
        self.rows[row["_id"]] = dict(row)

    def update_one(self, query, update):
        row = self.rows.get(query.get("_id"))
        if not row:
            return type("Result", (), {"matched_count": 0})()
        row.update(update.get("$set", {}))
        return _MemoryResult()


class _MemoryDatabase:
    def __init__(self):
        self.name = f"deepseek-ledger-test-{id(self)}"
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _MemoryCollection())


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


def test_response_parser_preserves_existing_legacy_state():
    parsed, mode, error = deepseek_classifier._extract_json_object(
        '{"state":"CORREDOR_PROBABLE","confidence":0.82,'
        '"reason":"Señales comerciales.","signals":["comisión"]}'
    )
    normalized, schema_error = deepseek_classifier._normalize_response_object(parsed)
    assert (mode, error, schema_error) == ("plain_json", "", "")
    assert normalized["state"] == "CORREDOR_PROBABLE"
    assert normalized["confidence"] == 0.82
    assert normalized["signals"] == ["comisión"]


def test_response_parser_recovers_fenced_json_with_surrounding_text():
    parsed, mode, error = deepseek_classifier._extract_json_object(
        'Respuesta:\n```json\n{"state":"INCIERTO","confidence":0.5,"reason":"Duda.","signals":[]}\n```\nFin.'
    )
    assert parsed["state"] == "INCIERTO"
    assert mode == "markdown_fenced_json"
    assert error == ""


def test_response_parser_recovers_one_unfenced_object_surrounded_by_text():
    parsed, mode, error = deepseek_classifier._extract_json_object(
        'Clasificación: {"state":"DUEÑO_SEGURO","confidence":0.91,'
        '"reason":"Declara propiedad.","signals":["vendo mi casa"]} respuesta final.'
    )
    assert parsed["state"] == "DUEÑO_SEGURO"
    assert mode == "embedded_json"
    assert error == ""


def test_response_parser_rejects_multiple_json_objects_as_ambiguous():
    parsed, mode, error = deepseek_classifier._extract_json_object(
        '{"state":"INCIERTO","confidence":0.5,"reason":"a","signals":[]} '
        '{"state":"DUEÑO_SEGURO","confidence":0.9,"reason":"b","signals":[]}'
    )
    assert parsed is None
    assert mode == "ambiguous_json_objects"
    assert error == "ambiguous"


def test_response_parser_keeps_empty_content_empty():
    parsed, mode, error = deepseek_classifier._extract_json_object("  \n\t")
    assert parsed is None
    assert (mode, error) == ("empty", "empty")


def test_response_parser_validates_tri_state_contract_conservatively():
    parsed, _, error = deepseek_classifier._extract_json_object(
        '{"classification":"BROKER","confidence":0.72,"evidence":"Señales comerciales."}'
    )
    normalized, schema_error = deepseek_classifier._normalize_response_object(parsed)
    assert (error, schema_error) == ("", "")
    assert normalized["state"] == "CORREDOR_PROBABLE"
    assert normalized["signals"] == ["Señales comerciales."]


def test_response_parser_rejects_invalid_contract_values_and_schema():
    cases = [
        '{"classification":"MAYBE","confidence":0.5,"evidence":"x"}',
        '{"classification":"OWNER","confidence":1.01,"evidence":"x"}',
        '{"classification":"OWNER","confidence":0.9,"evidence":["x"]}',
        '{"state":"INCIERTO","confidence":NaN,"reason":"x","signals":[]}',
    ]
    for content in cases:
        parsed, _, error = deepseek_classifier._extract_json_object(content)
        assert error == ""
        normalized, schema_error = deepseek_classifier._normalize_response_object(parsed)
        assert normalized is None
        assert schema_error


def test_response_parser_does_not_salvage_objects_from_json_arrays():
    parsed, _, error = deepseek_classifier._extract_json_object(
        '[{"state":"INCIERTO","confidence":0.5,"reason":"x","signals":[]}]'
    )
    assert parsed is None
    assert error == "schema"


def test_empty_provider_content_is_not_replaced_by_reasoning(monkeypatch):
    class Response:
        status_code = 200
        text = '{"choices":[{"message":{"content":""}}]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "deepseek-flash",
                "choices": [{
                    "message": {"content": "", "reasoning_content": "reasoning only"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            }

    monkeypatch.setattr(deepseek_classifier.requests, "post", lambda *args, **kwargs: Response())
    config = SimpleNamespace(
        deepseek_enabled=True, deepseek_api_key="test-key", deepseek_model="deepseek-v4-flash",
        deepseek_base_url="https://api.example.invalid", deepseek_timeout_seconds=5,
        deepseek_max_tokens=500, deepseek_max_attempts=1, deepseek_description_max_chars=6000,
    )
    result = classify_with_deepseek(
        {"title": "Casa", "description": "Texto ambiguo", "publicador_visible": "Persona"},
        {}, config, authorization_token=classification_service_token(),
    )
    assert result.status == deepseek_classifier.DeepSeekStatus.INVALID_EMPTY_CONTENT.value
    assert result.state == "INCIERTO"
    assert result.reasoning_content == "reasoning only"
    assert result.diagnostics["http_status"] == 200
    assert result.diagnostics["finish_reason"] == "stop"
    assert result.diagnostics["content_length_chars"] == 0
    assert result.diagnostics["usage"]["prompt_tokens"] == 12


def test_classification_entrypoint_recovers_fenced_json_and_reports_context(monkeypatch):
    class Response:
        status_code = 200
        text = '{"choices":[{"message":{"content":"..."}}]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "deepseek-flash",
                "choices": [{
                    "message": {
                        "content": 'Resultado:\n```json\n{"state":"INCIERTO","confidence":0.5,"reason":"Sin evidencia suficiente.","signals":[]}\n```',
                        "reasoning_content": "diagnóstico auxiliar",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 20, "completion_tokens": 18},
            }

    monkeypatch.setattr(deepseek_classifier.requests, "post", lambda *args, **kwargs: Response())
    config = SimpleNamespace(
        deepseek_enabled=True, deepseek_api_key="test-key", deepseek_model="deepseek-v4-flash",
        deepseek_base_url="https://api.example.invalid", deepseek_timeout_seconds=5,
        deepseek_max_tokens=500, deepseek_max_attempts=1, deepseek_description_max_chars=6000,
    )
    result = classify_with_deepseek(
        {"title": "Casa", "description": "Texto ambiguo", "publicador_visible": "Persona"},
        {}, config, authorization_token=classification_service_token(),
    )
    assert result.status == deepseek_classifier.DeepSeekStatus.VALID.value
    assert result.state == "INCIERTO"
    assert result.reason == "Sin evidencia suficiente."
    assert result.diagnostics["http_status"] == 200
    assert result.diagnostics["finish_reason"] == "stop"
    assert result.diagnostics["response_model"] == "deepseek-flash"
    assert result.diagnostics["requested_model"] == "deepseek-v4-flash"
    assert result.diagnostics["parse_mode"] == "markdown_fenced_json"


def test_invalid_truncated_response_is_persisted_raw_in_deepseek_ledger(monkeypatch):
    content = '{"state":"INCIERTO","confidence":0.5,"reason":"secret-test-key"'

    class Response:
        status_code = 200
        text = '{"model":"deepseek-flash","choices":[{"message":{"content":"' + content.replace('"', '\\"') + '"},"finish_reason":"length"}],"usage":{"prompt_tokens":321,"completion_tokens":500,"total_tokens":821}}'

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "deepseek-flash",
                "choices": [{
                    "message": {"content": content, "reasoning_content": "partial reasoning"},
                    "finish_reason": "length",
                }],
                "usage": {"prompt_tokens": 321, "completion_tokens": 500, "total_tokens": 821},
            }

    monkeypatch.setattr(deepseek_classifier.requests, "post", lambda *args, **kwargs: Response())
    database = _MemoryDatabase()
    ledger = DeepSeekCallLedger(database)
    config = SimpleNamespace(
        deepseek_enabled=True, deepseek_api_key="secret-test-key", deepseek_model="deepseek-v4-flash",
        deepseek_base_url="https://api.example.invalid", deepseek_timeout_seconds=5,
        deepseek_max_tokens=500, deepseek_max_attempts=1, deepseek_description_max_chars=6000,
    )
    result = classify_with_deepseek(
        {"listing_id": "4123456", "url": "https://example.test/4123456", "title": "Casa", "description": "Ambigua"},
        {}, config,
        authorization_token=classification_service_token(),
        call_ledger=ledger,
        call_context={
            "run_id": "retry-run", "pipeline_item_id": "retry-run:4123456",
            "property_id": "4123456", "listing_id": "4123456",
            "url": "https://example.test/4123456", "classification_fingerprint": "fingerprint-1",
            "extractor_version": "extractor-x", "rules_version": "rules-y",
            "classifier_version": "classifier-z", "prompt_version": "prompt-v",
            "model_version": "deepseek-v4-flash",
        },
    )

    rows = list(database["deepseek_call_ledger"].rows.values())
    assert result.status == deepseek_classifier.DeepSeekStatus.INVALID_JSON.value
    assert result.diagnostics["finish_reason"] == "length"
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "retry-run"
    assert row["attempt_number"] == 1
    assert row["http_status"] == 200
    assert row["model_requested"] == "deepseek-v4-flash"
    assert row["model_returned"] == "deepseek-flash"
    assert row["max_tokens_effective"] == 500
    assert row["parser_status"] == "TRUNCATED"
    assert row["raw_content"] == content.replace("secret-test-key", "[REDACTED]")
    assert row["raw_reasoning_content"] == "partial reasoning"
    assert row["raw_response_body"] == Response.text.replace("secret-test-key", "[REDACTED]")
    assert row["input_tokens"] == 321
    assert row["output_tokens"] == 500
    assert row["total_tokens"] == 821
    assert row["extractor_version"] == "extractor-x"
    assert row["rules_version"] == "rules-y"
    assert row["classifier_version"] == "classifier-z"
    assert row["prompt_version"] == "prompt-v"
    assert row["model_version"] == "deepseek-v4-flash"
    assert not any("key" in key.lower() or "authorization" in key.lower() for key in row)
    assert "secret-test-key" not in repr(row)


def test_call_ledger_attempts_are_stable_and_valid_result_is_not_recalled():
    ledger = DeepSeekCallLedger(_MemoryDatabase())
    context = {
        "run_id": "run-a", "listing_id": "listing-a",
        "classification_fingerprint": "same-inputs",
    }
    request = {"model": "deepseek-v4-flash", "max_tokens": 500, "response_format": {"type": "json_object"}}
    first = ledger.begin_attempt(context, request)
    ledger.finish_attempt(first, {"parser_status": "INVALID_JSON", "status": "INVALID_JSON"})
    second = ledger.begin_attempt({**context, "run_id": "run-b"}, request)
    assert first["attempt_number"] == 1
    assert second["attempt_number"] == 2
    ledger.finish_attempt(second, {
        "parser_status": "VALID", "status": "VALID",
        "classification": "INCIERTO", "confidence": 0.5,
    })
    latest = ledger.latest_attempt("listing-a", "same-inputs")
    assert latest["attempt_number"] == 2
    assert latest["parser_status"] == "VALID"
    try:
        ledger.begin_attempt({**context, "run_id": "run-c"}, request)
    except DeepSeekLedgerError:
        pass
    else:
        raise AssertionError("valid fingerprint must not be called again")


def test_classification_service_uses_durable_ledger_and_replays_valid_result(monkeypatch):
    provider_calls = []

    class Response:
        status_code = 200
        text = '{"choices":[{"message":{"content":"valid"}}]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "deepseek-flash",
                "choices": [{
                    "message": {
                        "content": '{"state":"INCIERTO","confidence":0.5,"reason":"Evidencia insuficiente.","signals":[]}',
                        "reasoning_content": "",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 40, "completion_tokens": 22, "total_tokens": 62},
            }

    def fake_post(*args, **kwargs):
        provider_calls.append(1)
        return Response()

    monkeypatch.setattr(deepseek_classifier.requests, "post", fake_post)
    monkeypatch.setattr(
        classification_service_module,
        "resolve_broker_identity",
        lambda db, document: {"matched": False, "evidence": []},
    )
    database = _MemoryDatabase()
    config = SimpleNamespace(
        deepseek_enabled=True,
        deepseek_api_key="test-key",
        deepseek_model="deepseek-v4-flash",
        deepseek_base_url="https://api.example.invalid",
        deepseek_timeout_seconds=5,
        deepseek_max_tokens=500,
        deepseek_max_attempts=1,
        deepseek_description_max_chars=6000,
        max_ai_calls_per_run=5,
        max_input_tokens_per_run=10000,
        max_output_tokens_per_run=10000,
        max_estimated_cost_per_run=10,
    )
    doc = {
        "portal": "TOCTOC",
        "listing_id": "ledger-service-test",
        "url": "https://example.test/ledger-service-test",
        "publicador_visible": "Juan Perez",
        "seller_type": "PARTICULAR",
        "title": "Casa familiar",
        "description": "Texto ambiguo para validar persistencia del resultado.",
        "comuna": "Maipu",
    }
    first = classify_capture(
        doc, config=config, db=database, health={"healthy": True, "degraded": False},
        cache=ClassificationCache(), run_id="ledger-service-run",
    )
    second = classify_capture(
        doc, config=config, db=database, health={"healthy": True, "degraded": False},
        cache=ClassificationCache(), run_id="later-run",
    )
    rows = list(database["deepseek_call_ledger"].rows.values())

    assert provider_calls == [1]
    assert first["ai_called"] is True
    assert second["cache_hit"] is True
    assert second["reason"] == "deepseek_ledger_cache_hit"
    assert first["classification"]["final"] == "UNCERTAIN"
    assert second["classification"]["final"] == "UNCERTAIN"
    assert len(rows) == 1
    assert rows[0]["run_id"] == "ledger-service-run"
    assert rows[0]["parser_status"] == "VALID"
    assert rows[0]["input_tokens"] == 40
    assert rows[0]["output_tokens"] == 22


def test_real_service_entrypoint_requires_database_for_durable_ai_ledger(monkeypatch):
    calls = []
    monkeypatch.setattr(deepseek_classifier.requests, "post", lambda *args, **kwargs: calls.append(1))
    result = classify_capture(
        {"portal": "TOCTOC", "listing_id": "no-db", "title": "Casa", "description": "Ambigua"},
        config=SimpleNamespace(
            deepseek_enabled=True, deepseek_api_key="test-key", deepseek_model="deepseek-v4-flash",
            deepseek_max_tokens=500, deepseek_description_max_chars=6000,
            max_ai_calls_per_run=5, max_input_tokens_per_run=10000,
            max_output_tokens_per_run=5000, max_estimated_cost_per_run=10,
        ),
        health={"healthy": True, "degraded": False},
    )
    assert calls == []
    assert result["reason"] == "deepseek_ledger_unavailable"
    assert result["classification"]["final"] == "UNCERTAIN"


def test_retry_utility_selects_only_approved_failed_residuals():
    source_rows = [
        {"listing_id": "empty", "ai_called": True, "classification": {"final_reason": "DEEPSEEK_INVALID_EMPTY_CONTENT"}},
        {"listing_id": "json", "ai_called": True, "classification": {"final_reason": "DEEPSEEK_INVALID_JSON"}},
        {"listing_id": "valid", "ai_called": True, "classification": {"final_reason": "AI_RESIDUAL_CLASSIFICATION"}},
        {"listing_id": "not-ai", "ai_called": False, "classification": {"final_reason": "DEEPSEEK_INVALID_JSON"}},
    ]
    failed = select_retryable_items(source_rows)
    assert [row["listing_id"] for row in failed] == ["empty", "json"]
    records = build_retry_records(
        failed,
        [
            {
                "listing_id": "empty",
                "title": "Casa a la venta",
                "description": "Descripción ambigua completa",
                "classification": {
                    "final_reason": "TOCTOC_IDTYPE_1_OWNER_CANDIDATE",
                    "rules_version": "historical-rules-v2",
                },
            },
            {"listing_id": "json", "title": "Departamento a la venta", "description": "Otra descripción ambigua"},
            {"listing_id": "valid", "title": "Ya válido", "description": "No debe entrar"},
        ],
        max_items=2,
    )
    assert [row["listing_id"] for row in records] == ["empty", "json"]
    assert all(row["rule_context"] and "classification_hint" in row for row in records)
    assert "classification" not in records[0]
    assert records[0]["rules_version"] == "historical-rules-v2"


def test_retry_utility_rejects_wrong_count_or_missing_original_detail():
    failed = [
        {"listing_id": "one", "ai_called": True, "classification": {"final_reason": "DEEPSEEK_INVALID_JSON"}},
    ]
    with pytest.raises(ValueError, match="retry scope mismatch"):
        build_retry_records(failed, [], max_items=2)
    with pytest.raises(ValueError, match="original detail unavailable"):
        build_retry_records(failed, [], max_items=1)
