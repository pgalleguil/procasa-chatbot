# -*- coding: utf-8 -*-
"""
audit_reclassification.py
=========================
Auditoría previa a reclasificación masiva de propiedades Yapo.

MODO SEGURO: Solo lectura. No modifica ningún documento.

Objetivo:
  - Muestrear registros con es_propietario_directo = true
  - Recalcular classify_seller_state() con la lógica actual
  - Comparar resultado almacenado vs resultado actual
  - Generar reporte con métricas y patrones de discrepancia

Uso:
  python scraping/audit_reclassification.py
  python scraping/audit_reclassification.py --sample 200
  python scraping/audit_reclassification.py --sample 100 --full-collection

Salida:
  audit_reclassification_report.txt  — reporte legible
  audit_reclassification_data.json   — datos crudos para análisis posterior
"""

import os
import sys
import io
import json
import random
import asyncio
import logging
import re
from datetime import datetime, timezone
from collections import defaultdict, Counter
from unicodedata import normalize
from collections import OrderedDict
from threading import Lock
import time

# Forzar UTF-8 en stdout/stderr para evitar UnicodeEncodeError en consolas Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── Path fix para importar config desde el proyecto raíz ──────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

from motor.motor_asyncio import AsyncIOMotorClient

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("audit")

# ── Configuración de la auditoría ──────────────────────────────────────────
AUDIT_CONFIG = {
    "collection": "yapo_propiedades",
    "sample_size": 150,           # mínimo recomendado: 100
    "discrepancy_threshold": 0.10, # 10%: si supera esto, se recomienda reclasificación
    "output_report": os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "audit_reclassification_report.txt"
    ),
    "output_json": os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "audit_reclassification_data.json"
    ),
}

# ===========================================================================
# COPIA EXACTA DE LA LÓGICA DE CLASIFICACIÓN (del scraping_yapo_proxys.py)
# Se copia para garantizar que la auditoría use EXACTAMENTE la misma lógica
# que el scraper actual, sin depender de imports circulares.
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
# LÓGICA DE AUDITORÍA
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
    """
    Reconstruye las señales de entrada para classify_seller_state()
    a partir de un documento MongoDB almacenado.
    Mapea los distintos layouts históricos de campos.
    """
    # El documento puede tener fields en top-level o dentro de 'details'
    details = doc.get("details", {}) or {}

    seller_name   = _safe_str(doc.get("publicador") or details.get("publicador"))
    description   = _safe_str(doc.get("raw_desc")   or details.get("raw_desc") or
                               doc.get("descripcion") or details.get("descripcion"))
    company_name  = _safe_str(doc.get("company_name") or details.get("company_name"))
    broker_brand  = _safe_str(doc.get("broker_brand") or details.get("broker_brand"))
    seller_profile_id = _safe_str(doc.get("seller_profile_id") or details.get("seller_profile_id"))

    # seller_is_pro puede estar en distintos niveles
    raw_pro = (
        doc.get("seller_is_pro") if doc.get("seller_is_pro") is not None
        else details.get("seller_is_pro")
    )
    seller_is_pro = _safe_bool(raw_pro)

    return {
        "seller_name": seller_name,
        "description": description,
        "company_name": company_name,
        "seller_profile_id": seller_profile_id,
        "seller_is_pro": seller_is_pro,
        "broker_brand": broker_brand,
    }


def get_stored_classification(doc: dict) -> dict:
    """Extrae la clasificación almacenada del documento."""
    details = doc.get("details", {}) or {}

    stored_state = _safe_str(
        doc.get("classification_state") or
        details.get("classification_state")
    )
    stored_es_propietario = _safe_bool(
        doc.get("es_propietario_directo") if doc.get("es_propietario_directo") is not None
        else details.get("es_propietario_directo")
    )
    stored_es_corredor = _safe_bool(
        doc.get("es_corredor") if doc.get("es_corredor") is not None
        else details.get("es_corredor")
    )

    # Reconstruir estado legible si no hay campo classification_state
    if stored_state == "N/A":
        if stored_es_corredor:
            stored_state = "CORREDOR_SEGURO"
        elif stored_es_propietario:
            stored_state = "DUEÑO_SEGURO"
        else:
            stored_state = "INCIERTO"

    return {
        "classification_state": stored_state,
        "es_propietario_directo": stored_es_propietario,
        "es_corredor": stored_es_corredor,
    }


