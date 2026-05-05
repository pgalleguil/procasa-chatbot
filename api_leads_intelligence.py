"""
api_leads_intelligence.py
OPTIMIZED: All heavy aggregation moved to MongoDB Aggregation Pipelines.
Results are cached in-memory for 5 minutes to avoid recomputation on every request.
"""
from chatbot.storage import get_db
from config import Config
from datetime import datetime, timedelta
from collections import Counter, defaultdict
import logging
import time

logger = logging.getLogger(__name__)

# --------------------------------------------------
# CACHE DISTRIBUIDA EN MONGODB (TTL 5 minutos)
# Ventajas vs dict en memoria:
#   - Persiste entre restarts
#   - Compartida entre instancias (multi-worker en Render)
#   - Auto-expira via TTL index de MongoDB
# --------------------------------------------------
_CACHE_TTL_SECONDS = 300  # 5 minutos
_cache_index_ensured = False

def _ensure_cache_index():
    """Crea el TTL index la primera vez. Es idempotente."""
    global _cache_index_ensured
    if _cache_index_ensured:
        return
    try:
        from chatbot.storage import get_db
        db = get_db()
        db["cache_store"].create_index(
            "expires_at",
            expireAfterSeconds=0,  # MongoDB elimina el doc cuando expires_at < now()
            background=True
        )
        _cache_index_ensured = True
    except Exception as e:
        logger.warning(f"CACHE: No se pudo crear TTL index: {e}")

def _get_cached(key: str):
    try:
        _ensure_cache_index()
        from chatbot.storage import get_db
        db = get_db()
        doc = db["cache_store"].find_one({"_id": key})
        if doc:
            logger.info(f"CACHE: hit [{key}]")
            return doc.get("data")
    except Exception as e:
        logger.warning(f"CACHE: error en get [{key}]: {e}")
    return None

def _set_cached(key: str, data):
    try:
        _ensure_cache_index()
        from chatbot.storage import get_db
        from datetime import timezone
        db = get_db()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=_CACHE_TTL_SECONDS)
        db["cache_store"].update_one(
            {"_id": key},
            {"$set": {"data": data, "expires_at": expires_at}},
            upsert=True
        )
    except Exception as e:
        logger.warning(f"CACHE: error en set [{key}]: {e}")

