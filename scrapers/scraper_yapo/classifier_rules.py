from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from typing import Any, Iterable

try:
    from .config import DATA_DIR
except ImportError:  # direct script execution
    from config import DATA_DIR


STRONG_PUBLISHER_FIELDS = (
    "publicador_visible",
    "contact_name",
    "contact_logo_alt",
    "seller_jsonld_name",
    "listing_advertiser",
    "contact_badges_text",
)
IDENTITY_FIELDS = frozenset({
    "publicador_visible", "contact_name", "contact_logo_alt",
    "listing_advertiser", "seller_jsonld_name",
})
_RULE_LOAD_ERRORS: dict[str, str] = {}

WEAK_CONTEXT_FIELDS = (
    "title",
    "description",
    "descripcion",
    "seller_text",
    "body_text",
    "seller_type",
)


@lru_cache(maxsize=1)
def load_rule_sets() -> dict[str, Any]:
    rule_files = {
        "known_broker_brands": ("known_broker_brands.json", "brands"),
        "hard_broker_terms": ("hard_broker_terms.json", "hard_broker_terms"),
        "company_shape_terms": ("company_shape_terms.json", "company_shape_terms"),
        "listing_removed_patterns": ("listing_removed_patterns.json", "patterns"),
        "search_terms": ("search_terms.json", "search_terms"),
        "owner_keywords": ("owner_keywords.json", "owner_terms"),
    }
    sets: dict[str, Any] = {}
    _RULE_LOAD_ERRORS.clear()
    for name, (filename, key) in rule_files.items():
        path = DATA_DIR / filename
        if not path.exists():
            sets[name] = []
            if name in {"known_broker_brands", "hard_broker_terms", "company_shape_terms"}:
                _RULE_LOAD_ERRORS[name] = "missing_file"
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            sets[name] = []
            if name in {"known_broker_brands", "hard_broker_terms", "company_shape_terms"}:
                _RULE_LOAD_ERRORS[name] = f"invalid_json:{type(exc).__name__}"
            continue
        if isinstance(data, dict):
            values = data.get(key, [])
        elif isinstance(data, list):
            values = data
        else:
            values = []
        if not isinstance(values, list):
            values = []
        sets[name] = [str(item) for item in values if str(item).strip()]
        if name in {"known_broker_brands", "hard_broker_terms", "company_shape_terms"} and not sets[name]:
            _RULE_LOAD_ERRORS[name] = "empty_rule_set"
    return sets


def validate_classification_rules(*, raise_on_error: bool = False) -> list[str]:
    """Checks the broker-detection rule sets actually loaded non-empty.

    Returns the list of critical sets that came back empty (or [] if OK).
    Previously a UTF-8 BOM in the JSON made every rule set load as [] silently,
    so brokers like RE/MAX were classified INCIERTO and could be assigned to the
    team. Every classification run must refuse to proceed when these are empty.
    """
    rs = load_rule_sets()
    issues = list(dict.fromkeys(
        [f"{name}:{reason}" for name, reason in _RULE_LOAD_ERRORS.items()]
        + [name for name in ("known_broker_brands", "hard_broker_terms", "company_shape_terms") if not rs.get(name)]
    ))
    if issues and raise_on_error:
        raise RuntimeError("Invalid broker classification rule sets: " + ", ".join(issues))
    return issues


