from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import AppConfig
from crm_schema import build_crm_document
try:
    from owner_scoring import calculate_owner_score
except ImportError:  # direct execution from scraper_toctoc/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from owner_scoring import calculate_owner_score

try:
    from pymongo import MongoClient, errors
except Exception:
    MongoClient = None


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
            raise RuntimeError("pymongo no esta instalado.")
        self.client = MongoClient(
            self.config.mongo_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
        self.client.admin.command("ping")
        return self.client

    def collection(self) -> Any:
        client = self.connect()
        return client[self.config.mongo_db][self.config.mongo_collection]

    def ensure_index(self) -> None:
        col = self.collection()
        existing = [idx["name"] for idx in col.list_indexes()]
        if "origen_1_listing_id_1" not in existing:
            try:
                col.create_index(
                    [("origen", 1), ("listing_id", 1)],
                    unique=True,
                    background=True,
                )
                print("  Created unique index: origen_1_listing_id_1")
            except Exception as e:
                print(f"  Index creation: {e}")

    def upsert_listing(self, record: dict[str, Any]) -> dict[str, Any]:
        collection = self.collection()
        crm_doc = build_crm_document(
            record,
            uf_valor_clp=self.config.uf_valor_clp,
            uf_fecha=self.config.uf_fecha,
        )
        classification = dict(crm_doc.get("classification") or {})
        classification.update(calculate_owner_score(crm_doc))
        crm_doc["classification"] = classification
        listing_id = crm_doc.get("listing_id", "")
        origen = crm_doc.get("origen", crm_doc.get("source_portal", "toctoc"))
        if not listing_id:
            raise ValueError("listing_id vacio, no se puede hacer upsert.")
        if crm_doc.get("origen") != crm_doc.get("source_portal"):
            raise ValueError(f"origen ({crm_doc.get('origen')}) != source_portal ({crm_doc.get('source_portal')})")
        query = {"origen": origen, "listing_id": listing_id}
        result = collection.update_one(query, {"$set": crm_doc}, upsert=True)
        return {
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
            "upserted_id": str(result.upserted_id) if result.upserted_id else None,
        }

    def write_many(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for record in records:
            try:
                results.append(self.upsert_listing(record))
            except Exception as e:
                results.append({"error": str(e), "url": record.get("url", "")})
        return results

    def read_back(self, listing_ids: list[str]) -> list[dict[str, Any]]:
        col = self.collection()
        cursor = col.find(
            {"origen": "toctoc", "listing_id": {"$in": listing_ids}},
            {"_id": 0, "updated_at": 0, "processed_at": 0},
        )
        return list(cursor)
