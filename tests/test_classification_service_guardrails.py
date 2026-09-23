from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_cost_guard import AIBudget, AICostGuard
from captacion_assignment_eligibility import can_assign_property
from classification_cache import ClassificationCache, classification_fingerprint
from classification_service import classify_capture, is_retryable_ai_classification

# The scraper's legacy modules use an unqualified ``config`` import while the
# CRM core has a different root-level module with the same name. Load the
# legacy provider under its expected import context, then restore the core
# module for the rest of this test file.
_root_config = sys.modules.get("config")
_scraper_dir = str(Path(__file__).resolve().parents[1] / "scrapers" / "scraper_toctoc")
sys.modules.pop("config", None)
sys.path.insert(0, _scraper_dir)
from scrapers.scraper_toctoc.deepseek_classifier import (
    DeepSeekResult,
    DeepSeekStatus,
    classify_with_deepseek,
)
sys.path.remove(_scraper_dir)
if _root_config is not None:
    sys.modules["config"] = _root_config


HEALTHY = {"healthy": True, "degraded": False, "sample_size": 100}


def test_ai_truncation_is_recognized_as_retryable_for_normal_historical_dedup():
    assert is_retryable_ai_classification({
        "status": "AI_FAILED_RETRYABLE",
        "failure_state": "AI_TRUNCATED_RETRYABLE",
        "final": "UNCERTAIN",
    }) is True
    assert is_retryable_ai_classification({"status": "CLASSIFIED", "final": "UNCERTAIN"}) is False


def test_retry_fingerprint_mismatch_never_starts_a_new_ai_attempt():
    calls = []
    result = classify_capture(
        _doc(required_classification_fingerprint="not-the-current-input-fingerprint"),
        config=_config(), health=HEALTHY,
        deepseek_callable=lambda *args, **kwargs: calls.append((args, kwargs)),
        allow_real_ai=True,
    )
    assert calls == []
    assert result["reason"] == "retry_fingerprint_mismatch"
    assert result["classification"]["final"] == "UNCERTAIN"


