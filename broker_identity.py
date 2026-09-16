"""Shared deterministic broker identity detection.

This module is deliberately limited to publisher/profile identity fields.  It
does not inspect the property description, title, or a person's name alone;
those signals belong to the portal classifier and must not become an
automatic broker veto here.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


PUBLISHER_IDENTITY_FIELDS = (
    "publicador_visible",
    "contact_name",
    "contact_logo_alt",
    "seller_jsonld_name",
    "listing_advertiser",
    "company_name",
    "broker_brand",
    "seller_profile_logo",
    "seller_type_evidence",
)

PROFILE_FIELDS = (
    "seller_profile_url",
    "seller_url",
    "profile_url",
    "seller_profile_id",
    "seller_profile_logo",
)

# A brand is hard evidence only when it appears in an identity/profile field.
# The values include the publishers found in the approved audit and the
# existing rule-set brands.  Matching is accent/case/punctuation insensitive.
KNOWN_BROKER_BRANDS = frozenset({
    "century 21",
    "century21 conecta",
    "remax",
    "re/max",
    "coldwell banker",
    "colliers",
    "cbre",
    "keller williams",
    "engel & völkers",
    "engel & volkers",
    "sotheby's",
    "era inmobiliaria",
    "inmobiliaria",
    "pro urbe",
    "proube",
    "kutt property",
    "grupo premium",
    "magnolia property",
    "property partners",
    "alejandro jaime realty corp",
    "dataprop.cl",
})

# These terms are intentionally commercial/real-estate-specific.  A generic
# name such as "Juan Pérez" does not match any of them.
BROKER_IDENTITY_TERMS = (
    "inmobiliaria",
    "inmobiliarias",
    "corredora",
    "corredoras",
    "corredor de propiedades",
    "corredores",
    "corretaje",
    "broker",
    "real estate",
    "realty",
    "property",
    "properties",
    "propiedades",
    "agente inmobiliario",
    "agentes inmobiliarios",
    "asesoria inmobiliaria",
    "asesoria en propiedades",
    "gestion inmobiliaria",
)

LEGAL_ENTITY_TERMS = (
    " ltda",
    " limitada",
    " eirl",
    " spa",
    " s a",
    " corp",
    " corporation",
    " incorporated",
    " inc",
)

BROKER_SELLER_TYPES = frozenset({
    "BROKER", "CORREDOR", "CORREDORA", "INMOBILIARIA", "CORRETAJE",
})


def normalize_identity_text(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "")).lower()
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.replace("/", " ").replace("-", " ").replace("_", " ")
    return re.sub(r"[^a-z0-9+ ]+", " ", raw).strip()


def _values(doc: dict[str, Any], extracted: dict[str, Any], fields: Iterable[str]):
    for field in fields:
        value = extracted.get(field)
        if value in (None, "", [], {}):
            value = doc.get(field)
        if value in (None, "", [], {}):
            for container_name in ("details", "source_signals", "source_signal_snapshot"):
                container = doc.get(container_name)
                if isinstance(container, dict) and container.get(field) not in (None, "", [], {}):
                    value = container[field]
                    break
        if isinstance(value, (list, tuple, set)):
            value = " ".join(str(item) for item in value)
        if isinstance(value, dict):
            value = " ".join(f"{key}={item}" for key, item in value.items())
        if str(value or "").strip():
            yield field, str(value).strip()


def detect_hard_broker_signal(
    doc: dict[str, Any] | None = None,
    extracted: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return deterministic publisher evidence or ``None``.

    The function is conservative by design: names of natural persons do not
    match unless another explicit commercial/profile signal is present.
    """
    doc = doc or {}
    extracted = extracted or {}

    for field, raw_value in _values(doc, extracted, PUBLISHER_IDENTITY_FIELDS):
        text = normalize_identity_text(raw_value)
        if not text:
            continue
        compact = text.replace(" ", "")
        if field == "seller_type_evidence":
            # A profile path under the portal's inmobiliaria section is an
            # explicit profile classification, not a personal-name guess.
            if (
                "/inmobiliarias/" in raw_value.lower()
                or "/corredora/" in raw_value.lower()
                or re.search(r"\bcorredor(?:a|es)?\b", text)
            ):
                return {
                    "source_field": field,
                    "value": raw_value,
                    "reason_code": "BROKER_PROFILE_PATH",
                    "evidence": f"{field}={raw_value}",
                }
            continue

        if field == "seller_type" and text.upper() in BROKER_SELLER_TYPES:
            return {
                "source_field": field,
                "value": raw_value,
                "reason_code": "BROKER_SELLER_TYPE",
                "evidence": f"{field}={raw_value}",
            }

        brand_hit = next(
            (brand for brand in KNOWN_BROKER_BRANDS
             if normalize_identity_text(brand).replace(" ", "") in compact),
            None,
        )
        if brand_hit:
            return {
                "source_field": field,
                "value": raw_value,
                "reason_code": "KNOWN_BROKER_BRAND",
                "evidence": f"{field}={raw_value}; brand={brand_hit}",
            }

        term_hit = next(
            (term for term in BROKER_IDENTITY_TERMS
             if normalize_identity_text(term) in text),
            None,
        )
        if term_hit:
            return {
                "source_field": field,
                "value": raw_value,
                "reason_code": "COMMERCIAL_BROKER_TERM",
                "evidence": f"{field}={raw_value}; term={term_hit}",
            }

        # Legal suffixes are meaningful only in an identity field and are
        # bounded to avoid matching a random description fragment.
        legal_hit = next(
            (term.strip() for term in LEGAL_ENTITY_TERMS if term in f" {text}"),
            None,
        )
        if legal_hit:
            return {
                "source_field": field,
                "value": raw_value,
                "reason_code": "COMMERCIAL_LEGAL_ENTITY",
                "evidence": f"{field}={raw_value}; legal_term={legal_hit}",
            }

    for field, raw_value in _values(doc, extracted, PROFILE_FIELDS):
        text = normalize_identity_text(raw_value)
        if (
            "/inmobiliarias/" in raw_value.lower()
            or "/corredora/" in raw_value.lower()
            or re.search(r"\bcorredor(?:a|es)?\b", text)
        ):
            return {
                "source_field": field,
                "value": raw_value,
                "reason_code": "BROKER_PROFILE_PATH",
                "evidence": f"{field}={raw_value}",
            }

    seller_type = normalize_identity_text(
        extracted.get("seller_type") or doc.get("seller_type") or ""
    )
    seller_type_source = normalize_identity_text(
        extracted.get("seller_type_source") or doc.get("seller_type_source") or ""
    )
    if seller_type in {normalize_identity_text(item) for item in BROKER_SELLER_TYPES}:
        return {
            "source_field": "seller_type",
            "value": extracted.get("seller_type") or doc.get("seller_type"),
            "reason_code": "BROKER_SELLER_TYPE",
            "evidence": f"seller_type={extracted.get('seller_type') or doc.get('seller_type')}",
        }
    if seller_type == "empresa" and (
        "inmobiliaria" in seller_type_source or "corredor" in seller_type_source
    ):
        return {
            "source_field": "seller_type",
            "value": extracted.get("seller_type") or doc.get("seller_type"),
            "reason_code": "BROKER_COMPANY_PROFILE",
            "evidence": f"seller_type={extracted.get('seller_type') or doc.get('seller_type')}; source={seller_type_source}",
        }
    return None
