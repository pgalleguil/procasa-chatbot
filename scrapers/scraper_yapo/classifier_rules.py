from __future__ import annotations

import json
import re
import unicodedata
from html import unescape
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
_GENERIC_SINGLE_TERM_ALIASES = frozenset({
    "nueva", "nuevo", "propiedad", "propiedades", "inmobiliaria", "inmobiliario",
    "corredora", "corredor", "corredores", "gestiones", "gestion", "inversiones",
    "servicios", "asesoria", "chile", "group", "grupo", "real", "property",
})

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
        "contextual_classification": ("contextual_classification.json", "rules"),
    }
    sets: dict[str, Any] = {}
    _RULE_LOAD_ERRORS.clear()
    for name, (filename, key) in rule_files.items():
        path = DATA_DIR / filename
        if not path.exists():
            sets[name] = []
            if name in {"known_broker_brands", "hard_broker_terms", "company_shape_terms", "contextual_classification"}:
                _RULE_LOAD_ERRORS[name] = "missing_file"
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            sets[name] = []
            if name in {"known_broker_brands", "hard_broker_terms", "company_shape_terms", "contextual_classification"}:
                _RULE_LOAD_ERRORS[name] = f"invalid_json:{type(exc).__name__}"
            continue
        if isinstance(data, dict):
            values = data.get(key, [])
            if name == "known_broker_brands":
                aliases = data.get("brand_aliases", {})
                sets["known_broker_brand_aliases"] = aliases if isinstance(aliases, dict) else {}
            if name == "contextual_classification":
                sets[name] = values if isinstance(values, dict) else {}
                if not sets[name]:
                    _RULE_LOAD_ERRORS[name] = "empty_rule_set"
                continue
        elif isinstance(data, list):
            values = data
        else:
            values = []
        if not isinstance(values, list):
            values = []
        sets[name] = [str(item) for item in values if str(item).strip()]
        if name in {"known_broker_brands", "hard_broker_terms", "company_shape_terms"} and not sets[name]:
            _RULE_LOAD_ERRORS[name] = "empty_rule_set"
        if name == "contextual_classification" and not sets.get(name):
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
        + [name for name in ("known_broker_brands", "hard_broker_terms", "company_shape_terms", "contextual_classification") if not rs.get(name)]
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


def _publisher_field_source(extracted: dict[str, Any], field: str) -> str:
    provenance = extracted.get("publisher_field_provenance") or {}
    item = provenance.get(field) if isinstance(provenance, dict) else None
    return str(item.get("source") or "") if isinstance(item, dict) else ""


def _confirmed_identity_values(extracted: dict[str, Any]) -> dict[str, str]:
    return {
        field: value
        for field, value in _field_values(extracted, IDENTITY_FIELDS).items()
        if value.strip() and _publisher_field_is_confirmed(extracted, field)
    }


def _unverified_identity_values(extracted: dict[str, Any]) -> dict[str, str]:
    """Raw imported identity fields are candidates, never extraction-confirmed facts."""
    stored = extracted.get("unverified_publisher_fields") or {}
    if isinstance(stored, dict) and stored:
        return {str(k): str(v) for k, v in stored.items() if str(v or "").strip()}
    return {
        field: value for field, value in _field_values(extracted, IDENTITY_FIELDS).items()
        if value.strip() and not _publisher_field_is_confirmed(extracted, field)
    }


def _phrase_matches(value: str, phrase: str) -> bool:
    """Match complete normalized tokens, allowing spacing/punctuation variants."""
    value_text = normalize_text(value)
    phrase_tokens = normalize_text(phrase).split()
    if not value_text or not phrase_tokens:
        return False
    pattern = _flexible_phrase_pattern(phrase)
    return bool(pattern and pattern.search(value_text))


@lru_cache(maxsize=8192)
def _flexible_phrase_pattern(phrase: str) -> re.Pattern[str] | None:
    """Compile a case/accent-insensitive phrase matcher tolerant of spaces.

    Normalization already lowercases text and removes accents/punctuation.
    Optional whitespace between letters also catches logos such as
    ``I N M O B I L A N D`` without permitting the brand inside a longer word.
    """
    tokens = normalize_text(phrase).split()
    if not tokens:
        return None
    body = r"\s*".join(r"\s*".join(re.escape(char) for char in token) for token in tokens)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


