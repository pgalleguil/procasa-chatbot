"""Single auditable assignment gate shared by both captacion scrapers."""
from __future__ import annotations

from typing import Any

FINAL_STATES = {"DUEÑO_SEGURO", "DUENO_SEGURO", "DUEÑO_PROBABLE", "DUENO_PROBABLE", "INCIERTO"}
COMMERCIAL_TERMS = ("inmobiliaria", "corredor", "corredora", "propiedades", "real estate", "broker")


def assignment_eligibility(doc: dict[str, Any]) -> tuple[bool, list[str]]:
    cls = doc.get("classification") or {}
    gestion = doc.get("gestion") or {}
    reasons: list[str] = []
    state = str(cls.get("state") or cls.get("final_state") or "").upper()
    if state not in FINAL_STATES:
        reasons.append("classification_not_assignable")
    if cls.get("assignment_ready") is not True:
        reasons.append("classification_not_final_or_not_persisted")
    if gestion.get("semantic_review_hold") is True or cls.get("manual_review_required") is True:
        reasons.append("manual_review_pending")
    if cls.get("exclude_from_assignment") is True or gestion.get("exclude_from_assignment") is True:
        reasons.append("explicitly_excluded")
    try:
        owner_probability = float(cls.get("owner_probability"))
    except (TypeError, ValueError):
        owner_probability = None
    if owner_probability is None:
        reasons.append("owner_probability_missing")
    elif owner_probability < 0.50:
        reasons.append("owner_probability_below_50")

    stage = str(doc.get("scrape_stage") or "").lower()
    html_status = str(doc.get("html_validation_status") or "").upper()
    if state == "AD_REMOVED" or stage in {"ad_removed", "needs_rescrape", "incomplete"}:
        reasons.append("removed_or_incomplete")
    if html_status in {"LISTING_REMOVED", "INVALID", "BLOCKED"}:
        reasons.append("invalid_source_document")
    if not str(doc.get("descripcion") or doc.get("description") or "").strip():
        reasons.append("missing_essential_fields")
    if not (doc.get("listing_id") or doc.get("url") or doc.get("source_url")):
        reasons.append("missing_essential_fields")
    if not (doc.get("comuna_slug") or doc.get("comuna")):
        reasons.append("missing_essential_fields")

    commercial_values = " ".join(str(doc.get(k) or "") for k in (
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
    deterministic = source in {"structural_rules", "rules_json", "html_validation", "profile_correlation"}
    deepseek_persisted = source == "deepseek" and ds_status == "VALID" and bool(trace.get("deepseek_raw") or cls.get("deepseek_raw"))
    if not (manual_approved or deterministic or deepseek_persisted):
        reasons.append("no_auditable_final_decision")
    return not reasons, sorted(set(reasons))


def mark_assignment_readiness(doc: dict[str, Any]) -> dict[str, Any]:
    cls = doc.setdefault("classification", {})
    cls["assignment_ready"] = True
    eligible, reasons = assignment_eligibility(doc)
    cls["assignment_ready"] = eligible
    cls["assignment_block_reasons"] = reasons
    cls["assignment_gate_version"] = "assignment-gate-v2"
    return doc
