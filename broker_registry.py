"""Persistent, exact-match broker identity registry.

The registry is intentionally independent of any portal classifier.  It stores
only exact identifiers and append-only evidence.  Fuzzy names are never used
as a hard broker match.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse


BROKER_IDENTITIES_COLLECTION = "broker_identities"
BROKER_IDENTITY_KEYS_COLLECTION = "broker_identity_keys"
BROKER_IDENTITY_EVENTS_COLLECTION = "broker_identity_events"
BROKER_REGISTRY_VERSION = "broker-registry-v1"

EXACT_MATCH_PRIORITY = (
    "EXACT_PROFILE_ID",
    "EXACT_CLIENT_ID",
    "EXACT_PHONE",
    "EXACT_EMAIL",
    "EXACT_DOMAIN",
    "HUMAN_CONFIRMED",
    "EXACT_CONFIRMED_ALIAS",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ascii(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char))


def normalize_name(value: Any) -> str:
    text = _ascii(value).lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_publisher(value: Any) -> str:
    return normalize_name(value)


def normalize_phone(value: Any) -> str:
    """Return a stable digits-only phone representation.

    This deliberately does not guess a phone when the input is absent or too
    short.  Existing values beginning with Chile's country code are preserved.
    """
    if isinstance(value, dict):
        value = value.get("normalized") or value.get("raw") or value.get("phone")
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("56") and len(digits) >= 11:
        return digits
    if len(digits) == 9 and digits.startswith("9"):
        return "56" + digits
    if len(digits) == 8:
        return "569" + digits
    return digits if len(digits) >= 9 else ""


def normalize_email(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^mailto:", "", text)
    return text if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text) else ""


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "@" in text and "://" not in text:
        text = text.rsplit("@", 1)[-1]
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_profile_id(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def normalize_client_id(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def normalize_portal(value: Any) -> str:
    return normalize_name(value).replace(" ", "_")


def _first(document: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = document.get(key)
        if value not in (None, "", [], {}):
            return value
    for container_name in (
        "details",
        "source_signals",
        "source_signal_snapshot",
        "canonical_identity",
    ):
        container = document.get(container_name)
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], {}):
                return value
    return ""


def _portal(document: dict[str, Any]) -> str:
    return normalize_portal(_first(document, "portal", "source_portal", "origen"))


def _profile_value(document: dict[str, Any]) -> str:
    return normalize_profile_id(_first(document, "seller_profile_id", "profile_id"))


def _client_value(document: dict[str, Any]) -> str:
    return normalize_client_id(_first(document, "seller_client_id", "client_id"))


def _phone_value(document: dict[str, Any]) -> str:
    return normalize_phone(
        _first(document, "phone_normalized", "phone", "telefono", "phone_original_value", "contact_phone")
    )


def _email_value(document: dict[str, Any]) -> str:
    return normalize_email(_first(document, "email", "email_contact", "contact_email"))


def _domain_value(document: dict[str, Any]) -> str:
    direct = _first(document, "domain", "seller_domain", "website", "seller_website")
    if direct:
        return normalize_domain(direct)
    for key in ("seller_profile_url", "seller_profile_url", "profile_url", "url"):
        domain = normalize_domain(document.get(key))
        if domain and domain not in {"toctoc.com", "yapo.cl", "chilepropiedades.cl"}:
            return domain
    return ""


def exact_identity_keys(
    document: dict[str, Any], *, include_alias: bool = False
) -> list[dict[str, str]]:
    """Build exact keys; portal-scoped IDs never become cross-portal matches."""
    portal = _portal(document)
    keys: list[dict[str, str]] = []

    profile = _profile_value(document)
    if portal and profile:
        keys.append({"key_type": "PORTAL_PROFILE_ID", "key_value": f"{portal}:{profile}"})
    client = _client_value(document)
    if portal and client:
        keys.append({"key_type": "PORTAL_CLIENT_ID", "key_value": f"{portal}:{client}"})
    phone = _phone_value(document)
    if phone:
        keys.append({"key_type": "PHONE", "key_value": phone})
    email = _email_value(document)
    if email:
        keys.append({"key_type": "EMAIL", "key_value": email})
    domain = _domain_value(document)
    if domain:
        keys.append({"key_type": "DOMAIN", "key_value": domain})
    if include_alias:
        alias = normalize_publisher(_first(document, "publicador_visible", "publisher", "seller_name"))
        if alias:
            keys.append({"key_type": "CONFIRMED_ALIAS", "key_value": alias})
    return keys


def canonical_identity_payload(document: dict[str, Any]) -> dict[str, Any]:
    """Return the portal-independent identity fields carried by a listing."""
    return {
        "portal": _portal(document),
        "listing_id": str(_first(document, "listing_id", "publication_id", "codigo") or ""),
        "publisher": str(_first(document, "publicador_visible", "publisher", "seller_name") or ""),
        "publisher_normalized": normalize_publisher(_first(document, "publicador_visible", "publisher", "seller_name")),
        "seller_profile_id": _profile_value(document),
        "seller_client_id": _client_value(document),
        "seller_profile_url": str(_first(document, "seller_profile_url", "profile_url") or ""),
        "seller_profile_logo": str(_first(document, "seller_profile_logo", "profile_logo") or ""),
        "seller_type": str(_first(document, "seller_type") or ""),
        "seller_type_evidence": str(_first(document, "seller_type_evidence") or ""),
        "phone": _phone_value(document),
        "email": _email_value(document),
        "domain": _domain_value(document),
        "title": str(_first(document, "title", "titulo") or ""),
        "description": str(_first(document, "description", "descripcion") or ""),
        "operation": str(_first(document, "operation", "operation_label_raw", "operacion") or ""),
        "structural_signals": dict(document.get("structural_signals") or {}),
    }


def _key_id(key_type: str, key_value: str) -> str:
    return f"{key_type}:{key_value}"


def _event_id(
    *, identity_id: str, document: dict[str, Any], source: str, evidence_type: str
) -> str:
    property_id = str(_first(document, "_id", "listing_id", "url") or "")
    raw = f"{identity_id}|{property_id}|{source}|{evidence_type}"
    return "broker-event:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_broker_registry_indexes(db: Any) -> bool:
    """Create only registry indexes when the registry is first used."""
    try:
        db[BROKER_IDENTITY_KEYS_COLLECTION].create_index(
            [("key_type", 1), ("key_value", 1)], unique=True, name="broker_identity_exact_key"
        )
        db[BROKER_IDENTITY_EVENTS_COLLECTION].create_index(
            "event_id", unique=True, name="broker_identity_event_id"
        )
        db[BROKER_IDENTITIES_COLLECTION].create_index(
            "normalized_name", name="broker_identity_normalized_name"
        )
        return True
    except Exception:
        return False


def _key_rows(db: Any, keys: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    collection = db[BROKER_IDENTITY_KEYS_COLLECTION]
    for key in keys:
        row = collection.find_one({"key_type": key["key_type"], "key_value": key["key_value"]})
        if row:
            rows.append(dict(row))
    return rows


def resolve_broker_identity(
    db: Any, document: dict[str, Any], *, include_alias: bool = True
) -> dict[str, Any]:
    """Resolve by exact keys only and report conflicts instead of guessing."""
    keys = exact_identity_keys(document, include_alias=include_alias)
    if not keys:
        return {"available": True, "matched": False, "match_type": None, "evidence": []}
    try:
        rows = _key_rows(db, keys)
        identity_ids = {str(row.get("identity_id")) for row in rows if row.get("identity_id")}
        if not identity_ids:
            return {"available": True, "matched": False, "match_type": None, "evidence": []}
        if len(identity_ids) > 1:
            return {
                "available": True,
                "matched": False,
                "conflict": True,
                "match_type": "IDENTITY_CONFLICT",
                "identity_ids": sorted(identity_ids),
                "evidence": rows,
            }
        identity_id = next(iter(identity_ids))
        identity = db[BROKER_IDENTITIES_COLLECTION].find_one({"_id": identity_id}) or {}
        matched_types = {str(row.get("key_type") or "") for row in rows}
        priority = {
            "PORTAL_PROFILE_ID": "EXACT_PROFILE_ID",
            "PORTAL_CLIENT_ID": "EXACT_CLIENT_ID",
            "PHONE": "EXACT_PHONE",
            "EMAIL": "EXACT_EMAIL",
            "DOMAIN": "EXACT_DOMAIN",
            "CONFIRMED_ALIAS": "EXACT_CONFIRMED_ALIAS",
        }
        match_type = next(
            (priority[key_type] for key_type in ("PORTAL_PROFILE_ID", "PORTAL_CLIENT_ID", "PHONE", "EMAIL", "DOMAIN", "CONFIRMED_ALIAS") if key_type in matched_types),
            "HUMAN_CONFIRMED",
        )
        return {
            "available": True,
            "matched": True,
            "broker_identity_id": identity_id,
            "match_type": match_type,
            "identity": identity,
            "evidence": rows,
        }
    except Exception as exc:
        return {
            "available": False,
            "matched": False,
            "match_type": "REGISTRY_UNAVAILABLE",
            "error_type": type(exc).__name__,
            "evidence": [],
        }


def learn_broker_identity(
    db: Any,
    *,
    document: dict[str, Any],
    source: str,
    evidence_type: str,
    actor: dict[str, Any] | None = None,
    include_alias: bool = True,
) -> dict[str, Any]:
    """Create/update a broker identity and append evidence idempotently."""
    actor = actor or {}
    keys = exact_identity_keys(document, include_alias=include_alias)
    if not keys:
        return {"status": "NO_EXACT_IDENTIFIER", "matched": False}
    ensure_broker_registry_indexes(db)
    existing = resolve_broker_identity(db, document, include_alias=include_alias)
    if existing.get("conflict"):
        return {"status": "CONFLICT", **existing}
    identity_id = str(existing.get("broker_identity_id") or f"broker:{uuid.uuid4().hex}")
    now = _now()
    canonical = canonical_identity_payload(document)
    evidence_id = _event_id(
        identity_id=identity_id,
        document=document,
        source=source,
        evidence_type=evidence_type,
    )
    evidence = {
        "event_id": evidence_id,
        "type": str(evidence_type).upper(),
        "source": str(source).upper(),
        "value": canonical,
        "property_id": canonical["listing_id"],
        "actor_user_id": str(actor.get("_id") or actor.get("id") or actor.get("actor_user_id") or ""),
        "actor_name": str(actor.get("nombre") or actor.get("name") or actor.get("actor_name_snapshot") or ""),
        "detected_at": now,
        "confidence": 1.0,
        "registry_version": BROKER_REGISTRY_VERSION,
    }
    add_to_set: dict[str, Any] = {
        "confirmation_sources": str(source).upper(),
    }
    if canonical["publisher_normalized"]:
        add_to_set["aliases"] = canonical["publisher_normalized"]
    if canonical["phone"]:
        add_to_set["phones"] = canonical["phone"]
    if canonical["email"]:
        add_to_set["emails"] = canonical["email"]
    if canonical["domain"]:
        add_to_set["domains"] = canonical["domain"]
    if canonical["listing_id"]:
        add_to_set["property_ids"] = canonical["listing_id"]
    profile = {
        "portal": canonical["portal"],
        "profile_id": canonical["seller_profile_id"],
        "client_id": canonical["seller_client_id"],
        "profile_url": canonical["seller_profile_url"],
        "logo_id": canonical["seller_profile_logo"],
    }
    if any(profile.values()):
        add_to_set["portal_profiles"] = profile
    identity_update = {
        "$setOnInsert": {
            "_id": identity_id,
            "entity_type": "BROKER",
            "canonical_name": canonical["publisher"],
            "normalized_name": canonical["publisher_normalized"],
            "first_seen": now,
            "active": True,
            "version": 1,
            "created_by": "broker_registry",
        },
        "$set": {
            "last_seen": now,
            "updated_at": now,
            "last_evidence_type": str(evidence_type).upper(),
            "last_source": str(source).upper(),
        },
        "$addToSet": add_to_set,
    }
    db[BROKER_IDENTITIES_COLLECTION].update_one({"_id": identity_id}, identity_update, upsert=True)
    for key in keys:
        key_doc = {
            "_id": _key_id(key["key_type"], key["key_value"]),
            "key_type": key["key_type"],
            "key_value": key["key_value"],
            "identity_id": identity_id,
            "created_at": now,
            "source": str(source).upper(),
        }
        existing_key = db[BROKER_IDENTITY_KEYS_COLLECTION].find_one(
            {"key_type": key["key_type"], "key_value": key["key_value"]}
        )
        if existing_key and str(existing_key.get("identity_id")) != identity_id:
            return {
                "status": "CONFLICT",
                "matched": False,
                "conflict_key": key,
                "existing_identity_id": str(existing_key.get("identity_id")),
                "broker_identity_id": identity_id,
            }
        db[BROKER_IDENTITY_KEYS_COLLECTION].update_one(
            {"_id": key_doc["_id"]}, {"$setOnInsert": key_doc}, upsert=True
        )
    db[BROKER_IDENTITY_EVENTS_COLLECTION].update_one(
        {"event_id": evidence_id}, {"$setOnInsert": {**evidence, "identity_id": identity_id}}, upsert=True
    )
    return {
        "status": "UPDATED" if existing.get("matched") else "CREATED",
        "matched": True,
        "broker_identity_id": identity_id,
        "keys": keys,
        "event_id": evidence_id,
    }


def learn_broker_identity_from_management(
    db: Any,
    *,
    property_doc: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    """Bridge a human ``Corredor`` outcome into the universal registry."""
    return learn_broker_identity(
        db,
        document=property_doc,
        source="EXECUTIVE",
        evidence_type="HUMAN_CONFIRMED",
        actor=event,
        include_alias=True,
    )
