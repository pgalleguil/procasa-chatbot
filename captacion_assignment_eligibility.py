"""Central, auditable assignment gate.

Each portal keeps its own classifier and persisted Mongo state. This module
only applies the shared post-classification policy: owner-strong,
owner-probable, and uncertain listings may enter the operational pool;
probable/strong broker states and invalid or blocked documents may not.
Human-confirmed broker phone identity remains a higher-priority exclusion.
"""
from __future__ import annotations

import re
import logging
from datetime import datetime, timezone
from typing import Any
from config import Config
from broker_identity import detect_hard_broker_signal
from canonical_classification import BROKER_CANONICAL_STATES


ASSIGNMENT_GATE_VERSION = "global-assignment-gate-v1-incierto-assignable"
CHILEPROPIEDADES_ORIGINS = frozenset({"chilepropiedades", "chilepropiedades.cl"})
OWNER_STATES = frozenset({"DUEÑO_SEGURO", "DUEÑO_PROBABLE"})
ASSIGNABLE_STATES = OWNER_STATES | frozenset({"INCIERTO"})
CLASSIFICATION_ASSIGNMENT_PRIORITY = {
    "DUEÑO_SEGURO": 0,
    "DUEÑO_PROBABLE": 1,
    "INCIERTO": 2,
}
# Kept for callers that imported the old constant; the CP decision itself uses
# the normalized ASSIGNABLE_STATES set above.
FINAL_STATES = frozenset({"DUEÑO_SEGURO", "DUENO_SEGURO", "DUEÑO_PROBABLE", "DUENO_PROBABLE"})
COMMERCIAL_TERMS = ("inmobiliaria", "corredor", "corredora", "propiedades", "real estate", "broker")
COMMERCIAL_IDENTITY_FIELDS = (
    "company_name",
    "broker_brand",
    "contact_logo_alt",
    "publicador_visible",
    "contact_badges_text",
)
logger = logging.getLogger(__name__)


def is_chilepropiedades_document(doc: dict[str, Any]) -> bool:
    origen = str(doc.get("origen") or "").strip().lower()
    portal = str(doc.get("source_portal") or "").strip().lower()
    return origen in CHILEPROPIEDADES_ORIGINS or portal in CHILEPROPIEDADES_ORIGINS


def _toctoc_id_type(doc: dict[str, Any]) -> str:
    portal = str(
        doc.get("portal")
        or doc.get("source_portal")
        or doc.get("origen")
        or ""
    ).strip().lower()
    if portal not in {"toctoc", "toctoc.com"}:
        return ""
    return str(
        doc.get("seller_id_type_raw")
        or doc.get("seller_id_type")
        or ""
    ).strip()


def normalize_classification_state(value: Any) -> str:
    state = str(value or "").strip().upper()
    return state.replace("DUENO", "DUEÑO")


def assignment_classification_priority(document: dict[str, Any]) -> int:
    """Return the stable operational order without creating a new score."""
    classification = document.get("classification") or {}
    state = normalize_classification_state(
        classification.get("state") or classification.get("final_state")
    )
    return CLASSIFICATION_ASSIGNMENT_PRIORITY.get(state, len(CLASSIFICATION_ASSIGNMENT_PRIORITY))