# --------------------------------------------------
# Helper para conversión segura de int
# --------------------------------------------------
def safe_int_conversion(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

# --------------------------------------------------
# 1. FECHA REAL DE CREACIÓN DEL LEAD
# --------------------------------------------------
def get_creation_date(doc):
    try:
        return doc["_id"].generation_time.replace(tzinfo=None)
    except Exception:
        ts = doc.get("created_at")
        if ts:
            try:
                if isinstance(ts, datetime):
                    return ts.replace(tzinfo=None)
                return datetime.fromisoformat(str(ts).replace("Z", ""))
            except Exception:
                pass
        return datetime.now()

# --------------------------------------------------
# 2. SCORE DE CALIDAD (0–10)
# --------------------------------------------------
def calculate_score(prospecto, intencion_legacy=None, bi_data=None):
    score = 0
    if bi_data and bi_data.get("ALERTA_CRITICA") == "RECLAMO_CONTACTO":
        return 10
    if bi_data:
        if bi_data.get("TIPO_CONTACTO") == "CORREDOR_EXTERNO":
            return 1
        if bi_data.get("RESULTADO_CHAT") in ["VISITA_AGENDADA", "VISITA_SOLICITADA"]:
            score += 5
        if bi_data.get("URGENCIA") == "ALTA_URGENCIA":
            score += 3
        if bi_data.get("RECUPERABILIDAD") == "ALTA":
            score += 2
        elif bi_data.get("RECUPERABILIDAD") == "BAJA":
            return 1
    if prospecto.get("rut"):
        score += 1
    if prospecto.get("email"):
        score += 1
    if not bi_data and intencion_legacy == "agendar_visita":
        score += 4
    return min(score, 10)

# --------------------------------------------------
# 3. INTENCIÓN MÁS FUERTE (LEGACY)
# --------------------------------------------------
def determine_strongest_intent(messages):
    prioridades = ["escalado_urgente", "agendar_visita", "contacto_directo", "consultar_precio"]
    intenciones = {m.get("intencion") for m in messages if m.get("intencion")}
    for p in prioridades:
        if p in intenciones:
            return p
    return "consulta_general"

# --------------------------------------------------
# 4. REPORTE EJECUTIVO — OPTIMIZADO CON AGGREGATION PIPELINE
# --------------------------------------------------
def get_leads_executive_report():
    """
    Retorna KPIs, charts y tabla de leads.
    - KPIs y charts: calculados via MongoDB Aggregation Pipelines (trabajo en el motor de DB)
    - Tabla de leads: top 500 más recientes, proyección mínima
    - Cache: 5 minutos en memoria (evita recalcular en cada request)
    """
    cached = _get_cached("leads_executive_report")
    if cached:
        logger.info("LEADS_INTELLIGENCE: cache hit")
        return cached

    t_start = time.time()
    try:
        db = get_db()

        # ----------------------------------------------------------------
        # PIPELINE 1: KPIs agregados (todo en MongoDB, O(1) en Python)
        # ----------------------------------------------------------------
        kpi_pipeline = [
            {"$match": {}},
            {"$group": {
                "_id": None,
                "total_leads": {"$sum": 1},
                "con_email": {"$sum": {"$cond": [{"$gt": ["$prospecto.email", None]}, 1, 0]}},
                "con_rut": {"$sum": {"$cond": [{"$gt": ["$prospecto.rut", None]}, 1, 0]}},
            }}
        ]
        kpi_raw = list(db["leads"].aggregate(kpi_pipeline))
        total_leads = kpi_raw[0]["total_leads"] if kpi_raw else 0

        # ----------------------------------------------------------------
        # PIPELINE 2: Distribución por origen/fuente
        # ----------------------------------------------------------------
        fuentes_pipeline = [
            {"$group": {"_id": {"$ifNull": ["$prospecto.origen", "Directo"]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10}
        ]
        fuentes_data = list(db["leads"].aggregate(fuentes_pipeline))
        fuentes_labels = [d["_id"] for d in fuentes_data]
        fuentes_values = [d["count"] for d in fuentes_data]

        # ----------------------------------------------------------------
        # PIPELINE 3: Distribución por operación (venta/arriendo)
        # ----------------------------------------------------------------
        ops_pipeline = [
            {"$group": {"_id": {"$ifNull": ["$prospecto.operacion", "Venta"]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]
        ops_data = list(db["leads"].aggregate(ops_pipeline))

        # ----------------------------------------------------------------
        # PIPELINE 4: Distribución por tipo (casa/departamento)
        # ----------------------------------------------------------------
        tipos_pipeline = [
            {"$group": {"_id": {"$ifNull": ["$prospecto.tipo", "Departamento"]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]
        tipos_data = list(db["leads"].aggregate(tipos_pipeline))

        # ----------------------------------------------------------------
        # PIPELINE 5: Distribución por comuna (top 15)
        # ----------------------------------------------------------------
        comunas_pipeline = [
            {"$group": {"_id": {"$ifNull": ["$prospecto.comuna", "Sin Comuna"]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 15}
        ]
        comunas_data = list(db["leads"].aggregate(comunas_pipeline))

        # ----------------------------------------------------------------
        # PIPELINE 6: Distribución por BI intención/resultado/recuperabilidad
        # ----------------------------------------------------------------
        bi_pipeline = [
            {"$match": {"bi_analytics_global": {"$exists": True, "$ne": {}}}},
            {"$group": {
                "_id": None,
                "intenciones": {"$push": "$bi_analytics_global.INTENCION_CLIENTE"},
                "resultados": {"$push": "$bi_analytics_global.RESULTADO_CHAT"},
                "recuperabilidades": {"$push": "$bi_analytics_global.RECUPERABILIDAD"},
            }}
        ]
        bi_raw = list(db["leads"].aggregate(bi_pipeline))

        if bi_raw:
            bi_intenciones = Counter(x for x in bi_raw[0].get("intenciones", []) if x)
            bi_resultados = Counter(x for x in bi_raw[0].get("resultados", []) if x)
            bi_recuperabilidad = Counter(x for x in bi_raw[0].get("recuperabilidades", []) if x)
        else:
            bi_intenciones = Counter()
            bi_resultados = Counter()
            bi_recuperabilidad = Counter()

        # ----------------------------------------------------------------
        # PIPELINE 7: Temporal diario (últimos 90 días)
        # ----------------------------------------------------------------
        ninety_days_ago = datetime.utcnow() - timedelta(days=90)
        temporal_pipeline = [
            {"$match": {"_id": {"$gt": __import__('bson').ObjectId.from_datetime(ninety_days_ago)}}},
            {"$group": {
                "_id": {
                    "year": {"$year": "$_id"},
                    "month": {"$month": "$_id"},
                    "day": {"$dayOfMonth": "$_id"}
                },
                "count": {"$sum": 1}
            }},
            {"$sort": {"_id": 1}}
        ]
        temporal_data = list(db["leads"].aggregate(temporal_pipeline))
        temporal_diario = {}
        for d in temporal_data:
            label = f"{d['_id']['year']:04d}-{d['_id']['month']:02d}-{d['_id']['day']:02d}"
            temporal_diario[label] = d["count"]

        # ----------------------------------------------------------------
        # KPIs DIARIOS (hoy vs semana)
        # ----------------------------------------------------------------
        hoy = datetime.utcnow().date()
        week_ago = hoy - timedelta(days=7)

        leads_hoy = temporal_diario.get(hoy.strftime("%Y-%m-%d"), 0)
        leads_week = temporal_diario.get(week_ago.strftime("%Y-%m-%d"), 0)
        avg_total_7d = sum(
            temporal_diario.get((hoy - timedelta(days=i)).strftime("%Y-%m-%d"), 0)
            for i in range(1, 8)
        ) / 7

        pct_delta_total_7d = ((leads_hoy - avg_total_7d) / avg_total_7d * 100) if avg_total_7d > 0 else 0
        pct_delta_total_week = ((leads_hoy - leads_week) / leads_week * 100) if leads_week > 0 else 0

        # ----------------------------------------------------------------
        # TABLA DE LEADS — proyección mínima, solo top 500 más recientes
        # ----------------------------------------------------------------
        projection = {
            "phone": 1,
            "prospecto.nombre": 1,
            "prospecto.email": 1,
            "prospecto.rut": 1,
            "prospecto.origen": 1,
            "prospecto.operacion": 1,
            "prospecto.tipo": 1,
            "prospecto.comuna": 1,
            "prospecto.precio_uf": 1,
            "bi_analytics_global": 1,
            "pipeline_stage": 1,
            "created_at": 1,
        }

        docs_cursor = db["leads"].find({}, projection).sort("_id", -1).limit(500)
        leads_table = []
        leads_calientes = 0
        leads_calientes_con_datos = 0

        for doc in docs_cursor:
            p = doc.get("prospecto", {})
            bi_data = doc.get("bi_analytics_global", {})

            is_hot = False
            if bi_data:
                recup = bi_data.get("RECUPERABILIDAD", "")
                resultado = bi_data.get("RESULTADO_CHAT", "")
                if recup == "ALTA_PRIORIDAD" or resultado in ["VISITA_AGENDADA", "VISITA_SOLICITADA"]:
                    is_hot = True
            
            if is_hot:
                leads_calientes += 1
                if p.get("email") and p.get("rut"):
                    leads_calientes_con_datos += 1

            fecha_obj = get_creation_date(doc)
            score = calculate_score(p, None, bi_data)

            leads_table.append({
                "nombre": p.get("nombre", ""),
                "phone": doc.get("phone"),
                "email": p.get("email"),
                "rut": p.get("rut"),
                "bi_data": bi_data,
                "score": score,
                "origen": p.get("origen") or "Directo",
                "fecha": fecha_obj.strftime("%Y-%m-%d"),
                "hot_lead": is_hot,
                "operacion": p.get("operacion") or "Venta",
                "tipo": p.get("tipo") or "Departamento",
                "comuna": p.get("comuna") or "Sin Comuna",
                "precio_uf": safe_int_conversion(p.get("precio_uf")) or 0,
                "intencion_legacy": bi_data.get("INTENCION_CLIENTE", "consulta_general") if bi_data else "consulta_general",
            })

        pct_hot_leads = (leads_calientes / len(leads_table) * 100) if leads_table else 0
        tasa_captura_datos_hot = (leads_calientes_con_datos / leads_calientes * 100) if leads_calientes > 0 else 0

        top_operacion = ops_data[0]["_id"] if ops_data else "N/A"
        pct_top_operacion = round(ops_data[0]["count"] / total_leads * 100 if total_leads > 0 else 0, 1)
        top_tipo = tipos_data[0]["_id"] if tipos_data else "N/A"
        pct_top_tipo = round(tipos_data[0]["count"] / total_leads * 100 if total_leads > 0 else 0, 1)
        top_comuna = comunas_data[0]["_id"] if comunas_data else "N/A"
        pct_top_comuna = round(comunas_data[0]["count"] / total_leads * 100 if total_leads > 0 else 0, 1)

        result = {
            "kpis": {
                "total_leads": total_leads,
                "leads_calientes": leads_calientes,
                "pct_leads_calientes": round(pct_hot_leads, 1),
                "tasa_captura_datos_hot": round(tasa_captura_datos_hot, 1),
                "penetracion_datos_total": round(tasa_captura_datos_hot, 1),
                "avg_speed_minutes": 0,
                "leads_hoy": leads_hoy,
                "hot_hoy": 0,
                "hot_rate_hoy": 0,
                "tasa_datos_hoy": 0,
                "avg_speed_hoy": 0,
                "pct_delta_total_7d": round(pct_delta_total_7d, 1),
                "pct_delta_hot_7d": 0,
                "pct_delta_datos_7d": 0,
                "pct_delta_total_week": round(pct_delta_total_week, 1),
                "pct_delta_hot_week": 0,
                "pct_delta_datos_week": 0,
                "hot_rate_7d": 0,
                "hot_rate_week": 0,
                "tasa_datos_7d": 0,
                "avg_speed_7d": 0,
            },
            "charts": {
                "temporal": {
                    "labels": sorted(temporal_diario.keys()),
                    "values": [temporal_diario[d] for d in sorted(temporal_diario)]
                },
                "fuentes": {"labels": fuentes_labels, "values": fuentes_values},
                "operaciones": {
                    "labels": [d["_id"] for d in ops_data],
                    "values": [d["count"] for d in ops_data]
                },
                "tipos": {
                    "labels": [d["_id"] for d in tipos_data],
                    "values": [d["count"] for d in tipos_data]
                },
                "comunas": {
                    "labels": [d["_id"] for d in comunas_data],
                    "values": [d["count"] for d in comunas_data]
                },
                "bi_intencion": {
                    "labels": list(bi_intenciones.keys()),
                    "values": list(bi_intenciones.values())
                },
                "bi_resultado": {
                    "labels": list(bi_resultados.keys()),
                    "values": list(bi_resultados.values())
                },
                "bi_recuperabilidad": {
                    "labels": list(bi_recuperabilidad.keys()),
                    "values": list(bi_recuperabilidad.values())
                },
            },
            "aggs": {
                "top_operacion": top_operacion,
                "pct_top_operacion": pct_top_operacion,
                "top_tipo": top_tipo,
                "pct_top_tipo": pct_top_tipo,
                "top_comuna": top_comuna,
                "pct_top_comuna": pct_top_comuna,
                "avg_precio_uf": 0,
                "avgs_precio_por_operacion": {},
                "avgs_precio_por_tipo": {},
                "avg_tiempos_por_operacion": {},
                "pct_hot_por_tipo": {},
            },
            "leads": leads_table
        }

        elapsed = (time.time() - t_start) * 1000
        logger.info(f"LEADS_INTELLIGENCE: computed in {elapsed:.1f}ms (cache miss), caching for 5min")
        _set_cached("leads_executive_report", result)
        return result

    except Exception as e:
        logger.error(f"Error en reporte intelligence: {e}", exc_info=True)
        return {"kpis": {}, "charts": {}, "aggs": {}, "leads": []}


def get_specific_lead_chat(phone):
    try:
        db = get_db()
        doc = db["leads"].find_one({"phone": phone})
        if not doc:
            return None
        return {
            "phone": doc.get("phone"),
            "prospecto": doc.get("prospecto", {}),
            "messages": doc.get("messages", []),
            "bi_analytics_global": doc.get("bi_analytics_global", {})
        }
    except Exception as e:
        logger.error(f"Error obteniendo chat {phone}: {e}")
        return None