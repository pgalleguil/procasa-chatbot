from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import AppConfig
from crm_schema import build_crm_document
try:
    from owner_probability import apply_owner_probability_to_document
    from owner_probability import expected_state_for_probability
except ImportError:  # direct execution from scraper_toctoc/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from owner_probability import apply_owner_probability_to_document
    from owner_probability import expected_state_for_probability

try:
    from pymongo import MongoClient, errors
except Exception:
    MongoClient = None


def validate_classification_probability_consistency(state, confidence):
    """Valida que estado y confianza canónica sean consistentes.
    The owner_probability bands are the canonical source of truth.
    Retorna lista de errores (vacia = OK)."""
    errors = []
    if not state: return errors
    try: conf = float(confidence)
    except (TypeError, ValueError): return ["INVALID_CONFIDENCE"]
    
    if state == "CORREDOR_SEGURO":
        if conf < 0.00 or conf >= 0.20: errors.append(f"CORREDOR_SEGURO_CONFIDENCE_OUT_OF_RANGE({conf})")
    elif state == "CORREDOR_PROBABLE":
        if conf < 0.20 or conf >= 0.50: errors.append(f"CORREDOR_PROBABLE_CONFIDENCE_OUT_OF_RANGE({conf})")
    elif state == "INCIERTO":
        if conf < 0.50: errors.append(f"INCIERTO_CONFIDENCE_TOO_LOW({conf})")
        if conf >= 0.70: errors.append(f"INCIERTO_CONFIDENCE_TOO_HIGH({conf})")
    elif state == "DUEÑO_PROBABLE":
        if conf < 0.70: errors.append(f"DUEÑO_PROBABLE_CONFIDENCE_TOO_LOW({conf})")
        if conf >= 0.90: errors.append(f"DUEÑO_PROBABLE_CONFIDENCE_TOO_HIGH({conf})")
    elif state == "DUEÑO_SEGURO":
        if conf < 0.90: errors.append(f"DUEÑO_SEGURO_CONFIDENCE_TOO_LOW({conf})")
        if conf > 1.00: errors.append(f"DUEÑO_SEGURO_CONFIDENCE_TOO_HIGH({conf})")
    return errors


def validate_property_for_canonical_insert(doc: dict[str, Any]) -> list[str]:
    """Valida que un documento este listo para insercion canonica en MongoDB.
    Retorna lista de errores (vacia = OK)."""
    errors = []

    # Campos minimos obligatorios
    lid = doc.get("listing_id", "")
    if not lid or not str(lid).isdigit():
        errors.append("MISSING_LISTING_ID")
    if not doc.get("url"):
        errors.append("MISSING_URL")
    if not doc.get("comuna"):
        errors.append("MISSING_COMUNA")
    if not doc.get("operacion"):
        errors.append("MISSING_OPERACION")
    if not doc.get("tipo_propiedad"):
        errors.append("MISSING_TIPO_PROPIEDAD")

    # Contenido minimo
    title = doc.get("title", "")
    desc = doc.get("description", doc.get("descripcion", ""))
    if not title and not desc:
        errors.append("MISSING_TITLE_AND_DESCRIPTION")

    # Clasificacion canonica
    classification = doc.get("classification") or {}
    state = classification.get("state", "")
    confidence = classification.get("confidence", 0)
    owner_probability = classification.get("owner_probability")
    if classification.get("status") in {"PENDING_LLM", "PENDING_SEMANTIC_REVIEW", "SEMANTIC_CLASSIFICATION_FAILED"}:
        errors.append(f"NON_FINAL_CLASSIFICATION_STATUS({classification.get('status')})")
    if doc.get("processing_status") in {"SKIP_PROFESSIONAL", "HISTORICAL_DUPLICATE", "DOWNLOAD_FAILED"}:
        errors.append(f"NON_FINAL_PROCESSING_STATUS({doc.get('processing_status')})")
    source = classification.get("source") or classification.get("decision_source", "")

    # Rechazar clasificacion solo por URL path
    if source == "url_path_signal":
        errors.append("CLASSIFICATION_FROM_URL_PATH_ONLY")

    # Rechazar sin clasificacion o sin estado
    if not state:
        errors.append("MISSING_CLASSIFICATION_STATE")
    if not source:
        errors.append("MISSING_CLASSIFICATION_SOURCE")

    # One source of truth: when owner_probability exists, it defines both the
    # final state and canonical confidence. Legacy documents still use the
    # state/confidence validation below until they are re-normalized.
    if owner_probability is not None:
        try:
            probability = float(owner_probability)
            if probability > 1: probability /= 100.0
            expected = expected_state_for_probability(probability)
            if state != expected:
                errors.append(f"STATE_DOES_NOT_MATCH_OWNER_PROBABILITY({state}!={expected})")
            if abs(float(confidence) - probability) > 0.001:
                errors.append("CANONICAL_CONFIDENCE_DOES_NOT_MATCH_OWNER_PROBABILITY")
        except (TypeError, ValueError):
            errors.append("INVALID_OWNER_PROBABILITY")
    else:
        errors.extend(validate_classification_probability_consistency(state, confidence))

    # Scrape stage valido
    scrape_stage = doc.get("scrape_stage", "")
    if scrape_stage in ("PROCESSING_BLOCKED", "needs_rescrape", "ad_removed", "incomplete"):
        errors.append(f"INVALID_SCRAPE_STAGE({scrape_stage})")
    if scrape_stage == "classified_from_listing":
        errors.append("SCRAPE_STAGE_CLASSIFIED_FROM_LISTING_ONLY")

    return errors


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
        apply_owner_probability_to_document(crm_doc)

        # Guard canonico de insercion
        validation_errors = validate_property_for_canonical_insert(crm_doc)
        if validation_errors:
            raise ValueError(f"CANONICAL_INSERT_VALIDATION_FAILED: {validation_errors}")

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