def _confirmed_identity_evidence(extracted: dict[str, Any], terms: Iterable[str]) -> list[str]:
    evidence = []
    for field, value in _confirmed_identity_values(extracted).items():
        for term in terms:
            if _phrase_matches(value, term):
                evidence.append(f"{field}:{term}")
    return evidence


def _surface_contains(value: str, phrase: str) -> bool:
    """Prefer an alias's visible spelling when recording match evidence."""
    source = unicodedata.normalize("NFKC", str(value or "")).casefold()
    target = unicodedata.normalize("NFKC", str(phrase or "")).casefold().strip()
    if not source or not target:
        return False
    body = r"\s+".join(re.escape(token) for token in re.split(r"\s+", target))
    return bool(re.search(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])", source))


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
    rules = load_rule_sets()
    brands = rules["known_broker_brands"]
    aliases = rules.get("known_broker_brand_aliases") or {}
    evidence: list[str] = []
    confirmed = _confirmed_identity_values(extracted)
    for field, value in confirmed.items():
        compact_value = "".join(normalize_text(value).split())
        for brand in brands:
            variants = [brand, *(aliases.get(brand) or [])]
            # Avoid compiling/testing a regex for every one of the 1,200+
            # catalog entries on every listing. Compact containment is a fast
            # prefilter; the boundary-aware matcher remains the final check.
            possible = [
                variant for variant in variants
                if (compact_term := "".join(normalize_text(str(variant)).split()))
                and compact_term in compact_value
                and (
                    len(normalize_text(str(variant)).split()) > 1
                    or normalize_text(str(variant)) not in _GENERIC_SINGLE_TERM_ALIASES
                    and (normalize_text(value) == normalize_text(str(variant)) or len(compact_term) >= 4)
                )
            ]
            if not possible:
                continue
            surface_matches = [variant for variant in possible if _surface_contains(value, str(variant))]
            if surface_matches:
                possible = surface_matches
            for variant in possible:
                if _phrase_matches(value, str(variant)):
                    evidence.append(f"{field}:{brand}|matched_variant:{variant}")
    if not evidence:
        # Saved pilot exports contain names but no extraction provenance. A
        # catalog match is useful evidence, but cannot confirm the publisher.
        for field, value in _unverified_identity_values(extracted).items():
            compact_value = "".join(normalize_text(value).split())
            for brand in brands:
                for variant in [brand, *(aliases.get(brand) or [])]:
                    compact_term = "".join(normalize_text(str(variant)).split())
                    if compact_term and compact_term in compact_value and _phrase_matches(value, str(variant)):
                        evidence.append(f"{field}:{brand}|matched_variant:{variant}|unverified_saved_identity")
        if not evidence:
            return None
        return {
            "state": "CORREDOR_PROBABLE", "confidence": 0.82,
            "reason": "Nombre coincidente con el catálogo, pero la exportación guardada no acredita su extracción como identidad del publicador.",
            "evidence": evidence[:8], "source": "catalog_unverified_identity",
        }
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
        evidence = [
            f"{field}:{term}|unverified_saved_identity"
            for field, value in _unverified_identity_values(extracted).items()
            for term in terms if _phrase_matches(value, term)
        ]
    for field, value in _unverified_identity_values(extracted).items():
        if re.match(r"^\s*corredor\s*[-–—:]\s*", value, re.I):
            evidence.append(f"{field}:publisher_role_prefix:corredor|unverified_saved_identity")
    if not evidence:
        return None
    return {
        "state": "CORREDOR_PROBABLE",
        "confidence": 0.9,
        "reason": "Hard broker terms found in strong publisher fields.",
        "evidence": evidence[:8],
        "source": "rules_json" if _confirmed_identity_values(extracted) else "unverified_saved_identity",
    }


def detect_owner_signals(extracted: dict[str, Any]) -> list[str]:
    terms = load_rule_sets()["owner_keywords"]
    if not terms:
        return []
    text = _all_text(extracted)
    return _search_terms(text, terms)


