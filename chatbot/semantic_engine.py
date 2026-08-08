
import re
import logging
import numpy as np
from typing import Optional, List
import pymongo
from pymongo import UpdateOne
from config import Config
from .storage import get_db

logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN ---
# NOTA: FastEmbed + ONNX Runtime consume ~200-250MB en carga, causando OOM en Render (512MB).
# Solución: Se elimina la carga del modelo local. El sistema de RAG funciona en modo
# "structured-only" cuando no hay embeddings disponibles, usando los vectores pre-calculados
# en MongoDB para el ranking de cosine similarity (sin necesidad de re-generar embeddings
# en runtime). Para re-generar embeddings, usar run_embeddings.py de forma local/offline.
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_model_instance = None
_model_load_attempted = False  # Evita reintentos infinitos

def get_model():
    """
    Singleton lazy-load del modelo de embeddings.
    IMPORTANTE: Solo se usa para el script offline run_embeddings.py.
    En producción (Render 512MB), el modelo NO se carga para evitar OOM.
    Los vectores ya están pre-calculados en MongoDB.
    """
    global _model_instance, _model_load_attempted
    if _model_instance is not None:
        return _model_instance
    if _model_load_attempted:
        return None  # Ya falló antes, no reintentar

    _model_load_attempted = True
    try:
        import psutil
        available_mb = psutil.virtual_memory().available / (1024 * 1024)
        if available_mb < 250:
            logger.warning(
                f"[EMBEDDING] Memoria disponible insuficiente ({available_mb:.0f}MB < 250MB). "
                "Saltando carga del modelo para evitar OOM. "
                "La búsqueda semántica usará solo filtros estructurados."
            )
            return None
    except ImportError:
        pass  # psutil no disponible, intentar igual

    try:
        from fastembed import TextEmbedding
        logger.info(f"Cargando modelo de embeddings (FastEmbed): {MODEL_NAME} ...")
        _model_instance = TextEmbedding(model_name=MODEL_NAME)
        logger.info("Modelo FastEmbed cargado exitosamente.")
        return _model_instance
    except ImportError:
        logger.error("Módulo 'fastembed' no encontrado.")
        return None
    except Exception as e:
        logger.error(f"FATAL: No se pudo cargar el modelo FastEmbed {MODEL_NAME}: {e}")
        return None


# --- LIMPIEZA DE TEXTO ---
STOPWORDS = {
    'vende', 'arrienda', 'propiedad', 'cod', 'codigo', 'interno', 'procasa',
    'oficina', 'contacto', 'fono', 'llamar', 'excelente', 'oportunidad',
    'el', 'la', 'los', 'las', 'un', 'una', 'de', 'del', 'y', 'o', 'a', 'en', 'para', 'por',
    'con', 'su', 'sus', 'es', 'son', 'al', 'lo', 'se', 'que'
}

def clean_desc_for_embedding(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'\.0\b', '', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'procasa\s*\w*', '', text)
    text = re.sub(r'c[óo]digo\s*\w*', '', text)
    text = re.sub(r'(\d{7,})', '', text)
    tokens = text.split()
    clean_tokens = [t for t in tokens if (t.isdigit() or (t not in STOPWORDS and len(t) > 2))]
    return " ".join(clean_tokens)


