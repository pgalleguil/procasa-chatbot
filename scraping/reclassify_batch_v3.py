# -*- coding: utf-8 -*-
import os
import sys
import json
import asyncio
import logging
from datetime import datetime, timezone
from collections import Counter
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

from scraping_yapo_proxys import classify_seller_state, is_likely_broker

def _safe_str(val, default="N/A") -> str:
    if val is None:
        return default
    return str(val).strip() or default

def _safe_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)

def reconstruct_signals_from_doc(doc: dict) -> dict:
    details = doc.get("details", {}) or {}
    return {
        "seller_name": _safe_str(doc.get("publicador") or details.get("publicador")),
        "description": _safe_str(doc.get("raw_desc") or details.get("raw_desc") or doc.get("descripcion") or details.get("descripcion")),
        "company_name": _safe_str(doc.get("company_name") or details.get("company_name")),
        "broker_brand": _safe_str(doc.get("broker_brand") or details.get("broker_brand")),
        "seller_profile_id": _safe_str(doc.get("seller_profile_id") or details.get("seller_profile_id")),
        "seller_is_pro": _safe_bool(doc.get("seller_is_pro") if doc.get("seller_is_pro") is not None else details.get("seller_is_pro")),
    }
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("reclassify")

def extract_old_classification(doc: dict) -> dict:
    details = doc.get("details", {}) or {}
    
    classification_state = _safe_str(doc.get("classification_state") or details.get("classification_state"))
    es_propietario_directo = _safe_bool(doc.get("es_propietario_directo") if doc.get("es_propietario_directo") is not None else details.get("es_propietario_directo"))
    es_corredor = _safe_bool(doc.get("es_corredor") if doc.get("es_corredor") is not None else details.get("es_corredor"))
    confianza_propietario = doc.get("confianza_propietario") if "confianza_propietario" in doc else details.get("confianza_propietario")
    
    if classification_state == "N/A":
        if es_corredor: classification_state = "CORREDOR_SEGURO"
        elif es_propietario_directo: classification_state = "DUEÑO_SEGURO"
        else: classification_state = "INCIERTO"

    return {
        "classification_state": classification_state,
        "es_propietario_directo": es_propietario_directo,
        "es_corredor": es_corredor,
        "confianza_propietario": confianza_propietario
    }

def get_confianza_from_state(state: str) -> float:
    if state == "CORREDOR_SEGURO":
        return 1.0
    elif state == "DUEÑO_SEGURO":
        return 0.9
    else:
        return 0.5

async def run_reclassification():
    BATCH_SIZE = 500
    COLLECTION_NAME = "yapo_propiedades"
    
    log.info(f"Conectando a MongoDB: {Config.DB_NAME}.{COLLECTION_NAME}")
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db[COLLECTION_NAME]

    transitions = Counter()
    total_processed = 0
    total_changed = 0

    log.info("Iniciando escaneo de colección completa...")
    
    cursor = coll.find({}).batch_size(BATCH_SIZE)
    bulk_updates = []

    async for doc in cursor:
        total_processed += 1
        doc_id = doc["_id"]

        signals = reconstruct_signals_from_doc(doc)
        old_state_data = extract_old_classification(doc)
        
        # IMPORTAMOS LA FUNCION EN VIVO DESDE scraping_yapo_proxys
        recalc = classify_seller_state(**signals)
        new_state = recalc["classification_state"]
        new_confianza = get_confianza_from_state(new_state)

        old_state = old_state_data["classification_state"]

        if old_state != new_state:
            total_changed += 1
            transitions[f"{old_state} → {new_state}"] += 1
            
            backup_data = old_state_data.copy()
            backup_data["fecha_respaldo"] = datetime.now(timezone.utc).isoformat()
            backup_data["motivo_cambio"] = "Reclasificacion masiva v3 (property)"

            update_doc = {
                "$set": {
                    "classification_state": new_state,
                    "es_propietario_directo": recalc["es_propietario_directo"],
                    "es_corredor": recalc["es_corredor"],
                    "es_incierto": recalc["es_incierto"],
                    "confianza_propietario": new_confianza,
                    "score_corredor": recalc["score_corredor"],
                    "score_dueno": recalc["score_dueno"],
                    "motivos_corredor": recalc["motivos_corredor"],
                    "motivos_dueno": recalc["motivos_dueno"],
                    "pre_reclassification_backup": backup_data
                }
            }
            if "details" in doc:
                update_doc["$set"]["details.classification_state"] = new_state
                update_doc["$set"]["details.es_propietario_directo"] = recalc["es_propietario_directo"]
                update_doc["$set"]["details.es_corredor"] = recalc["es_corredor"]
                update_doc["$set"]["details.es_incierto"] = recalc["es_incierto"]
                update_doc["$set"]["details.confianza_propietario"] = new_confianza

            bulk_updates.append(UpdateOne({"_id": doc_id}, update_doc))

        if len(bulk_updates) >= BATCH_SIZE:
            res = await coll.bulk_write(bulk_updates, ordered=False)
            log.info(f"Lote insertado: {res.modified_count} documentos modificados.")
            bulk_updates.clear()

        if total_processed % 1000 == 0:
            log.info(f"Progreso: {total_processed} documentos analizados... (Cambios detectados: {total_changed})")

    if bulk_updates:
        res = await coll.bulk_write(bulk_updates, ordered=False)
        log.info(f"Último lote insertado: {res.modified_count} documentos modificados.")

    log.info("Escaneo finalizado.")
    print("\n" + "="*60)
    print("RESUMEN DE TRANSICIONES (POST-FIX)")
    for trans, count in sorted(transitions.items(), key=lambda x: -x[1]):
        print(f"  {trans:<40} {count:>6}")
    print("="*60)
    client.close()

if __name__ == "__main__":
    asyncio.run(run_reclassification())