def _context_text(extracted: dict[str, Any]) -> str:
    # Yapo descriptions can contain HTML separators/entities in saved CSVs and
    # cached runs. Strip markup without shortening the source text so phrases
    # split by <br> remain visible to the contextual rules.
    values = []
    for key in ("title", "description", "descripcion"):
        value = unescape(str(extracted.get(key) or ""))
        values.append(re.sub(r"<[^>]*>", " ", value))
    return normalize_text(" ".join(values))


def _description_business_signature(extracted: dict[str, Any]) -> list[str]:
    """Find an advertiser identifying its real-estate business in the ad text.

    A broker can publish under an individual's account name while signing the
    description with a company brand. Require both a business designation and
    a first-party business verb, plus an explicit service/commercial cue; a
    passing mention of an agency or a nearby property is not enough.
    """
    segments = []
    for key in ("title", "description", "descripcion"):
        source_text = unescape(str(extracted.get(key) or ""))
        source_text = re.sub(r"<[^>]*>", " ", source_text)
        segments.extend(re.split(r"(?<=[.!?;])\s+|[\r\n]+", source_text))
    company_statement = re.compile(
        r"\b(?P<identity>(?:[a-z0-9&+]+\s+){0,3}"
        r"(?:propiedades|inmobiliaria|inmobiliario|corredora|corretaje))\s+"
        r"(?P<verb>actua|actuamos|ofrece|ofrecemos|brinda|brindamos|"
        r"asesora|asesoramos|trabaja|trabajamos)\b"
    )
    service_cue = re.compile(
        r"\b(profesionalismo|etica|asesoria|asesoramiento|corretaje|"
        r"honorarios|servicio(?:s)? de calidad|operacion(?:es)? inmobiliaria(?:s)?|"
        r"para la venta|en venta|comercializacion inmobiliaria)\b"
    )
    business_verb = re.compile(
        r"^(?:actua|actuamos|ofrece|ofrecemos|brinda|brindamos|asesora|"
        r"asesoramos|gestiona|gestionamos|administra|administramos|"
        r"entrega|entregamos|presta|prestamos)$"
    )
    known_brands = _known_brand_compact_index()
    evidence = []
    for segment in segments:
        text = normalize_text(segment)
        for match in company_statement.finditer(text):
            # Signatures and listing headers identify the advertiser near the
            # start of a sentence. Later mentions can describe a third party.
            if len(text[:match.start()].split()) > 4:
                continue
            cue = service_cue.search(text[match.start():match.start() + 260])
            if cue:
                evidence.append(
                    f"description_business_signature:{match.group('identity')} "
                    f"{match.group('verb')}|{cue.group(1)}"
                )
        tokens = text.split()
        for start in range(min(5, len(tokens))):
            for end in range(start + 1, min(len(tokens), start + 13) + 1):
                canonical = known_brands.get("".join(tokens[start:end]))
                if not canonical:
                    continue
                verb_index = end
                while verb_index < len(tokens) and tokens[verb_index] in {
                    "chile", "propiedades", "inmobiliaria", "corretaje", "servicios"
                } and verb_index < end + 3:
                    verb_index += 1
                if verb_index >= len(tokens) or not business_verb.fullmatch(tokens[verb_index]):
                    continue
                cue = service_cue.search(" ".join(tokens[verb_index:verb_index + 35]))
                if cue:
                    evidence.append(
                        f"description_known_brand_signature:{canonical}|"
                        f"{tokens[verb_index]}|{cue.group(1)}"
                    )
    return evidence[:5]