def _legacy_assignment_eligibility(doc: dict[str, Any]) -> tuple[bool, list[str]]:
    """Apply the global gate while preserving portal-specific classification.

    Historical ``assignment_ready`` and owner-probability fields were written
    by portal-specific pipelines. They remain compatibility checks for owner
    states. A clean ``INCIERTO`` with a complete, auditable pipeline is
    eligible for human validation unless an independent blocker applies.
    """
    cls = doc.get("classification") or {}
    gestion = doc.get("gestion") or {}
    reasons: list[str] = []
    state = normalize_classification_state(cls.get("state") or cls.get("final_state"))
    hard_broker_signal = detect_hard_broker_signal(doc, extracted=doc)
    toctoc_id_type = _toctoc_id_type(doc)
    if toctoc_id_type == "2":
        reasons.append("toctoc_id_type_broker")
    elif toctoc_id_type == "3":
        reasons.append("out_of_scope_new_development")
    if hard_broker_signal:
        reasons.append("hard_broker_publisher_veto")
    if state not in FINAL_STATES and state != "INCIERTO":
        reasons.append("classification_not_assignable")
    if state != "INCIERTO" and cls.get("assignment_ready") is not True:
        reasons.append("classification_not_final_or_not_persisted")
    if gestion.get("semantic_review_hold") is True or cls.get("manual_review_required") is True or doc.get("manual_review_required") is True:
        reasons.append("manual_review_pending")
    if (state != "INCIERTO" and cls.get("exclude_from_assignment") is True) or gestion.get("exclude_from_assignment") is True:
        reasons.append("explicitly_excluded")
    if state != "INCIERTO":
        try:
            owner_probability = float(cls.get("owner_probability"))
        except (TypeError, ValueError):
            owner_probability = None
        if owner_probability is None:
            reasons.append("owner_probability_missing")
        elif state == "DUEÑO_PROBABLE":
            if owner_probability < 0.70 or owner_probability >= 0.90:
                reasons.append("owner_probability_inconsistent_with_state")
        elif state == "DUEÑO_SEGURO" and owner_probability < 0.90:
            reasons.append("owner_probability_inconsistent_with_state")
    stage = str(doc.get("scrape_stage") or "").lower()
    html_status = str(doc.get("html_validation_status") or "").upper()
    pipeline_state = str(
        doc.get("pipeline_state")
        or cls.get("pipeline_state")
        or ""
    ).strip().upper()
    if state == "AD_REMOVED" or stage in {"ad_removed", "needs_rescrape", "incomplete", "processing_blocked", "classified_from_listing"}:
        reasons.append("removed_or_incomplete")
    if stage in {"needs_rescrape", "incomplete", "processing_blocked"}:
        reasons.append("extraction_incomplete")
    if html_status in {"LISTING_REMOVED", "INVALID", "BLOCKED"}:
        reasons.append("invalid_source_document")
        reasons.append("extraction_incomplete")
    if doc.get("block_reason") or stage == "processing_blocked":
        reasons.append("processing_blocked")
    if pipeline_state in {"EXTRACTED_ONLY", "CLASSIFYING", "UNCERTAIN_PENDING_AI", "EXTRACTOR_DEGRADED"}:
        reasons.append("pipeline_incomplete")
    if str(doc.get("extractor_health") or cls.get("extractor_health") or "").upper() in {"DEGRADED", "FAILED", "UNAVAILABLE"}:
        reasons.append("extraction_incomplete")
    source_cls = str(cls.get("source") or "").lower()
    if source_cls == "url_path_signal":
        reasons.append("classification_from_url_path_only")
    if not str(doc.get("descripcion") or doc.get("description") or "").strip() and not str(doc.get("title") or "").strip():
        reasons.append("missing_essential_fields")
    if not (doc.get("listing_id") or doc.get("url") or doc.get("source_url")):
        reasons.append("missing_essential_fields")
    if not (doc.get("comuna_slug") or doc.get("comuna")):
        reasons.append("missing_essential_fields")
    commercial_values = " ".join(str(doc.get(key) or "") for key in (
        "company_name", "broker_brand", "publicador_visible", "contact_logo_alt",
        "listing_advertiser", "seller_jsonld_name",
    )).lower()
    profile = cls.get("publisher_profile_context") or doc.get("publisher_profile_context") or {}
    if profile.get("commercial_identity_confirmed") or profile.get("confirmed_broker_count", 0):
        reasons.append("commercial_identity_or_profile")
    elif any(term in commercial_values for term in COMMERCIAL_TERMS):
        reasons.append("commercial_identity_or_profile")
    source = str(cls.get("decision_source") or cls.get("source") or "").lower()
    ds_status = str(cls.get("deepseek_status") or "").upper()
    trace = cls.get("trace") or {}
    manual_approved = bool(cls.get("manual_review_approved") or trace.get("manual_review_approved"))
    deterministic = source in {
        "structural_rules", "rules_json", "html_validation", "profile_correlation",
        "rules", "rules_fallback", "toctoc_id_type", "portal_structure",
    }
    deepseek_persisted = source == "deepseek" and ds_status == "VALID" and bool(trace.get("deepseek_raw") or cls.get("deepseek_raw"))
    projected_auditable = doc.get("_active_workable_auditable_final_decision")
    if projected_auditable is True:
        auditable_final_decision = True
    elif projected_auditable is False:
        auditable_final_decision = False
    else:
        auditable_final_decision = bool(
            manual_approved
            or deterministic
            or deepseek_persisted
        )
    # A clean, fully completed classification may remain INCIERTO for human
    # validation. Technical pending/failure states are blocked above.
    evidence_engine_complete = (
        state == "INCIERTO"
        and str(cls.get("owner_probability_source") or "").lower() == "deterministic_evidence_engine"
        and (cls.get("owner_probability_completeness") or {}).get("complete") is True
        and cls.get("owner_probability") is not None
    )
    v5_rule_decision = (
        state == "INCIERTO"
        and str(cls.get("version") or "").lower() == "v5-rule-based"
        and bool(cls.get("reason") or cls.get("evidence"))
    )
    clean_uncertain_with_complete_pipeline = (
        state == "INCIERTO"
        and pipeline_state == "CLASSIFIED"
        and cls.get("pipeline_complete") is True
        and "pipeline_incomplete" not in reasons
        and "extraction_incomplete" not in reasons
    )
    if not (
        auditable_final_decision
        or evidence_engine_complete
        or v5_rule_decision
        or clean_uncertain_with_complete_pipeline
    ):
        reasons.append("no_auditable_final_decision")
    return not reasons, sorted(set(reasons))


