from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    from .config import AppConfig
except ImportError:  # direct script execution
    from config import AppConfig
try:
    from owner_probability import apply_owner_probability_to_document
except ImportError:  # direct execution from scraper/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from owner_probability import apply_owner_probability_to_document

try:
    from comuna_utils import normalize_commune_slug
except ImportError:  # direct execution from scraper/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from comuna_utils import normalize_commune_slug

try:
    from pymongo import MongoClient  # type: ignore
except Exception:  # pragma: no cover
    MongoClient = None  # type: ignore


@dataclass(slots=True)
class MongoStore:
    config: AppConfig
    client: Any | None = None

    def connect(self) -> Any:
        if self.client is not None:
            return self.client
        if not self.config.mongo_uri:
            raise RuntimeError("MONGO_URI no configurado.")
        if MongoClient is None:
            raise RuntimeError("pymongo no está instalado.")
        self.client = MongoClient(self.config.mongo_uri)
        return self.client

    def collection(self) -> Any:
        client = self.connect()
        return client[self.config.mongo_db][self.config.mongo_collection]

    def publisher_profile_context(self, record: dict[str, Any], window_days: int = 90) -> dict[str, Any]:
        """Correlate a Yapo publisher without treating publication count as guilt."""
        profile_id = str(record.get("seller_profile_id") or "").strip()
        if not profile_id:
            return {"profile_id": "", "window_days": window_days, "linked_publications": 0,
                    "confirmed_broker_count": 0, "commercial_identity_confirmed": False,
                    "evidence": []}
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        docs = list(self.collection().find(
            {"seller_profile_id": profile_id},
            {"listing_id": 1, "url": 1, "processed_at": 1, "created_at": 1,
             "classification.state": 1, "company_name": 1, "broker_brand": 1,
             "publicador_visible": 1, "listing_advertiser": 1},
        ))
        names: set[str] = set()
        distinct: set[str] = set()
        confirmed = 0
        in_window = 0
        for doc in docs:
            distinct.add(str(doc.get("listing_id") or doc.get("url") or doc.get("_id")))
            state = str((doc.get("classification") or {}).get("state") or "")
            if state == "CORREDOR_SEGURO":
                confirmed += 1
            for key in ("company_name", "broker_brand", "publicador_visible", "listing_advertiser"):
                value = str(doc.get(key) or "").strip()
                if value:
                    names.add(value)
            stamp = doc.get("processed_at") or doc.get("created_at")
            try:
                dt = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    in_window += 1
            except (TypeError, ValueError):
                pass
        evidence: list[str] = []
        if confirmed:
            evidence.append(f"same_profile_confirmed_brokers={confirmed}")
        if names:
            evidence.append("commercial_names=" + " | ".join(sorted(names)[:12]))
        if in_window:
            evidence.append(f"publications_in_{window_days}d={in_window}")
        return {
            "profile_id": profile_id, "window_days": window_days,
            "linked_publications": len(docs), "distinct_properties": len(distinct),
            "publications_in_window": in_window, "confirmed_broker_count": confirmed,
            "commercial_names": sorted(names),
            "commercial_identity_confirmed": confirmed > 0 and bool(names),
            "evidence": evidence,
            "rule": "count_alone_never_classifies",
        }

    def _sanitize_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        payload.pop("html", None)
        payload.pop("raw_html", None)
        payload["updated_at"] = record.get("updated_at") or datetime.utcnow().isoformat()
        payload["source"] = "owner_hunt"
        payload["origen"] = "yapo"
        payload["source_portal"] = "yapo"
        payload.setdefault("schema_version", "yapo_propiedades_v1")
        batch_id = str(record.get("batch_id") or record.get("run_id") or self.config.generate_batch_id())
        payload.setdefault("batch_id", batch_id)
        payload.setdefault("run_id", str(record.get("run_id") or batch_id))
        comuna = record.get("comuna") or record.get("details", {}).get("comuna") or ""
        payload["comuna_slug"] = normalize_commune_slug(comuna) or payload.get("comuna_slug") or ""
        # The shared probability engine serves other portals and may derive a
        # different public state. For Yapo, preserve the deterministic state
        # and surface any disagreement as a blocking review flag.
        classification = dict(payload.get("classification") or {})
        yapo_state = str(classification.get("state") or classification.get("final_state") or "").upper()
        classification.setdefault("classifier_version", "yapo-deterministic-v6")
        classification.setdefault("deepseek_status", "disabled")
        classification.setdefault("deepseek_call_executed", False)
        canonical_state = {
            "DUEÑO_SEGURO": "OWNER_CONFIRMED", "DUEÑO_PROBABLE": "OWNER_PROBABLE",
            "CORREDOR_SEGURO": "BROKER_CONFIRMED", "CORREDOR_PROBABLE": "BROKER_PROBABLE",
            "INCIERTO": "UNCERTAIN",
        }.get(yapo_state)
        if canonical_state:
            # Bridge the Yapo enum to the canonical enum consumed by the
            # shared probability engine; do not let it infer a new identity.
            classification.setdefault("final", canonical_state)
            classification.setdefault("identity_classification", canonical_state)
            payload["classification"] = classification
        apply_owner_probability_to_document(payload)
        computed = dict(payload.get("classification") or {})
        computed_state = str(computed.get("state") or computed.get("final_state") or "").upper()
        if yapo_state:
            computed["yapo_classifier_state"] = yapo_state
            computed["probability_derived_state"] = computed_state
            computed["state"] = yapo_state
            computed["final_state"] = yapo_state
            if canonical_state:
                # Older shared probability-engine versions may rewrite the
                # canonical `final` value to UNCERTAIN when owner probability
                # is intentionally absent. Preserve the Yapo state bridge;
                # the missing probability remains a separate assignment gate.
                computed["final"] = canonical_state
                computed["identity_classification"] = canonical_state
            canonical_states = {"OWNER_CONFIRMED", "OWNER_PROBABLE", "BROKER_CONFIRMED", "BROKER_PROBABLE", "UNCERTAIN"}
            derived_state_conflicts = bool(
                computed_state in canonical_states
                and computed_state != canonical_state
                and not (computed_state == "UNCERTAIN" and computed.get("owner_probability") is None)
            )
            if derived_state_conflicts:
                computed["yapo_probability_state_conflict"] = True
                computed["yapo_probability_state_conflict_reason"] = (
                    "El motor global derivó un estado distinto; se conserva la clasificación Yapo para revisión."
                )
                computed["pipeline_complete"] = False
                computed["pipeline_state"] = "CLASSIFICATION_CONFLICT_REVIEW"
            payload["classification"] = computed
        # The owner-probability helper supplies canonical probability fields,
        # but its historical assignment flags predate the global gate where
        # INCIERTO is assignable. Re-derive the persisted readiness fields from
        # the central gate so Yapo cannot overwrite the shared policy with
        # NON_OWNER_STATE_NOT_ASSIGNABLE.
        from captacion_assignment_eligibility import apply_assignment_eligibility_fields
        apply_assignment_eligibility_fields(payload)
        if payload.get("classification", {}).get("yapo_probability_state_conflict"):
            classification = dict(payload["classification"])
            classification["assignment_ready"] = False
            classification["exclude_from_assignment"] = True
            reasons = set(classification.get("assignment_block_reasons") or [])
            reasons.add("yapo_owner_probability_state_conflict")
            classification["assignment_block_reasons"] = sorted(reasons)
            payload["classification"] = classification
        return payload

    @staticmethod
    def _yapo_identity_query(record: dict[str, Any]) -> dict[str, Any]:
        """Match a Yapo listing only when both its ID and portal are proven."""
        url = MongoStore.normalize_listing_url(str(record.get("canonical_url") or record.get("url") or ""))
        if not url:
            raise ValueError("La URL del registro no pertenece a yapo.cl.")
        listing_id = str(record.get("listing_id") or MongoStore._listing_id_from_url(url)).strip()
        portal = {"$or": [
            {"origen": "yapo"}, {"source_portal": "yapo"},
            {"url": {"$regex": r"^https?://(?:www\.)?yapo\.cl/", "$options": "i"}},
            {"canonical_url": {"$regex": r"^https?://(?:www\.)?yapo\.cl/", "$options": "i"}},
        ]}
        if listing_id:
            alternatives: list[Any] = [listing_id]
            if listing_id.isdigit():
                alternatives.append(int(listing_id))
            identity = {"listing_id": {"$in": alternatives}}
        else:
            identity = {"$or": [{"url": url}, {"canonical_url": url}]}
        return {"$and": [identity, portal]}

    @staticmethod
    def _human_confirmed_classification(classification: Any) -> bool:
        if not isinstance(classification, dict):
            return False
        source = str(classification.get("source") or classification.get("confirmed_by") or "").lower()
        return bool(
            classification.get("human_confirmed") is True
            or classification.get("confirmed_by_human") is True
            or classification.get("manual_confirmation") is True
            or source in {"human", "manual", "crm_manual", "human_review"}
        )

    def upsert_listing(self, record: dict[str, Any]) -> dict[str, Any]:
        collection = self.collection()
        url = record.get("url") or record.get("source_url")
        if not url:
            raise ValueError("El registro no contiene URL.")
        payload = self._sanitize_payload(record)
        query = self._yapo_identity_query({**record, "url": url})
        existing = collection.find_one(query, {"_id": 1, "created_at": 1, "classification": 1})
        protected = {
            "_id", "created_at", "gestion", "asignacion", "asignaciones", "executive_assignments",
            "assigned_to", "assigned_executive", "historial_gestion", "management_history",
            "captacion_management_events", "captacion_management_date", "manual_review",
            "telefono_normalizado", "phone_identity",
            "phone_identity_decision", "captacion_priority", "captacion_probability_sort",
            "captacion_comuna_sort", "captacion_price_sort", "captacion_sort_date",
        }
        if existing:
            protected |= {key for key in payload if key.startswith(("gestion_", "asignacion_", "executive_", "crm_"))}
            if self._human_confirmed_classification(existing.get("classification")):
                protected.add("classification")
            update = {key: value for key, value in payload.items() if key not in protected}
            result = collection.update_one(query, {"$set": update})
        else:
            insert_only = {key: value for key, value in payload.items() if key in {
                "schema_version", "run_id", "batch_id", "source", "source_portal", "origen",
                "listing_id", "url", "canonical_url", "created_at",
            }}
            insert_only.setdefault("created_at", payload.get("created_at") or datetime.utcnow().isoformat())
            update = {key: value for key, value in payload.items() if key not in protected and key not in insert_only}
            result = collection.update_one(query, {"$set": update, "$setOnInsert": insert_only}, upsert=True)
        return {
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
            "upserted_id": result.upserted_id,
        }

    def existing_listing_ids(self, listing_ids: list[str]) -> set[str]:
        clean = list(dict.fromkeys(str(value).strip() for value in listing_ids if str(value).strip()))
        if not clean:
            return set()
        return {
            str(doc.get("listing_id"))
            for doc in self.collection().find({"listing_id": {"$in": clean}}, {"listing_id": 1, "_id": 0})
        }

    @staticmethod
    def normalize_listing_url(url: str) -> str:
        value = str(url or "").strip()
        if not value:
            return ""
        parsed = urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"} or (parsed.hostname or "").lower() not in {"www.yapo.cl", "yapo.cl"}:
            return ""
        path = parsed.path.rstrip("/") or "/"
        query = sorted(
            (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"clid", "gclid", "fbclid"}
        )
        return urlunparse(parsed._replace(scheme="https", netloc="www.yapo.cl", path=path, query=urlencode(query), fragment=""))

    @staticmethod
    def _listing_id_from_url(url: str) -> str:
        match = re.search(r"/(\d{6,})(?:/|$)", urlparse(url).path)
        return match.group(1) if match else ""

    def existing_listing_urls(self, listing_urls: list[str]) -> set[str]:
        """Read-only URL match across historical formatting variants.

        Mongo receives selective exact and anchored-path predicates rather than a
        collection scan. Returned values are normalized again before acceptance.
        """
        clean = list(dict.fromkeys(self.normalize_listing_url(value) for value in listing_urls))
        clean = [value for value in clean if value]
        if not clean:
            return set()
        requested_by_id = {
            self._listing_id_from_url(value): value
            for value in clean if self._listing_id_from_url(value)
        }
        exact_candidates: set[str] = set(clean)
        path_patterns = []
        for value in clean:
            parsed = urlparse(value)
            path = parsed.path.rstrip("/") or "/"
            escaped_path = re.escape(path)
            path_patterns.append({"$regex": rf"^https?://(?:www\.)?yapo\.cl{escaped_path}/?(?:\?[^#]*)?$", "$options": "i"})
            listing_id = MongoStore._listing_id_from_url(value)
            if listing_id:
                path_patterns.append({
                    "$regex": rf"^https?://(?:www\.)?yapo\.cl/(?:[^?#]*/)?{re.escape(listing_id)}/?(?:\?[^#]*)?$",
                    "$options": "i",
                })
            for scheme in ("https", "http"):
                for host in ("www.yapo.cl", "yapo.cl"):
                    for slash in ("", "/"):
                        exact_candidates.add(urlunparse(parsed._replace(scheme=scheme, netloc=host, path=path + slash)))
        fields = ("url", "source_url", "canonical_url")
        clauses = []
        for field in fields:
            clauses.append({field: {"$in": sorted(exact_candidates)}})
            clauses.extend({field: pattern} for pattern in path_patterns)
        query = {"$or": clauses}
        projection = {"url": 1, "source_url": 1, "canonical_url": 1, "_id": 0}
        matches: set[str] = set()
        for doc in self.collection().find(query, projection):
            for field in ("url", "source_url", "canonical_url"):
                stored = str(doc.get(field) or "")
                normalized = self.normalize_listing_url(stored)
                stored_id = self._listing_id_from_url(stored)
                if stored_id and stored_id in requested_by_id:
                    matches.add(requested_by_id[stored_id])
                if normalized in clean:
                    requested_id = self._listing_id_from_url(normalized)
                    if not requested_id or not stored_id or requested_id == stored_id:
                        matches.add(normalized)
        return matches

    def write_many(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for record in records:
            results.append(self.upsert_listing(record))
        return results