def _unknown_business_signature(extracted: dict[str, Any]) -> list[str]:
    """Find an unknown publisher identity paired with explicit brokerage cues.

    This deliberately returns probable evidence only: a new company-like name
    must be reviewed before it can enter the confirmed brand catalog.
    """
    rules = load_rule_sets()
    known = _known_brand_compact_index()
    shape_terms = rules.get("company_shape_terms", [])
    identity_values = _confirmed_identity_values(extracted)
    if not identity_values:
        # Some saved pilot records predate publisher provenance. The visible
        # publisher is still usable as a candidate, never as confirmed proof.
        identity_values = {
            key: str(extracted.get(key) or "")
            for key in ("publicador_visible", "contact_name", "contact_logo_alt", "listing_advertiser")
            if extracted.get(key)
        }
    business_cue = re.compile(
        r"\b(corretaje|comision(?:es)?|honorarios|"
        r"gestion inmobiliaria|servicios inmobiliarios|asesoria inmobiliaria|"
        r"administracion de propiedades|intermediacion inmobiliaria)\b"
    )
    full_text = _context_text(extracted)
    business_matches = list(business_cue.finditer(full_text))
    has_brokerage_service = any(
        not re.search(r"\b(?:sin|no|libre de)(?:\s+(?:cobro|pago|comision))?\s*$", full_text[max(0, match.start() - 30):match.start()])
        for match in business_matches
    )
    specific_broker_shapes = {
        "corredora", "corredores", "corretaje", "inmobiliaria", "inmobiliario",
        "gestion inmobiliaria", "asesoria inmobiliaria", "servicios inmobiliarios",
    }
    profile = extracted.get("publisher_profile_context") or {}
    repeat_count = int(profile.get("distinct_listing_count") or 0) if isinstance(profile, dict) else 0
    logo_identity = normalize_text(str(extracted.get("contact_logo_alt") or ""))
    repeated_associates = (
        repeat_count >= 2
        and "asociados" in normalize_text(" ".join(identity_values.values()))
        and logo_identity
        and "asociados" in logo_identity
    )
    result: list[str] = []
    for field, value in identity_values.items():
        value = re.sub(r"^\s*(?:foto|imagen|logo)\s+de\s+perfil\s+de\s+", "", value, flags=re.I)
        normalized = normalize_text(value)
        compact = "".join(normalized.split())
        if not compact or any(compact in brand or brand in compact for brand in known):
            continue
        shape_hits = [term for term in shape_terms if _phrase_matches(value, str(term))]
        # Avoid interpreting isolated legal abbreviations or short personal
        # names as a business identity.
        if shape_hits and len(normalized.split()) >= 2:
            normalized_shapes = {normalize_text(term) for term in shape_hits}
            strong_name = bool(normalized_shapes & specific_broker_shapes)
            strength = "probable" if (strong_name or has_brokerage_service or (repeat_count >= 2 and shape_hits)) else "review_candidate"
            result.append(
                f"unknown_business_identity:{field}:{value.strip()}|shape:{shape_hits[0]}|"
                f"repeat_listings:{repeat_count}|strength:{strength}"
            )
        elif repeated_associates:
            result.append(
                f"unknown_business_identity:{field}:{value.strip()}|shape:repeated_associates|"
                f"repeat_listings:{repeat_count}|strength:probable"
            )
    return result[:4]


@lru_cache(maxsize=1)
def _known_brand_compact_index() -> dict[str, str]:
    """Index brand and alias spellings for efficient description checks."""
    rules = load_rule_sets()
    aliases = rules.get("known_broker_brand_aliases") or {}
    result: dict[str, str] = {}
    for brand in rules.get("known_broker_brands") or []:
        for variant in (brand, *(aliases.get(brand) or [])):
            compact = "".join(normalize_text(str(variant)).split())
            normalized_variant = normalize_text(str(variant))
            if normalized_variant not in _GENERIC_SINGLE_TERM_ALIASES and (
                len(compact) >= 4 and (len(normalized_variant.split()) > 1 or len(compact) >= 4)
            ):
                result.setdefault(compact, str(brand))
    return result


def _context_term_hits(text: str, terms: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(term) for term in terms if _phrase_matches(text, str(term))))


