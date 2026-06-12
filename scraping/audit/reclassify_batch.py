# -*- coding: utf-8 -*-
"""
reclassify_batch.py
===================
Script masivo para reclasificar propiedades en Yapo según la lógica actual.

Requisitos implementados:
1. Recorre la colección en lotes.
2. Modo seguro por defecto (--dry-run). Usa --apply para guardar en BD.
3. Respaldos granulares: Guarda un objeto `pre_reclassification_backup` en cada documento modificado.
4. Genera un changelog JSON y resumen de transiciones al finalizar.

Uso:
  python scraping/reclassify_batch.py             (Modo simulación por defecto)
  python scraping/reclassify_batch.py --apply     (Modo escritura en MongoDB)
"""

import os
import sys
import json
import asyncio
import logging
import re
from datetime import datetime, timezone
from collections import Counter
from unicodedata import normalize
import argparse

# ── Path fix para importar config desde el proyecto raíz ──────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

# Forzar UTF-8 en stdout/stderr para evitar UnicodeEncodeError en consolas Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("reclassify")

# ===========================================================================
# LÓGICA DE CLASIFICACIÓN (COPIA EXACTA DE scraping_yapo_proxys.py)
# ===========================================================================

def normalize_text(text: str, max_chars: int = None) -> str:
    if not text or text == "N/A":
        return "N/A"
    text = normalize('NFKD', text.lower()).encode('ASCII', 'ignore').decode('ASCII')
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_chars] if max_chars else text

_BROKER_KEYWORDS = {
    "remax", "re/max", "re max", "re-max", "century 21", "c21", "engel", "völkers", "volkers",
    "keller williams", "kw chile", "coldwell banker", "sothebys", "realty",
    "betterhomes", "property partners", "zillow", "houm", "isbast", "buydepa",
    "fuenzalida", "ahumada", "valdivieso", "larrain", "mclean", "prevost",
    "quinteros", "skyline", "one propiedades", "maca", "habitab", "grupocasa",
    "buscapro", "copro", "toctoc", "procasa", "mateo sánchez", "marcos sánchez",
    "pablo cassini", "fajre", "besnier", "uribe", "soza", "morandé", "dante",
    "matias ruffat", "vivaqui", "golden", "infofit", "p&g", "pgr", "pro urbe",
    "urzúa", "jaime masmela", "pizarro propiedades", "carreño", "puelma",
    "assetplan", "nexxos", "hyc", "h y c", "arrendo", "arriendo plus",
    "mueve chile", "plusrent", "rentahouse", "findep", "arrendaplus", "alucerto",
    "socovesa", "almagro", "aconcagua", "ingevec", "imagina", "rvc", "salfacorp",
    "sinergia", "ebco", "euroinmobiliaria", "manquehue", "moller", "siena",
    "paz corp", "besalco", "su ksa", "fundamenta", "activa", "armas", "iman",
    "ictinos", "desa", "claro vicuña", "valmar", "enaco", "pocuro", "indesa",
    "colliers", "jll", "cushman", "wakefield", "gps property", "fitzroy",
    "asset", "capital", "management", "investment", "inversión", "inversiones",
    "renta", "patrimonio", "valoriza", "tasaciones", "gestión", "proyectos",
    "cia ltda", "compañia limitada", "sociedad", "spa", "s.a.", "eirl", "asociados",
    "group", "partners", "consulting", "holding", "legal", "estudio", "propiedades",
    "inmobiliaria", "corredora", "corretaje", "broker", "real estate",
    "ejecutivo", "asesor", "habitacional", "comercializadora", "bienes raices",
    "corredor de propiedades", "gestora", "admon", "administración",
    "comisión más iva", "vende inmobiliaria", "arrienda inmobiliaria"
}

_BROKER_ABREVIATIONS = {"sa", "spa", "kw", "c21", "id", "p&g", "pgr", "m2", "sii", "esa", "val"}
_BROKER_REGEXES = [
    re.compile(rf'\b{re.escape(kw)}\b')
    for kw in _BROKER_KEYWORDS
    if len(kw) <= 5 or kw in _BROKER_ABREVIATIONS
]