def _config(**overrides):
    values = {
        "deepseek_enabled": True,
        "deepseek_api_key": "test-only",
        "deepseek_max_tokens": 40,
        "max_ai_calls_per_run": 10,
        "max_input_tokens_per_run": 100000,
        "max_output_tokens_per_run": 100000,
        "max_estimated_cost_per_run": 100.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _doc(**overrides):
    value = {
        "portal": "TOCTOC",
        "listing_id": "listing-1",
        "publicador_visible": "Juan Pérez",
        "seller_type": "PARTICULAR",
        "title": "Casa familiar",
        "description": "Vendo mi casa directamente, documentación disponible.",
        "comuna": "Maipu",
        "url": "https://example.test/listing-1",
    }
    value.update(overrides)
    return value


def _valid_owner_result():
    return DeepSeekResult(
        state="DUEÑO_PROBABLE",
        confidence=0.82,
        reason="test owner evidence",
        evidence=["first-person owner statement"],
        raw={"usage": {"completion_tokens": 8}},
        status=DeepSeekStatus.VALID.value,
    )


def test_same_fingerprint_uses_cache_on_second_execution():
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return _valid_owner_result()

    cache = ClassificationCache()
    budget = AICostGuard()
    first = classify_capture(
        _doc(), config=_config(), health=HEALTHY, cache=cache, budget=budget,
        deepseek_callable=fake,
    )
    second = classify_capture(
        _doc(), config=_config(), health=HEALTHY, cache=cache, budget=budget,
        deepseek_callable=fake,
    )
    assert first["ai_called"] is True
    assert second["cache_hit"] is True
    assert second["classification"]["final"] == "OWNER_PROBABLE"
    assert len(calls) == 1


def test_description_change_is_cache_miss():
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return _valid_owner_result()

    cache = ClassificationCache()
    budget = AICostGuard()
    classify_capture(_doc(), config=_config(), health=HEALTHY, cache=cache, budget=budget, deepseek_callable=fake)
    changed = classify_capture(
        _doc(description="Vendo mi casa familiar con patio y estacionamiento."),
        config=_config(), health=HEALTHY, cache=cache, budget=budget,
        deepseek_callable=fake,
    )
    assert changed["cache_hit"] is False
    assert len(calls) == 2


def test_rules_version_change_is_cache_miss():
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return _valid_owner_result()

    cache = ClassificationCache()
    budget = AICostGuard()
    classify_capture(_doc(rules_version="rules-v1"), config=_config(), health=HEALTHY, cache=cache, budget=budget, deepseek_callable=fake)
    changed = classify_capture(
        _doc(rules_version="rules-v2"), config=_config(), health=HEALTHY, cache=cache, budget=budget,
        deepseek_callable=fake,
    )
    assert changed["cache_hit"] is False
    assert len(calls) == 2


def test_fingerprint_changes_with_prompt_model_and_seller_metadata():
    document = _doc()
    base = classification_fingerprint(
        document, prompt_version="prompt-v1", model_version="model-v1"
    )
    assert classification_fingerprint(
        document, prompt_version="prompt-v2", model_version="model-v1"
    ) != base
    assert classification_fingerprint(
        document, prompt_version="prompt-v1", model_version="model-v2"
    ) != base
    assert classification_fingerprint(
        {**document, "seller_type_evidence": "idType=1"},
        prompt_version="prompt-v1", model_version="model-v1",
    ) != base


def test_registry_match_never_calls_deepseek():
    calls = []
    result = classify_capture(
        _doc(broker_identity_match={"matched": True, "match_type": "EXACT_PROFILE_ID"}),
        config=_config(), health=HEALTHY, deepseek_callable=lambda *a, **k: calls.append(1),
    )
    assert result["classification"]["final"] == "BROKER_CONFIRMED"
    assert result["ai_called"] is False
    assert calls == []


def test_structural_broker_never_calls_deepseek():
    calls = []
    result = classify_capture(
        _doc(seller_type_evidence="/corredora/"),
        config=_config(), health=HEALTHY, deepseek_callable=lambda *a, **k: calls.append(1),
    )
    assert result["classification"]["final"] == "BROKER_CONFIRMED"
    assert calls == []


def test_toctoc_corredorasr_url_is_hard_broker_before_detail_or_ai():
    calls = []
    result = classify_capture(
        _doc(url="https://www.toctoc.com/propiedades/compracorredorasr/casa/maipu/test/123"),
        config=_config(), health=HEALTHY,
        deepseek_callable=lambda *a, **k: calls.append(1),
    )
    classification = result["classification"]
    assert classification["final"] == "BROKER_CONFIRMED"
    assert classification["state"] == "CORREDOR_SEGURO"
    assert classification["assignment_ready"] is False
    assert classification["hard_broker_signal"] is True
    assert calls == []


def test_toctoc_compranuevo_url_is_out_of_scope_before_detail_or_ai():
    calls = []
    result = classify_capture(
        _doc(url="https://www.toctoc.com/propiedades/compranuevo/casa/maipu/proyecto/123"),
        config=_config(), health=HEALTHY,
        deepseek_callable=lambda *a, **k: calls.append(1),
    )
    classification = result["classification"]
    assert classification["final"] == "OUT_OF_SCOPE_NEW_DEVELOPMENT"
    assert classification["assignment_ready"] is False
    assert calls == []


def test_toctoc_compraparticularsr_url_is_only_owner_candidate_evidence():
    calls = []
    result = classify_capture(
        _doc(url="https://www.toctoc.com/propiedades/compraparticularsr/casa/maipu/test/123"),
        config=_config(), health=HEALTHY,
        deepseek_callable=lambda *a, **k: calls.append(1),
    )
    classification = result["classification"]
    assert classification["final"] == "UNCERTAIN"
    assert classification["final"] != "BROKER_CONFIRMED"
    assert classification["assignment_ready"] is False


def test_idtype1_particular_metadata_does_not_short_circuit_legacy_ai_residual():
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return _valid_owner_result()

    result = classify_capture(
        _doc(
            seller_id_type_raw="1",
            publicador_visible="Particular",
            description="Casa usada, consultar por disponibilidad y coordinar visita.",
        ),
        config=_config(),
        health=HEALTHY,
        deepseek_callable=fake,
    )
    assert calls == [1]
    assert result["ai_called"] is True
    assert result["classification"]["final"] == "OWNER_PROBABLE"


def test_stale_idtype1_owner_cache_is_ignored_and_sent_through_current_residual():
    document = _doc(
        seller_id_type_raw="1",
        publicador_visible="Particular",
        description="Casa usada, consultar por disponibilidad y coordinar visita.",
    )
    cache = ClassificationCache()
    cache.put(
        classification_fingerprint(document),
        {
            "final": "OWNER_PROBABLE",
            "state": "DUEÑO_PROBABLE",
            "source": "toctoc_id_type",
            "reason": "TOCTOC_IDTYPE_1_OWNER_CANDIDATE",
            "final_reason": "TOCTOC_IDTYPE_1_OWNER_CANDIDATE",
        },
        input_payload=document,
    )
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return _valid_owner_result()

    result = classify_capture(
        document,
        config=_config(),
        health=HEALTHY,
        cache=cache,
        deepseek_callable=fake,
    )
    assert result["cache_hit"] is False
    assert result["ai_called"] is True
    assert calls == [1]


def test_extractor_degraded_makes_zero_calls():
    calls = []
    result = classify_capture(
        _doc(), config=_config(), health={"healthy": False, "degraded": True},
        deepseek_callable=lambda *a, **k: calls.append(1),
    )
    assert result["classification"]["status"] == "UNCERTAIN_PENDING_AI"
    assert calls == []


def test_max_calls_blocks_additional_call():
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return _valid_owner_result()

    budget = AICostGuard(budget=AIBudget(max_calls=1, max_input_tokens=100000, max_output_tokens=100000, max_estimated_cost=100))
    cache = ClassificationCache()
    classify_capture(_doc(listing_id="one"), config=_config(), health=HEALTHY, cache=cache, budget=budget, deepseek_callable=fake)
    blocked = classify_capture(_doc(listing_id="two"), config=_config(), health=HEALTHY, cache=cache, budget=budget, deepseek_callable=fake)
    assert len(calls) == 1
    assert blocked["classification"]["status"] == "UNCERTAIN_PENDING_AI"
    assert blocked["metrics"]["AI_BUDGET_EXCEEDED"] is True


def test_max_input_tokens_blocks_call():
    calls = []
    budget = AICostGuard(budget=AIBudget(max_calls=10, max_input_tokens=1, max_output_tokens=100000, max_estimated_cost=100))
    result = classify_capture(
        _doc(), config=_config(), health=HEALTHY, budget=budget,
        deepseek_callable=lambda *a, **k: calls.append(1),
    )
    assert calls == []
    assert result["classification"]["status"] == "UNCERTAIN_PENDING_AI"


def test_cache_hit_returns_canonical_classification():
    cache = ClassificationCache()
    seed = classify_capture(
        _doc(), config=_config(), health=HEALTHY, cache=cache,
        deepseek_callable=lambda *a, **k: _valid_owner_result(),
    )
    cached = classify_capture(
        _doc(), config=_config(), health=HEALTHY, cache=cache,
        deepseek_callable=lambda *a, **k: (_ for _ in ()).throw(AssertionError("AI called on cache hit")),
    )
    assert seed["classification"]["final"] == "OWNER_PROBABLE"
    assert cached["classification"]["final"] == "OWNER_PROBABLE"
    assert cached["cache_hit"] is True


def test_uncertain_pending_ai_is_not_assignable():
    result = classify_capture(_doc(), config=_config(), health={"healthy": False, "degraded": True})
    decision = can_assign_property({
        **_doc(),
        "classification": result["classification"],
        "pipeline_state": "UNCERTAIN_PENDING_AI",
        "pipeline_complete": False,
    })
    assert decision["assignment_ready"] is False


def test_legacy_direct_entry_point_fails_closed_without_token():
    result = classify_with_deepseek(_doc(), {}, _config())
    assert result.status == DeepSeekStatus.NOT_NEEDED.value
    assert result.state == "INCIERTO"


def test_deepseek_error_never_defaults_to_owner():
    result = classify_capture(
        _doc(), config=_config(), health=HEALTHY,
        deepseek_callable=lambda *a, **k: SimpleNamespace(status="API_ERROR", state="DUEÑO_SEGURO"),
    )
    assert result["classification"]["status"] == "UNCERTAIN_PENDING_AI"
    assert result["classification"]["final_state"] == "INCIERTO"
    assert result["classification"]["final"] == "UNCERTAIN"
