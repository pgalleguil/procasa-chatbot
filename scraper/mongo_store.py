from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import AppConfig
try:
    from owner_scoring import calculate_owner_score
except ImportError:  # direct execution from scraper/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from owner_scoring import calculate_owner_score

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
        classification = dict(payload.get("classification") or {})
        classification.update(calculate_owner_score(payload))
        payload["classification"] = classification
        return payload

    def upsert_listing(self, record: dict[str, Any]) -> dict[str, Any]:
        collection = self.collection()
        url = record.get("url") or record.get("source_url")
        if not url:
            raise ValueError("El registro no contiene URL.")
        payload = self._sanitize_payload(record)
        listing_id = record.get("listing_id")
        query = {"listing_id": listing_id} if listing_id else {"url": url}
        result = collection.update_one(query, {"$set": payload}, upsert=True)
        return {
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
            "upserted_id": result.upserted_id,
        }

    def existing_listing_ids(self, listing_ids: list[str]) -> set[str]:
        clean = [str(value) for value in listing_ids if str(value).strip()]
        if not clean:
            return set()
        return {
            str(doc.get("listing_id"))
            for doc in self.collection().find({"listing_id": {"$in": clean}}, {"listing_id": 1, "_id": 0})
        }

    def write_many(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for record in records:
            results.append(self.upsert_listing(record))
        return results
