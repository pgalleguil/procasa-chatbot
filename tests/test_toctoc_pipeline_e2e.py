from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_cost_guard import AIBudget, AICostGuard
from classification_cache import ClassificationCache
from toctoc_pipeline import InMemoryPipelineLedger, PipelineOptions, run_toctoc_pipeline


def _record(code: str, **extra):
    value = {
        "listing_id": code,
        "url": f"https://www.toctoc.com/test/{code}",
        "source_portal": "toctoc",
        "publicador_visible": "Juan Perez",
        "seller_type": "PARTICULAR",
        "title": "Casa familiar en venta",
        "description": "Vendo mi casa familiar directamente.",
        "comuna": "Maipu",
        "precio_clp": 150000000,
    }
    value.update(extra)
    return value


def _owner_hint():
    return {"state": "DUEÑO_SEGURO", "confidence": 0.95, "source": "rules_json", "reason": "owner rule"}


def _ai_owner(*args, **kwargs):
    return SimpleNamespace(
        state="DUEÑO_PROBABLE", confidence=0.82, reason="mock residual",
        evidence=["mock"], raw={"usage": {"completion_tokens": 5}}, status="VALID",
    )


def _options(**overrides):
    value = PipelineOptions(test_mode=True, **overrides)
    return value


def test_a_owner_clear_is_assignable_without_real_io():
    report = run_toctoc_pipeline([_record("A", classification_hint=_owner_hint())], options=_options())
    assert report["run_status"] == "SUCCESS"
    assert report["assignable"] == 1
    assert report["real_scraping"] is False
    assert report["real_deepseek_calls"] == 0


def test_b_structural_broker_is_blocked_before_ai():
    calls = []
    report = run_toctoc_pipeline(
        [_record("B", seller_type_evidence="/corredora/")],
        options=_options(), deepseek_callable=lambda *a, **k: calls.append(1),
    )
    assert report["assignable"] == 0
    assert report["invariants"]["BROKER_ASSIGNED"] == 0
    assert calls == []
    assert report["qa_sample"]["BROKER"]


def test_c_known_registry_identity_is_blocked_before_ai():
    calls = []
    report = run_toctoc_pipeline(
        [_record("C", broker_identity_match={"matched": True, "match_type": "EXACT_PROFILE_ID"})],
        options=_options(), deepseek_callable=lambda *a, **k: calls.append(1),
    )
    assert report["assignable"] == 0
    assert calls == []


def test_d_cross_portal_identity_is_blocked_before_ai():
    calls = []
    report = run_toctoc_pipeline(
        [_record("D", cross_portal_match=True)],
        options=_options(), deepseek_callable=lambda *a, **k: calls.append(1),
    )
    assert report["assignable"] == 0
    assert calls == []


def test_e_cache_hit_avoids_second_ai_call():
    cache = ClassificationCache()
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return _ai_owner()

    first = run_toctoc_pipeline([_record("E")], options=_options(run_id="e-first"), cache=cache, deepseek_callable=fake)
    second = run_toctoc_pipeline([_record("E")], options=_options(run_id="e-second"), cache=cache, deepseek_callable=fake)
    assert first["ai"]["AI_CALLS_EXECUTED"] == 1
    assert second["ai"]["AI_CACHE_HITS"] == 1
    assert len(calls) == 1


def test_f_ambiguous_residual_without_ai_stays_pending_and_blocked():
    report = run_toctoc_pipeline([_record("F")], options=PipelineOptions())
    assert report["assignable"] == 0
    assert report["qa_sample"]["UNCERTAIN"]


def test_g_extractor_degraded_stops_ai_and_assignment():
    baseline = {field: 1.0 for field in (
        "publisher_present", "seller_type_present", "profile_id_present",
        "client_id_present", "description_present", "title_present",
        "price_present", "commune_present", "structural_signal_present",
    )}
    calls = []
    report = run_toctoc_pipeline(
        [_record("G")], options=_options(health_min_sample=1), health_baseline=baseline,
        deepseek_callable=lambda *a, **k: calls.append(1),
    )
    assert report["extractor_health"] == "DEGRADED"
    assert report["real_deepseek_calls"] == 0
    assert calls == []
    assert report["assignable"] == 0


def test_h_ai_budget_exhaustion_blocks_residual():
    budget = AICostGuard(budget=AIBudget(max_calls=0, max_input_tokens=100000, max_output_tokens=100000, max_estimated_cost=100))
    report = run_toctoc_pipeline([_record("H")], options=_options(), budget=budget, deepseek_callable=_ai_owner)
    assert report["ai"]["AI_BUDGET_EXCEEDED"] is True
    assert report["assignable"] == 0
    assert report["real_deepseek_calls"] == 0


def test_i_interruption_can_resume_without_duplicate_persist():
    ledger = InMemoryPipelineLedger()
    calls = []
    first_failure = {"value": True}

    def persist(item):
        calls.append(item["listing_id"])
        if first_failure["value"]:
            first_failure["value"] = False
            raise RuntimeError("controlled interruption")

    first = run_toctoc_pipeline(
        [_record("I", classification_hint=_owner_hint())],
        options=_options(run_id="resume-i", dry_run=False, allow_mongo_writes=True),
        ledger=ledger, persist_fn=persist,
    )
    second = run_toctoc_pipeline(
        [_record("I", classification_hint=_owner_hint())],
        options=_options(run_id="resume-i", dry_run=False, allow_mongo_writes=True),
        ledger=ledger, persist_fn=persist,
    )
    assert first["run_status"] == "COMPLETED_WITH_ANOMALIES"
    assert second["run_status"] == "SUCCESS"
    assert calls == ["I", "I"]
    assert ledger.get_item("resume-i", "I")["status"] == "PERSISTED"


def test_j_broker_already_assigned_is_reported_as_invariant_violation():
    report = run_toctoc_pipeline(
        [_record("J", assigned_executive="exec-1", seller_type_evidence="/corredora/")],
        options=_options(),
    )
    assert report["invariants"]["BROKER_ASSIGNED"] == 1
    assert report["run_status"] == "COMPLETED_WITH_ANOMALIES"


def test_k_concurrent_same_run_is_locked():
    ledger = InMemoryPipelineLedger()
    entered = threading.Event()
    release = threading.Event()
    results = []

    def preflight():
        entered.set()
        release.wait(timeout=3)
        return {"ok": True}

    def first():
        results.append(run_toctoc_pipeline([_record("K", classification_hint=_owner_hint())], options=_options(run_id="same-k"), ledger=ledger, preflight_fn=preflight))

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(timeout=3)
    second = run_toctoc_pipeline([_record("K", classification_hint=_owner_hint())], options=_options(run_id="same-k"), ledger=ledger)
    release.set()
    thread.join(timeout=3)
    assert second["reason"] == "RUN_LOCKED"
    assert len(results) == 1


def test_l_repeated_complete_run_does_not_persist_twice():
    ledger = InMemoryPipelineLedger()
    calls = []

    def persist(item):
        calls.append(item["listing_id"])

    first = run_toctoc_pipeline(
        [_record("L", classification_hint=_owner_hint())],
        options=_options(run_id="repeat-l", dry_run=False, allow_mongo_writes=True),
        ledger=ledger, persist_fn=persist,
    )
    second = run_toctoc_pipeline(
        [_record("L", classification_hint=_owner_hint())],
        options=_options(run_id="repeat-l", dry_run=False, allow_mongo_writes=True),
        ledger=ledger, persist_fn=persist,
    )
    assert first["run_status"] == "SUCCESS"
    assert second["run_status"] == "SUCCESS"
    assert calls == ["L"]
