"""Versioned classification cache shared by portal pipelines.

The cache is keyed by the complete classification input, not by listing id.
It can use MongoDB in production and a local JSON file for the local scraper;
tests can use the in-memory store without touching either system.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLASSIFICATION_CACHE_COLLECTION = "classification_cache"
CLASSIFICATION_CACHE_VERSION = "classification-cache-v1"
CLASSIFICATION_SERVICE_VERSION = "classification-service-v1"


def normalize_full_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _first(document: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = document.get(key)
        if value not in (None, "", [], {}):
            return value
    for container_name in ("details", "canonical_identity", "source_signals"):
        container = document.get(container_name)
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], {}):
                return value
    return ""


def _version(document: dict[str, Any], key: str, default: str) -> str:
    classification = document.get("classification") or {}
    metadata = document.get("source_metadata") or {}
    return str(
        document.get(key)
        or classification.get(key)
        or metadata.get(key)
        or default
    )


def classification_fingerprint(
    document: dict[str, Any],
    *,
    extractor_version: str | None = None,
    rules_version: str | None = None,
    broker_registry_version: str | None = None,
    classifier_version: str | None = None,
) -> str:
    """Hash all relevant identity/text/version inputs deterministically."""
    try:
        from broker_registry import BROKER_REGISTRY_VERSION
    except Exception:
        BROKER_REGISTRY_VERSION = "broker-registry-v1"

    identity = document.get("canonical_identity") or {}
    payload = {
        "portal": _first(document, "portal", "source_portal", "origen") or identity.get("portal", ""),
        "listing_id": str(_first(document, "listing_id", "publication_id", "codigo") or identity.get("listing_id", "")),
        "publisher_normalized": normalize_full_text(
            _first(document, "publisher_normalized", "publicador_visible", "publisher", "seller_name")
            or identity.get("publisher_normalized", "")
        ).casefold(),
        "seller_profile_id": normalize_full_text(
            _first(document, "seller_profile_id", "profile_id") or identity.get("seller_profile_id", "")
        ).casefold(),
        "seller_client_id": normalize_full_text(
            _first(document, "seller_client_id", "client_id") or identity.get("seller_client_id", "")
        ).casefold(),
        "phone": normalize_full_text(
            _first(document, "phone_normalized", "telefono_normalizado", "phone", "telefono")
            or identity.get("phone", "")
        ),
        "email": normalize_full_text(
            _first(document, "email", "email_contact", "contact_email") or identity.get("email", "")
        ).casefold(),
        "domain": normalize_full_text(
            _first(document, "domain", "seller_domain", "website", "seller_website")
            or identity.get("domain", "")
        ).casefold(),
        "title": normalize_full_text(_first(document, "title", "titulo") or identity.get("title", "")),
        # Deliberately keep the complete normalized description. Truncation is
        # an LLM transport concern and must not create a false cache hit.
        "description": normalize_full_text(
            _first(document, "description", "descripcion") or identity.get("description", "")
        ),
        "operation": normalize_full_text(
            _first(document, "operation", "operation_label_raw", "operacion") or identity.get("operation", "")
        ).casefold(),
        "extractor_version": extractor_version or _version(document, "extractor_version", "toctoc-extractor-v3"),
        "rules_version": rules_version or _version(document, "rules_version", "rules-v1"),
        "broker_registry_version": broker_registry_version or BROKER_REGISTRY_VERSION,
        "classifier_version": classifier_version or _version(document, "classifier_version", CLASSIFICATION_SERVICE_VERSION),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClassificationCache:
    """Best-effort persistent cache with a deterministic in-memory fallback."""

    def __init__(
        self,
        *,
        db: Any | None = None,
        path: str | os.PathLike[str] | None = None,
        collection_name: str = CLASSIFICATION_CACHE_COLLECTION,
    ) -> None:
        self.db = db
        self.collection_name = collection_name
        self.path = Path(path) if path else None
        self._memory: dict[str, dict[str, Any]] = {}
        self._file_loaded = False
        self._lock = threading.RLock()

    def _load_file(self) -> None:
        if self._file_loaded or self.path is None:
            return
        self._file_loaded = True
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._memory.update({str(k): v for k, v in raw.items() if isinstance(v, dict)})
        except Exception:
            # A corrupt local cache is a miss, never a reason to classify as
            # owner or to stop an otherwise healthy scrape.
            self._memory = {}

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            self._load_file()
            if self.db is not None:
                try:
                    row = self.db[self.collection_name].find_one({"fingerprint": fingerprint})
                    if row:
                        return dict(row)
                except Exception:
                    pass
            row = self._memory.get(str(fingerprint))
            return dict(row) if row else None

    def put(
        self,
        fingerprint: str,
        classification: dict[str, Any],
        *,
        input_payload: dict[str, Any] | None = None,
        source: str = "classification_service",
    ) -> dict[str, Any]:
        now = _utcnow()
        row = {
            "fingerprint": str(fingerprint),
            "classification": dict(classification),
            "final": classification.get("final"),
            "final_state": classification.get("final_state") or classification.get("state"),
            "reason": classification.get("final_reason") or classification.get("reason", ""),
            "evidence": list(classification.get("final_evidence") or classification.get("evidence") or []),
            "source": source,
            "cache_version": CLASSIFICATION_CACHE_VERSION,
            "classifier_version": classification.get("classifier_version") or CLASSIFICATION_SERVICE_VERSION,
            "created_at": now,
            "last_used_at": now,
        }
        if input_payload is not None:
            row["input_hash"] = hashlib.sha256(
                json.dumps(input_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
        with self._lock:
            self._load_file()
            self._memory[str(fingerprint)] = row
            if self.db is not None:
                try:
                    update_row = dict(row)
                    update_row.pop("created_at", None)
                    self.db[self.collection_name].update_one(
                        {"fingerprint": str(fingerprint)},
                        {"$set": update_row, "$setOnInsert": {"created_at": now}},
                        upsert=True,
                    )
                except Exception:
                    pass
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temp = self.path.with_suffix(self.path.suffix + ".tmp")
                    temp.write_text(json.dumps(self._memory, ensure_ascii=False, default=str), encoding="utf-8")
                    temp.replace(self.path)
                except Exception:
                    pass
        return row

    def touch(self, fingerprint: str) -> None:
        row = self.get(fingerprint)
        if row:
            row["last_used_at"] = _utcnow()
            with self._lock:
                self._memory[str(fingerprint)] = row
