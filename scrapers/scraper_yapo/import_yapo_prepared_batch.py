"""Prepare or explicitly import a previously audited Yapo batch.

This command never scrapes Yapo, calls DeepSeek, assigns leads, or runs any
post-scrape/distribution process. Default mode is read-only preflight. Writes
require both --apply and an explicit approval ID and target confirmation.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .mongo_store import MongoStore
from .simulate_captacion_compatibility import _validate

TARGET_DATABASE = "URLS"
TARGET_COLLECTION = "propiedades_captacion"
EXPECTED_STATE_COUNTS = {
    "CORREDOR_SEGURO": 369,
    "CORREDOR_PROBABLE": 11,
    "DUEÑO_PROBABLE": 10,
    "INCIERTO": 92,
}
CRM_PROTECTED_FIELDS = {
    "_id", "created_at", "gestion", "asignacion", "asignaciones", "executive_assignments",
    "assigned_to", "assigned_executive", "historial_gestion", "management_history",
    "captacion_management_events", "captacion_management_date", "manual_review",
    "telefono_normalizado", "phone_identity", "phone_identity_decision", "captacion_priority",
    "captacion_probability_sort", "captacion_comuna_sort", "captacion_price_sort", "captacion_sort_date",
    "notas", "actividades", "fecha_ultima_gestion", "events", "phone_original_value",
    "phone_source", "phone_version", "phone_added_at", "phone_added_by", "phone_normalized",
}
CRM_PREFIXES = ("gestion_", "asignacion_", "executive_", "crm_")
ASSIGNMENT_BLOCKER_MEANINGS = {
    "no_auditable_final_decision": "técnico/evidencia: no existe decisión final auditable",
    "owner_probability_missing": "técnico/elegibilidad: probabilidad de propietario ausente; no se deriva en esta etapa",
    "commercial_identity_or_profile": "comercial: identidad o perfil del anunciante aún no resuelto",
    "hard_broker_publisher_veto": "comercial: evidencia del publicador bloquea tratarlo como propietario",
    "yapo_owner_probability_state_conflict": "técnico/consistencia: conflicto entre estado Yapo y estado del motor compartido",
}
ASSIGNMENT_FIELDS = {
    "assigned_to", "assigned_executive", "asignacion", "asignaciones", "executive_assignments",
    "captacion_assignment", "captacion_assigned_to", "assignment",
}
EMPTY = (None, "", [], {})
URL_FIELDS = ("url", "canonical_url", "source_url")


def _id_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _url_listing_id(value: Any) -> str:
    match = re.search(r"/(\d{6,})(?:/|$|[?#])", str(value or ""))
    return match.group(1) if match else ""


def _is_yapo(doc: dict[str, Any]) -> bool:
    if str(doc.get("origen") or "").casefold() == "yapo" or str(doc.get("source_portal") or "").casefold() == "yapo":
        return True
    return any(MongoStore.normalize_listing_url(str(doc.get(key) or "")) for key in URL_FIELDS)


def _nonempty(value: Any) -> bool:
    return value not in EMPTY


def _mongo_type(value: Any) -> str:
    if value is None: return "null"
    if isinstance(value, bool): return "bool"
    if isinstance(value, int): return "int"
    if isinstance(value, float): return "double"
    if isinstance(value, str): return "string"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    if isinstance(value, datetime): return "date"
    return type(value).__name__


def _types_compatible(left: Any, right: Any) -> bool:
    a, b = _mongo_type(left), _mongo_type(right)
    if a == "null" or b == "null":
        return True
    if a in {"int", "long", "double", "decimal"} and b in {"int", "long", "double", "decimal"}:
        return True
    return a == b


def _load_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("El archivo debe ser una lista o contener records[].")
    docs = []
    for row in records:
        doc = row.get("document") if isinstance(row, dict) else None
        if not isinstance(doc, dict):
            raise ValueError("Cada registro debe contener document canónico.")
        docs.append(doc)
    ids = [_id_text(doc.get("listing_id")) for doc in docs]
    if len(docs) != 482 or len(set(ids)) != 482 or any(not value for value in ids):
        raise ValueError(f"Se requieren 482 documentos e IDs únicos; recibidos {len(docs)}.")
    states = Counter(str((doc.get("classification") or {}).get("state") or "") for doc in docs)
    if dict(states) != EXPECTED_STATE_COUNTS:
        raise ValueError(f"Distribución de estados distinta de la auditoría aprobada: {dict(states)}")
    return docs


def _validate_prepared_doc(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    validation = _validate(doc)
    if not validation.get("structural_valid"):
        errors.append("estructura_invalida")
    if not validation.get("extraction_complete"):
        errors.append("extraccion_incompleta")
    if not validation.get("technical_valid"):
        errors.append("estado_tecnico_no_apto")
    cls = doc.get("classification") or {}
    if cls.get("owner_probability") is not None:
        errors.append("owner_probability_no_debe_inventarse")
    if cls.get("assignment_ready") is True:
        errors.append("asignacion_automatica_debe_permanecer_bloqueada")
    if any(key in doc for key in ASSIGNMENT_FIELDS):
        errors.append("payload_contiene_campo_de_asignacion")
    if str(doc.get("html_validation_status") or "").upper() != "OK":
        errors.append("html_no_validado_ok")
    if doc.get("scrape_stage") != "processed":
        errors.append("scrape_stage_no_procesado")
    return errors


def _mongo_identity_snapshot(collection: Any) -> list[dict[str, Any]]:
    """Read only the identity, human-decision, and CRM-field-presence columns."""
    projection: dict[str, Any] = {
        "listing_id": 1, "url": 1, "canonical_url": 1, "source_url": 1,
        "origen": 1, "source_portal": 1, "source": 1,
        "classification.state": 1, "classification.final_state": 1,
        "classification.source": 1, "classification.confirmed_by": 1,
        "classification.human_confirmed": 1, "classification.confirmed_by_human": 1,
        "classification.manual_confirmation": 1,
    }
    for field in CRM_PROTECTED_FIELDS:
        if field != "_id":
            projection[field] = 1
    return list(collection.find({}, projection))


def _identity_matches(doc: dict[str, Any], mongo_doc: dict[str, Any]) -> dict[str, Any]:
    listing_id = _id_text(doc.get("listing_id"))
    stored_id = _id_text(mongo_doc.get("listing_id"))
    stored_urls = {str(mongo_doc.get(key) or "") for key in URL_FIELDS if mongo_doc.get(key)}
    incoming_urls = {MongoStore.normalize_listing_url(str(doc.get(key) or "")) for key in ("url", "canonical_url") if doc.get(key)}
    incoming_urls.discard("")
    stored_normalized = {MongoStore.normalize_listing_url(value) for value in stored_urls}
    stored_normalized.discard("")
    url_ids = {_url_listing_id(value) for value in stored_urls}
    url_ids.discard("")
    same_id = stored_id == listing_id or listing_id in url_ids
    same_url = bool(incoming_urls & stored_normalized)
    portal_yapo = _is_yapo(mongo_doc)
    internal_ids_conflict = bool(url_ids and any(value != listing_id for value in url_ids)) or bool(stored_id and url_ids and stored_id not in url_ids)
    canonical_match = portal_yapo and (same_id or same_url) and not internal_ids_conflict
    conflict = bool((same_id or same_url) and not canonical_match) or internal_ids_conflict
    reasons = []
    if same_id and not portal_yapo:
        reasons.append("listing_id_coincide_pero_documento_no_es_yapo")
    if same_url and stored_id and stored_id != listing_id:
        reasons.append("url_coincide_con_listing_id_distinto")
    if internal_ids_conflict:
        reasons.append("ids_de_documento_y_url_discrepan")
    return {"same_id": same_id, "same_url": same_url, "is_yapo": portal_yapo,
            "canonical_match": canonical_match, "identity_conflict": conflict, "conflict_reasons": reasons}


def _identity_candidate_map(docs: list[dict[str, Any]], mongo_docs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_url_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in mongo_docs:
        stored_id = _id_text(candidate.get("listing_id"))
        if stored_id:
            by_id[stored_id].append(candidate)
        for key in URL_FIELDS:
            value = str(candidate.get(key) or "")
            if value:
                normalized = MongoStore.normalize_listing_url(value)
                if normalized:
                    by_url[normalized].append(candidate)
                listing_id = _url_listing_id(value)
                if listing_id:
                    by_url_id[listing_id].append(candidate)
    result: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        listing_id = _id_text(doc.get("listing_id"))
        urls = {MongoStore.normalize_listing_url(str(doc.get(key) or "")) for key in ("url", "canonical_url") if doc.get(key)}
        urls.discard("")
        candidates: dict[str, dict[str, Any]] = {}
        for candidate in by_id.get(listing_id, []):
            candidates[str(candidate.get("_id"))] = candidate
        for value in urls:
            for candidate in by_url.get(value, []):
                candidates[str(candidate.get("_id"))] = candidate
        for candidate in by_url_id.get(listing_id, []):
            candidates[str(candidate.get("_id"))] = candidate
        result[listing_id] = list(candidates.values())
    return result


def _is_nonempty_human_classification(store: MongoStore, mongo_doc: dict[str, Any]) -> bool:
    return store._human_confirmed_classification(mongo_doc.get("classification"))


def _crm_nonempty_fields(mongo_doc: dict[str, Any]) -> list[str]:
    fields = []
    for key, value in mongo_doc.items():
        if key in CRM_PROTECTED_FIELDS and key != "_id" and _nonempty(value):
            fields.append(key)
        elif key.startswith(CRM_PREFIXES) and _nonempty(value):
            fields.append(key)
    return sorted(set(fields))


def _safe_update_plan(incoming: dict[str, Any], existing: dict[str, Any], store: MongoStore) -> dict[str, Any]:
    human_classification = _is_nonempty_human_classification(store, existing)
    update_fields: list[str] = []
    preserved_crm: list[str] = []
    preserved_missing: list[str] = []
    type_conflicts: list[dict[str, str]] = []
    for key, value in incoming.items():
        if key == "_id":
            continue
        if key in CRM_PROTECTED_FIELDS or key.startswith(CRM_PREFIXES):
            if key in existing and _nonempty(existing[key]):
                preserved_crm.append(key)
            continue
        if key == "classification" and human_classification:
            preserved_crm.append("classification")
            continue
        if (value is None or value == "" or value == [] or value == {}) and _nonempty(existing.get(key)):
            preserved_missing.append(key)
            continue
        if key in existing and existing[key] is not None and value is not None and not _types_compatible(value, existing[key]):
            type_conflicts.append({"field": key, "incoming_type": _mongo_type(value), "stored_type": _mongo_type(existing[key])})
        update_fields.append(key)
    return {"update_fields": sorted(update_fields), "crm_fields_preserved": sorted(set(preserved_crm)),
            "stored_values_preserved_when_incoming_empty": sorted(set(preserved_missing)),
            "field_type_conflicts": type_conflicts, "human_classification_preserved": human_classification}


def _top_level_type_sets(collection: Any) -> dict[str, set[str]]:
    pipeline = [
        {"$match": {"$or": [{"origen": "yapo"}, {"source_portal": "yapo"},
                              {"url": {"$regex": r"^https?://(?:www\.)?yapo\.cl/", "$options": "i"}}]}},
        {"$project": {"pairs": {"$objectToArray": "$$ROOT"}}},
        {"$unwind": "$pairs"},
        {"$group": {"_id": {"field": "$pairs.k", "type": {"$type": "$pairs.v"}}, "count": {"$sum": 1}}},
    ]
    observed: dict[str, set[str]] = defaultdict(set)
    for row in collection.aggregate(pipeline, allowDiskUse=False, maxTimeMS=60000):
        observed[str(row["_id"]["field"])].add(str(row["_id"]["type"]))
    return observed


def _server_validator(database: Any, collection_name: str) -> dict[str, Any]:
    rows = list(database.list_collections(filter={"name": collection_name}, nameOnly=False))
    if not rows:
        raise RuntimeError(f"La colección operativa no existe: {database.name}.{collection_name}")
    return rows[0].get("options", {}).get("validator") or {}


def _field_schema_conflicts(doc: dict[str, Any], type_sets: dict[str, set[str]]) -> list[dict[str, str]]:
    conflicts = []
    bson = _mongo_type
    for key, value in doc.items():
        if value is None or key not in type_sets:
            continue
        incoming = bson(value)
        observed = type_sets[key]
        if incoming in observed:
            continue
        numeric = {"int", "long", "double", "decimal"}
        if incoming in numeric and observed & numeric:
            continue
        conflicts.append({"field": key, "incoming_type": incoming, "observed_types": ",".join(sorted(observed))})
    return conflicts


def _read_preflight(docs: list[dict[str, Any]], config: AppConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not config.mongo_uri:
        raise RuntimeError("MONGO_URI no configurado.")
    if config.mongo_db != TARGET_DATABASE or config.mongo_collection != TARGET_COLLECTION:
        raise RuntimeError("La configuración debe apuntar a URLS.propiedades_captacion.")
    from pymongo import MongoClient, ReadPreference
    from pymongo.read_concern import ReadConcern
    client = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=20000,
                         read_preference=ReadPreference.SECONDARY_PREFERRED)
    try:
        database = client.get_database(TARGET_DATABASE, read_concern=ReadConcern("majority"),
                                       read_preference=ReadPreference.SECONDARY_PREFERRED)
        collection = database[TARGET_COLLECTION]
        identity_docs = _mongo_identity_snapshot(collection)
        light_candidates = _identity_candidate_map(docs, identity_docs)
        full_by_id: dict[str, dict[str, Any]] = {
            str(candidate.get("_id")): candidate
            for candidates in light_candidates.values() for candidate in candidates
        }
        complete_matches = list(collection.find({"_id": {"$in": [item["_id"] for item in identity_docs if str(item.get("_id")) in full_by_id]}})) if full_by_id else []
        full_map = {str(item.get("_id")): item for item in complete_matches}
        type_sets = _top_level_type_sets(collection)
        validator = _server_validator(database, TARGET_COLLECTION)
        total = collection.count_documents({})
        yapo_count = collection.count_documents({"$or": [{"origen": "yapo"}, {"source_portal": "yapo"}]})
        store = MongoStore(config)
        per_record: list[dict[str, Any]] = []
        hard_storage_errors = 0
        for doc in docs:
            identity_rows = []
            for light in light_candidates.get(_id_text(doc.get("listing_id")), []):
                match = _identity_matches(doc, light)
                if match["same_id"] or match["same_url"]:
                    full = full_map.get(str(light.get("_id")), light)
                    identity_rows.append((full, match))
            canonical = [(row, match) for row, match in identity_rows if match["canonical_match"]]
            conflicts = [(row, match) for row, match in identity_rows if match["identity_conflict"]]
            normalized_urls = {MongoStore.normalize_listing_url(str(doc.get(key) or "")) for key in ("url", "canonical_url") if doc.get(key)}
            incoming_id = _id_text(doc.get("listing_id"))
            property_duplicate_peers = []
            result_validation = _validate(doc)
            errors = _validate_prepared_doc(doc)
            schema_conflicts = _field_schema_conflicts(doc, type_sets)
            if schema_conflicts:
                errors.extend(f"tipo_schema_observado:{item['field']}" for item in schema_conflicts)
            if validator:
                errors.append("server_validator_requires_manual_validation")
            hard_storage_errors += bool(errors)
            human_matches = [str(row.get("_id")) for row, _ in canonical if _is_nonempty_human_classification(store, row)]
            crm_fields = sorted({field for row, _ in canonical for field in _crm_nonempty_fields(row)})
            safe_plans = [_safe_update_plan(doc, row, store) for row, _ in canonical]
            type_conflicts = [conflict for plan in safe_plans for conflict in plan["field_type_conflicts"]]
            if type_conflicts:
                errors.extend(f"tipo_existente_incompatible:{item['field']}" for item in type_conflicts)
                hard_storage_errors += 1
            fields_preserved = sorted({field for plan in safe_plans for field in plan["crm_fields_preserved"]})
            empty_preserved = sorted({field for plan in safe_plans for field in plan["stored_values_preserved_when_incoming_empty"]})
            if conflicts or len(canonical) > 1:
                status = "IDENTITY_CONFLICT"
            elif canonical:
                status = "EXISTS_CANONICAL"
            else:
                status = "WOULD_INSERT"
            per_record.append({
                "listing_id": incoming_id, "classification": (doc.get("classification") or {}).get("state"),
                "identity_status": status, "canonical_matches": len(canonical),
                "conflicting_matches": len(conflicts),
                "mongo_document_ids": [str(row.get("_id")) for row, _ in identity_rows],
                "stored_ids": sorted({_id_text(row.get("listing_id")) for row, _ in identity_rows if row.get("listing_id")}),
                "stored_urls": sorted({str(row.get(key)) for row, _ in identity_rows for key in URL_FIELDS if row.get(key)}),
                "identity_conflict_reasons": sorted({reason for _, match in conflicts for reason in match["conflict_reasons"]}),
                "human_classification_preserved": bool(human_matches), "human_classification_document_ids": human_matches,
                "existing_crm_fields_to_preserve": crm_fields, "explicitly_protected_fields_in_update": fields_preserved,
                "existing_nonempty_values_preserved_from_empty_input": empty_preserved,
                "field_type_incompatibilities": type_conflicts,
                "schema_type_incompatibilities": schema_conflicts,
                "storage_exceptions": sorted(set(errors)),
                "structural_valid": result_validation["structural_valid"],
                "extraction_complete": result_validation["extraction_complete"],
                "technical_valid": result_validation["technical_valid"],
                "assignment_ready": (doc.get("classification") or {}).get("assignment_ready") is True,
                "assignment_block_reasons": (doc.get("classification") or {}).get("assignment_block_reasons") or [],
            })
        schema_summary = {
            "database": database.name, "collection": collection.name,
            "collection_documents": total, "yapo_documents_by_origin_or_portal": yapo_count,
            "server_validator_defined": bool(validator), "server_validator": validator,
            "observed_top_level_bson_types": {key: sorted(values) for key, values in sorted(type_sets.items())},
            "read_only": True, "read_preference": "secondaryPreferred", "read_concern": "majority",
            "mongo_writes": 0,
        }
        return per_record, schema_summary
    finally:
        client.close()


def _build_update(existing: dict[str, Any], payload: dict[str, Any], store: MongoStore) -> tuple[dict[str, Any], list[str]]:
    """Prepare a $set-only update; never clear stored values with empty input."""
    plan = _safe_update_plan(payload, existing, store)
    update = {key: payload[key] for key in plan["update_fields"]}
    return update, plan["stored_values_preserved_when_incoming_empty"]


def _upsert_one(collection: Any, store: MongoStore, record: dict[str, Any]) -> dict[str, Any]:
    # Input is already the sanitized canonical document from the audited
    # simulation. Do not rerun the global probability/eligibility adapter here.
    payload = dict(record)
    if (payload.get("classification") or {}).get("assignment_ready") is True:
        raise RuntimeError("El lote no puede importar registros elegibles para asignación automática.")
    if (payload.get("classification") or {}).get("owner_probability") is not None:
        raise RuntimeError("El lote no puede importar owner_probability inventada o calculada durante la importación.")
    query = store._yapo_identity_query(record)
    existing = collection.find_one(query)
    if existing:
        update, _ = _build_update(existing, payload, store)
        if not update:
            return {"matched_count": 1, "modified_count": 0, "upserted_id": None}
        result = collection.update_one(query, {"$set": update})
    else:
        insert_only = {key: payload[key] for key in (
            "schema_version", "run_id", "batch_id", "source", "source_portal", "origen",
            "listing_id", "url", "canonical_url", "created_at",
        ) if key in payload}
        insert_only.setdefault("created_at", payload.get("created_at") or datetime.now(timezone.utc).isoformat())
        protected = CRM_PROTECTED_FIELDS | {key for key in payload if key.startswith(CRM_PREFIXES)}
        update = {key: value for key, value in payload.items() if key not in protected and key not in insert_only}
        result = collection.update_one(query, {"$set": update, "$setOnInsert": insert_only}, upsert=True)
    return {"matched_count": result.matched_count, "modified_count": result.modified_count,
            "upserted_id": str(result.upserted_id) if result.upserted_id is not None else None}


def run_preflight(input_path: Path, output_dir: Path, *, apply: bool = False,
                  approval_id: str = "", confirm_target: str = "") -> dict[str, Any]:
    docs = _load_records(input_path)
    config = AppConfig()
    per_record, schema = _read_preflight(docs, config)
    identity_counts = Counter(item["identity_status"] for item in per_record)
    exception_records = [item for item in per_record if item["storage_exceptions"] or item["identity_status"] == "IDENTITY_CONFLICT"]
    assignment_ready = sum(item["assignment_ready"] for item in per_record)
    if apply:
        if not approval_id.strip() or confirm_target != f"{TARGET_DATABASE}.{TARGET_COLLECTION}":
            raise RuntimeError("Escritura bloqueada: requiere --approval-id y confirmación exacta del destino.")
        if exception_records:
            raise RuntimeError(f"Escritura bloqueada: {len(exception_records)} excepciones de identidad/esquema/almacenamiento.")
        if assignment_ready:
            raise RuntimeError("Escritura bloqueada: existen documentos autorizados para asignación automática.")
        if config.mongo_db != TARGET_DATABASE or config.mongo_collection != TARGET_COLLECTION:
            raise RuntimeError("Escritura bloqueada: configuración no coincide con el destino aprobado.")
        from pymongo import MongoClient
        client = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=20000)
        try:
            collection = client[TARGET_DATABASE][TARGET_COLLECTION]
            store = MongoStore(config)
            outcomes = [_upsert_one(collection, store, doc) for doc in docs]
        finally:
            client.close()
    else:
        outcomes = []

    duplicate_peer_ids: dict[str, set[str]] = defaultdict(set)
    source_json = json.loads(input_path.read_text(encoding="utf-8-sig"))
    for group in (source_json.get("duplicates", {}).get("possible_same_property_groups", []) if isinstance(source_json, dict) else []):
        ids = [str(value) for value in group.get("listing_ids", [])]
        for left in ids:
            duplicate_peer_ids[left].update(value for value in ids if value != left)
    for item in per_record:
        item["possible_same_property_duplicate_ids"] = sorted(duplicate_peer_ids.get(item["listing_id"], set()))

    owner_blockers = Counter()
    uncertain_blockers = Counter()
    for item in per_record:
        if item["classification"] == "DUEÑO_PROBABLE":
            owner_blockers.update(item["assignment_block_reasons"])
        elif item["classification"] == "INCIERTO":
            uncertain_blockers.update(item["assignment_block_reasons"])

    valid_storage_count = sum(not item["storage_exceptions"] and item["identity_status"] != "IDENTITY_CONFLICT" for item in per_record)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "write_attempt" if apply else "read_only_preimport",
        "target": f"{TARGET_DATABASE}.{TARGET_COLLECTION}",
        "input": str(input_path), "input_count": len(docs),
        "classification_counts": dict(Counter(str((doc.get("classification") or {}).get("state")) for doc in docs)),
        "identity_counts": dict(identity_counts),
        "new_insert_candidates": identity_counts.get("WOULD_INSERT", 0),
        "already_existing_canonical": identity_counts.get("EXISTS_CANONICAL", 0),
        "identity_conflicts": identity_counts.get("IDENTITY_CONFLICT", 0),
        "possible_property_duplicate_pairs": sum(bool(item["possible_same_property_duplicate_ids"]) for item in per_record) // 2,
        "records_with_storage_exceptions": len(exception_records),
        "valid_for_storage": valid_storage_count,
        "potential_captations": sum(item["classification"] in {"DUEÑO_PROBABLE", "INCIERTO"} for item in per_record),
        "automatic_assignment_authorized": assignment_ready,
        "assignment_blockers": {"DUEÑO_PROBABLE": dict(owner_blockers), "INCIERTO": dict(uncertain_blockers)},
        "mongo_schema": schema,
        "crm_preservation": {
            "records_with_existing_crm_fields": sum(bool(item["existing_crm_fields_to_preserve"]) for item in per_record),
            "human_classifications_protected": sum(item["human_classification_preserved"] for item in per_record),
            "protected_field_policy": "CRM fields absent from the incoming payload are preserved; known protected fields are excluded from $set; human-confirmed classification is excluded from $set.",
        },
        "idempotency": {
            "identity_query": "MongoStore._yapo_identity_query (Yapo portal + listing_id; URL fallback)",
            "update_operator_policy": "$set plus $setOnInsert for new documents only; never $unset",
            "empty_input_policy": "Existing nonempty values are retained when incoming values are null/empty.",
            "second_run_behavior": "Same prepared batch resolves to the same 482 identities; fixed batch/timestamps make same-value updates idempotent.",
        },
        "external_effects": {"deepseek_calls": 0, "yapo_requests": 0, "assignment_runs": 0,
                             "distribution_runs": 0, "notifications": 0,
                             "mongo_writes": len(outcomes)},
        "import_route": "scrapers/scraper_yapo/import_yapo_prepared_batch.py",
        "not_run": not apply,
        "records": per_record,
        "write_outcomes": outcomes,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "yapo_maipu_preimport_482.json").write_text(json.dumps(docs, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (output_dir / "yapo_maipu_preimport_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    csv_fields = ["listing_id", "classification", "identity_status", "canonical_matches", "conflicting_matches",
                  "human_classification_preserved", "existing_crm_fields_to_preserve", "explicitly_protected_fields_in_update",
                  "existing_nonempty_values_preserved_from_empty_input", "field_type_incompatibilities",
                  "schema_type_incompatibilities", "possible_same_property_duplicate_ids", "storage_exceptions",
                  "structural_valid", "extraction_complete", "technical_valid", "assignment_ready", "assignment_block_reasons"]
    import csv
    with (output_dir / "yapo_maipu_preimport_482.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for item in per_record:
            writer.writerow({key: json.dumps(item.get(key), ensure_ascii=False) if isinstance(item.get(key), (list, dict)) else item.get(key) for key in csv_fields})
    (output_dir / "yapo_maipu_preimport_audit.md").write_text(_render_summary(report), encoding="utf-8")
    return report


def _render_summary(report: dict[str, Any]) -> str:
    counts = report.get("identity_counts", {})
    blockers = report.get("assignment_blockers", {})
    return "\n".join([
        "# Preimportación controlada Yapo Maipú (482)", "",
        f"- Destino: `{report['target']}`; modo: `{report['mode']}`.",
        f"- Insertarían: **{report['new_insert_candidates']}**; ya existen canónicamente: **{report['already_existing_canonical']}**; conflictos: **{report['identity_conflicts']}**.",
        f"- Posibles captaciones: **{report['potential_captations']}**; corredores excluidos comercialmente: **{report['classification_counts'].get('CORREDOR_SEGURO',0)+report['classification_counts'].get('CORREDOR_PROBABLE',0)}**.",
        f"- Válidos para almacenamiento: **{report['valid_for_storage']}/482**; excepciones: **{report['records_with_storage_exceptions']}**.",
        f"- Asignación automática autorizada: **{report['automatic_assignment_authorized']}**.",
        f"- Bloqueos de dueños probables: `{json.dumps(blockers.get('DUEÑO_PROBABLE',{}),ensure_ascii=False)}`.",
        f"- Bloqueos de inciertos: `{json.dumps(blockers.get('INCIERTO',{}),ensure_ascii=False)}`.",
        "- Significado de bloqueos: " + "; ".join(f"`{key}`: {meaning}" for key, meaning in ASSIGNMENT_BLOCKER_MEANINGS.items()) + ".",
        f"- Tipos de schema de servidor: {'definidos; requiere revisión' if report['mongo_schema'].get('server_validator_defined') else 'sin validator restrictivo'}.",
        f"- Campos CRM existentes detectados: {report['crm_preservation']['records_with_existing_crm_fields']} documentos; clasificaciones humanas a preservar: {report['crm_preservation']['human_classifications_protected']}.",
        "- Idempotencia: identidad Yapo + listing_id, actualizaciones `$set`/`$setOnInsert`, sin `$unset`; se omiten valores entrantes vacíos para no borrar datos.",
        "- DeepSeek, scraping Yapo, asignación, distribución, notificaciones y escrituras Mongo en este preflight: **0**.",
        "", "## Comando propuesto (no ejecutado)", "",
        '```powershell',
        "$env:ENV_FILE='C:\\Users\\pgall\\Desktop\\Python\\ChatBot_v4_Grok\\.env'",
        'python -m scrapers.scraper_yapo.import_yapo_prepared_batch `',
        '  --input "C:\\Users\\pgall\\Documents\\PROCASA\\YapoMaipu\\auditoria_compatibilidad_20261103\\yapo_maipu_482_captacion_simulation.json" `',
        '  --output-dir "C:\\Users\\pgall\\Documents\\PROCASA\\YapoMaipu\\preimportacion_20261103" `',
        '  --apply --approval-id "<folio-aprobado>" --confirm-target URLS.propiedades_captacion',
        '```',
        "", "El script es import-only; no llama a `run_owner_hunt.py` ni a `_run_post_scrape_distribution()`. La ruta queda bloqueada sin aprobación explícita.",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight read-only and approved import for saved Yapo batch")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write only after separate approval")
    parser.add_argument("--approval-id", default="")
    parser.add_argument("--confirm-target", default="")
    args = parser.parse_args()
    report = run_preflight(args.input, args.output_dir, apply=args.apply,
                           approval_id=args.approval_id, confirm_target=args.confirm_target)
    print(json.dumps({key: report[key] for key in (
        "target", "input_count", "identity_counts", "records_with_storage_exceptions",
        "valid_for_storage", "potential_captations", "automatic_assignment_authorized", "external_effects", "not_run",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
