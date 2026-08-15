"""Deterministic, explainable estimate that a listing was posted by its owner.

This module deliberately does not use the classifier's ``rule_confidence`` and
does not let an LLM choose a percentage. Portal-specific extractors normalize
their evidence and this module applies the same versioned rules to Yapo and
TocToc.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable


OWNER_PROBABILITY_VERSION = "owner-probability-evidence-v1"
OWNER_PROBABILITY_SOURCE = "deterministic_evidence_engine"

FINAL_CLASSIFICATION_STATES = frozenset({
    "CORREDOR_SEGURO", "CORREDOR_PROBABLE", "INCIERTO",
    "DUEÑO_PROBABLE", "DUEÑO_SEGURO", "AD_REMOVED",
})

STRUCTURED_EVIDENCE_RULES = {
    "OWNER_FIRST_PERSON_EXPLICIT": ("owner_identity", 35),
    "OWNER_FIRST_PERSON_POSSESSION": ("owner_identity", 25),
    "OWNER_NO_COMMISSION_EXPLICIT": ("description_language", 15),
    "SELLER_TYPE_OWNER": ("seller_type_and_badge", 10),
    "SELLER_TYPE_PARTICULAR": ("seller_type_and_badge", 5),
    "PERSONAL_IDENTITY_NO_COMMERCIAL": ("owner_identity", 5),
    "EXPLICIT_COMMERCIAL_IDENTITY": ("commercial_identity", -60),
    "PROFESSIONAL_BADGE": ("seller_type_and_badge", -40),
    "SELLER_TYPE_AGENT_OR_COMPANY": ("seller_type_and_badge", -35),
    "COMMISSION_OR_BROKERAGE_FEES": ("description_language", -40),
    "COMMERCIAL_DESCRIPTION": ("description_language", -25),
    "COMMERCIAL_PROFILE_CORRELATION": ("profile_activity", -30),
    "FOUR_TO_SEVEN_PROPERTIES_90D": ("profile_activity", -15),
    "EIGHT_OR_MORE_PROPERTIES_90D": ("profile_activity", -30),
}

REMOVED_STAGES = {"ad_removed", "removed", "listing_removed"}
INCOMPLETE_STAGES = {"needs_rescrape", "incomplete", "download_failed", "extraction_failed"}
INVALID_HTML_STATUSES = {"LISTING_REMOVED", "REMOVED", "INVALID", "BLOCKED", "ERROR"}
INVALID_DEEPSEEK_STATUSES = {
    "PENDING", "ERROR", "API_ERROR", "INVALID_EMPTY_CONTENT", "INVALID_JSON",
    "INVALID_STATE", "HTTP_ERROR", "TIMEOUT", "NO_RESULT",
}

PLACEHOLDERS = {
    "", "n/a", "na", "s/i", "si", "no disponible", "desconocido",
    "unknown", "usuario", "publicador",
}
PERSONAL_PLACEHOLDERS = PLACEHOLDERS | {"particular", "dueno", "duena", "propietario", "propietaria"}

COMMERCIAL_IDENTITY_TERMS = (
    "inmobiliaria", "corredor", "corredora", "corretaje", "propiedades",
    "real estate", "broker", "gestora", "gestion inmobiliaria", "asesoria inmobiliaria",
    "limitada", " ltda", " spa", " eirl", "constructora",
)

PROPERTY_WORDS = (
    "casa", "departamento", "depto", "propiedad", "parcela", "terreno",
    "sitio", "local", "oficina", "bodega",
)


def _ascii(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode("ascii")


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", _ascii(value).lower()).strip()


def _nested(doc: dict[str, Any], key: str) -> Any:
    for container_name in ("", "details", "source_signals", "source_signal_snapshot"):
        container = doc if not container_name else doc.get(container_name) or {}
        if isinstance(container, dict):
            value = container.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _first(doc: dict[str, Any], extracted: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = extracted.get(key)
        if value not in (None, "", [], {}):
            return value
        value = _nested(doc, key)
        if value not in (None, "", [], {}):
            return value
    return None


def _all_values(doc: dict[str, Any], extracted: dict[str, Any], keys: Iterable[str]) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    for key in keys:
        value = extracted.get(key)
        if value in (None, "", [], {}):
            value = _nested(doc, key)
        if value not in (None, "", [], {}):
            result.append((key, value))
    return result


def normalize_state(value: Any) -> str:
    state = re.sub(r"[^A-Z0-9]+", "_", _ascii(value).upper()).strip("_")
    aliases = {
        "DUENO_SEGURO": "DUEÑO_SEGURO",
        "DUENO_PROBABLE": "DUEÑO_PROBABLE",
    }
    return aliases.get(state, state)


def probability_band(probability: float | None) -> str:
    if probability is None:
        return "S/I"
    percentage = round(probability * 100)
    if percentage <= 19:
        return "0-19"
    if percentage <= 49:
        return "20-49"
    if percentage <= 69:
        return "50-69"
    if percentage <= 89:
        return "70-89"
    return "90-100"


def expected_state_for_probability(probability: float | None) -> str:
    band = probability_band(probability)
    return {
        "S/I": "INCOMPLETE",
        "0-19": "CORREDOR_SEGURO",
        "20-49": "CORREDOR_PROBABLE",
        "50-69": "INCIERTO",
        "70-89": "DUEÑO_PROBABLE",
        "90-100": "DUEÑO_SEGURO",
    }[band]


def _deepseek_required_and_valid(doc: dict[str, Any], extracted: dict[str, Any]) -> tuple[bool, bool, str]:
    cls = doc.get("classification") or {}
    semantic = cls.get("semantic_check") or doc.get("semantic_check") or {}
    source = _text(cls.get("decision_source") or cls.get("state_source") or cls.get("source"))
    rule_state = normalize_state(cls.get("rule_state") or "")
    status = str(
        extracted.get("deepseek_structured_evidence_status")
        or
        cls.get("deepseek_status") or semantic.get("status")
        or doc.get("deepseek_status") or ""
    ).upper()

    structural_final = source in {"structural_rules", "profile_correlation", "html_validation"}
    explicit_required = bool(semantic.get("required"))
    required = explicit_required or source in {"deepseek", "rules_fallback", "error"}
    if not structural_final and rule_state in {"", "INCONCLUSIVE", "INCIERTO"}:
        required = True

    if not required:
        return False, True, "NOT_REQUIRED"
    if status in INVALID_DEEPSEEK_STATUSES or status.startswith("INVALID"):
        return True, False, status
    if status == "VALID":
        has_structured_result = bool(
            extracted.get("deepseek_structured_evidence") is not None
            or cls.get("deepseek_structured_evidence") is not None
            or
            cls.get("deepseek_raw") or cls.get("deepseek_message_content")
            or (cls.get("trace") or {}).get("deepseek_raw")
            or (
                (doc.get("deepseek_state") or semantic.get("reason") or cls.get("reason"))
                and (doc.get("deepseek_evidence") or cls.get("evidence"))
            )
        )
        return True, has_structured_result, "VALID" if has_structured_result else "VALID_WITHOUT_STRUCTURED_RESULT"

    # Historic Yapo documents predate deepseek_status but persist a structured
    # adjudication: model, reason and evidence. Accept it as legacy evidence,
    # while keeping the provenance explicit in the dry-run.
    legacy_valid = bool(
        cls.get("ai_used")
        and cls.get("ai_model")
        and (cls.get("ai_reason") or cls.get("reason"))
        and (cls.get("evidence") or cls.get("classification_debug"))
    )
    if legacy_valid:
        return True, True, "VALID_LEGACY_STRUCTURED"
    return True, False, status or "MISSING"


def _completeness(doc: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    cls = doc.get("classification") or {}
    state = normalize_state(cls.get("state") or cls.get("final_state"))
    stage = _text(doc.get("scrape_stage"))
    html_status = str(doc.get("html_validation_status") or cls.get("html_validation_status") or "").upper()
    reasons: list[str] = []

    if state == "AD_REMOVED" or stage in REMOVED_STAGES or html_status in {"LISTING_REMOVED", "REMOVED"}:
        reasons.append("REMOVED_LISTING")
    elif stage in INCOMPLETE_STAGES or html_status in INVALID_HTML_STATUSES:
        reasons.append("INCOMPLETE_EXTRACTION")

    description = str(_first(doc, extracted, "description", "descripcion") or "").strip()
    if len(description) < 20:
        reasons.append("MISSING_ESSENTIAL_DESCRIPTION")

    identity_values = _all_values(doc, extracted, (
        "publicador_visible", "seller_name", "contact_name", "listing_advertiser",
        "company_name", "broker_brand", "seller_type",
    ))
    candidates = doc.get("publisher_identity_candidates") or []
    has_candidate = any(
        isinstance(item, dict) and _text(item.get("value")) not in PLACEHOLDERS
        for item in candidates
    )
    has_identity = has_candidate or any(_text(value) not in PLACEHOLDERS for _, value in identity_values)
    if not has_identity:
        reasons.append("MISSING_ESSENTIAL_PUBLISHER_IDENTITY")

    ds_required, ds_valid, ds_status = _deepseek_required_and_valid(doc, extracted)
    if ds_required and not ds_valid:
        reasons.append(f"DEEPSEEK_REQUIRED_{ds_status}")

    return {
        "complete": not reasons,
        "reasons": sorted(set(reasons)),
        "description_length": len(description),
        "identity_sources": sorted({key for key, _ in identity_values}),
        "deepseek_required": ds_required,
        "deepseek_valid": ds_valid,
        "deepseek_status": ds_status,
    }


def _make_signal(code: str, family: str, weight: int, evidence: str, source: str) -> dict[str, Any]:
    return {
        "code": code,
        "family": family,
        "weight": weight,
        "evidence": re.sub(r"\s+", " ", str(evidence)).strip()[:300],
        "source": source,
    }


def _select_family_signals(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply one strongest signal per family to avoid duplicated evidence."""
    selected: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    by_family: dict[str, list[dict[str, Any]]] = {}
    for signal in candidates:
        by_family.setdefault(signal["family"], []).append(signal)
    for family in sorted(by_family):
        signals = by_family[family]
        strongest = max(signals, key=lambda item: (abs(item["weight"]), -item["weight"], item["code"]))
        selected.append(strongest)
        suppressed.extend(item for item in signals if item is not strongest)
    return selected, suppressed


