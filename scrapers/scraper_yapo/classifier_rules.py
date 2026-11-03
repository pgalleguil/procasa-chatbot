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


@lru_cache(maxsize=1)
def _verified_broker_candidate_profiles() -> dict[str, dict[str, Any]]:
    """Load manually verified profile evidence without promoting brands to the confirmed catalog."""
    path = DATA_DIR / "verified_broker_candidate_profiles.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    profiles = payload.get("profiles", []) if isinstance(payload, dict) else []
    return {
        str(item.get("seller_profile_id")): item
        for item in profiles if isinstance(item, dict) and item.get("seller_profile_id")
    }


def verified_broker_candidate_profile(extracted: dict[str, Any]) -> dict[str, Any] | None:
    """Classify only reviewed profile IDs as probable; confirmed brand catalog remains unchanged."""
    profile_id = str(extracted.get("seller_profile_id") or "").strip()
    candidate = _verified_broker_candidate_profiles().get(profile_id)
    if not candidate:
        return None
    if not _publisher_field_is_confirmed(extracted, "publicador_visible"):
        return None
    actual_name = normalize_text(str(extracted.get("publicador_visible") or ""))
    expected_name = normalize_text(str(candidate.get("display_name") or ""))
    if not actual_name or actual_name != expected_name:
        return None
    if not _publisher_field_is_confirmed(extracted, "seller_type") or normalize_text(str(extracted.get("seller_type") or "")) != "profesional":
        return None
    return {
        "state": "CORREDOR_PROBABLE", "confidence": 0.88,
        "reason": "Perfil comercial verificado por fuente externa y coincidente con identidad profesional extraída; marca pendiente de incorporación al catálogo confirmado.",
        "evidence": [
            f"verified_profile_id:{profile_id}",
            f"confirmed_publisher_identity:{candidate['display_name']}",
            "seller_type:PROFESIONAL",
            f"external_business_evidence:{candidate.get('evidence_source', '')}",
        ],
        "source": "verified_candidate_profile_not_confirmed_brand",
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
    for field, value in _field_values(extracted, STRONG_PUBLISHER_FIELDS).items():
        if re.match(r"^\s*corredor\s*[-–—:]\s*", value, re.I):
            suffix = "" if _publisher_field_is_confirmed(extracted, field) else "|unverified_saved_identity"
            evidence.append(f"{field}:publisher_role_prefix:corredor{suffix}")
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
    language = analyze_owner_language(extracted)
    return [
        str(item["fragment"])
        for item in language["owner_declarations"]
        if item.get("attribution") == "advertiser_declaration_candidate"
    ]


def _context_text(extracted: dict[str, Any]) -> str:
    # Yapo descriptions can contain HTML separators/entities in saved CSVs and
    # cached runs. Strip markup without shortening the source text so phrases
    # split by <br> remain visible to the contextual rules.
    values = []
    for key in ("title", "description", "descripcion"):
        value = unescape(str(extracted.get(key) or ""))
        values.append(re.sub(r"<[^>]*>", " ", value))
    return normalize_text(" ".join(values))


def _plain_text_with_offsets(value: Any) -> tuple[str, list[int]]:
    """Normalize markup/case/accents while retaining offsets into the full source text."""
    plain = re.sub(r"<[^>]*>", " ", unescape(str(value or "")))
    chars: list[str] = []
    offsets: list[int] = []
    for index, source_char in enumerate(plain):
        expanded = unicodedata.normalize("NFKD", source_char).casefold()
        for char in expanded:
            if unicodedata.combining(char):
                continue
            if char.isalnum():
                chars.append(char)
                offsets.append(index)
            elif chars and chars[-1] != " ":
                chars.append(" ")
                offsets.append(index)
    while chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()
    return "".join(chars), offsets


_OWNER_DECLARATION_RULES = (
    ("OWNER_SUBJECT_SELLS", "owner_explicit", "ALTA", r"\b(?:(?:el|la|los|las|su|sus)\s+)?(?:propio\s+)?(?:dueno|duena|duenos|duenas|propietario|propietaria|propietarios|propietarias|titular|titulares)\s+(?:vende|venden|vendemos)\b"),
    ("OWNER_SALE_BY_OWNER", "owner_explicit", "ALTA", r"\b(?:vende|venden|vendemos|venta|se vende|vta)\s+(?:(?:directamente|directa|por|de|del|su|sus|el|la|los|las|legitimo|legitima|legitimos|legitimas)\s+){0,4}(?:propio\s+)?(?:dueno|duena|duenos|duenas|propietario|propietaria|propietarios|propietarias|titular|titulares)\b|\bpropiedad\s+(?:vendida|en venta)\s+(?:por|de)\s+(?:(?:su|el|la|sus|los|las)\s+)?(?:dueno|duena|propietario|propietaria|propietarios|duenos)\b|\b(?:casa|departamento|propiedad|vivienda|inmueble)\s+en venta\s+por\s+(?:(?:sus|su|el|la|los|las)\s+)?(?:duenos|dueno|duena|propietario|propietaria|propietarios)\b"),
    ("OWNER_SUBJECT_SELLS", "owner_explicit", "ALTA", r"\b(?:(?:el|la|los|las|su|sus)\s+)?(?:propio\s+)?(?:dueno|duena|duenos|duenas|propietario|propietaria|propietarios|propietarias|titular|titulares)\s+(?:directamente\s+)?(?:vende|venden|vendemos)\b"),
    ("OWNER_DIRECT_IDENTITY", "owner_explicit", "ALTA", r"\b(?:dueno|duena|duenos|duenas|propietario|propietaria|propietarios|propietarias)\s+directo(?:s)?\b|\bdirecto\s+(?:el\s+|la\s+|su\s+)?(?:dueno|duena|propietario|propietaria)\b"),
    ("OWNER_FIRST_PERSON_IDENTITY", "owner_first_person", "ALTA", r"\b(?:soy|somos)\s+(?:(?:el|la|los|las)\s+)?(?:dueno|duena|duenos|duenas|propietario|propietaria|propietarios|propietarias|titular|copropietario|copropietaria)\b(?:\s+de\s+(?:(?:la|el|mi|esta|este|su)\s+)?(?:propiedad|casa|departamento|vivienda|inmueble|hogar|terreno|bien raiz))?"),
    ("OWNER_FIRST_PERSON_SELLS_OWN_HOME", "owner_first_person", "ALTA", r"\b(?:vendo|vendemos|estoy vendiendo|estamos vendiendo|me encuentro vendiendo|pongo en venta|pongo a la venta|ofrezco en venta)\s+(?:(?:mi|mis|nuestro|nuestra|nuestros|nuestras)\s+)(?:propio\s+|propia\s+)?(?:casa|casas|departamento|departamentos|propiedad|propiedades|vivienda|viviendas|inmueble|inmuebles|hogar|hogares|terreno|terrenos|bien raiz|bienes raices)\b"),
    ("OWNER_FIRST_PERSON_SELLS_OWN_HOME", "owner_first_person", "ALTA", r"\b(?:ofrezco|ofrecemos)\s+(?:(?:mi|mis|nuestro|nuestra|nuestros|nuestras)\s+)(?:propio\s+|propia\s+)?(?:casa|casas|departamento|departamentos|propiedad|propiedades|vivienda|viviendas|inmueble|inmuebles|hogar|hogares|terreno|terrenos)\s+en venta\b"),
    ("OWNER_FIRST_PERSON_SELF_REASON", "owner_first_person", "ALTA", r"\b(?:vendo|vendemos|estoy vendiendo|estamos vendiendo)\b.{0,70}\b(?:porque soy|por ser|al ser|x ser)\s+(?:(?:el|la)\s+)?(?:dueno|duena|propietario|propietaria)\b"),
    ("OWNER_ABBREVIATED_DIRECT_SALE", "owner_explicit", "ALTA", r"\b(?:vta|venta)\s+directa?\s+(?:por\s+)?(?:(?:el|la|su|sus)\s+)?(?:dueno|duena|duenos|duenas|propietario|propietaria|propietarios|propietarias)\b|\bvta\s+particular\b|\bventa\s+particular\s+sin\s+intermediarios\b|\btrato\s+dir\.?\s*(?:con\s+)?(?:el\s+|la\s+|su\s+)?(?:dueno|duena|propietario|propietaria)\b"),
    ("OWNER_HOME_IS_FOR_SALE", "owner_first_person", "ALTA", r"\b(?:mi|nuestra|nuestro)\s+(?:casa|propiedad|departamento|vivienda|inmueble|hogar)\s+(?:esta|se encuentra)\s+en venta\b"),
    ("OWNER_SALE_AS_PARTICULAR", "private_party_sale", "MEDIA", r"\b(?:venta|vta)\s+(?:de|por)\s+(?:(?:un|una)\s+)?particular\b|\bparticular\s+vende\b|\b(?:vende|venden|vendemos)\s+(?:directamente\s+)?(?:un\s+)?particular\b|\bsoy particular\b"),
)

_OWNER_SUPPORT_RULES = (
    ("NO_INTERMEDIARY", "no_intermediary", "MEDIA", r"\b(?:sin\s+(?:corredor(?:a|es)?|intermediarios?|intermediacion(?:\s+inmobiliaria)?|agentes inmobiliarios?|corretaje)|venta\s+sin\s+(?:corretaje|intermediacion(?:\s+inmobiliaria)?)|no\s+(?:hay|interviene|trabajo con|trabajamos con|acepto|aceptamos)\s+(?:corredor(?:a|es)?|intermediarios?)|corredores?\s+abstenerse|corredoras?\s+abstenerse|agencias?\s+abstenerse|inmobiliarias?\s+abstenerse|solo\s+particulares|solo\s+compradores directos|s\s*[/ ]\s*intermediarios?)\b"),
    ("NO_COMMISSION", "no_commission", "BAJA", r"\b(?:sin\s+comision(?:es)?(?:\s+(?:de|para)\s+(?:el\s+)?(?:comprador|corretaje|corredor(?:a)?|intermediacion|inmobiliaria))?|sin\s+honorarios(?:\s+de corretaje|\s+de intermediacion)?|no\s+(?:se\s+)?(?:paga|pagan|pagamos|cobramos|hay|existe|se cobran)\s+(?:la\s+)?comision(?:es)?|libre\s+de\s+comision(?:es)?|comision\s+cero|0\s*por\s*ciento\s+de comision|0\s*%\s+de comision)\b|\bs\s*[/ ]\s*comision(?:es)?\b"),
    ("DIRECT_DEAL_NEUTRAL", "direct_deal", "BAJA", r"\b(?:trato directo|venta directa|compra directa|directamente con el propietario|sin intermediacion de terceros)\b"),
    ("DIRECT_OWNER_CONTACT", "direct_deal", "MEDIA", r"\b(?:trato|contacto|negociacion|venta)\s+direct(?:o|a)(?:mente)?\s+(?:con\s+)?(?:(?:el|la|su|sus)\s+)?(?:dueno|duena|propietario|propietaria|propietarios|duenos)\b|\b(?:directamente|directo)\s+(?:con\s+)?(?:el\s+|la\s+|su\s+)?(?:dueno|duena|propietario|propietaria)\b"),
)

_OWNER_FAMILY_RULES = (
    ("FAMILY_OR_REPRESENTATIVE", "family_or_representative", "MEDIA", r"\b(?:familia(?:r)?|propiedad familiar|mis padres|los padres|mi madre|mi padre|herederos?|heredada|heredado|copropietarios?|copropietarias?|representante|familiar del dueno|mi porcentaje de propiedad)\b|\b(?:vendo|vendemos)\s+(?:la\s+)?(?:casa|propiedad)\s+de\s+(?:mis|los)\s+padres\b"),
)

_OWNER_NEGATION_RULES = (
    ("OWNER_IDENTITY_NEGATED", r"\b(?:no soy|no somos|no es|no era|no fue)\s+(?:(?:el|la|un|una|los|las)\s+)?(?:dueno|duena|duenos|duenas|propietario|propietaria|propietarios|propietarias|titular|titulares)\b"),
    ("OWNER_LOOKUP_NOT_SELLER", r"\b(?:busco|buscamos|se busca|solo)\s+(?:un\s+)?(?:dueno|duena|propietario|propietaria|propietarios)\b|\bbusco\s+(?:una\s+)?casa\s+de\s+(?:un\s+)?propietario\b|\bsolo propietarios?\s*,?\s*contactar\b"),
    ("OWNER_DOES_NOT_SELL_DIRECTLY", r"\b(?:el|la|su)?\s*(?:dueno|duena|propietario|propietaria)\s+no\s+vende\s+directamente\b"),
)

_OWNER_THIRD_PARTY_RULES = (
    ("OWNER_MENTION_THIRD_PARTY", r"\b(?:el|la|su|sus|nuestro|nuestros|mi|mis)\s+(?:dueno|duena|propietario|propietaria)\s+(?:acepta ofertas|autoriza|entrega|paga|acepta corredores?)\b"),
    ("BROKER_REPRESENTS_OWNER", r"\b(?:nuestro cliente es|gestionamos la venta para|representamos al?|asesoramos al?|tramitamos venta directa con|contactamos al?|trabajamos con|propiedad captada directamente del?|tenemos mandato del?|comision a cargo del?|vende por cuenta del?)\s+(?:el\s+|la\s+)?(?:dueno|duena|propietario|propietaria)\b"),
    ("BROKER_SELLS_OWNER_PROPERTY", r"\b(?:corredora|corredor|inmobiliaria|agencia)\s+encargada\s+de\s+vender\b|\bvendemos\s+(?:la\s+)?(?:casa|propiedad)\s+de\s+(?:un\s+)?(?:propietario|dueno)\b|\bagendamos visitas?\s+directamente con\s+(?:los\s+)?(?:duenos|propietarios)\b"),
)


def _find_language_evidence(extracted: dict[str, Any], rules: Iterable[tuple[str, str, str, str]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str]] = set()
    for field in ("title", "description", "descripcion"):
        raw = extracted.get(field)
        if not raw:
            continue
        source_identity = (field if field == "title" else "description", str(raw))
        if source_identity in seen_sources:
            continue
        seen_sources.add(source_identity)
        plain = re.sub(r"<[^>]*>", " ", unescape(str(raw)))
        normalized, offsets = _plain_text_with_offsets(raw)
        for rule_id, family, strength, pattern in rules:
            for match in re.finditer(pattern, normalized):
                if not offsets or match.start() >= len(offsets) or match.end() <= 0:
                    continue
                start, end = offsets[match.start()], offsets[match.end() - 1] + 1
                sentence_start = max((plain.rfind(mark, 0, start) + 1 for mark in (".", "!", "?", ";", "\n", "\r")), default=0)
                sentence_ends = [plain.find(mark, end) for mark in (".", "!", "?", ";", "\n", "\r")]
                sentence_end = min((pos for pos in sentence_ends if pos >= 0), default=len(plain))
                context = plain[sentence_start:sentence_end]
                evidence.append({
                    "rule_id": rule_id, "family": family, "field": field,
                    "fragment": plain[start:end].strip(), "strength": strength,
                    "position_start": start, "position_end": end,
                    "context": context.strip(),
                })
    dedup: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in evidence:
        dedup[(item["rule_id"], item["field"], item["position_start"], item["fragment"].casefold())] = item
    return list(dedup.values())


def analyze_owner_language(extracted: dict[str, Any]) -> dict[str, Any]:
    declarations = _find_language_evidence(extracted, _OWNER_DECLARATION_RULES)
    support = _find_language_evidence(extracted, _OWNER_SUPPORT_RULES)
    family = _find_language_evidence(extracted, _OWNER_FAMILY_RULES)
    negations = _find_language_evidence(extracted, tuple((rule, "owner_negation", "MEDIA", pattern) for rule, pattern in _OWNER_NEGATION_RULES))
    third_party = _find_language_evidence(extracted, tuple((rule, "third_party_reference", "MEDIA", pattern) for rule, pattern in _OWNER_THIRD_PARTY_RULES))
    for item in declarations:
        normalized_context = normalize_text(item["context"])
        if any(normalize_text(other["context"]) == normalized_context for other in (*negations, *third_party)):
            item["attribution"] = "negated_or_third_party"
            item["related_rule_ids"] = [other["rule_id"] for other in (*negations, *third_party) if normalize_text(other["context"]) == normalized_context]
        else:
            item["attribution"] = "advertiser_declaration_candidate"
            item["related_rule_ids"] = []
    return {
        "owner_declarations": declarations,
        "owner_support_signals": support,
        "family_or_representative_signals": family,
        "owner_negations": negations,
        "owner_third_party_mentions": third_party,
    }


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
    self_declaration_matches = _find_language_evidence(extracted, (
        ("BROKER_FIRST_PERSON_IDENTITY", "broker_self_declaration", "ALTA", r"\b(?:soy|somos)\s+(?:un\s+|una\s+|el\s+|la\s+)?(?:corredor(?:a)?(?:\s+de\s+propiedades)?|agente inmobiliario|asesor inmobiliario)\b"),
        ("BROKER_SELF_SERVICE_OFFER", "broker_self_declaration", "ALTA", r"\b(?:corredor(?:a)?|inmobiliaria|agencia de propiedades)\s+(?:ofrece|ofrecemos|ofrecen|asesora|asesoramos|gestionamos)\b"),
    ))
    self_declarations = list(dict.fromkeys(
        [*_context_term_hits(text, rules.get("broker_self_declarations", [])), *(item["fragment"] for item in self_declaration_matches)]
    ))
    third_party_mentions = _context_term_hits(text, rules.get("broker_third_party_mentions", []))
    negations = _context_term_hits(text, rules.get("broker_negations", []))
    owner_language = analyze_owner_language(extracted)
    attributed_owner_declarations = [
        item for item in owner_language["owner_declarations"]
        if item.get("attribution") == "advertiser_declaration_candidate"
    ]
    owner_candidates = list(dict.fromkeys(item["fragment"] for item in attributed_owner_declarations))
    business_signature_evidence = _description_business_signature(extracted)
    unknown_business_evidence = _unknown_business_signature(extracted)
    trato_directo = [
        item["fragment"] for item in owner_language["owner_support_signals"]
        if item.get("family") == "direct_deal"
    ]
    commercial_contradictions = _context_term_hits(text, rules.get("commercial_contradiction_signals", []))
    commercial_contradictions.extend(
        item["fragment"] for item in _find_language_evidence(extracted, (
            ("BROKERAGE_COMMISSION_CHARGED", "commercial_activity", "MEDIA", r"\b(?:se\s+)?(?:cobra|cobran|cobramos|cobro)\s+(?:una\s+)?comision(?:es)?\s+(?:de\s+)?(?:corretaje|intermediacion|corredor(?:a)?)\b"),
            ("BROKERAGE_SERVICE_OFFER", "commercial_activity", "MEDIA", r"\b(?:ofrecemos|ofrezco|brindamos|prestamos)\s+(?:servicios?\s+)?(?:de\s+)?(?:corretaje|intermediacion inmobiliaria|gestion inmobiliaria)\b"),
        ))
    )
    commercial_contradictions = list(dict.fromkeys(commercial_contradictions))
    # A phrase such as "se acepta corredor" or "trabajamos con corredor" refers
    # to another party and must not be read as the publisher identifying itself.
    self_declaration_confirmed = bool(self_declarations) and not negations and not any(
        term in text for term in ("busco corredor", "se acepta corredor", "con corredor", "trabajamos con corredor")
    )
    direct_deal = bool(trato_directo)
    contradiction = bool(owner_candidates or trato_directo) and bool(self_declarations or commercial_contradictions or third_party_mentions)
    trato_directo_contradiction = bool(trato_directo) and bool(
        self_declarations or commercial_contradictions or third_party_mentions
    )
    return {
        "broker_self_declarations": self_declarations,
        "broker_self_declaration_evidence": self_declaration_matches,
        "broker_third_party_mentions": third_party_mentions,
        "broker_negations": negations,
        "broker_business_signature_evidence": business_signature_evidence,
        "unknown_business_identity_evidence": unknown_business_evidence,
        "owner_signal_candidates": list(dict.fromkeys([*owner_candidates, *trato_directo, *( ["trato directo"] if trato_directo else [] )])),
        "owner_declarations": owner_language["owner_declarations"],
        "owner_support_signals": owner_language["owner_support_signals"],
        "owner_negations": owner_language["owner_negations"],
        "owner_third_party_mentions": owner_language["owner_third_party_mentions"],
        "family_or_representative_signals": owner_language["family_or_representative_signals"],
        "commercial_contradiction_signals": commercial_contradictions,
        "broker_self_declaration_confirmed": self_declaration_confirmed,
        "direct_deal_candidate": direct_deal,
        "direct_deal_with_commercial_contradictions": contradiction,
        "identity_and_owner_signal_conflict": bool(owner_candidates or trato_directo) and bool(classify_obvious_broker(extracted)),
        "trato_directo_candidate": bool(trato_directo),
        "trato_directo_with_commercial_contradictions": trato_directo_contradiction,
        "owner_classification_confirmed": False,
    }


def company_shape_in_publisher_fields(extracted: dict[str, Any]) -> dict[str, Any]:
    terms = load_rule_sets()["company_shape_terms"]
    # Yapo profile names commonly concatenate a specific real-estate marker
    # with the brand (for example, "CastroPropiedades"). Accept only these
    # explicit brokerage descriptors in compact form; legal abbreviations
    # such as S.A. remain boundary-sensitive to avoid personal-name matches.
    compact_business_terms = {"propiedades", "inmobiliaria", "corredora", "corredores", "corretaje", "bienes raices", "real estate"}
    values = _field_values(extracted, STRONG_PUBLISHER_FIELDS)
    evidence: list[str] = []
    for field, value in values.items():
        compact_value = "".join(_matchable(value).split())
        for term in terms:
            norm = _matchable(term)
            if not norm:
                continue
            compact_term = "".join(norm.split())
            if _phrase_matches(value, term) or (
                norm in compact_business_terms and compact_term in compact_value
            ):
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


def _explicit_seller_contact_mismatch(extracted: dict[str, Any]) -> dict[str, str] | None:
    """Find an explicitly named contact who differs from the publisher identity."""
    publisher = str(extracted.get("publicador_visible") or extracted.get("contact_name") or "").strip()
    publisher_words = normalize_text(publisher).split()
    if not publisher or "@" in publisher or len(publisher_words) < 2:
        return None
    description = re.sub(r"<[^>]+>", " ", str(extracted.get("description") or extracted.get("descripcion") or ""))
    pattern = re.compile(
        r"\b(?:contactar(?:\s+al)?|contacto|comunicarse\s+con)\s*(?:a|con)?\s*:?\s*"
        r"([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑa-záéíóúüñ'-]+(?:\s+[A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑa-záéíóúüñ'-]+){1,3})",
        re.IGNORECASE,
    )
    publisher_norm = " ".join(publisher_words)
    for match in pattern.finditer(description):
        contact = re.sub(r"[.,;:!?]+$", "", match.group(1)).strip()
        contact_words = normalize_text(contact).split()
        contact_norm = " ".join(contact_words)
        if len(contact_words) >= 2 and contact_norm not in publisher_norm and publisher_norm not in contact_norm:
            return {"publisher": publisher, "named_contact": contact, "fragment": match.group(0)}
    return None


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
    # A Yapo PROFESSIONAL label and its profile badge are structural account
    # metadata, not independent proof of brokerage. Keep them observable, but
    # require commercial identity, brokerage services, a known brand, or a
    # confirmed profile correlation before classifying the publisher as broker.
    confirmed_company = company_shape_in_publisher_fields(extracted)
    confirmed_company_profile = professional_type and confirmed_company["company_like_suspected"] and any(
        _publisher_field_is_confirmed(extracted, evidence.split(":", 1)[0])
        for evidence in confirmed_company["company_like_evidence"]
    )
    if confirmed_company_profile:
        return {
            "state": "CORREDOR_SEGURO", "confidence": 0.99,
            "reason": "El nombre comercial asociado al anuncio y la etiqueta profesional del perfil identifican al publicador como corredor.",
            "evidence": [*confirmed_company["company_like_evidence"], f"seller_type:{seller_type}"],
            "source": "confirmed_publisher_company_and_profile",
        }
    brand = known_broker_brand_in_strong_fields(extracted)
    if brand:
        return brand
    # Do not let an isolated PROFESSIONAL account tag create a broker state.

    hard = hard_broker_terms_in_strong_fields(extracted)
    if hard:
        return hard

    reviewed_profile = verified_broker_candidate_profile(extracted)
    if reviewed_profile:
        return reviewed_profile

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
    """Classify seller identity while retaining owner, broker, and conflict evidence."""
    technical_status = str(extracted.get("html_validation_status") or extracted.get("validation_status") or "").upper()
    if technical_status in {"BLOCKED", "INVALID"}:
        # Technical fetch/validation failures are not commercial classifications.
        return None
    removed = is_removed_listing(extracted)
    if removed:
        return removed

    contextual = analyze_contextual_signals(extracted)
    owner_evidence = [
        {**item, "evidence_id": f"{item['rule_id']}:{item['field']}:{item['position_start']}"}
        for item in contextual["owner_declarations"]
        if item.get("attribution") == "advertiser_declaration_candidate"
    ]
    listing_advertiser = normalize_text(str(extracted.get("listing_advertiser_type") or ""))
    if listing_advertiser in {"propietario", "dueno", "particular"}:
        owner_evidence.append({
            "evidence_id": "LISTING_ADVERTISER_OWNER_TYPE:listing_advertiser_type",
            "rule_id": "LISTING_ADVERTISER_OWNER_TYPE", "family": "portal_listing_attribute",
            "field": "listing_advertiser_type", "fragment": str(extracted.get("listing_advertiser_type")),
            "strength": "MEDIA", "position_start": None, "position_end": None,
            "attribution": "portal_listing_attribute",
        })

    broker_identity = classify_obvious_broker(extracted)
    if broker_identity and broker_identity.get("state") == "AD_REMOVED":
        return broker_identity
    company = company_shape_in_publisher_fields(extracted)
    broker_evidence: list[dict[str, Any]] = []
    broker_rule_ids: list[str] = []
    if broker_identity:
        for evidence in broker_identity.get("evidence") or []:
            field, _, detail = str(evidence).partition(":")
            broker_evidence.append({
                "evidence_id": f"BROKER_IDENTITY:{field}:{len(broker_evidence)}",
                "rule_id": f"BROKER_IDENTITY_{str(broker_identity.get('source') or 'RULE').upper()}",
                "family": "publisher_identity", "field": field or "publisher_identity",
                "fragment": detail or str(evidence), "strength": "ALTA" if broker_identity.get("state") == "CORREDOR_SEGURO" else "MEDIA",
                "position_start": None, "position_end": None,
                "provenance": _publisher_field_source(extracted, field) if field else "",
            })
            broker_rule_ids.append(broker_evidence[-1]["rule_id"])
    if company["company_like_suspected"]:
        for evidence in company["company_like_evidence"]:
            field, _, fragment = evidence.partition(":")
            broker_evidence.append({
                "evidence_id": f"PUBLISHER_COMPANY_SHAPE:{field}:{len(broker_evidence)}",
                "rule_id": "PUBLISHER_IDENTITY_COMPANY_SHAPE",
                "family": "publisher_company_identity", "field": field,
                "fragment": fragment, "strength": "MEDIA" if _publisher_field_is_confirmed(extracted, field) else "BAJA",
                "position_start": None, "position_end": None,
                "provenance": _publisher_field_source(extracted, field),
            })
            if _publisher_field_is_confirmed(extracted, field):
                broker_rule_ids.append("PUBLISHER_IDENTITY_COMPANY_SHAPE")
    for item in contextual["broker_self_declaration_evidence"]:
        broker_evidence.append({
            **item, "evidence_id": f"{item['rule_id']}:{item['field']}:{item['position_start']}",
        })
        broker_rule_ids.append(item["rule_id"])
    for item in contextual["broker_business_signature_evidence"]:
        broker_evidence.append({
            "evidence_id": f"BROKER_DESCRIPTION_SIGNATURE:{len(broker_evidence)}",
            "rule_id": "BROKER_DESCRIPTION_BUSINESS_SIGNATURE", "family": "first_party_business_signature",
            "field": "description", "fragment": item, "strength": "ALTA",
            "position_start": None, "position_end": None,
        })
        broker_rule_ids.append("BROKER_DESCRIPTION_BUSINESS_SIGNATURE")
    for item in contextual["unknown_business_identity_evidence"]:
        probable = "|strength:probable" in item
        broker_evidence.append({
            "evidence_id": f"UNKNOWN_BUSINESS_IDENTITY:{len(broker_evidence)}",
            "rule_id": "UNKNOWN_BUSINESS_IDENTITY_REPEATED_OR_SERVICE_CUE",
            "family": "unknown_commercial_identity", "field": item.split(":", 2)[1] if ":" in item else "publisher_identity",
            "fragment": item, "strength": "MEDIA" if probable else "BAJA",
            "position_start": None, "position_end": None,
        })
        if probable:
            broker_rule_ids.append("UNKNOWN_BUSINESS_IDENTITY_REPEATED_OR_SERVICE_CUE")

    strong_owner = any(item.get("strength") == "ALTA" for item in owner_evidence)
    private_party_declaration = any(item.get("family") == "private_party_sale" for item in owner_evidence)
    strong_broker_identity = bool(broker_identity and broker_identity.get("state") == "CORREDOR_SEGURO") or bool(contextual["broker_business_signature_evidence"])
    probable_broker = bool(broker_identity and broker_identity.get("state") == "CORREDOR_PROBABLE") or contextual["broker_self_declaration_confirmed"] or any(
        "|strength:probable" in item for item in contextual["unknown_business_identity_evidence"]
    )
    confirmed_company_identity = bool(company["company_like_suspected"] and any(
        _publisher_field_is_confirmed(extracted, item.split(":", 1)[0])
        for item in company["company_like_evidence"]
    ))
    commercial_conflict = bool(contextual["commercial_contradiction_signals"] or contextual["broker_self_declarations"])
    contradictions: list[dict[str, Any]] = []
    if owner_evidence and (strong_broker_identity or probable_broker or commercial_conflict or confirmed_company_identity):
        contradictions.append({
            "rule_id": "CONFLICT_OWNER_AND_BROKER_SIGNALS",
            "reason": "Coexisten una declaración/atributo de propietario y señales profesionales, comerciales o de identidad empresarial.",
            "owner_evidence_ids": [item["evidence_id"] for item in owner_evidence],
            "broker_rule_ids": list(dict.fromkeys(broker_rule_ids)) or ["COMMERCIAL_TEXT_SIGNAL"],
        })
    if contextual["owner_third_party_mentions"]:
        contradictions.append({
            "rule_id": "OWNER_MENTION_IS_THIRD_PARTY",
            "reason": "El texto menciona a un propietario tercero; no se atribuye esa identidad al anunciante.",
            "evidence_ids": [f"{item['rule_id']}:{item['field']}:{item['position_start']}" for item in contextual["owner_third_party_mentions"]],
        })
    seller_contact_mismatch = _explicit_seller_contact_mismatch(extracted)
    if owner_evidence and seller_contact_mismatch:
        contradictions.append({
            "rule_id": "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH",
            "reason": "La declaración de venta propia nombra un contacto distinto del publicador; requiere verificar si es representante o tercero.",
            "publisher": seller_contact_mismatch["publisher"],
            "named_contact": seller_contact_mismatch["named_contact"],
            "evidence": seller_contact_mismatch["fragment"],
        })

    if strong_broker_identity:
        state = "CORREDOR_SEGURO"
        reason = broker_identity.get("reason") if broker_identity else "La descripción identifica al anunciante con una firma y servicios inmobiliarios de primera parte."
        source = broker_identity.get("source") if broker_identity else "description_business_signature"
    elif owner_evidence and probable_broker:
        state = "INCIERTO"
        reason = "La declaración o atributo de propietario coexiste con una señal profesional sin corroboración independiente; requiere revisión."
        source = "contradictory_context"
    elif probable_broker and not strong_owner:
        state = "CORREDOR_PROBABLE"
        reason = broker_identity.get("reason") if broker_identity else "Hay señales comerciales suficientes para marcar al anunciante como corredor probable."
        source = broker_identity.get("source") if broker_identity else (
            "unknown_business_context" if contextual["unknown_business_identity_evidence"] else "contextual_rules"
        )
    elif strong_owner and not (
        strong_broker_identity or probable_broker or commercial_conflict
        or confirmed_company_identity or contextual["owner_third_party_mentions"]
        or seller_contact_mismatch
    ):
        state = "DUEÑO_PROBABLE"
        reason = "Una declaración atribuible indica que vende su propia propiedad; no verifica titularidad legal."
        source = "owner_declaration_grammar"
    elif private_party_declaration and not (strong_broker_identity or probable_broker or commercial_conflict or confirmed_company_identity):
        state = "DUEÑO_PROBABLE"
        reason = "El anunciante se identifica como particular; es un indicio de venta privada, no acredita titularidad legal."
        source = "private_party_declaration"
    elif owner_evidence and (probable_broker or confirmed_company_identity):
        state = "INCIERTO"
        reason = "La declaración de propietario coexiste con una señal profesional o comercial no corroborada; requiere revisión."
        source = "contradictory_context"
    else:
        state = "INCIERTO"
        reason = "No hay evidencia suficiente para atribuir esta publicación a un propietario o corredor."
        source = "contextual_rules"

    missing_information = []
    if not any(str(extracted.get(key) or "").strip() for key in ("publicador_visible", "contact_name", "seller_jsonld_name")):
        missing_information.append("nombre_publicador")
    if not str(extracted.get("seller_profile_id") or "").strip():
        missing_information.append("id_perfil_yapo")
    if not str(extracted.get("seller_type") or extracted.get("listing_advertiser_type") or "").strip():
        missing_information.append("tipo_de_anunciante_y_perfil")
    if extracted.get("html_source_missing"):
        missing_information.append("html_local_no_disponible")

    requires_review = bool(
        contradictions or contextual["family_or_representative_signals"]
        or missing_information or state == "INCIERTO"
    )
    review_reasons = []
    if contradictions:
        review_reasons.extend(item["reason"] for item in contradictions)
    if contextual["family_or_representative_signals"]:
        review_reasons.append("Se detectaron herencia, copropiedad o representación familiar; no acredita titularidad del anunciante.")
    if missing_information:
        review_reasons.append("Falta información del publicador: " + ", ".join(missing_information) + ".")
    if state == "INCIERTO" and not review_reasons:
        review_reasons.append("Revisión manual: no se detectó una señal concluyente de propietario o corredor.")

    if strong_broker_identity or (
        strong_owner and not contextual["owner_third_party_mentions"] and not seller_contact_mismatch
    ):
        evidence_strength = "ALTA"
    elif probable_broker or owner_evidence or contextual["family_or_representative_signals"]:
        evidence_strength = "MEDIA"
    elif contextual["owner_support_signals"] or company["company_like_suspected"]:
        evidence_strength = "BAJA"
    else:
        evidence_strength = "INSUFICIENTE"

    all_evidence = [
        f"{item['rule_id']}:{item['field']}:{item['fragment']}" for item in owner_evidence
    ] + [str(item.get("fragment") or "") for item in broker_evidence]
    all_evidence += [f"owner_support:{item['rule_id']}:{item['fragment']}" for item in contextual["owner_support_signals"]]
    rule_ids = list(dict.fromkeys(
        [item["rule_id"] for item in owner_evidence]
        + broker_rule_ids
        + [item["rule_id"] for item in contextual["owner_support_signals"]]
        + [item["rule_id"] for item in contextual["owner_negations"]]
        + [item["rule_id"] for item in contextual["owner_third_party_mentions"]]
        + [item["rule_id"] for item in contextual["family_or_representative_signals"]]
        + [item["rule_id"] for item in contradictions]
    ))
    return {
        **(broker_identity or {}),
        "state": state,
        "classification_state": state,
        "confidence": None,
        "confidence_type": "not_calibrated_probability",
        "evidence_strength": evidence_strength,
        "reason": reason,
        "classification_reason": reason,
        "source": source,
        "evidence": list(dict.fromkeys(item for item in all_evidence if item)),
        "owner_evidence": owner_evidence,
        "broker_evidence": broker_evidence,
        "contradictions": contradictions,
        "missing_information": missing_information,
        "requires_review": requires_review,
        "review_reason": " ".join(dict.fromkeys(review_reasons)),
        "classification_rule_ids": rule_ids,
        "owner_signal_candidates": contextual["owner_signal_candidates"],
        "owner_signal_evidence": owner_evidence,
        "owner_classification_confirmed": False,
        "family_or_representative_signals": contextual["family_or_representative_signals"],
        "direct_deal_with_commercial_contradictions": bool(
            contradictions or contextual["direct_deal_with_commercial_contradictions"]
        ),
        "company_like_suspected": company["company_like_suspected"],
        "company_like_evidence": company["company_like_evidence"],
        "contextual_analysis": contextual,
    }