def _contact_identity_reasons(contact_identity: dict[str, Any] | None) -> list[str]:
    if not contact_identity:
        return []
    status = str(contact_identity.get("status") or "").upper()
    broker_count = int(contact_identity.get("confirmed_corredor_count") or 0)
    if status == "CONFLICT":
        return ["contact_identity_conflict"]
    if (status == "CORREDOR_CONFIRMED" or broker_count) and status != "OWNER_CONFIRMED":
        return ["contact_identity_broker_confirmed"]
    return []


def _phone_identity_audit(
    document: dict[str, Any],
    contact_identity: dict[str, Any],
    *,
    original_state: str,
    effective_state: str,
    reason: str,
) -> dict[str, Any]:
    """Build the auditable payload for a phone-based assignment block."""
    phone = str(contact_identity.get("phone_normalized") or "")
    evidence = list(contact_identity.get("evidence") or [])
    return {
        "audit_version": "phone-learning-block-audit-v1",
        "property_id": str(document.get("_id") or document.get("listing_id") or document.get("url") or ""),
        "portal": str(document.get("origen") or document.get("source_portal") or "").strip().lower(),
        "evaluated_at": datetime.now(timezone.utc),
        "phone_masked": f"***{phone[-4:]}" if phone else "(sin teléfono)",
        "identity_key": contact_identity.get("identity_key") or (f"phone:{phone}" if phone else ""),
        "identity_status": contact_identity.get("status") or "",
        "identity_event_ids": [str(item) for item in contact_identity.get("evidence_event_ids") or []],
        "identity_event_timestamps": [
            item.get("occurred_at") for item in evidence if item.get("occurred_at") is not None
        ],
        "identity_last_human_confirmation_at": contact_identity.get("last_human_confirmation_at"),
        "phone_original_value": contact_identity.get("phone_original_value") or "",
        "original_state": original_state,
        "effective_state": effective_state,
        "reason": reason,
    }