def _publisher_activity(doc: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    activity = _first(doc, extracted, "publisher_activity")
    return activity if isinstance(activity, dict) else {}


def calculate_owner_probability(
    doc: dict[str, Any],
    *,
    extracted: dict[str, Any] | None = None,
    calculated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return probability metadata without modifying ``doc`` or its state."""
    extracted = extracted or {}
    completeness = _completeness(doc, extracted)
    when = calculated_at or datetime.now(timezone.utc)
    base = {
        "owner_probability": None,
        "owner_probability_signals": {"base": 50, "applied": [], "suppressed": []},
        "owner_probability_version": OWNER_PROBABILITY_VERSION,
        "owner_probability_calculated_at": when,
        "owner_probability_evidence_quality": "INCOMPLETE",
        "owner_probability_source": OWNER_PROBABILITY_SOURCE,
        "owner_probability_completeness": completeness,
        "owner_probability_band": "S/I",
        "owner_probability_expected_state": "INCOMPLETE",
        "owner_probability_contradiction": None,
    }
    if not completeness["complete"]:
        return base

    candidates: list[dict[str, Any]] = []
    description_raw = str(_first(doc, extracted, "description", "descripcion") or "")
    description = _text(description_raw)
    title_raw = str(_first(doc, extracted, "title", "titulo") or "")
    title = _text(title_raw)
    combined = f"{title} {description}".strip()

    explicit_patterns = (
        r"\bsoy (?:el |la )?(?:dueno|duena|propietario|propietaria)\b",
        r"\bsomos (?:los |las )?(?:duenos|duenas|propietarios|propietarias)\b",
        r"\b(?:vendo|arriendo|alquilo) como (?:dueno|duena|propietario|propietaria)\b",
        rf"\b(?:vendo|arriendo|alquilo) mi (?:{'|'.join(PROPERTY_WORDS)})\b",
        r"\batte[,. ]+(?:soy )?(?:uno|una) de los (?:propietarios|duenos)\b",
    )
    explicit = next((match.group(0) for pattern in explicit_patterns if (match := re.search(pattern, combined))), "")
    if explicit:
        candidates.append(_make_signal(
            "OWNER_FIRST_PERSON_EXPLICIT", "owner_identity", 35, explicit, "title/description",
        ))
    else:
        possession = re.search(
            rf"\b(?:mi|nuestra|nuestro) (?:{'|'.join(PROPERTY_WORDS)})\b", combined
        )
        if possession:
            candidates.append(_make_signal(
                "OWNER_FIRST_PERSON_POSSESSION", "owner_identity", 25,
                possession.group(0), "title/description",
            ))

    no_commission = re.search(r"\b(?:sin comision|no cobro comision|sin corredor(?:a|es)?)\b", combined)
    if no_commission:
        candidates.append(_make_signal(
            "OWNER_NO_COMMISSION_EXPLICIT", "description_language", 15,
            no_commission.group(0), "title/description",
        ))

    commercial_scan = re.sub(
        r"\b(?:sin comision(?: de)? corretaje|sin corredora? de propiedades|"
        r"sin corredores? de propiedades)\b",
        " ",
        combined,
    )
    fee = re.search(
        r"\b(?:(?<!sin )comision(?: de)? corretaje|honorarios?(?: de)? corretaje|"
        r"comision mas iva|mes de comision|paga comision|se paga comision)\b", combined
    )
    if fee:
        candidates.append(_make_signal(
            "COMMISSION_OR_BROKERAGE_FEES", "description_language", -40,
            fee.group(0), "title/description",
        ))
    else:
        commercial_description = re.search(
            r"\b(?:corredora? de propiedades|(?:empresa|servicios?) de corretaje|asesor(?:a|es)? inmobiliari[oa]s?|"
            r"gestion inmobiliaria|codigo interno|sala de ventas|ultimas unidades|bono pie)\b",
            commercial_scan,
        )
        if commercial_description:
            candidates.append(_make_signal(
                "COMMERCIAL_DESCRIPTION", "description_language", -25,
                commercial_description.group(0), "title/description",
            ))

    seller_type_raw = _first(doc, extracted, "seller_type")
    seller_type = _text(seller_type_raw)
    if seller_type in {"dueno", "duena", "propietario", "propietaria", "owner"}:
        candidates.append(_make_signal(
            "SELLER_TYPE_OWNER", "seller_type_and_badge", 10,
            str(seller_type_raw), "seller_type",
        ))
    elif seller_type in {"particular", "persona", "private"}:
        candidates.append(_make_signal(
            "SELLER_TYPE_PARTICULAR", "seller_type_and_badge", 5,
            str(seller_type_raw), "seller_type",
        ))
    elif any(term in seller_type for term in ("empresa", "profesional", "agente", "corredor", "inmobiliaria")):
        candidates.append(_make_signal(
            "SELLER_TYPE_AGENT_OR_COMPANY", "seller_type_and_badge", -35,
            str(seller_type_raw), "seller_type",
        ))

    seller_is_pro = _first(doc, extracted, "seller_is_pro")
    badge_text = " ".join(str(value) for _, value in _all_values(
        doc, extracted, ("contact_badges_text", "seller_type_evidence")
    ))
    if bool(seller_is_pro) or "profesional" in _text(badge_text):
        candidates.append(_make_signal(
            "PROFESSIONAL_BADGE", "seller_type_and_badge", -40,
            badge_text or "seller_is_pro=true", "seller_is_pro/badge",
        ))

    identity_values = _all_values(doc, extracted, (
        "company_name", "broker_brand", "publicador_visible", "seller_name", "contact_name",
        "listing_advertiser", "contact_logo_alt",
    ))
    explicit_business = [
        f"{key}={value}" for key, value in identity_values
        if key in {"company_name", "broker_brand"} and _text(value) not in PLACEHOLDERS
    ]
    commercial_named = [
        f"{key}={value}" for key, value in identity_values
        if any(term in f" {_text(value)}" for term in COMMERCIAL_IDENTITY_TERMS)
    ]
    if explicit_business or commercial_named:
        candidates.append(_make_signal(
            "EXPLICIT_COMMERCIAL_IDENTITY", "commercial_identity", -60,
            "; ".join((explicit_business + commercial_named)[:6]), "publisher_identity",
        ))
    else:
        has_commercial_context = any(signal["weight"] < 0 for signal in candidates)
        personal = next((
            (key, str(value)) for key, value in identity_values
            if not has_commercial_context
            and _text(value) not in PERSONAL_PLACEHOLDERS
            and re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ' -]{4,80}", str(value).strip())
            and 2 <= len(re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,}", str(value))) <= 5
        ), None)
        if personal:
            candidates.append(_make_signal(
                "PERSONAL_IDENTITY_NO_COMMERCIAL", "owner_identity", 5,
                personal[1], personal[0],
            ))

    activity = _publisher_activity(doc, extracted)
    try:
        window_days = int(activity.get("window_days") or 0)
        distinct = int(
            activity.get("distinct_properties_in_window")
            or activity.get("distinct_listings_in_window")
            or activity.get("publications_in_window")
            or 0
        )
        confirmed_brokers = int(activity.get("confirmed_broker_count") or 0)
    except (TypeError, ValueError):
        window_days = distinct = confirmed_brokers = 0
    activity_is_temporal = 0 < window_days <= 90 and distinct > 0
    has_complementary_commercial = any(signal["weight"] < 0 for signal in candidates)
    if activity_is_temporal and distinct == 1 and activity.get("coverage_complete") is True:
        candidates.append(_make_signal(
            "SINGLE_DISTINCT_PROPERTY_90D", "profile_activity", 5,
            f"1 inmueble distinto en {window_days} días", "publisher_activity",
        ))
    elif activity_is_temporal and confirmed_brokers > 0 and has_complementary_commercial:
        candidates.append(_make_signal(
            "COMMERCIAL_PROFILE_CORRELATION", "profile_activity", -30,
            f"{confirmed_brokers} corredores confirmados vinculados en {window_days} días",
            "publisher_activity",
        ))
    elif activity_is_temporal and has_complementary_commercial and distinct >= 8:
        candidates.append(_make_signal(
            "EIGHT_OR_MORE_PROPERTIES_90D", "profile_activity", -30,
            f"{distinct} inmuebles distintos en {window_days} días", "publisher_activity",
        ))
    elif activity_is_temporal and has_complementary_commercial and 4 <= distinct <= 7:
        candidates.append(_make_signal(
            "FOUR_TO_SEVEN_PROPERTIES_90D", "profile_activity", -15,
            f"{distinct} inmuebles distintos en {window_days} días", "publisher_activity",
        ))

    structured = extracted.get("deepseek_structured_evidence")
    if structured is None:
        structured = (doc.get("classification") or {}).get("deepseek_structured_evidence")
    for item in structured or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").upper()
        rule = STRUCTURED_EVIDENCE_RULES.get(code)
        quote = str(item.get("quote") or item.get("evidence") or "").strip()
        if not rule or not quote:
            continue
        family, weight = rule
        candidates.append(_make_signal(
            code, family, weight, quote,
            f"deepseek_structured:{item.get('source_field') or 'unknown'}",
        ))

    applied, suppressed = _select_family_signals(candidates)
    raw_score = max(0, min(100, 50 + sum(signal["weight"] for signal in applied)))
    probability = round(raw_score / 100, 2)
    state = normalize_state((doc.get("classification") or {}).get("state"))
    contradiction = None
    if state == "INCIERTO" and raw_score < 50:
        contradiction = "INCIERTO_BELOW_50"
    elif state == "DUEÑO_PROBABLE" and raw_score < 70:
        contradiction = "DUEÑO_PROBABLE_BELOW_70"
    elif state == "DUEÑO_SEGURO" and raw_score < 90:
        contradiction = "DUEÑO_SEGURO_BELOW_90"
    elif state == "CORREDOR_SEGURO" and raw_score > 19:
        contradiction = "CORREDOR_SEGURO_ABOVE_19"

    max_strength = max((abs(signal["weight"]) for signal in applied), default=0)
    quality = "COMPLETE_NEUTRAL" if not applied else (
        "COMPLETE_STRONG_EVIDENCE" if max_strength >= 35 else "COMPLETE_PARTIAL_EVIDENCE"
    )
    base.update({
        "owner_probability": probability,
        "owner_probability_signals": {
            "base": 50,
            "applied": applied,
            "suppressed": suppressed,
            "raw_score": raw_score,
            "neutral": not applied,
            "family_rule": "one strongest signal per family",
        },
        "owner_probability_evidence_quality": quality,
        "owner_probability_band": probability_band(probability),
        "owner_probability_expected_state": expected_state_for_probability(probability),
        "owner_probability_contradiction": contradiction,
    })
    return base


def apply_owner_probability_to_document(
    doc: dict[str, Any], *, extracted: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Persist the single owner probability and derive the public state.

    This is the mandatory final gate used by both production scrapers.  It
    ``classification.rule_confidence`` preserves technical classifier output;
    canonical ``classification.confidence`` is derived from owner_probability.
    """
    classification = dict(doc.get("classification") or {})
    if classification.get("status") in {
        "PENDING_LLM", "PENDING_SEMANTIC_REVIEW", "SEMANTIC_CLASSIFICATION_FAILED",
    }:
        # Processing states are not classification decisions and must not be
        # converted into an artificial INCIERTO document.
        doc["classification"] = classification
        return doc
    # Keep the classifier's technical confidence separate from the final,
    # deterministic owner probability exposed as canonical confidence.
    if "rule_confidence" not in classification and classification.get("confidence") is not None:
        classification["rule_confidence"] = classification.get("confidence")
    previous_state = normalize_state(
        classification.get("state") or classification.get("final_state")
    )
    classification_source = str(classification.get("source") or "").lower()
    hard_veto = str(
        classification.get("hard_veto")
        or classification.get("professional_hard_veto")
        or ""
    ).upper()
    broker_confirmed = (
        previous_state.startswith("CORREDOR")
        and classification_source in {"structural_rules", "rules_json", "profile_correlation"}
    )
    if (
        not hard_veto
        and previous_state == "CORREDOR_SEGURO"
        and classification_source in {"structural_rules", "rules_json", "structural_professional_rule"}
        and classification.get("strong_signal_found", True) is not False
    ):
        hard_veto = "PROFESSIONAL"
    if hard_veto == "PROFESSIONAL":
        classification["hard_veto"] = "PROFESSIONAL"
        classification["professional_hard_veto"] = True
    result = calculate_owner_probability(doc, extracted=extracted or doc)
    classification.update(result)
    probability = result["owner_probability"]
    completeness = result["owner_probability_completeness"]

    if "REMOVED_LISTING" in completeness.get("reasons", []):
        final_state = "AD_REMOVED"
    elif hard_veto == "PROFESSIONAL":
        final_state = "CORREDOR_SEGURO"
    elif probability is None:
        final_state = previous_state if broker_confirmed else "PENDIENTE"
    else:
        final_state = expected_state_for_probability(probability)

    classification["previous_classification_state"] = previous_state
    if probability is not None:
        if hard_veto != "PROFESSIONAL":
            classification["confidence"] = probability
            classification["canonical_confidence"] = probability
        classification["state"] = final_state
        classification["final_state"] = final_state
        classification["classification_semantics"] = (
            "professional_hard_veto" if hard_veto == "PROFESSIONAL"
            else "owner_probability_band"
        )
    else:
        classification["state"] = final_state
        classification["final_state"] = final_state
        classification["classification_semantics"] = "pending_or_structural_without_probability"
    classification["state_source"] = (
        "classification.professional_hard_veto"
        if hard_veto == "PROFESSIONAL"
        else "classification.owner_probability_band"
    )
    classification["classification_rule_version"] = OWNER_PROBABILITY_VERSION
    classification["assignment_ready"] = bool(
        final_state in {"DUEÑO_PROBABLE", "DUEÑO_SEGURO"}
        and probability is not None and probability >= 0.50
        and not classification.get("manual_review_required")
        and hard_veto != "PROFESSIONAL"
    )
    classification["exclude_from_assignment"] = not classification["assignment_ready"]
    classification["assignment_block_reasons"] = (
        [] if classification["assignment_ready"]
        else ["PROFESSIONAL_HARD_VETO"] if hard_veto == "PROFESSIONAL"
        else ["BROKER_CONFIRMED"] if broker_confirmed
        else completeness.get("reasons", []) or ["OWNER_PROBABILITY_BELOW_50"]
    )
    doc["classification"] = classification
    return doc
