"""Canonical classification contract shared by portal pipelines and CRM."""
from __future__ import annotations

from typing import Any, Iterable

from broker_identity import detect_hard_broker_signal


CANONICAL_CLASSIFICATION_VERSION = "canonical-classification-v1"
CANONICAL_STATES = frozenset({
    "OWNER_CONFIRMED",
    "OWNER_PROBABLE",
    "BROKER_CONFIRMED",
    "BROKER_PROBABLE",
    "UNCERTAIN",
    "OUT_OF_SCOPE_NEW_DEVELOPMENT",
    "INVALID",
    "REMOVED",
    "EXPIRED",
})

BROKER_CANONICAL_STATES = frozenset({"BROKER_CONFIRMED", "BROKER_PROBABLE"})


def _state(value: Any) -> str:
    return str(value or "").strip().upper().replace("DUENO", "DUEÑO")


def _as_evidence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def legacy_to_canonical(value: Any) -> str:
    state = _state(value)
    return {
        "OWNER_CONFIRMED": "OWNER_CONFIRMED",
        "OWNER_PROBABLE": "OWNER_PROBABLE",
        "BROKER_CONFIRMED": "BROKER_CONFIRMED",
        "BROKER_PROBABLE": "BROKER_PROBABLE",
        "DUEÑO_SEGURO": "OWNER_CONFIRMED",
        "DUEÑO_PROBABLE": "OWNER_PROBABLE",
        "CORREDOR_SEGURO": "BROKER_CONFIRMED",
        "CORREDOR_CONFIRMED": "BROKER_CONFIRMED",
        "CORREDOR_PROBABLE": "BROKER_PROBABLE",
        "AD_REMOVED": "REMOVED",
        "REMOVED": "REMOVED",
        "INVALID": "INVALID",
        "BLOCKED": "INVALID",
        "EXPIRED": "EXPIRED",
        "PUBLICACION_EXPIRADA": "EXPIRED",
        "INCIERTO": "UNCERTAIN",
        "INCONCLUSIVE": "UNCERTAIN",
        "UNCERTAIN": "UNCERTAIN",
        "OUT_OF_SCOPE_NEW_DEVELOPMENT": "OUT_OF_SCOPE_NEW_DEVELOPMENT",
        "FUERA_DE_ALCANCE": "OUT_OF_SCOPE_NEW_DEVELOPMENT",
        "PENDIENTE": "UNCERTAIN",
    }.get(state, "UNCERTAIN")


def canonicalize_classification(
    document: dict[str, Any],
    *,
    registry_match: dict[str, Any] | None = None,
    human_broker_match: bool = False,
    cross_portal_match: bool = False,
    strong_text_broker: bool = False,
    cache_hit: bool = False,
    ai_state: Any = None,
    evidence: Iterable[Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Return a canonical result using strict broker-veto precedence."""
    classification = dict(document.get("classification") or {})
    structural = detect_hard_broker_signal(document, extracted=document)
    contact_identity = document.get("contact_identity") or {}
    phone_human_broker = str(contact_identity.get("status") or "").upper() == "CORREDOR_CONFIRMED"
    registry_matched = bool((registry_match or {}).get("matched"))
    registry_conflict = bool((registry_match or {}).get("conflict"))
    current = (
        classification.get("final")
        or classification.get("final_state")
        or classification.get("state")
        or classification.get("rule_state")
    )

    if _state(current) == "OUT_OF_SCOPE_NEW_DEVELOPMENT" or _state(current) == "FUERA_DE_ALCANCE":
        final = "OUT_OF_SCOPE_NEW_DEVELOPMENT"
        final_reason = reason or "OUT_OF_SCOPE_NEW_DEVELOPMENT"
        final_evidence = list(evidence or classification.get("evidence") or [])
    elif structural:
        final = "BROKER_CONFIRMED"
        final_reason = "STRUCTURAL_BROKER_VETO"
        final_evidence = [structural.get("evidence") or structural.get("reason_code")]
    elif registry_matched or human_broker_match or cross_portal_match or phone_human_broker:
        final = "BROKER_CONFIRMED"
        if registry_matched:
            final_reason = str((registry_match or {}).get("match_type") or "UNIVERSAL_BROKER_IDENTITY")
        elif phone_human_broker:
            final_reason = "HUMAN_CONFIRMED_PHONE"
        else:
            final_reason = "HUMAN_CONFIRMED_BROKER" if human_broker_match else "CROSS_PORTAL_BROKER_IDENTITY"
        final_evidence = list((registry_match or {}).get("evidence") or [])
    elif registry_conflict:
        final = "UNCERTAIN"
        final_reason = "BROKER_IDENTITY_CONFLICT"
        final_evidence = list((registry_match or {}).get("evidence") or [])
    elif strong_text_broker:
        final = "BROKER_CONFIRMED"
        final_reason = "STRONG_TEXT_BROKER"
        final_evidence = list(evidence or [])
    elif cache_hit:
        cached = (classification.get("cached_final") or classification.get("final") or current)
        final = legacy_to_canonical(cached)
        final_reason = reason or "VALID_CLASSIFICATION_CACHE"
        final_evidence = list(evidence or classification.get("evidence") or [])
    elif ai_state is not None:
        final = legacy_to_canonical(ai_state)
        final_reason = reason or "AI_RESIDUAL_CLASSIFICATION"
        final_evidence = list(evidence or [])
    else:
        final = legacy_to_canonical(current)
        final_reason = reason or str(classification.get("reason") or "CANONICALIZED_LEGACY_STATE")
        final_evidence = list(evidence or classification.get("evidence") or [])

    final_evidence = [str(item) for item in final_evidence if str(item).strip()]
    classification.update({
        "final": final,
        "final_reason": final_reason,
        "final_evidence": final_evidence,
        "canonical_classification_version": CANONICAL_CLASSIFICATION_VERSION,
        "hard_broker_veto": bool(structural),
        "broker_identity_match": registry_matched or human_broker_match or cross_portal_match or phone_human_broker,
    })
    if structural:
        classification["hard_veto"] = "PROFESSIONAL"
        classification["professional_hard_veto"] = True
    return classification


def is_canonical_broker(document: dict[str, Any]) -> bool:
    return str((document.get("classification") or {}).get("final") or "").upper() in BROKER_CANONICAL_STATES
