
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
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'procasa\s*\w*', '', text)
    text = re.sub(r'c[óo]digo\s*\w*', '', text)
    text = re.sub(r'(\d{7,})', '', text)
    tokens = text.split()
    clean_tokens = [t for t in tokens if t not in STOPWORDS and len(t) > 2]
    return " ".join(clean_tokens)


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
    SOLO usar este método desde run_embeddings.py (script local/offline).
    NO llamar desde el servidor de producción.
    """
    db = get_db()
    collection = db[Config.COLLECTION_NAME]

    # 1. Asegurar limpieza primero
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

    # 2. Generar Embeddings
    query = {
        "descripcion_clean": {"$exists": True, "$ne": ""},
        "vector_descripcion": {"$exists": False}
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
        desc = doc.get("descripcion_clean", "")
        amenities = doc.get("amenities", "")
        tipo = doc.get("tipo", "")
        comuna = doc.get("comuna", "")
        text_to_embed = f"{desc} | {tipo} | {comuna} | {amenities}".strip()
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
