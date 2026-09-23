from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import AppConfig
from crm_schema import build_crm_document
try:
    from broker_registry import learn_broker_identity, resolve_broker_identity
except ImportError:  # ejecución directa desde scrapers/scraper_toctoc/
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from broker_registry import learn_broker_identity, resolve_broker_identity
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


def _close_active_assignment_cycles_for_broker(db, property_id, *, reason="broker_veto") -> int:
    """Keep the local scraper independent from the server-only modules."""
    property_key = str(property_id or "").strip()
    if not property_key:
        return 0
    result = db["captacion_assignment_cycles"].update_many(
        {"property_id": property_key, "status": "active"},
        {"$set": {
            "status": "closed",
            "closed_at": datetime.utcnow(),
            "closed_reason": str(reason),
            "updated_at": datetime.utcnow(),
        }},
    )
    return int(getattr(result, "modified_count", 0) or 0)


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

    canonical_final = str(classification.get("final") or "").upper()
    canonical_versioned = bool(
        canonical_final and classification.get("canonical_classification_version")
    )
    hard_veto = (
        classification.get("hard_veto") == "PROFESSIONAL"
        or classification.get("professional_hard_veto") is True
        or classification.get("hard_broker_veto") is True
        or classification.get("hard_broker_signal") is True
    )

    # assignment_ready is a derived safety flag, never a proxy for source or
    # confidence. A completed clean INCIERTO may be handed to an executive for
    # human validation; pending/failed pipeline items cannot.
    non_assignable_reasons = {
        "CORREDOR_SEGURO": "ASSIGNMENT_READY_INVALID_FOR_CORREDOR_SEGURO",
        "CORREDOR_PROBABLE": "ASSIGNMENT_READY_INVALID_FOR_CORREDOR_PROBABLE",
        "AD_REMOVED": "ASSIGNMENT_READY_INVALID_FOR_AD_REMOVED",
    }
    if classification.get("assignment_ready") is True and state in non_assignable_reasons:
        errors.append(non_assignable_reasons[state])
    if classification.get("assignment_ready") is True and state == "INCIERTO":
        pipeline_state = str(
            classification.get("pipeline_state") or doc.get("pipeline_state") or ""
        ).strip().upper()
        pipeline_complete = (
            classification.get("pipeline_complete") is True
            or doc.get("pipeline_complete") is True
        )
        if not pipeline_complete or pipeline_state in {
            "EXTRACTED_ONLY", "CLASSIFYING", "UNCERTAIN_PENDING_AI",
            "AI_FAILED_RETRYABLE", "EXTRACTOR_DEGRADED", "INVALID",
        }:
            errors.append("ASSIGNMENT_READY_INVALID_FOR_INCOMPLETE_INCIERTO")
    if hard_veto and not state.startswith("CORREDOR"):
        errors.append("PROFESSIONAL_HARD_VETO_STATE_LOST")

    # Rechazar clasificacion solo por URL path
    if source == "url_path_signal":
        errors.append("CLASSIFICATION_FROM_URL_PATH_ONLY")

    # Rechazar sin clasificacion o sin estado
    if not state:
        errors.append("MISSING_CLASSIFICATION_STATE")
    if not source:
        errors.append("MISSING_CLASSIFICATION_SOURCE")

    # Canonical final/state is authoritative for versioned documents. The
    # owner-probability estimate is retained as evidence and must be numeric,
    # but it cannot rewrite a structural/portal canonical decision.
    if owner_probability is not None:
        try:
            probability = float(owner_probability)
            if probability > 1: probability /= 100.0
            if probability < 0.0 or probability > 1.0:
                errors.append("OWNER_PROBABILITY_OUT_OF_RANGE")
            if hard_veto and (canonical_final != "BROKER_CONFIRMED" or state != "CORREDOR_SEGURO"):
                errors.append("PROFESSIONAL_HARD_VETO_STATE_INVALID")
            elif canonical_versioned:
                expected_legacy = {
                    "OWNER_CONFIRMED": "DUEÑO_SEGURO",
                    "OWNER_PROBABLE": "DUEÑO_PROBABLE",
                    "BROKER_CONFIRMED": "CORREDOR_SEGURO",
                    "BROKER_PROBABLE": "CORREDOR_PROBABLE",
                    "UNCERTAIN": "INCIERTO",
                    "OUT_OF_SCOPE_NEW_DEVELOPMENT": "INCIERTO",
                }.get(canonical_final)
                if expected_legacy and state != expected_legacy:
                    errors.append(f"CANONICAL_FINAL_STATE_MISMATCH({canonical_final}!={state})")
                if state in {"DUEÑO_PROBABLE", "DUEÑO_SEGURO"} and not classification.get("assignment_ready"):
                    # It may be blocked for lifecycle/conflict reasons; only
                    # validate that it is not incorrectly exposed as ready.
                    pass
            else:
                expected = expected_state_for_probability(probability)
                if not hard_veto and state != expected:
                    errors.append(f"STATE_DOES_NOT_MATCH_OWNER_PROBABILITY({state}!={expected})")
                if not hard_veto and abs(float(confidence) - probability) > 0.001:
                    errors.append("CANONICAL_CONFIDENCE_DOES_NOT_MATCH_OWNER_PROBABILITY")
                if hard_veto and state != "CORREDOR_SEGURO":
                    errors.append("PROFESSIONAL_HARD_VETO_STATE_INVALID")
        except (TypeError, ValueError):
            errors.append("INVALID_OWNER_PROBABILITY")
    else:
        # A professional hard veto intentionally keeps the classifier's
        # technical confidence (for example 0.95) while the canonical owner
        # probability is unavailable. That confidence is not an owner-band
        # value and must not be rejected as if it were one.
        if not hard_veto:
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
        db = self.connect()[self.config.mongo_db]
        crm_doc = build_crm_document(
            record,
            uf_valor_clp=self.config.uf_valor_clp,
            uf_fecha=self.config.uf_fecha,
        )
        # Resolve the persistent universal registry before owner probability.
        # A known broker must be terminal for classification and must never be
        # allowed to become an owner merely because the local score is high.
        registry_match = resolve_broker_identity(db, crm_doc)
        human_broker_match = False
        try:
            from captacion_contact_identity import get_contact_identity_evidence

            identity_evidence = get_contact_identity_evidence(db, crm_doc) or {}
            human_broker_match = (
                str(identity_evidence.get("status") or "").upper()
                == "CORREDOR_CONFIRMED"
            )
        except Exception:
            # Phone Learning remains an optional enrichment for the local
            # scraper; registry and structural vetoes still work without it.
            human_broker_match = False
        apply_owner_probability_to_document(
            crm_doc,
            registry_match=registry_match,
            human_broker_match=human_broker_match,
        )

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
        classification = crm_doc.get("classification") or {}
        if (
            classification.get("hard_broker_signal")
            or classification.get("hard_veto") == "PROFESSIONAL"
            or classification.get("final") == "BROKER_CONFIRMED"
        ):
            learn_broker_identity(
                db,
                document=crm_doc,
                source="PORTAL_STRUCTURE" if classification.get("hard_broker_signal") else "CLASSIFIER",
                evidence_type="STRUCTURAL_BROKER" if classification.get("hard_broker_signal") else "BROKER_CONFIRMED",
                include_alias=True,
            )
        # A local scrape can reclassify a property that still has a legacy
        # active cycle.  Keep the cycle ledger consistent with the hard broker
        # veto so future CRM views cannot resurrect that assignment.
        if (
            classification.get("hard_broker_signal")
            or classification.get("hard_veto") == "PROFESSIONAL"
            or classification.get("final") == "BROKER_CONFIRMED"
        ):
            stored = collection.find_one(query, {"_id": 1})
            if stored:
                try:
                    from captacion_management import reconcile_broker_assignment

                    reconcile_broker_assignment(
                        db,
                        stored,
                        reason="hard_broker_veto_reprocessed",
                    )
                except Exception:
                    # The local scraper remains usable if the optional CRM
                    # reconciliation module is unavailable; cycle closure is
                    # still fail-safe and retriable.
                    _close_active_assignment_cycles_for_broker(
                        db,
                        stored.get("_id"),
                        reason="hard_broker_veto_reprocessed",
                    )
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