def analyze_contextual_signals(extracted: dict[str, Any]) -> dict[str, Any]:
    """Extract contextual broker/owner candidates without confirming owner state."""
    rules = load_rule_sets().get("contextual_classification") or {}
    text = _context_text(extracted)
    if not isinstance(rules, dict):
        rules = {}
    self_declarations = _context_term_hits(text, rules.get("broker_self_declarations", []))
    third_party_mentions = _context_term_hits(text, rules.get("broker_third_party_mentions", []))
    negations = _context_term_hits(text, rules.get("broker_negations", []))
    owner_candidates = _context_term_hits(text, rules.get("owner_candidate_signals", []))
    business_signature_evidence = _description_business_signature(extracted)
    unknown_business_evidence = _unknown_business_signature(extracted)
    trato_directo_terms = [str(rules.get("direct_deal_phrase") or "")]
    trato_directo = _context_term_hits(text, trato_directo_terms)
    commercial_contradictions = _context_term_hits(text, rules.get("commercial_contradiction_signals", []))
    # A phrase such as "se acepta corredor" or "trabajamos con corredor" refers
    # to another party and must not be read as the publisher identifying itself.
    self_declaration_confirmed = bool(self_declarations) and not negations and not any(
        term in text for term in ("busco corredor", "se acepta corredor", "con corredor", "trabajamos con corredor")
    )
    direct_deal = bool(owner_candidates)
    contradiction = direct_deal and bool(self_declarations or commercial_contradictions or third_party_mentions)
    trato_directo_contradiction = bool(trato_directo) and bool(
        self_declarations or commercial_contradictions or third_party_mentions
    )
    return {
        "broker_self_declarations": self_declarations,
        "broker_third_party_mentions": third_party_mentions,
        "broker_negations": negations,
        "broker_business_signature_evidence": business_signature_evidence,
        "unknown_business_identity_evidence": unknown_business_evidence,
        "owner_signal_candidates": owner_candidates,
        "commercial_contradiction_signals": commercial_contradictions,
        "broker_self_declaration_confirmed": self_declaration_confirmed,
        "direct_deal_candidate": direct_deal,
        "direct_deal_with_commercial_contradictions": contradiction,
        "trato_directo_candidate": bool(trato_directo),
        "trato_directo_with_commercial_contradictions": trato_directo_contradiction,
        "owner_classification_confirmed": False,
    }


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
            if _phrase_matches(value, term):
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
    professional_type = (
        seller_type in {"profesional", "empresa", "agente", "corredor"}
        and _publisher_field_is_confirmed(extracted, "seller_type")
    )
    professional_badge = (
        "profesional" in badges
        and _publisher_field_is_confirmed(extracted, "contact_badges_text")
    )
    independent_professional_evidence = (
        professional_type
        and professional_badge
        and _publisher_field_source(extracted, "seller_type") != "publisher_scoped_badge"
    )
    if independent_professional_evidence:
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
    if professional_type or professional_badge:
        return {
            "state": "CORREDOR_PROBABLE",
            "confidence": 0.85,
            "reason": "Una señal profesional confirmada está vinculada al bloque del publicador, sin corroboración independiente.",
            "evidence": [
                *( [f"seller_type:{seller_type}"] if professional_type else [] ),
                *( ["contact_badges_text:profesional"] if professional_badge else [] ),
            ],
            "source": "structural_rules",
        }

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
    contextual = analyze_contextual_signals(extracted)
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
        "owner_signal_evidence": contextual["owner_signal_candidates"],
        "owner_signal_candidates": contextual["owner_signal_candidates"],
        "contextual_analysis": contextual,
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


def classify_with_rules(extracted: dict[str, Any]) -> dict[str, Any] | None:
    """Compatibility name for the single deterministic Yapo classification path."""
    return classify_yapo_listing(extracted)