def is_likely_broker(seller_name: str, description: str, company_name: str = "N/A",
                     seller_profile_id: str = "N/A", seller_is_pro: bool = False) -> bool:
    score = 0
    full_text = f"{seller_name} {company_name} {description}".lower()

    has_strong_base = (
        company_name != "N/A" or
        any(k in full_text for k in ["remax", "century 21", "inmobiliaria", "propiedades", "corretaje"])
    )

    if any(k in full_text for k in [
        "remax", "re/max", "century 21", "c21", "inmobiliaria", "propiedades",
        "ltda", "spa", "eirl", "real estate", "corredora", "corretaje"
    ]):
        score += 3

    if any(k in full_text for k in [
        "comision", "honorarios", "corretaje", "subsidio", "financiamiento",
        "credito hipotecario", "gestion", "evaluacion", "preaprobado"
    ]):
        if has_strong_base or any(k in full_text for k in ["comision", "honorarios", "corretaje"]):
            score += 2

    if any(k in full_text for k in [
        "agenda tu visita", "agendar visita", "plusvalia", "rentabilidad",
        "compra sin pie", "sin pie", "inversionista", "oportunidad inversion"
    ]):
        if has_strong_base or any(k in full_text for k in ["oportunidad inversion", "rentabilidad"]):
            score += 2

    if any(k in full_text for k in ["agente", "asesor", "ejecutivo", "vendedor"]):
        score += 1

    if company_name and company_name != "N/A":
        score += 2

    if score >= 3:
        return True

    s_name = normalize_text(seller_name).lower()
    c_name = normalize_text(company_name).lower()

    for rx in _BROKER_REGEXES:
        if rx.search(f"{s_name} {c_name}"):
            return True

    for kw in _BROKER_KEYWORDS:
        if len(kw) > 5 and kw not in _BROKER_ABREVIATIONS:
            if kw in s_name or kw in c_name:
                return True

    if re.search(r'\by\s+cia\b|\bltda\b|\bs\.a\b|\bspa\b|\beirl\b', s_name):
        return True

    broker_terms = [
        "corretaje", "orden de visita",
        "corredor de propiedades", "gestion de arriendo", "exclusividad",
        "gastos comunes aprox", "metraje aproximado", "agendar visita", "plusvalia"
    ]
    formatted_desc = normalize_text(description).lower()
    for term in broker_terms:
        if term in formatted_desc:
            return True

    if "comision" in formatted_desc and "sin comision" not in formatted_desc and "no comision" not in formatted_desc:
        return True
    if "honorarios" in formatted_desc and "sin honorarios" not in formatted_desc:
        return True

    if s_name in ["agente", "vendedor"]:
        pass

    return False


def classify_seller_state(
    seller_name: str,
    description: str,
    company_name: str = "N/A",
    seller_profile_id: str = "N/A",
    seller_is_pro: bool = False,
    broker_brand: str = "N/A",
    multi_publisher_count: int | None = None,
) -> dict:
    full_text = f"{seller_name} {company_name} {description} {broker_brand}".lower()
    broker_signals = []
    owner_signals = []

    if broker_brand and broker_brand != "N/A":
        broker_signals.append(("broker_brand", 4, "broker_brand detectado"))
    if seller_is_pro:
        broker_signals.append(("seller_is_pro", 3, "badge Profesional detectado"))
    if company_name and company_name != "N/A" and any(k in normalize_text(company_name).lower() for k in [
        "remax", "re/max", "century 21", "c21", "inmobiliaria", "propiedades",
        "corretaje", "ltda", "spa", "eirl", "real estate"
    ]):
        broker_signals.append(("company_name", 3, "nombre corporativo detectable"))
    if any(k in full_text for k in ["contact_logo", "agency_logo"]):
        broker_signals.append(("logo_corporativo", 4, "logo corporativo en full_text"))
    if multi_publisher_count is not None and multi_publisher_count >= 5:
        broker_signals.append(("multi_publicador", 4, f"mismo publicador con {multi_publisher_count} avisos"))
    if is_likely_broker(seller_name, description, company_name, seller_profile_id, seller_is_pro):
        broker_signals.append(("heuristica_broker", 2, "heurística local de corredor"))

    normalized_desc = normalize_text(description).lower()
    if seller_name and seller_name not in ("N/A", "") and not any(k in full_text for k in [
        "remax", "re/max", "century 21", "c21", "inmobiliaria", "propiedades",
        "corretaje", "ltda", "spa", "eirl", "real estate"
    ]):
        owner_signals.append(("nombre_no_corporativo", 1, "nombre no corporativo"))
    if seller_is_pro is False:
        owner_signals.append(("sin_badge_pro", 1, "sin badge Profesional"))
    if broker_brand == "N/A":
        owner_signals.append(("sin_broker_brand", 1, "sin broker_brand"))
    if company_name == "N/A":
        owner_signals.append(("sin_company", 1, "sin company_name"))
    if not any(k in normalized_desc for k in [
        "comision", "honorarios", "corretaje", "subsidio", "financiamiento",
        "inmobiliaria", "propiedades"
    ]):
        owner_signals.append(("sin_lexico_corporativo", 1, "descripción sin léxico corporativo"))
    if "sin comision" in normalized_desc or "sin comisión" in normalized_desc:
        owner_signals.append(("sin_comision", 1, "menciona sin comisión"))
    if "trato directo" in normalized_desc or "dueño" in normalized_desc or "dueno" in normalized_desc:
        owner_signals.append(("trato_directo", 2, "menciona trato directo o dueño"))

    broker_score = sum(x[1] for x in broker_signals)
    owner_score = sum(x[1] for x in owner_signals)

    strong_broker = (
        broker_score >= 5 and
        len([x for x in broker_signals if x[0] in {
            "broker_brand", "seller_is_pro", "company_name",
            "logo_corporativo", "multi_publicador"
        }]) >= 2
    )
    strong_owner = owner_score >= 4 and broker_score == 0

    if strong_broker:
        state = "CORREDOR_SEGURO"
    elif strong_owner:
        state = "DUEÑO_SEGURO"
    else:
        state = "INCIERTO"

    return {
        "classification_state": state,
        "es_propietario_directo": state == "DUEÑO_SEGURO",
        "es_corredor": state == "CORREDOR_SEGURO",
        "es_incierto": state == "INCIERTO",
        "score_corredor": broker_score,
        "score_dueno": owner_score,
        "motivos_corredor": [{"señal": s, "peso": p, "motivo": m} for s, p, m in broker_signals],
        "motivos_dueno": [{"señal": s, "peso": p, "motivo": m} for s, p, m in owner_signals],
    }