@lru_cache(maxsize=4096)
def normalize_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.replace("/", " ").replace("-", " ").replace("_", " ")
    raw = re.sub(r"[^a-z0-9+ ]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


@lru_cache(maxsize=4096)
def _matchable(text: str) -> str:
    normalized = normalize_text(text)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


def _field_values(extracted: dict[str, Any], fields: Iterable[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in fields:
        value = extracted.get(field, "")
        if value is None:
            value = ""
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        elif isinstance(value, dict):
            value = " ".join(f"{k} {v}" for k, v in value.items())
        values[field] = str(value)
    return values


def _all_text(extracted: dict[str, Any]) -> str:
    values = _field_values(extracted, STRONG_PUBLISHER_FIELDS + WEAK_CONTEXT_FIELDS)
    return normalize_text(" ".join(values.values()))


def _search_terms(text: str, terms: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for term in terms:
        norm = normalize_text(term)
        if norm and norm in text:
            hits.append(term)
    return hits


def _evidence_from_fields(extracted: dict[str, Any], fields: Iterable[str], terms: Iterable[str]) -> list[str]:
    values = _field_values(extracted, fields)
    evidence: list[str] = []
    for field, value in values.items():
        text = _matchable(value)
        if not text:
            continue
        for term in terms:
            norm = _matchable(term)
            if not norm:
                continue
            if norm in text:
                evidence.append(f"{field}:{term}")
    return evidence


def _publisher_field_is_confirmed(extracted: dict[str, Any], field: str) -> bool:
    provenance = extracted.get("publisher_field_provenance") or {}
    item = provenance.get(field) if isinstance(provenance, dict) else None
    return bool(
        isinstance(item, dict)
        and item.get("status") == "confirmed"
        and item.get("relationship") == "publisher"
    )


def _confirmed_identity_values(extracted: dict[str, Any]) -> dict[str, str]:
    return {
        field: value
        for field, value in _field_values(extracted, IDENTITY_FIELDS).items()
        if value.strip() and _publisher_field_is_confirmed(extracted, field)
    }


def _phrase_matches(value: str, phrase: str) -> bool:
    """Match complete normalized tokens, allowing spacing/punctuation variants."""
    value_tokens = normalize_text(value).split()
    phrase_tokens = normalize_text(phrase).split()
    if not value_tokens or not phrase_tokens:
        return False
    target = "".join(phrase_tokens)
    width = len(phrase_tokens)
    return any("".join(value_tokens[i:i + width]) == target for i in range(len(value_tokens) - width + 1))


def _confirmed_identity_evidence(extracted: dict[str, Any], terms: Iterable[str]) -> list[str]:
    evidence = []
    for field, value in _confirmed_identity_values(extracted).items():
        for term in terms:
            if _phrase_matches(value, term):
                evidence.append(f"{field}:{term}")
    return evidence


def is_removed_listing(extracted: dict[str, Any]) -> dict[str, Any] | None:
    status = str(extracted.get("html_validation_status", "") or extracted.get("validation_status", "")).upper()
    if status == "LISTING_REMOVED":
        return {
            "state": "AD_REMOVED",
            "confidence": 1.0,
            "reason": "HTML validation detected removed listing.",
            "evidence": [str(extracted.get("html_validation_reason", "listing_removed"))],
            "source": "html_validation",
        }
    return None


def known_broker_brand_in_strong_fields(extracted: dict[str, Any]) -> dict[str, Any] | None:
    validate_classification_rules(raise_on_error=True)
    brands = load_rule_sets()["known_broker_brands"]
    evidence = _confirmed_identity_evidence(extracted, brands)
    if not evidence:
        return None
    return {
        "state": "CORREDOR_SEGURO",
        "confidence": 0.99,
        "reason": "Known broker brand found in strong publisher fields.",
        "evidence": evidence[:8],
        "source": "rules_json",
    }


def hard_broker_terms_in_strong_fields(extracted: dict[str, Any]) -> dict[str, Any] | None:
    validate_classification_rules(raise_on_error=True)
    terms = load_rule_sets()["hard_broker_terms"]
    evidence = _confirmed_identity_evidence(extracted, terms)
    if not evidence:
        return None
    return {
        "state": "CORREDOR_PROBABLE",
        "confidence": 0.9,
        "reason": "Hard broker terms found in strong publisher fields.",
        "evidence": evidence[:8],
        "source": "rules_json",
    }


def detect_owner_signals(extracted: dict[str, Any]) -> list[str]:
    terms = load_rule_sets()["owner_keywords"]
    if not terms:
        return []
    text = _all_text(extracted)
    return _search_terms(text, terms)


def company_shape_in_publisher_fields(extracted: dict[str, Any]) -> dict[str, Any]:
    terms = load_rule_sets()["company_shape_terms"]
    values = _field_values(extracted, STRONG_PUBLISHER_FIELDS)
    evidence: list[str] = []
    for field, value in values.items():
        text = _matchable(value)
        for term in terms:
            norm = _matchable(term)
            if not norm:
                continue
            if norm in text:
                evidence.append(f"{field}:{term}")
                break
    return {
        "company_like_suspected": bool(evidence),
        "company_like_evidence": evidence[:8],
    }


def _all_evidence_context(extracted: dict[str, Any]) -> dict[str, Any]:
    brand = known_broker_brand_in_strong_fields(extracted)
    hard = hard_broker_terms_in_strong_fields(extracted)
    company = company_shape_in_publisher_fields(extracted)
    owner = detect_owner_signals(extracted)
    removed = is_removed_listing(extracted)
    return {
        "removed": removed,
        "known_brand_evidence": brand["evidence"] if brand else [],
        "hard_broker_evidence": hard["evidence"] if hard else [],
        "owner_signal_evidence": owner,
        "weak_broker_evidence": [],
        **company,
    }


def classify_obvious_broker(extracted: dict[str, Any]) -> dict[str, Any] | None:
    removed = is_removed_listing(extracted)
    if removed:
        return removed

    validate_classification_rules(raise_on_error=True)
    seller_type = normalize_text(str(extracted.get("seller_type") or ""))
    badges = normalize_text(str(extracted.get("contact_badges_text") or ""))
    if (
        seller_type in {"profesional", "empresa", "agente", "corredor"}
        and "profesional" in badges
        and _publisher_field_is_confirmed(extracted, "seller_type")
        and _publisher_field_is_confirmed(extracted, "contact_badges_text")
    ):
        return {
            "state": "CORREDOR_SEGURO",
            "confidence": 0.99,
            "reason": "El portal identifica estructuralmente al publicador como profesional.",
            "evidence": [f"seller_type:{seller_type}", f"contact_badges_text:{badges}"],
            "source": "structural_rules",
        }

    brand = known_broker_brand_in_strong_fields(extracted)
    if brand:
        return brand

    hard = hard_broker_terms_in_strong_fields(extracted)
    if hard:
        return hard

    profile = extracted.get("publisher_profile_context") or {}
    if profile.get("commercial_identity_confirmed") and profile.get("confirmed_broker_count", 0) > 0:
        return {
            "state": "CORREDOR_SEGURO", "confidence": 1.0,
            "reason": "Perfil Yapo correlacionado con identidad comercial y avisos confirmados como corredor.",
            "evidence": list(profile.get("evidence") or []), "source": "profile_correlation",
            "publisher_profile_context": profile,
        }

    return None


def build_deepseek_context(extracted: dict[str, Any]) -> dict[str, Any]:
    context = _all_evidence_context(extracted)
    return {
        "publicador_visible": extracted.get("publicador_visible", ""),
        "contact_name": extracted.get("contact_name", ""),
        "contact_logo_alt": extracted.get("contact_logo_alt", ""),
        "seller_type": extracted.get("seller_type", ""),
        "listing_advertiser": extracted.get("listing_advertiser", ""),
        "seller_jsonld_name": extracted.get("seller_jsonld_name", ""),
        "contact_badges_text": extracted.get("contact_badges_text", ""),
        "company_like_suspected": context["company_like_suspected"],
        "company_like_evidence": context["company_like_evidence"],
        "known_brand_evidence": context["known_brand_evidence"],
        "hard_broker_evidence": context["hard_broker_evidence"],
        "owner_signal_evidence": context["owner_signal_evidence"],
        "weak_broker_evidence": context["weak_broker_evidence"],
        "publisher_profile_context": extracted.get("publisher_profile_context") or {},
        "descripcion": extracted.get("descripcion", extracted.get("description", "")),
    }


def build_rule_context(extracted: dict[str, Any]) -> dict[str, Any]:
    context = _all_evidence_context(extracted)
    return {
        **build_deepseek_context(extracted),
        "html_validation_status": extracted.get("html_validation_status", ""),
        "html_validation_reason": extracted.get("html_validation_reason", ""),
        "removed_listing": bool(context["removed"]),
    }


def classify_with_rules(extracted: dict[str, Any]) -> dict[str, Any]:
    obvious = classify_obvious_broker(extracted)
    if obvious:
        return obvious

    company = company_shape_in_publisher_fields(extracted)
    return {
        "state": "INCONCLUSIVE",
        "confidence": 0.35 if company["company_like_suspected"] else 0.2,
        "reason": "Rules are only providing context here; DeepSeek should decide.",
        "evidence": company["company_like_evidence"],
        "source": "rules_json",
        **company,
        "owner_signal_evidence": detect_owner_signals(extracted),
        "weak_broker_evidence": [],
    }