# --- CONSTRUCCIÓN DE TEXTO PARA EMBEDDING (esquema Prop360 anidado) ---
_EXTRA_FEATURE_NAMES = {
    "rbpiscina": "piscina", "rbHAS_TERRACE": "terraza", "rbHAS_GRILL": "quincho",
    "rbmascotas": "mascotas permitidas", "rbcondominio": "condominio",
    "rbHAS_GATED_COMMUNITY": "condominio cerrado", "rbHAS_BALCONY": "balcón",
    "rbHAS_ROOF_GARDEN": "terraza", "rbHAS_PLAYGROUND": "juegos infantiles",
    "rbHAS_SECURITY": "seguridad", "rbHAS_GUEST_PARKING": "estacionamiento visitas",
    "rbgimnasio": "gimnasio", "rbHAS_PARTY_ROOM": "salón de eventos",
    "rbHAS_COMMON_LAUNDRY": "lavandería", "rbrecepcion": "recepción",
    "rbbanoVisita": "baño de visita", "rbbodega": "bodega", "rbamoblado": "amoblado",
    "rbregularizada": "regularizada", "rbHAS_CLOSET": "closet", "rbHAS_DINNING_ROOM": "comedor de diario",
    "rbHAS_BOILER": "calefacción", "rbHAS_TAP_WATER": "agua potable", "rbHAS_ELECTRIC_GENERATOR": "grupo electrógeno",
    "rbHAS_FARM_HOUSE": "casa patronal", "rbHAS_FORESTATION": "plantaciones forestales",
    "rbHAS_DRAINAGE": "drenaje", "rbHAS_FARMYARD": "corral", "rbHAS_BASCULE": "báscula",
    "rbHAS_WATERERS": "riego automático", "rbHAS_STABLES": "caballerizas", "rbHAS_MILLS": "galpones",
    "rbHAS_FITTING_ROOM": "camarines", "rbHAS_BREAKFAST_SERVICE": "servicio desayuno",
    "rbHAS_CUTLERY": "cubiertos", "rbHAS_WHEELCHAIR_RAMP": "acceso discapacitados",
    "rbHAS_HOUSEKEEPING_SERVICE": "servicio housekeeping", "rbHAS_ELECTRIC_LIGHT": "luz eléctrica",
    "rbHAS_TELEPHONE_LINE": "línea telefónica", "rbHAS_NATURAL_GAS": "gas natural",
    "rbHAS_ELECTRIC_GENERATOR": "grupo electrógeno", "rbCANCHA_TENIS": "cancha de tenis",
    "rbbarbecue": "quincho", "rbcercoElectrico": "cerco eléctrico",
    "rbcircuitoCerradoVigilancia": "circuito cerrado", "rbjuegosInfantiles": "juegos infantiles",
    "rbportonAutomatico": "portón automático", "rbriegoAutomatico": "riego automático",
    "rbaguaDeRiego": "agua de riego", "rbaguaPozo": "agua de pozo", "rbpozo": "pozo",
    "rbletrero": "letrero", "rbalarma": "alarma", "rbcocina": "cocina",
    "rblavadero": "lavadero", "rbsalaJuegos": "sala de juegos", "rbwalkinCloset": "walk-in closet",
    "rbCHILDREN_WELCOME": "acepta niños", "rbHAS_TAP_WATER": "agua potable",
}


def _extra_features_text(caracteristicas: dict) -> str:
    """Convierte caracteristicas.extra (rbX1 = activo) y features[] en texto legible."""
    palabras = []
    features = caracteristicas.get("features") or []
    if isinstance(features, list):
        for f in features:
            v = str(f).strip()
            if v and v not in ("0", "False", "false"):
                palabras.append(v.lower())
    extra = caracteristicas.get("extra") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            activo = str(v).strip() in ("1", "true", "True", "on")
            if not activo:
                continue
            base_key = re.sub(r'[0-9]+$', '', k)
            nombre = _EXTRA_FEATURE_NAMES.get(base_key) or _EXTRA_FEATURE_NAMES.get(k)
            if not nombre:
                nombre = k.replace("rbHAS_", "").replace("rb", "").lower().replace("_", " ")
                nombre = re.sub(r'[0-9]+$', '', nombre).strip()
            if nombre:
                palabras.append(nombre)
    return " ".join(sorted(set(palabras)))


def build_embedding_text(doc: dict) -> str:
    """Construye el texto que se embebe para una propiedad del universo Prop360.
    Considera los campos según el tipo de propiedad (casa/depto vs sitio/parcela vs
    local/oficina/bodega/industrial vs estacionamiento)."""
    tipo_operacion = doc.get("tipo_operacion") or {}
    ubicacion = doc.get("ubicacion") or {}
    caracteristicas = doc.get("caracteristicas") or {}
    observaciones = doc.get("observaciones") or {}
    resumen = doc.get("resumen") or {}
    snapshot = resumen.get("snapshot_listado") or {}

    tipo = (tipo_operacion.get("tipo") or doc.get("tipo") or snapshot.get("tipo") or
            (doc.get("metadata") or {}).get("tipo_propiedad") or "")
    operacion = ("Venta" if tipo_operacion.get("venta") else "Arriendo"
                 if tipo_operacion.get("arriendo") else snapshot.get("operacion") or doc.get("operacion") or "")
    comuna = ubicacion.get("comuna") or snapshot.get("comuna") or doc.get("comuna") or ""
    region = ubicacion.get("region") or doc.get("region") or ""
    sector = ubicacion.get("sector") or ""
    descripcion = observaciones.get("descripcion") or doc.get("descripcion") or ""
    titulo = observaciones.get("titulo") or doc.get("titulo") or ""

    partes = []
    if titulo:
        partes.append(titulo)
    if tipo:
        partes.append(tipo)
    if operacion:
        partes.append(operacion)
    if comuna:
        partes.append(f"en {comuna}")
    if region:
        partes.append(region)
    if sector:
        partes.append(sector)

    t_norm = (tipo or "").upper()
    es_sitio = any(k in t_norm for k in ("SITIO", "PARCELA", "TERRENO"))
    es_local = any(k in t_norm for k in ("LOCAL", "OFICINA", "BODEGA", "INDUSTRIAL", "ESTACIONAMIENTO"))
    es_casa = any(k in t_norm for k in ("CASA", "DEPARTAMENTO"))

    def _campo(key):
        v = caracteristicas.get(key)
        try:
            if v is None or float(v) == 0:
                return None
        except (TypeError, ValueError):
            pass
        return v

    if es_sitio:
        for f in ("superficie_terreno", "superficie_total"):
            v = _campo(f)
            if v:
                partes.append(f"{v} m2 de terreno")
        if _campo("ano_construccion"):
            partes.append(f"año {_campo('ano_construccion')}")
    elif es_local:
        for f in ("superficie_util", "superficie_construida"):
            v = _campo(f)
            if v:
                partes.append(f"{v} m2")
        if _campo("banos"):
            partes.append(f"{_campo('banos')} baños")
        if _campo("numero_pisos"):
            partes.append(f"{_campo('numero_pisos')} pisos")
    else:
        if es_casa or not es_local:
            if _campo("dormitorios"):
                partes.append(f"{_campo('dormitorios')} dormitorios")
            if _campo("banos"):
                partes.append(f"{_campo('banos')} baños")
        for f in ("superficie_util", "superficie_construida", "superficie_terreno", "superficie_total"):
            v = _campo(f)
            if v:
                partes.append(f"{v} m2")
        if _campo("orientacion"):
            partes.append(f"orientación {_campo('orientacion')}")
        if _campo("numero_pisos"):
            partes.append(f"{_campo('numero_pisos')} pisos")
        if _campo("ano_construccion"):
            partes.append(f"año {_campo('ano_construccion')}")

    extras = _extra_features_text(caracteristicas)
    if extras:
        partes.append(extras)

    if descripcion:
        partes.append(descripcion)

    texto = " | ".join(p for p in partes if p)
    return clean_desc_for_embedding(texto)