def classify_yapo_listing(extracted: dict[str, Any]) -> dict[str, Any] | None:
    """Apply identity-first broker rules, then contextual title/description rules."""
    technical_status = str(extracted.get("html_validation_status") or extracted.get("validation_status") or "").upper()
    if technical_status in {"BLOCKED", "INVALID"}:
        # Technical fetch/validation failures are not commercial classifications.
        return None
    obvious = classify_obvious_broker(extracted)
    if obvious:
        if obvious.get("state") == "AD_REMOVED":
            return obvious
        contextual = analyze_contextual_signals(extracted)
        contextual["direct_deal_with_commercial_contradictions"] = bool(
            contextual["direct_deal_with_commercial_contradictions"]
            or (contextual["direct_deal_candidate"] and obvious.get("state") in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE"})
        )
        contextual["identity_and_owner_signal_conflict"] = bool(
            contextual["direct_deal_candidate"] and obvious.get("state") in {"CORREDOR_SEGURO", "CORREDOR_PROBABLE"}
        )
        if contextual["identity_and_owner_signal_conflict"] and contextual["trato_directo_candidate"]:
            contextual["trato_directo_with_commercial_contradictions"] = True
        return {**obvious, "contextual_analysis": contextual,
                "owner_signal_candidates": contextual["owner_signal_candidates"],
                "direct_deal_with_commercial_contradictions": contextual["direct_deal_with_commercial_contradictions"]}

    contextual = analyze_contextual_signals(extracted)
    if contextual["broker_self_declaration_confirmed"]:
        return {
            "state": "CORREDOR_PROBABLE", "confidence": 0.72,
            "reason": "El título o la descripción identifica al anunciante como corredor, sin corroboración independiente de identidad.",
            "evidence": [f"context_broker_declaration:{term}" for term in contextual["broker_self_declarations"]],
            "source": "contextual_rules", "contextual_analysis": contextual,
            "owner_signal_candidates": contextual["owner_signal_candidates"],
            "direct_deal_with_commercial_contradictions": contextual["direct_deal_with_commercial_contradictions"],
        }

    if contextual["broker_business_signature_evidence"]:
        return {
            "state": "CORREDOR_SEGURO", "confidence": 0.99,
            "reason": "La descripción firma el aviso con una identidad inmobiliaria y una declaración comercial de servicios.",
            "evidence": contextual["broker_business_signature_evidence"],
            "source": "description_business_signature", "contextual_analysis": contextual,
            "owner_signal_candidates": contextual["owner_signal_candidates"],
            "direct_deal_with_commercial_contradictions": True,
        }

    strong_unknown_identity = any(
        "|strength:probable" in item
        for item in contextual["unknown_business_identity_evidence"]
    )
    if strong_unknown_identity:
        return {
            "state": "CORREDOR_PROBABLE", "confidence": 0.78,
            "reason": "Identidad comercial no incluida en el catálogo, respaldada por señales explícitas de corretaje; requiere revisión antes de confirmarse.",
            "evidence": contextual["unknown_business_identity_evidence"],
            "source": "unknown_business_context",
            "contextual_analysis": contextual,
            "owner_signal_candidates": contextual["owner_signal_candidates"],
            "direct_deal_with_commercial_contradictions": True,
        }

    company = company_shape_in_publisher_fields(extracted)
    strong_owner_declarations = {
        "dueño vende", "dueno vende", "propietario vende", "particular vende",
        "soy dueño", "soy dueno", "soy propietario", "vendo mi casa",
        "vendo mi departamento", "vendo mi propiedad", "directo con el dueño",
        "directo con propietario", "trato directo",
    }
    owner_declarations = [
        signal for signal in contextual["owner_signal_candidates"]
        if normalize_text(signal) in {normalize_text(term) for term in strong_owner_declarations}
    ]
    if (
        owner_declarations
        and not company["company_like_suspected"]
        and not contextual["direct_deal_with_commercial_contradictions"]
        and not contextual["trato_directo_with_commercial_contradictions"]
        and not contextual["broker_third_party_mentions"]
    ):
        return {
            "state": "DUEÑO_PROBABLE", "confidence": 0.72,
            "reason": "El texto declara una venta directa o una identidad de propietario, sin señales comerciales contradictorias.",
            "evidence": [f"owner_declaration:{signal}" for signal in owner_declarations],
            "source": "contextual_rules", "contextual_analysis": contextual,
            "owner_signal_candidates": contextual["owner_signal_candidates"],
            "owner_classification_confirmed": False,
        }

    return {
        "state": "INCIERTO",
        "confidence": 0.35 if company["company_like_suspected"] else 0.2,
        "reason": "Evidence is insufficient for a commercial classification; manual review is pending.",
        "evidence": [*company["company_like_evidence"],
                     *(f"context_third_party_broker:{term}" for term in contextual["broker_third_party_mentions"]),
                     *(f"context_broker_negation:{term}" for term in contextual["broker_negations"])],
        "source": "contextual_rules" if contextual["owner_signal_candidates"] or contextual["broker_third_party_mentions"] else "rules_json",
        **company,
        "owner_signal_evidence": contextual["owner_signal_candidates"],
        "owner_signal_candidates": contextual["owner_signal_candidates"],
        "contextual_analysis": contextual,
        "direct_deal_with_commercial_contradictions": contextual["direct_deal_with_commercial_contradictions"],
        "weak_broker_evidence": [],
    }