# ===========================================================================
# HELPERS DE EXTRACCIÓN Y RESPALDO
# ===========================================================================

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

# ===========================================================================
# LÓGICA PRINCIPAL (BATCH RUNNER)
# ===========================================================================

async def run_reclassification(apply_mode: bool):
    BATCH_SIZE = 500
    COLLECTION_NAME = "yapo_propiedades"
    
    log.info(f"Conectando a MongoDB: {Config.DB_NAME}.{COLLECTION_NAME}")
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db[COLLECTION_NAME]

    changelog = []
    transitions = Counter()
    total_processed = 0
    total_changed = 0

    log.info("Iniciando escaneo de colección completa...")
    
    # Cursor en lotes
    cursor = coll.find({}).batch_size(BATCH_SIZE)
    bulk_updates = []

    async for doc in cursor:
        total_processed += 1
        
        doc_id = doc["_id"]
        url = doc.get("url", "N/A")

        signals = reconstruct_signals_from_doc(doc)
        old_state_data = extract_old_classification(doc)
        
        recalc = classify_seller_state(**signals)
        new_state = recalc["classification_state"]
        new_confianza = get_confianza_from_state(new_state)

        old_state = old_state_data["classification_state"]

        if old_state != new_state:
            total_changed += 1
            transitions[f"{old_state} → {new_state}"] += 1
            
            changelog.append({
                "_id": str(doc_id),
                "url": url,
                "old_state": old_state,
                "new_state": new_state,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

            if apply_mode:
                # 1. Crear backup en el documento
                backup_data = old_state_data.copy()
                backup_data["fecha_respaldo"] = datetime.now(timezone.utc).isoformat()
                backup_data["motivo_cambio"] = "Reclasificacion masiva v2"

                # 2. Generar el update dictionary
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
                
                # Sincronizamos los campos si estaban duplicados en 'details' para mayor limpieza
                if "details" in doc:
                    update_doc["$set"]["details.classification_state"] = new_state
                    update_doc["$set"]["details.es_propietario_directo"] = recalc["es_propietario_directo"]
                    update_doc["$set"]["details.es_corredor"] = recalc["es_corredor"]
                    update_doc["$set"]["details.es_incierto"] = recalc["es_incierto"]
                    update_doc["$set"]["details.confianza_propietario"] = new_confianza

                bulk_updates.append(UpdateOne({"_id": doc_id}, update_doc))

        # Ejecutar bulk write en lotes
        if len(bulk_updates) >= BATCH_SIZE:
            if apply_mode:
                res = await coll.bulk_write(bulk_updates, ordered=False)
                log.info(f"Lote insertado: {res.modified_count} documentos modificados.")
            bulk_updates.clear()

        if total_processed % 1000 == 0:
            log.info(f"Progreso: {total_processed} documentos analizados... (Cambios detectados: {total_changed})")

    # Guardar últimos documentos pendientes
    if bulk_updates and apply_mode:
        res = await coll.bulk_write(bulk_updates, ordered=False)
        log.info(f"Último lote insertado: {res.modified_count} documentos modificados.")

    log.info("Escaneo finalizado.")
    
    # ── RESUMEN FINAL ────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("RESUMEN DE TRANSICIONES")
    print("="*60)
    if total_changed == 0:
        print("No se encontraron discrepancias. La colección está al día.")
    else:
        for trans, count in sorted(transitions.items(), key=lambda x: -x[1]):
            print(f"  {trans:<40} {count:>6}")

        print(f"\nTotal documentos procesados: {total_processed}")
        print(f"Total documentos modificados: {total_changed}")
    print("="*60)

    # ── GUARDAR CHANGELOG ────────────────────────────────────────────────────
    changelog_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reclassification_changelog.json")
    with open(changelog_path, "w", encoding="utf-8") as f:
        json.dump(changelog, f, ensure_ascii=False, indent=2)
    log.info(f"Changelog guardado en: {changelog_path}")

    if not apply_mode:
        log.info("⚠️ EJECUCIÓN EN MODO DRY-RUN. NO SE APLICÓ NINGÚN CAMBIO EN LA BD.")
        log.info("Usa '--apply' para escribir los cambios.")
    else:
        log.info("✅ RECLASIFICACIÓN APLICADA CON ÉXITO EN LA BD.")

    client.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reclasificación masiva de Yapo")
    parser.add_argument("--apply", action="store_true", help="Aplica los cambios en la BD. Si no se usa, corre en dry-run.")
    args = parser.parse_args()

    asyncio.run(run_reclassification(args.apply))