def calculate_assignment_eligibility(
    document: dict[str, Any], *, contact_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the complete assignment decision for one document.

    The portal's original state is never rewritten. The global
    post-classification policy is applied here, including the
    human-confirmed broker phone gate.
    """
    if not is_chilepropiedades_document(document):
        hard_broker_signal = detect_hard_broker_signal(document, extracted=document)
        eligible, reasons = _legacy_assignment_eligibility(document)
        if getattr(Config, "PHONE_LEARNING_ENABLED", False):
            identity_reasons = _contact_identity_reasons(contact_identity)
            reasons = sorted(set(reasons + identity_reasons))
        else:
            identity_reasons = []
        original_state = normalize_classification_state((document.get("classification") or {}).get("state"))
        if "contact_identity_broker_confirmed" in identity_reasons:
            effective_state = "CORREDOR_SEGURO"
            effective_reason = "PHONE_HUMAN_CONFIRMED_BROKER"
        elif hard_broker_signal:
            effective_state = "CORREDOR_SEGURO"
            effective_reason = "HARD_BROKER_PUBLISHER_VETO"
        elif "contact_identity_conflict" in identity_reasons:
            effective_state = "INCIERTO"
            effective_reason = "CONTACT_IDENTITY_CONFLICT"
        else:
            effective_state = original_state
            effective_reason = str((document.get("classification") or {}).get("reason") or "CLASSIFICATION_ORIGINAL")
        identity_audit = None
        if contact_identity and identity_reasons:
            identity_audit = _phone_identity_audit(
                document,
                contact_identity,
                original_state=original_state,
                effective_state=effective_state,
                reason=effective_reason,
            )
        return {
            "assignment_ready": not reasons,
            "exclude_from_assignment": bool(reasons),
            "assignment_block_reasons": reasons,
            "eligibility_version": ASSIGNMENT_GATE_VERSION,
            "effective_state": effective_state,
            "effective_reason": effective_reason,
            "phone_identity_audit": identity_audit,
        }

    cls = document.get("classification") or {}
    state = normalize_classification_state(cls.get("state") or cls.get("final_state"))
    hard_broker_signal = detect_hard_broker_signal(document, extracted=document)
    if not getattr(Config, "PHONE_LEARNING_ENABLED", False):
        contact_identity = None
    reasons = _contact_identity_reasons(contact_identity)
    if hard_broker_signal:
        reasons.append("hard_broker_publisher_veto")
    if state not in ASSIGNABLE_STATES:
        reasons.append("classification_not_assignable")

    stage = str(document.get("scrape_stage") or "").lower()
    html_status = str(document.get("html_validation_status") or "").upper()
    if state in {"AD_REMOVED", "BLOCKED", "INVALID"} or stage in {"ad_removed", "needs_rescrape", "incomplete", "processing_blocked"}:
        reasons.append("removed_or_invalid_document")
    if html_status in {"LISTING_REMOVED", "INVALID", "BLOCKED"}:
        reasons.append("invalid_source_document")
    if document.get("block_reason") or stage == "processing_blocked":
        reasons.append("processing_blocked")
    if (document.get("gestion") or {}).get("semantic_review_hold") is True or cls.get("manual_review_required") is True or document.get("manual_review_required") is True:
        reasons.append("manual_review_pending")
    if not (document.get("listing_id") or document.get("url") or document.get("source_url")):
        reasons.append("missing_essential_fields")
    if not (document.get("comuna_slug") or document.get("comuna")):
        reasons.append("missing_essential_fields")
    if not str(document.get("title") or document.get("description") or document.get("descripcion") or "").strip():
        reasons.append("missing_essential_fields")

    commercial_values = " ".join(
        str(document.get(key) or "")
        for key in COMMERCIAL_IDENTITY_FIELDS
    ).lower()
    profile = cls.get("publisher_profile_context") or document.get("publisher_profile_context") or {}
    if profile.get("commercial_identity_confirmed") or profile.get("confirmed_broker_count", 0):
        reasons.append("commercial_identity_or_profile")
    elif any(re.search(rf"\b{re.escape(term)}\b", commercial_values) for term in COMMERCIAL_TERMS):
        reasons.append("commercial_identity_or_profile")

    reasons = sorted(set(reasons))
    if "contact_identity_broker_confirmed" in reasons:
        effective_state = "CORREDOR_SEGURO"
        effective_reason = "PHONE_HUMAN_CONFIRMED_BROKER"
    elif hard_broker_signal:
        effective_state = "CORREDOR_SEGURO"
        effective_reason = "HARD_BROKER_PUBLISHER_VETO"
    elif "contact_identity_conflict" in reasons:
        effective_state = "INCIERTO"
        effective_reason = "CONTACT_IDENTITY_CONFLICT"
    else:
        effective_state = state
        effective_reason = str(cls.get("reason") or "CLASSIFICATION_ORIGINAL")
    identity_audit = None
    if contact_identity and {"contact_identity_broker_confirmed", "contact_identity_conflict"}.intersection(reasons):
        identity_audit = _phone_identity_audit(
            document,
            contact_identity,
            original_state=state,
            effective_state=effective_state,
            reason=effective_reason,
        )
    eligible = state in ASSIGNABLE_STATES and not reasons
    return {
        "assignment_ready": eligible,
        "exclude_from_assignment": not eligible,
        "assignment_block_reasons": reasons,
        "eligibility_version": ASSIGNMENT_GATE_VERSION,
        "effective_state": effective_state,
        "effective_reason": effective_reason,
        "phone_identity_audit": identity_audit,
    }


def assignment_eligibility(
    doc: dict[str, Any], *, contact_identity: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Backward-compatible tuple API backed by the single central gate."""
    decision = calculate_assignment_eligibility(doc, contact_identity=contact_identity)
    return bool(decision["assignment_ready"]), list(decision["assignment_block_reasons"])


def can_assign_property(
    property_document: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single public assignment contract.

    ``calculate_assignment_eligibility`` remains the backwards-compatible
    portal-policy evaluator.  This wrapper adds the canonical pipeline and
    identity invariants that every writer must enforce before mutation.
    """
    context = context or {}
    document = property_document or {}
    classification = document.get("classification") or {}
    reasons: set[str] = set()

    final_state = str(
        classification.get("final")
        or classification.get("canonical_final")
        or ""
    ).strip().upper()
    if final_state in BROKER_CANONICAL_STATES:
        reasons.add("canonical_broker_veto")
    if final_state == "OUT_OF_SCOPE_NEW_DEVELOPMENT":
        reasons.add("out_of_scope_new_development")
    toctoc_id_type = _toctoc_id_type(document)
    if toctoc_id_type == "2":
        reasons.add("toctoc_id_type_broker")
    elif toctoc_id_type == "3":
        reasons.add("out_of_scope_new_development")
    if classification.get("hard_broker_veto") or classification.get("hard_veto") == "PROFESSIONAL":
        reasons.add("hard_broker_veto")
    if classification.get("classification_conflict") or str(classification.get("conflict_state") or "").upper() == "IDENTITY_CONFLICT":
        reasons.add("classification_conflict")

    registry_match = context.get("broker_identity_match") or {}
    if registry_match.get("conflict"):
        reasons.add("broker_identity_conflict")
    elif registry_match.get("matched"):
        reasons.add("universal_broker_identity")

    contact_identity = context.get("contact_identity")
    if contact_identity and str(contact_identity.get("status") or "").upper() == "CORREDOR_CONFIRMED":
        reasons.add("contact_identity_broker_confirmed")

    pipeline_state = str(
        document.get("pipeline_state")
        or classification.get("pipeline_state")
        or ""
    ).strip().upper()
    incomplete_states = {
        "EXTRACTED_ONLY",
        "CLASSIFYING",
        "UNCERTAIN_PENDING_AI",
        "EXTRACTOR_DEGRADED",
        "INVALID",
    }
    pipeline_complete = document.get("pipeline_complete")
    if pipeline_complete is None:
        pipeline_complete = classification.get("pipeline_complete")
    # The central writer contract is fail-closed: a document is assignable
    # only after the pipeline explicitly marks it complete.  Legacy
    # ``assignment_eligibility`` callers remain available for read-only
    # compatibility, but all productive writers use this function.
    if pipeline_complete is not True or pipeline_state in incomplete_states:
        reasons.add("pipeline_incomplete")

    if final_state in {"REMOVED", "EXPIRED", "INVALID"}:
        reasons.add("canonical_operational_block")

    decision = calculate_assignment_eligibility(
        document,
        contact_identity=contact_identity,
    )
    reasons.update(decision.get("assignment_block_reasons") or [])
    decision = dict(decision)
    decision["assignment_block_reasons"] = sorted(reasons)
    decision["assignment_ready"] = not reasons
    decision["exclude_from_assignment"] = bool(reasons)
    decision["canonical_final"] = final_state or None
    decision["pipeline_complete"] = pipeline_complete is True and pipeline_state not in incomplete_states
    return decision


def apply_assignment_eligibility_fields(
    doc: dict[str, Any], *, contact_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist deterministic flags derived from the current document."""
    decision = calculate_assignment_eligibility(doc, contact_identity=contact_identity)
    classification = dict(doc.get("classification") or {})
    classification["assignment_ready"] = decision["assignment_ready"]
    classification["exclude_from_assignment"] = decision["exclude_from_assignment"]
    classification["assignment_block_reasons"] = decision["assignment_block_reasons"]
    classification["eligibility_version"] = decision["eligibility_version"]
    classification["effective_state"] = decision["effective_state"]
    classification["effective_classification_reason"] = decision.get("effective_reason")
    if decision.get("phone_identity_audit"):
        classification["phone_identity_audit"] = decision["phone_identity_audit"]
    else:
        classification.pop("phone_identity_audit", None)
    doc["classification"] = classification
    if is_chilepropiedades_document(doc):
        property_id = doc.get("_id") or doc.get("listing_id") or doc.get("url") or "(sin-id)"
        original_state = normalize_classification_state(
            (doc.get("classification") or {}).get("state") or (doc.get("classification") or {}).get("final_state")
        )
        logger.info(
            "[CP_ASSIGNMENT_GATE] property_id=%s original_state=%s effective_state=%s eligible=%s reason=%s identity_involved=%s",
            property_id,
            original_state,
            decision.get("effective_state"),
            decision.get("assignment_ready"),
            ",".join(decision.get("assignment_block_reasons") or []) or "none",
            bool(contact_identity),
        )
    return decision


def mark_assignment_readiness(doc: dict[str, Any]) -> dict[str, Any]:
    """Compatibility helper; it no longer trusts historical flags."""
    apply_assignment_eligibility_fields(doc)
    return doc
