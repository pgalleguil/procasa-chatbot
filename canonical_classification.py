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


def _toctoc_id_type(document: dict[str, Any]) -> str:
    portal = str(
        document.get("portal")
        or document.get("source_portal")
        or document.get("origen")
        or ""
    ).strip().lower()
    if portal and portal not in {"toctoc", "toctoc.com"}:
        return ""
    return str(
        document.get("seller_id_type_raw")
        or document.get("seller_id_type")
        or ""
    ).strip()


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
    id_type = _toctoc_id_type(document)
    owner_structural = id_type == "1"
    broker_structural = id_type == "2"
    new_development_structural = id_type == "3"
    current = (
        classification.get("final")
        or classification.get("final_state")
        or classification.get("state")
        or classification.get("rule_state")
    )
    evidence_strength = str(classification.get("evidence_strength") or "").upper()
    declared_conflict = bool(
        classification.get("classification_conflict")
        or str(classification.get("conflict_state") or "").upper() == "IDENTITY_CONFLICT"
    )
    effective_strong_text = bool(
        strong_text_broker
        and (
            evidence_strength in {"HARD", "STRONG"}
            or (evidence_strength == "" and _state(current) == "CORREDOR_SEGURO")
        )
    )
    broker_evidence_for_owner = bool(
        structural
        or broker_structural
        or registry_matched
        or registry_conflict
        or human_broker_match
        or cross_portal_match
        or phone_human_broker
        or effective_strong_text
    )
    if declared_conflict:
        final = "UNCERTAIN"
        final_reason = reason or "IDENTITY_CONFLICT"
        final_evidence = list(evidence or classification.get("evidence") or [])
    elif owner_structural and broker_evidence_for_owner:
        final = "UNCERTAIN"
        final_reason = "IDENTITY_CONFLICT"
        final_evidence = ["client.idType=1"]
        if structural:
            final_evidence.append(structural.get("evidence") or structural.get("reason_code"))
        if registry_matched or registry_conflict:
            final_evidence.extend((registry_match or {}).get("evidence") or [])
        if effective_strong_text:
            final_evidence.extend(evidence or [])
    elif new_development_structural or _state(current) in {"OUT_OF_SCOPE_NEW_DEVELOPMENT", "FUERA_DE_ALCANCE"}:
        final = "OUT_OF_SCOPE_NEW_DEVELOPMENT"
        final_reason = reason or "OUT_OF_SCOPE_NEW_DEVELOPMENT"
        final_evidence = list(evidence or classification.get("evidence") or [])
    elif broker_structural:
        final = "BROKER_CONFIRMED"
        final_reason = "TOCTOC_IDTYPE_2_BROKER"
        final_evidence = ["client.idType=2"]
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
    elif owner_structural:
        final = "OWNER_PROBABLE"
        final_reason = "TOCTOC_IDTYPE_1_OWNER_CANDIDATE"
        final_evidence = ["client.idType=1"]
    elif effective_strong_text:
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
    if declared_conflict or (owner_structural and broker_evidence_for_owner):
        classification.update({
            "classification_conflict": True,
            "conflict_state": "IDENTITY_CONFLICT",
            "evidence_type": "IDENTITY_CONFLICT",
            "evidence_strength": "CONFLICT",
            "evidence_source": "owner_structural_vs_broker_evidence",
        })
    elif new_development_structural:
        classification.update({
            "evidence_type": "OUT_OF_SCOPE_SCOPE",
            "evidence_strength": "HARD",
            "evidence_source": "detail_next_data.client.idType",
        })
    elif broker_structural:
        classification.update({
            "evidence_type": "STRUCTURAL_BROKER",
            "evidence_strength": "HARD",
            "evidence_source": "detail_next_data.client.idType",
        })
    elif owner_structural:
        classification.update({
            "evidence_type": "EXPLICIT_OWNER_STRUCTURAL",
            "evidence_strength": "EXPLICIT_OWNER_STRUCTURAL",
            "evidence_source": "detail_next_data.client.idType",
        })
    elif effective_strong_text:
        classification.update({
            "evidence_type": "STRONG_TEXT_BROKER",
            "evidence_strength": "STRONG",
            "evidence_source": "text_rules",
        })
    elif classification.get("evidence_type") is None:
        classification.update({
            "evidence_type": "WEAK_TEXT_SIGNAL" if evidence_strength == "WEAK" else "NONE",
            "evidence_strength": evidence_strength or "NONE",
            "evidence_source": classification.get("source") or "classifier",
        })
    if structural:
        classification["hard_veto"] = "PROFESSIONAL"
        classification["professional_hard_veto"] = True
    return classification


def is_canonical_broker(document: dict[str, Any]) -> bool:
    return str((document.get("classification") or {}).get("final") or "").upper() in BROKER_CANONICAL_STATES