# --- GENERACIÓN DE VECTORES ---
def generate_embedding(text: str) -> Optional[List[float]]:
    """
    Genera el vector para un texto dado usando FastEmbed.
    Retorna None si el modelo no está disponible (modo sin embeddings).
    """
    model = get_model()
    if not model or not text:
        return None
    try:
        embeddings_generator = model.embed([text])
        vector = next(embeddings_generator).tolist()
        return vector
    except Exception as e:
        logger.error(f"Error generando embedding con FastEmbed: {e}")
        return None


# --- PROCESO BULK (Para correr OFFLINE / script local solamente) ---
def update_embeddings_bulk(batch_size=100):
    """
    Busca propiedades sin vector o con vector desactualizado y regenera.
    Soportes ambos esquemas:
      - Esquema plano heredado (descripcion / descripcion_clean).
      - Esquema Prop360 anidado (observaciones.descripcion, tipo_operacion, ...).
    SOLO usar este método desde run_embeddings.py (script local/offline).
    NO llamar desde el servidor de producción.
    """
    db = get_db()
    collection = db[Config.COLLECTION_NAME]

    # 1. Asegurar limpieza primero (solo esquema plano)
    pending_clean = collection.count_documents({
        "descripcion": {"$exists": True},
        "descripcion_clean": {"$exists": False}
    })

    if pending_clean > 0:
        logger.info(f"Limpiando {pending_clean} descripciones pendientes...")
        cursor = collection.find(
            {"descripcion": {"$exists": True}, "descripcion_clean": {"$exists": False}}
        ).limit(1000)
        ops_clean = []
        for doc in cursor:
            clean_text = clean_desc_for_embedding(doc.get("descripcion"))
            if clean_text:
                ops_clean.append(UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {"descripcion_clean": clean_text}}
                ))
        if ops_clean:
            collection.bulk_write(ops_clean, ordered=False)
            logger.info(f"Limpieza completada para {len(ops_clean)} docs.")

    # 2. Generar Embeddings: docs sin vector con datos (esquema plano o Prop360)
    query = {
        "vector_descripcion": {"$exists": False},
        "$or": [
            {"descripcion_clean": {"$exists": True, "$ne": ""}},
            {"observaciones.descripcion": {"$exists": True, "$nin": ["", None]}},
        ],
    }
    total_pending = collection.count_documents(query)
    logger.info(f"Docs pendientes de embedding: {total_pending}")

    if total_pending == 0:
        return 0

    cursor = collection.find(query).limit(batch_size)
    ops = []

    if not get_model():
        logger.error("[BULK] Modelo no disponible. Abortando generación de embeddings.")
        return 0

    for doc in cursor:
        if doc.get("descripcion_clean"):
            desc = doc.get("descripcion_clean", "")
            amenities = doc.get("amenities", "")
            tipo = doc.get("tipo", "")
            comuna = doc.get("comuna", "")
            text_to_embed = f"{desc} | {tipo} | {comuna} | {amenities}".strip()
        else:
            text_to_embed = build_embedding_text(doc)
        if not text_to_embed:
            continue
        vector = generate_embedding(text_to_embed)
        if vector:
            ops.append(UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {"vector_descripcion": vector}}
            ))

    if ops:
        res = collection.bulk_write(ops, ordered=False)
        logger.info(f"Embeddings generados: {res.modified_count}")
        return res.modified_count

    return 0
