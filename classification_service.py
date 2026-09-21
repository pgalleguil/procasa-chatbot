"""Authorized classification service for captacion listings.

Every productive DeepSeek call must pass through this module. Deterministic
broker evidence and cache hits terminate before the provider, while health and
budget failures fail closed as ``UNCERTAIN_PENDING_AI``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

from ai_cost_guard import AICostGuard, budget_from_config, estimate_tokens
from ai_entrypoint import classification_service_token
from broker_identity import detect_hard_broker_signal
from broker_registry import resolve_broker_identity
from canonical_classification import canonicalize_classification, legacy_to_canonical
from classification_cache import (
    CLASSIFICATION_SERVICE_VERSION,
    ClassificationCache,
    classification_fingerprint,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _legacy_state(canonical_state: str) -> str:
    return {
        "OWNER_CONFIRMED": "DUEÑO_SEGURO",
        "OWNER_PROBABLE": "DUEÑO_PROBABLE",
        "BROKER_CONFIRMED": "CORREDOR_SEGURO",
        "BROKER_PROBABLE": "CORREDOR_PROBABLE",
        "UNCERTAIN": "INCIERTO",
        "INVALID": "INVALID",
        "REMOVED": "AD_REMOVED",
        "EXPIRED": "EXPIRED",
        "OUT_OF_SCOPE_NEW_DEVELOPMENT": "FUERA_DE_ALCANCE",
    }.get(canonical_state, "INCIERTO")


def _is_removed_or_invalid(document: dict[str, Any]) -> str | None:
    classification = document.get("classification") or {}
    html_status = str(document.get("html_validation_status") or "").upper()
    state = str(
        classification.get("final")
        or classification.get("final_state")
        or classification.get("state")
        or ""
    ).upper()
    if state in {"AD_REMOVED", "REMOVED"} or html_status in {"LISTING_REMOVED", "REMOVED"}:
        return "REMOVED"
    if state in {"INVALID", "BLOCKED"} or html_status in {"INVALID", "BLOCKED", "ERROR"}:
        return "INVALID"
    if state in {"EXPIRED", "PUBLICACION_EXPIRADA"}:
        return "EXPIRED"
    return None


def detect_out_of_scope_new_development(document: dict[str, Any]) -> dict[str, Any] | None:
    """Detect Toctoc new-development inventory before broker/AI layers.

    ``Venta Nuevo`` is an operational scope decision for this owner-capture
    pipeline, not a broker classification.  Keep the raw ``idType`` and the
    operation label as evidence, but do not infer this state from a publisher
    name alone.
    """
    portal = str(
        document.get("portal")
        or document.get("source_portal")
        or document.get("origen")
        or ""
    ).strip().upper()
    if portal and portal not in {"TOCTOC", "TOCTOC.COM"}:
        return None

    operation = str(
        document.get("operation_label_raw")
        or document.get("operation")
        or document.get("operacion")
        or ""
    ).strip()
    seller_id_type = str(
        document.get("seller_id_type_raw")
        or document.get("seller_id_type")
        or ""
    ).strip()
    operation_is_new = bool(re.search(r"\b(?:venta|arriendo)\s+nuevo\b", operation, re.IGNORECASE))
    id_type_is_new = seller_id_type == "3"
    if not operation_is_new and not id_type_is_new:
        return None
    evidence = []
    if operation:
        evidence.append(f"operation_label={operation}")
    if seller_id_type:
        evidence.append(f"idType={seller_id_type}")
    return {
        "reason_code": "TOCTOC_NEW_DEVELOPMENT_OPERATION",
        "evidence": evidence,
        "operation_label": operation,
        "seller_id_type_raw": seller_id_type,
    }


def detect_toctoc_id_type_signal(document: dict[str, Any]) -> dict[str, Any] | None:
    """Return the structured Toctoc seller signal, without invoking AI."""
    portal = str(
        document.get("portal")
        or document.get("source_portal")
        or document.get("origen")
        or ""
    ).strip().upper()
    if portal and portal not in {"TOCTOC", "TOCTOC.COM"}:
        return None
    raw = str(
        document.get("seller_id_type_raw")
        or document.get("seller_id_type")
        or ""
    ).strip()
    if raw == "1":
        return {
            "kind": "OWNER",
            "raw": raw,
            "evidence": ["client.idType=1"],
            "evidence_type": "EXPLICIT_OWNER_STRUCTURAL",
            "evidence_strength": "EXPLICIT_OWNER_STRUCTURAL",
            "evidence_source": "detail_next_data.client.idType",
        }
    if raw == "2":
        return {
            "kind": "BROKER",
            "raw": raw,
            "evidence": ["client.idType=2"],
            "evidence_type": "STRUCTURAL_BROKER",
            "evidence_strength": "HARD",
            "evidence_source": "detail_next_data.client.idType",
        }
    if raw == "3":
        return {
            "kind": "NEW_DEVELOPMENT",
            "raw": raw,
            "evidence": ["client.idType=3"],
            "evidence_type": "OUT_OF_SCOPE_SCOPE",
            "evidence_strength": "HARD",
            "evidence_source": "detail_next_data.client.idType",
        }
    return None


def _health_is_healthy(health: dict[str, Any] | None) -> bool:
    return bool(health and health.get("healthy") is True and not health.get("degraded"))


def _classification_with_canonical(
    document: dict[str, Any],
    classification: dict[str, Any],
    *,
    registry_match: dict[str, Any] | None,
    human_broker_match: bool,
    cross_portal_match: bool,
    strong_text_broker: bool,
    cache_hit: bool = False,
    ai_state: Any = None,
    reason: str | None = None,
    evidence: list[Any] | None = None,
    pipeline_complete: bool = True,
) -> dict[str, Any]:
    result = dict(classification)
    canonical = canonicalize_classification(
        {**document, "classification": result},
        registry_match=registry_match,
        human_broker_match=human_broker_match,
        cross_portal_match=cross_portal_match,
        strong_text_broker=strong_text_broker,
        cache_hit=cache_hit,
        ai_state=ai_state,
        reason=reason,
        evidence=evidence,
    )
    result.update(canonical)
    final = str(result.get("final") or "UNCERTAIN").upper()
    result["state"] = _legacy_state(final)
    result["final_state"] = result["state"]
    result["canonical_confidence"] = result.get("confidence", result.get("canonical_confidence"))
    if final in {"OWNER_CONFIRMED", "OWNER_PROBABLE"} and result.get("owner_probability") is None:
        result["owner_probability"] = result.get("confidence", result.get("canonical_confidence"))
        result["owner_probability_source"] = "canonical_classifier_confidence"
    if final in {"OWNER_CONFIRMED", "OWNER_PROBABLE"} and result.get("owner_probability") is not None:
        # The canonical Mongo schema validates confidence against the
        # persisted owner probability.  Keep both fields aligned when the
        # structured owner signal supplies the probability directly.
        if result.get("confidence") is None:
            result["confidence"] = result["owner_probability"]
        if result.get("canonical_confidence") is None:
            result["canonical_confidence"] = result["owner_probability"]
    result["pipeline_state"] = "CLASSIFIED" if pipeline_complete else "UNCERTAIN_PENDING_AI"
    result["pipeline_complete"] = bool(pipeline_complete)
    portal = str(
        document.get("portal")
        or document.get("source_portal")
        or document.get("origen")
        or ""
    ).strip().lower()
    uncertain_not_assignable = portal in {"toctoc", "toctoc.com"} and final == "UNCERTAIN"
    result["assignment_ready"] = bool(
        pipeline_complete
        and not result.get("classification_conflict")
        and str(result.get("conflict_state") or "").upper() != "IDENTITY_CONFLICT"
        and not uncertain_not_assignable
        and final not in {
            "BROKER_CONFIRMED", "BROKER_PROBABLE", "OUT_OF_SCOPE_NEW_DEVELOPMENT",
            "INVALID", "REMOVED", "EXPIRED",
        }
    )
    result["exclude_from_assignment"] = not result["assignment_ready"]
    if not result["assignment_ready"]:
        result["assignment_block_reasons"] = sorted(set(
            list(result.get("assignment_block_reasons") or [])
            + (["UNCERTAIN_PENDING_AI"] if not pipeline_complete else [])
            + (["classification_not_assignable"] if uncertain_not_assignable else [])
            + (["OUT_OF_SCOPE_NEW_DEVELOPMENT"] if final == "OUT_OF_SCOPE_NEW_DEVELOPMENT" else [])
        ))
    result["classifier_version"] = CLASSIFICATION_SERVICE_VERSION
    result["classified_at"] = _now()
    return result


def _pending_classification(reason: str, *, evidence: list[Any] | None = None) -> dict[str, Any]:
    pending_evidence = list(evidence or [])
    return {
        "state": "INCIERTO",
        "final_state": "INCIERTO",
        "final": "UNCERTAIN",
        "final_reason": reason,
        "final_evidence": pending_evidence,
        "canonical_classification_version": "canonical-classification-v1",
        "status": "UNCERTAIN_PENDING_AI",
        "source": "classification_service",
        "reason": reason,
        "evidence": pending_evidence,
        "confidence": 0.5,
        "pipeline_state": "UNCERTAIN_PENDING_AI",
        "pipeline_complete": False,
        "assignment_ready": False,
        "exclude_from_assignment": True,
        "assignment_block_reasons": ["UNCERTAIN_PENDING_AI"],
    }


def _default_deepseek_callable():
    try:
        from scrapers.scraper_toctoc.deepseek_classifier import classify_with_deepseek
    except ImportError:
        from deepseek_classifier import classify_with_deepseek
    return classify_with_deepseek


def _deepseek_output_tokens(result: Any, fallback: int) -> int:
    raw = getattr(result, "raw", None) or {}
    usage = raw.get("usage") if isinstance(raw, dict) else {}
    if isinstance(usage, dict):
        value = usage.get("completion_tokens") or usage.get("output_tokens")
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return fallback


def classify_capture(
    extracted: dict[str, Any],
    *,
    rule_context: dict[str, Any] | None = None,
    config: Any | None = None,
    db: Any | None = None,
    health: dict[str, Any] | None = None,
    cache: ClassificationCache | None = None,
    budget: AICostGuard | None = None,
    classification_hint: dict[str, Any] | None = None,
    human_broker_match: bool = False,
    cross_portal_match: bool = False,
    strong_text_broker: bool = False,
    deepseek_callable: Callable[..., Any] | None = None,
    allow_real_ai: bool = True,
) -> dict[str, Any]:
    """Classify one listing through the only authorized AI path."""
    document = dict(extracted or {})
    rule_context = dict(rule_context or {})
    config = config or SimpleNamespace(
        deepseek_enabled=False,
        deepseek_api_key="",
        deepseek_description_max_chars=6000,
        max_ai_calls_per_run=50,
        max_input_tokens_per_run=100000,
        max_output_tokens_per_run=20000,
        max_estimated_cost_per_run=5.0,
    )
    budget = budget or AICostGuard(budget=budget_from_config(config))
    cache = cache or ClassificationCache()
    scope_signal = detect_out_of_scope_new_development(document)
    id_type_signal = detect_toctoc_id_type_signal(document)
    structural = None if scope_signal else detect_hard_broker_signal(document, extracted=document)
    if id_type_signal and id_type_signal["kind"] == "BROKER" and not structural:
        structural = {
            "reason_code": "TOCTOC_IDTYPE_2_BROKER",
            "evidence": "client.idType=2",
            "evidence_type": "STRUCTURAL_BROKER",
            "evidence_strength": "HARD",
            "evidence_source": "detail_next_data.client.idType",
        }
    registry_match = document.get("broker_identity_match") or {}
    if db is not None and not scope_signal:
        registry_match = resolve_broker_identity(db, document)
    contact_identity = document.get("contact_identity") or {}
    human_broker_match = bool(
        human_broker_match
        or str(contact_identity.get("status") or "").upper() == "CORREDOR_CONFIRMED"
    )
    hint = classification_hint or {}
    hint_strength = str(
        hint.get("evidence_strength")
        or rule_context.get("evidence_strength")
        or ""
    ).upper()
    hint_state = str(hint.get("state") or hint.get("final_state") or "").upper()
    strong_text_broker = bool(
        (strong_text_broker or rule_context.get("strong_text_broker"))
        and (
            hint_strength in {"HARD", "STRONG"}
            or (hint_strength == "" and hint_state == "CORREDOR_SEGURO")
        )
    )
    fingerprint = classification_fingerprint(document)
    base_result = {
        "fingerprint": fingerprint,
        "health": health or {"healthy": True, "degraded": False, "implicit": True},
        "ai_called": False,
        "cache_hit": False,
        "registry_match": registry_match,
        "metrics": budget.report(),
    }

    removed_state = _is_removed_or_invalid(document)
    if removed_state:
        classification = _classification_with_canonical(
            document,
            {"state": removed_state, "source": "operational_gate", "reason": removed_state},
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            reason=f"{removed_state}_LISTING",
            pipeline_complete=True,
        )
        base_result.update({"classification": classification, "reason": removed_state, "metrics": budget.report()})
        return base_result

    if scope_signal:
        classification = _classification_with_canonical(
            document,
            {
                "state": "OUT_OF_SCOPE_NEW_DEVELOPMENT",
                "source": "scope_rules",
                "reason": scope_signal["reason_code"],
                "evidence": scope_signal["evidence"],
                "ai_eligible": False,
                "out_of_scope": True,
            },
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            reason=scope_signal["reason_code"],
            evidence=scope_signal["evidence"],
            pipeline_complete=True,
        )
        classification["ai_eligible"] = False
        classification["out_of_scope"] = True
        base_result.update({"classification": classification, "reason": "out_of_scope_new_development", "metrics": budget.report()})
        return base_result

    owner_structural = bool(id_type_signal and id_type_signal["kind"] == "OWNER")
    owner_broker_evidence = bool(
        structural
        or registry_match.get("matched")
        or registry_match.get("conflict")
        or human_broker_match
        or cross_portal_match
        or strong_text_broker
        or str(contact_identity.get("status") or "").upper() == "CORREDOR_CONFIRMED"
    )
    if owner_structural and owner_broker_evidence:
        budget.skipped_identity += 1
        conflict_evidence = list(id_type_signal.get("evidence") or [])
        if structural:
            conflict_evidence.append(structural.get("evidence") or structural.get("reason_code"))
        if registry_match.get("matched") or registry_match.get("conflict"):
            conflict_evidence.extend(registry_match.get("evidence") or [])
        classification = _classification_with_canonical(
            document,
            {
                "state": "INCIERTO",
                "source": "evidence_precedence",
                "reason": "IDENTITY_CONFLICT",
                "evidence": conflict_evidence,
                "classification_conflict": True,
                "conflict_state": "IDENTITY_CONFLICT",
                "evidence_type": "IDENTITY_CONFLICT",
                "evidence_strength": "CONFLICT",
                "evidence_source": "owner_structural_vs_broker_evidence",
            },
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            reason="IDENTITY_CONFLICT",
            evidence=conflict_evidence,
            pipeline_complete=True,
        )
        classification["classification_conflict"] = True
        classification["conflict_state"] = "IDENTITY_CONFLICT"
        classification["assignment_ready"] = False
        classification["exclude_from_assignment"] = True
        classification["assignment_block_reasons"] = ["IDENTITY_CONFLICT"]
        base_result.update({"classification": classification, "reason": "identity_conflict", "metrics": budget.report()})
        return base_result

    if structural:
        budget.skipped_structural += 1
        classification = _classification_with_canonical(
            document,
            {
                "state": "CORREDOR_SEGURO",
                "source": "structural_rules",
                "hard_veto": "PROFESSIONAL",
                "hard_broker_signal": True,
                "evidence": [structural.get("evidence", "")],
                "reason": structural.get("reason_code", "STRUCTURAL_BROKER_VETO"),
            },
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            reason="STRUCTURAL_BROKER_VETO",
            evidence=[structural.get("evidence", "")],
            pipeline_complete=True,
        )
        base_result.update({"classification": classification, "reason": "structural_broker", "metrics": budget.report()})
        return base_result

    if registry_match.get("matched") or human_broker_match or cross_portal_match:
        budget.skipped_identity += 1
        classification = _classification_with_canonical(
            document,
            {
                "state": "CORREDOR_SEGURO",
                "source": "broker_identity_registry",
                "evidence": list(registry_match.get("evidence") or []),
                "reason": registry_match.get("match_type") or "HUMAN_CONFIRMED_BROKER",
            },
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            reason=registry_match.get("match_type") or "HUMAN_CONFIRMED_BROKER",
            pipeline_complete=True,
        )
        base_result.update({"classification": classification, "reason": "identity_match", "metrics": budget.report()})
        return base_result

    if owner_structural:
        budget.skipped_structural += 1
        classification = _classification_with_canonical(
            document,
            {
                "state": "DUEÑO_PROBABLE",
                "source": "toctoc_id_type",
                "reason": "TOCTOC_IDTYPE_1_OWNER_CANDIDATE",
                "evidence": list(id_type_signal.get("evidence") or []),
                "evidence_type": id_type_signal["evidence_type"],
                "evidence_strength": id_type_signal["evidence_strength"],
                "evidence_source": id_type_signal["evidence_source"],
                # The shared assignment gate reserves >=0.90 for the
                # DUEÑO_SEGURO state.  idType=1 is strong owner evidence, but
                # remains DUEÑO_PROBABLE until the portal supplies a stronger
                # ownership confirmation.
                "owner_probability": 0.89,
            },
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            reason="TOCTOC_IDTYPE_1_OWNER_CANDIDATE",
            evidence=list(id_type_signal.get("evidence") or []),
            pipeline_complete=True,
        )
        classification["ai_eligible"] = False
        base_result.update({"classification": classification, "reason": "toctoc_idtype_owner", "metrics": budget.report()})
        return base_result

    if strong_text_broker:
        budget.skipped_text_rule += 1
        classification = _classification_with_canonical(
            document,
            classification_hint or {"state": "CORREDOR_SEGURO", "source": "text_rules"},
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=True,
            reason="STRONG_TEXT_BROKER",
            pipeline_complete=True,
        )
        base_result.update({"classification": classification, "reason": "strong_text_rule", "metrics": budget.report()})
        return base_result

    cached = cache.get(fingerprint)
    if cached and isinstance(cached.get("classification"), dict):
        budget.cache_hits += 1
        classification = _classification_with_canonical(
            document,
            dict(cached["classification"]),
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            cache_hit=True,
            reason="VALID_CLASSIFICATION_CACHE",
            evidence=cached.get("evidence") or [],
            pipeline_complete=True,
        )
        base_result.update({"classification": classification, "reason": "cache_hit", "cache_hit": True, "metrics": budget.report()})
        return base_result

    if classification_hint and str(
        classification_hint.get("status") or ""
    ).upper() not in {"PENDING_LLM", "PENDING_SEMANTIC_REVIEW", "SEMANTIC_CLASSIFICATION_FAILED"}:
        hinted_state = str(classification_hint.get("state") or classification_hint.get("final_state") or "").upper()
        hinted_probable_is_weak = (
            hinted_state == "CORREDOR_PROBABLE"
            and hint_strength not in {"HARD", "STRONG"}
        )
        if (
            hinted_state
            and hinted_state not in {"INCIERTO", "INCONCLUSIVE", "PENDIENTE"}
            and not hinted_probable_is_weak
        ):
            classification = _classification_with_canonical(
                document,
                classification_hint,
                registry_match=registry_match,
                human_broker_match=human_broker_match,
                cross_portal_match=cross_portal_match,
                strong_text_broker=False,
                pipeline_complete=True,
            )
            cache.put(fingerprint, classification, input_payload=document)
            base_result.update({"classification": classification, "reason": "deterministic_hint", "metrics": budget.report()})
            return base_result

    if not _health_is_healthy(health):
        reason = "EXTRACTOR_DEGRADED" if health and health.get("degraded") else "EXTRACTOR_HEALTH_UNAVAILABLE"
        classification = _pending_classification(reason)
        base_result.update({"classification": classification, "reason": reason, "metrics": budget.report()})
        return base_result

    description = str(document.get("description") or document.get("descripcion") or "")
    input_tokens = estimate_tokens(json.dumps({
        "publisher": document.get("publicador_visible") or document.get("seller_name"),
        "seller_type": document.get("seller_type"),
        "title": document.get("title") or document.get("titulo"),
        "description": description,
        "rule_context": rule_context,
    }, ensure_ascii=False, default=str))
    output_tokens = int(getattr(config, "deepseek_max_tokens", 500) or 500)
    budget.mark_attempted()
    budget_decision = budget.check_budget(input_tokens, output_tokens)
    if not budget_decision["allowed"]:
        classification = _pending_classification(
            "AI_BUDGET_EXCEEDED:" + ",".join(budget_decision["reasons"])
        )
        base_result.update({"classification": classification, "reason": "AI_BUDGET_EXCEEDED", "metrics": budget.report()})
        return base_result

    if not allow_real_ai or not bool(getattr(config, "deepseek_enabled", False)) or not getattr(config, "deepseek_api_key", ""):
        # A healthy, fully extracted residual with no deterministic broker
        # evidence is a valid INCIERTO business candidate.  DeepSeek being
        # disabled is an operating policy, not an extraction failure; keep
        # the existing classification name and complete the local pipeline so
        # the normal CRM flow can handle it.
        classification = _classification_with_canonical(
            document,
            {
                "state": "INCIERTO",
                "source": "classification_service",
                "reason": "INCONCLUSIVE",
                "evidence": [],
                "confidence": 0.5,
            },
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            reason="INCONCLUSIVE",
            evidence=[],
            pipeline_complete=True,
        )
        base_result.update({"classification": classification, "reason": "deterministic_uncertain", "metrics": budget.report()})
        return base_result

    callable_ = deepseek_callable or _default_deepseek_callable()
    budget.mark_executed(input_tokens, 0)
    try:
        result = callable_(
            document,
            rule_context,
            config,
            authorization_token=classification_service_token(),
        )
        output_tokens = _deepseek_output_tokens(result, output_tokens)
        # Correct the provisional zero-output accounting without ever allowing
        # the result to exceed the budget silently.
        budget.output_tokens += output_tokens
        budget.estimated_cost = round(
            budget.estimated_cost + budget.estimate_cost(0, output_tokens), 8
        )
        if result is None or str(getattr(result, "status", "")).upper() != "VALID":
            status = str(getattr(result, "status", "ERROR") or "ERROR")
            classification = _pending_classification(
                f"DEEPSEEK_{status}", evidence=list(getattr(result, "evidence", []) or [])
            )
            base_result.update({"classification": classification, "reason": "deepseek_error", "metrics": budget.report()})
            return base_result
        classification = _classification_with_canonical(
            document,
            {
                "state": getattr(result, "state", "INCIERTO"),
                "confidence": getattr(result, "confidence", 0.5),
                "source": "deepseek",
                "reason": getattr(result, "reason", ""),
                "evidence": list(getattr(result, "evidence", []) or []),
                "deepseek_status": getattr(result, "status", "VALID"),
                "deepseek_raw": getattr(result, "raw", {}) or {},
            },
            registry_match=registry_match,
            human_broker_match=human_broker_match,
            cross_portal_match=cross_portal_match,
            strong_text_broker=False,
            ai_state=getattr(result, "state", "INCIERTO"),
            evidence=list(getattr(result, "evidence", []) or []),
            pipeline_complete=True,
        )
        cache.put(fingerprint, classification, input_payload=document)
        base_result.update({"classification": classification, "reason": "deepseek_valid", "ai_called": True, "metrics": budget.report()})
        return base_result
    except Exception as exc:
        classification = _pending_classification(f"DEEPSEEK_ERROR:{type(exc).__name__}")
        base_result.update({"classification": classification, "reason": "deepseek_exception", "metrics": budget.report()})
        return base_result