def detect_discrepancy_pattern(signals: dict, stored: dict, recalculated: dict) -> list[str]:
    """Identifica el patrón que causó la discrepancia."""
    patterns = []

    if stored["es_propietario_directo"] and recalculated["es_corredor"]:
        patterns.append("FALSO_DUEÑO→CORREDOR")

    if signals["seller_is_pro"] and stored["es_propietario_directo"]:
        patterns.append("seller_is_pro=True_pero_almacenado_como_dueño")

    if signals["broker_brand"] != "N/A" and stored["es_propietario_directo"]:
        patterns.append(f"broker_brand='{signals['broker_brand']}'_pero_dueño")

    if signals["company_name"] != "N/A" and stored["es_propietario_directo"]:
        patterns.append(f"company_name='{signals['company_name']}'_pero_dueño")

    if not signals["seller_is_pro"] and not stored["es_corredor"] and recalculated["es_corredor"]:
        patterns.append("heuristica_nueva_detecta_corredor")

    if recalculated["es_incierto"] and (stored["es_propietario_directo"] or stored["es_corredor"]):
        patterns.append("estado_cambió_a_INCIERTO")

    return patterns if patterns else ["sin_patron_claro"]


async def run_audit(sample_size: int = None):
    sample_size = sample_size or AUDIT_CONFIG["sample_size"]

    log.info("=" * 70)
    log.info("🔍 AUDITORÍA DE RECLASIFICACIÓN — MODO SOLO LECTURA")
    log.info(f"   Colección: {AUDIT_CONFIG['collection']}")
    log.info(f"   Muestra objetivo: {sample_size} registros")
    log.info(f"   Umbral de alerta: {AUDIT_CONFIG['discrepancy_threshold']*100:.0f}%")
    log.info("=" * 70)

    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db[AUDIT_CONFIG["collection"]]

    # ── FASE 0: Estadísticas globales de la colección ────────────────────
    log.info("\n📊 FASE 0: Conteos globales de la colección...")

    total_docs     = await coll.count_documents({})
    total_duenos   = await coll.count_documents({"es_propietario_directo": True})
    total_corred   = await coll.count_documents({"es_corredor": True})
    total_incierto = await coll.count_documents({
        "es_propietario_directo": {"$ne": True},
        "es_corredor": {"$ne": True}
    })

    # Contar también los que viven dentro de 'details'
    total_duenos_det = await coll.count_documents({"details.es_propietario_directo": True})
    total_corred_det = await coll.count_documents({"details.es_corredor": True})

    log.info(f"   Total documentos:              {total_docs:,}")
    log.info(f"   es_propietario_directo=True:   {total_duenos:,} (top-level)")
    log.info(f"   es_propietario_directo=True:   {total_duenos_det:,} (details.*)")
    log.info(f"   es_corredor=True:              {total_corred:,} (top-level)")
    log.info(f"   es_corredor=True:              {total_corred_det:,} (details.*)")
    log.info(f"   Estado incierto/sin campo:     {total_incierto:,}")

    # ── FASE 1: Muestreo aleatorio ────────────────────────────────────────
    log.info(f"\n🎲 FASE 1: Muestreo aleatorio de {sample_size} registros con es_propietario_directo=True...")

    # Usamos $sample de MongoDB para aleatoriedad real sin sesgo
    # Cubrimos ambos layouts (top-level y dentro de details)
    pipeline = [
        {"$match": {
            "$or": [
                {"es_propietario_directo": True},
                {"details.es_propietario_directo": True}
            ]
        }},
        {"$sample": {"size": sample_size * 2}},  # oversample para filtrar inválidos
        {"$limit": sample_size * 2}
    ]

    raw_docs = []
    async for doc in coll.aggregate(pipeline, allowDiskUse=True):
        raw_docs.append(doc)

    log.info(f"   Documentos recuperados del pipeline: {len(raw_docs)}")

    # ── FASE 2: Recalculación ──────────────────────────────────────────────
    log.info(f"\n⚙️  FASE 2: Recalculando classify_seller_state() para cada registro...")

    results = []
    skipped = 0

    for doc in raw_docs[:sample_size]:
        doc_id = str(doc.get("_id", ""))
        url    = _safe_str(doc.get("url"))

        signals    = reconstruct_signals_from_doc(doc)
        stored     = get_stored_classification(doc)
        recalc     = classify_seller_state(**signals)
        patterns   = detect_discrepancy_pattern(signals, stored, recalc)

        changed = (
            stored["classification_state"] != recalc["classification_state"]
        )

        result = {
            "doc_id": doc_id,
            "url": url,
            # Señales de entrada
            "signals": signals,
            # Clasificación almacenada
            "stored": stored,
            # Clasificación recalculada
            "recalculated": {
                "classification_state": recalc["classification_state"],
                "es_propietario_directo": recalc["es_propietario_directo"],
                "es_corredor": recalc["es_corredor"],
                "es_incierto": recalc["es_incierto"],
                "score_corredor": recalc["score_corredor"],
                "score_dueno": recalc["score_dueno"],
            },
            "changed": changed,
            "discrepancy_patterns": patterns,
            # Metadata útil para post-reclasificación
            "audit_metadata": {
                "fecha_scraping": _safe_str(doc.get("fecha_scraping") or
                                            (doc.get("details") or {}).get("fecha_scraping")),
                "quality_score": (doc.get("details") or {}).get("quality_score", "N/A"),
                "has_audit_fix": "audit_fix" in doc,
            }
        }
        results.append(result)

    total_sample = len(results)
    log.info(f"   Registros procesados: {total_sample}")

    # ── FASE 3: Métricas ───────────────────────────────────────────────────
    log.info("\n📈 FASE 3: Calculando métricas...")

    changed_docs      = [r for r in results if r["changed"]]
    unchanged_docs    = [r for r in results if not r["changed"]]

    # Transiciones
    transitions = Counter()
    for r in results:
        key = f"{r['stored']['classification_state']} → {r['recalculated']['classification_state']}"
        transitions[key] += 1

    # Patrones de discrepancia
    all_patterns = []
    for r in changed_docs:
        all_patterns.extend(r["discrepancy_patterns"])
    pattern_counts = Counter(all_patterns)

    # Señales presentes en docs que cambiaron
    changed_signals = {
        "con_broker_brand": sum(1 for r in changed_docs if r["signals"]["broker_brand"] != "N/A"),
        "con_seller_is_pro": sum(1 for r in changed_docs if r["signals"]["seller_is_pro"]),
        "con_company_name": sum(1 for r in changed_docs if r["signals"]["company_name"] != "N/A"),
        "sin_broker_brand": sum(1 for r in changed_docs if r["signals"]["broker_brand"] == "N/A"),
        "sin_company_name": sum(1 for r in changed_docs if r["signals"]["company_name"] == "N/A"),
    }

    # Tasa de discrepancia
    discrepancy_rate = len(changed_docs) / total_sample if total_sample > 0 else 0

    # ── FASE 4: Decisión de reclasificación ───────────────────────────────
    recomienda_reclasificacion = discrepancy_rate > AUDIT_CONFIG["discrepancy_threshold"]

    # ── Salvar JSON crudo ─────────────────────────────────────────────────
    output_data = {
        "audit_timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "collection": AUDIT_CONFIG["collection"],
            "sample_size": total_sample,
            "discrepancy_threshold": AUDIT_CONFIG["discrepancy_threshold"],
        },
        "global_counts": {
            "total_docs": total_docs,
            "es_propietario_directo_total": total_duenos + total_duenos_det,
            "es_corredor_total": total_corred + total_corred_det,
        },
        "metrics": {
            "total_sample": total_sample,
            "total_changed": len(changed_docs),
            "total_unchanged": len(unchanged_docs),
            "discrepancy_rate": round(discrepancy_rate, 4),
            "transitions": dict(transitions),
            "pattern_counts": dict(pattern_counts),
            "changed_signals_breakdown": changed_signals,
        },
        "recommendation": {
            "recomienda_reclasificacion": recomienda_reclasificacion,
            "umbral_aplicado": AUDIT_CONFIG["discrepancy_threshold"],
            "discrepancy_rate_actual": round(discrepancy_rate, 4),
        },
        "records": results,
    }

    with open(AUDIT_CONFIG["output_json"], "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"\n💾 JSON crudo guardado: {AUDIT_CONFIG['output_json']}")

    # ── Generar reporte texto ─────────────────────────────────────────────
    await _write_report(output_data, results, changed_docs, transitions, pattern_counts, changed_signals)

    client.close()
    return output_data


async def _write_report(data, results, changed_docs, transitions, pattern_counts, changed_signals):
    """Escribe el reporte .txt con todas las fases."""
    lines = []
    ts = data["audit_timestamp"]
    total_sample = data["metrics"]["total_sample"]
    n_changed    = data["metrics"]["total_changed"]
    disc_rate    = data["metrics"]["discrepancy_rate"]
    threshold    = data["recommendation"]["umbral_aplicado"]
    recomienda   = data["recommendation"]["recomienda_reclasificacion"]

    # ── HEADER ────────────────────────────────────────────────────────────
    lines.append("=" * 80)
    lines.append("AUDITORÍA DE RECLASIFICACIÓN MASIVA — YAPO PROPIEDADES")
    lines.append(f"Generado: {ts}")
    lines.append(f"Colección: {AUDIT_CONFIG['collection']} | DB: {Config.DB_NAME}")
    lines.append("MODO: SOLO LECTURA — Sin modificaciones aplicadas")
    lines.append("=" * 80)

    # ── RESUMEN GLOBAL ────────────────────────────────────────────────────
    gc = data["global_counts"]
    lines.append("\n[RESUMEN GLOBAL DE LA COLECCIÓN]")
    lines.append(f"  Total documentos en colección:     {gc['total_docs']:,}")
    lines.append(f"  Total es_propietario_directo=True: {gc['es_propietario_directo_total']:,}")
    lines.append(f"  Total es_corredor=True:            {gc['es_corredor_total']:,}")

    # ── FASE 1: MUESTRA ───────────────────────────────────────────────────
    lines.append("\n" + "─" * 80)
    lines.append("FASE 1 — MUESTRA ANALIZADA")
    lines.append("─" * 80)
    lines.append(f"  Registros en muestra:        {total_sample}")
    lines.append(f"  Registros con cambio:        {n_changed}")
    lines.append(f"  Registros sin cambio:        {total_sample - n_changed}")
    lines.append(f"  Tasa de discrepancia:        {disc_rate*100:.1f}%")
    lines.append(f"  Umbral de alerta:            {threshold*100:.0f}%")

    # Tabla de los primeros 50 registros con cambio (o todos si son menos)
    lines.append("\n  TABLA DE CAMBIOS (muestra de cambios detectados):")
    lines.append(f"  {'ID MongoDB':<26} {'Estado Almacenado':<20} {'Estado Recalculado':<20} {'Cambio'}")
    lines.append("  " + "─" * 74)

    shown = 0
    for r in results:
        if r["changed"] and shown < 60:
            _id_short = r["doc_id"][-20:] if len(r["doc_id"]) > 20 else r["doc_id"]
            lines.append(
                f"  {_id_short:<26} "
                f"{r['stored']['classification_state']:<20} "
                f"{r['recalculated']['classification_state']:<20} "
                f"{'✓ SÍ'}"
            )
            shown += 1

    if n_changed > shown:
        lines.append(f"  ... y {n_changed - shown} registros más con cambio (ver JSON para detalle completo)")

    # ── FASE 2: MÉTRICAS ─────────────────────────────────────────────────
    lines.append("\n" + "─" * 80)
    lines.append("FASE 2 — MÉTRICAS DE TRANSICIÓN")
    lines.append("─" * 80)
    lines.append(f"\n  {'TRANSICIÓN':<45} {'N':<8} {'%'}")
    lines.append("  " + "─" * 60)
    for trans, count in sorted(transitions.items(), key=lambda x: -x[1]):
        pct = count / total_sample * 100
        lines.append(f"  {trans:<45} {count:<8} {pct:.1f}%")

    # ── FASE 3: PATRONES ─────────────────────────────────────────────────
    lines.append("\n" + "─" * 80)
    lines.append("FASE 3 — PATRONES DE DISCREPANCIA")
    lines.append("─" * 80)
    lines.append(f"\n  En los {n_changed} registros con cambio:")
    lines.append(f"\n  {'PATRÓN':<50} {'N':<8} {'%'}")
    lines.append("  " + "─" * 65)
    for pat, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        pct = count / max(n_changed, 1) * 100
        lines.append(f"  {pat:<50} {count:<8} {pct:.1f}%")

    lines.append("\n  SEÑALES PRESENTES EN DOCUMENTOS CON CAMBIO:")
    lines.append(f"    Con broker_brand informado:  {changed_signals['con_broker_brand']}")
    lines.append(f"    Con seller_is_pro=True:      {changed_signals['con_seller_is_pro']}")
    lines.append(f"    Con company_name informado:  {changed_signals['con_company_name']}")
    lines.append(f"    Sin broker_brand (N/A):      {changed_signals['sin_broker_brand']}")
    lines.append(f"    Sin company_name (N/A):      {changed_signals['sin_company_name']}")

    # Muestra detallada de los primeros 15 docs con cambio
    lines.append("\n  DETALLE DE LOS PRIMEROS 15 REGISTROS CON CAMBIO:")
    lines.append("  " + "─" * 78)
    detail_shown = 0
    for r in results:
        if r["changed"] and detail_shown < 15:
            detail_shown += 1
            lines.append(f"\n  [{detail_shown}] ID: {r['doc_id']}")
            lines.append(f"      URL: {r['url']}")
            lines.append(f"      publicador:   {r['signals']['seller_name']}")
            lines.append(f"      company_name: {r['signals']['company_name']}")
            lines.append(f"      broker_brand: {r['signals']['broker_brand']}")
            lines.append(f"      seller_is_pro:{r['signals']['seller_is_pro']}")
            lines.append(f"      Almacenado:   {r['stored']['classification_state']}")
            lines.append(f"      Recalculado:  {r['recalculated']['classification_state']}")
            lines.append(f"      score_corr:   {r['recalculated']['score_corredor']}  |  score_dueno: {r['recalculated']['score_dueno']}")
            lines.append(f"      Patrones:     {', '.join(r['discrepancy_patterns'])}")

    # ── FASE 4: DECISIÓN ─────────────────────────────────────────────────
    lines.append("\n" + "=" * 80)
    lines.append("FASE 4 — DECISIÓN Y RECOMENDACIÓN")
    lines.append("=" * 80)

    if recomienda:
        lines.append(f"\n  🔴 RECLASIFICACIÓN MASIVA RECOMENDADA")
        lines.append(f"  Tasa de discrepancia: {disc_rate*100:.1f}% > umbral {threshold*100:.0f}%")
    else:
        lines.append(f"\n  🟢 RECLASIFICACIÓN MASIVA NO URGENTE")
        lines.append(f"  Tasa de discrepancia: {disc_rate*100:.1f}% ≤ umbral {threshold*100:.0f}%")
        lines.append(f"  Sin embargo, existen {n_changed} registros individuales incorrectos.")

    lines.append("\n  PLAN DE EJECUCIÓN (si se decide proceder):")
    lines.append("""
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PASO 1 — RESPALDO                                                  │
  │  Antes de cualquier modificación, exportar la colección completa:   │
  │                                                                     │
  │  mongodump --uri="<MONGO_URI>" \\                                    │
  │    --db=URLS \\                                                      │
  │    --collection=yapo_propiedades \\                                  │
  │    --out=./backup_yapo_$(date +%Y%m%d_%H%M%S)                       │
  │                                                                     │
  │  PASO 2 — CAMPOS A PRESERVAR POR DOCUMENTO                          │
  │  Antes de update, guardar en campo 'pre_reclassification_backup':   │
  │    - classification_state                                           │
  │    - es_propietario_directo                                         │
  │    - es_corredor                                                    │
  │    - es_incierto                                                    │
  │    - score_corredor / score_dueno (si existen)                      │
  │    - motivos_corredor / motivos_dueno (si existen)                  │
  │    - fecha de modificación (timestamp)                              │
  │                                                                     │
  │  PASO 3 — QUERY DE RECLASIFICACIÓN MASIVA                           │
  │  Ejecutar el script: reclassify_batch.py (pendiente de crear)       │
  │  Lógica:                                                            │
  │    - Iterar en batches de 500 documentos                            │
  │    - Para cada doc: reconstruct_signals → classify_seller_state()   │
  │    - Si el resultado difiere: aplicar $set con nuevos campos        │
  │      y $set pre_reclassification_backup con valores viejos          │
  │    - Logging detallado de cada cambio                               │
  │                                                                     │
  │  PASO 4 — VALIDACIÓN POST-RECLASIFICACIÓN                           │
  │    - Correr nuevamente esta auditoría sobre la colección actualizada │
  │    - Verificar que discrepancy_rate baje a < 1%                     │
  │    - Revisar manualmente 10 casos al azar de cada transición        │
  │                                                                     │
  │  PASO 5 — REPORTE FINAL                                             │
  │    - El script generará reclassification_changelog.json con:        │
  │      {doc_id, url, estado_anterior, estado_nuevo, timestamp}        │
  └─────────────────────────────────────────────────────────────────────┘
""")

    lines.append("\n  PROYECCIÓN SOBRE TODA LA COLECCIÓN:")
    total_propietarios = gc["es_propietario_directo_total"]
    proyeccion_cambios = int(total_propietarios * disc_rate)
    lines.append(f"  Total es_propietario_directo en colección: {total_propietarios:,}")
    lines.append(f"  Tasa de discrepancia en muestra:           {disc_rate*100:.1f}%")
    lines.append(f"  Cambios proyectados en colección completa: ~{proyeccion_cambios:,} documentos")

    lines.append("\n" + "=" * 80)
    lines.append(f"Reporte generado: {ts}")
    lines.append(f"Archivo JSON con datos completos: {AUDIT_CONFIG['output_json']}")
    lines.append("=" * 80)

    report_text = "\n".join(lines)
    # Imprimir con manejo seguro de caracteres no-ASCII en Windows
    safe_text = report_text.encode('utf-8', errors='replace').decode('utf-8')
    try:
        print("\n" + safe_text)
    except UnicodeEncodeError:
        print(report_text.encode('ascii', errors='replace').decode('ascii'))

    with open(AUDIT_CONFIG["output_report"], "w", encoding="utf-8") as f:
        f.write(report_text)
    log.info(f"\n📄 Reporte guardado: {AUDIT_CONFIG['output_report']}")


# ===========================================================================
# ENTRY POINT
# ===========================================================================

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Auditoría de reclasificación masiva — SOLO LECTURA")
    parser.add_argument("--sample", type=int, default=AUDIT_CONFIG["sample_size"],
                        help=f"Tamaño de muestra (default: {AUDIT_CONFIG['sample_size']})")
    parser.add_argument("--threshold", type=float, default=AUDIT_CONFIG["discrepancy_threshold"],
                        help="Umbral de discrepancia para recomendar reclasificación (default: 0.10)")
    args = parser.parse_args()

    AUDIT_CONFIG["sample_size"]             = args.sample
    AUDIT_CONFIG["discrepancy_threshold"]   = args.threshold

    if not Config.MONGO_URI:
        log.error("❌ MONGO_URI no configurado en .env")
        sys.exit(1)

    result = await run_audit(args.sample)

    disc = result["metrics"]["discrepancy_rate"]
    n    = result["metrics"]["total_changed"]
    log.info(f"\n✅ Auditoría completada. Discrepancia: {disc*100:.1f}% ({n} registros cambian).")
    return result


if __name__ == "__main__":
    asyncio.run(main())
