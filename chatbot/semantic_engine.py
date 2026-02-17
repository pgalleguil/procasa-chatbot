
# chatbot/semantic_engine.py
import re
import logging
from typing import Optional, List
from fastembed import TextEmbedding
import pymongo
from pymongo import UpdateOne
from config import Config
from .storage import get_db

logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN ---
# Modelo ligero y rápido (~80MB), ideal para español/inglés y CPU.
# Usamos FastEmbed para reducir RAM (Render 512MB limit)
MODEL_NAME = "BAAI/bge-small-en-v1.5" # Modelo por defecto de FastEmbed, muy similar en tamaño y mejor performance

_model_instance = None

def get_model():
    """Singleton para cargar el modelo solo una vez usando FastEmbed."""
    global _model_instance
    if _model_instance is None:
        logger.info(f"Cargando modelo de embeddings (FastEmbed): {MODEL_NAME} ...")
        try:
            # FastEmbed carga el modelo de forma eficiente
            _model_instance = TextEmbedding(model_name=MODEL_NAME)
            logger.info("Modelo FastEmbed cargado exitosamente.")
        except Exception as e:
            logger.error(f"FATAL: No se pudo cargar el modelo FastEmbed {MODEL_NAME}: {e}")
            return None
    return _model_instance

# --- LIMPIEZA DE TEXTO (Adaptado de tu script limpieza_descripcion.py) ---
STOPWORDS = {
    'vende', 'arrienda', 'propiedad', 'cod', 'codigo', 'interno', 'procasa', 
    'oficina', 'contacto', 'fono', 'llamar', 'excelente', 'oportunidad',
    'el', 'la', 'los', 'las', 'un', 'una', 'de', 'del', 'y', 'o', 'a', 'en', 'para', 'por',
    'con', 'su', 'sus', 'es', 'son', 'al', 'lo', 'se', 'que'
}

def clean_desc_for_embedding(text: Optional[str]) -> str:
    if not text:
        return ""
    
    # 1. Minúsculas y normalización básica
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text) # Quitar puntuación
    
    # 2. Quitar códigos y patrones basura comunes
    text = re.sub(r'procasa\s*\w*', '', text)
    text = re.sub(r'c[óo]digo\s*\w*', '', text)
    text = re.sub(r'(\d{7,})', '', text) # Teléfonos largos
    
    # 3. Tokenización y Stopwords
    tokens = text.split()
    clean_tokens = [t for t in tokens if t not in STOPWORDS and len(t) > 2]
    
    # 4. Reconstrucción
    return " ".join(clean_tokens)

# --- GENERACIÓN DE VECTORES ---
def generate_embedding(text: str) -> Optional[List[float]]:
    """Genera el vector para un texto dado usando FastEmbed."""
    model = get_model()
    if not model or not text:
        return None
    
    try:
        # FastEmbed model.embed devuelve un generador de vectores
        # Usamos list() o next() para obtener el vector
        embeddings_generator = model.embed([text])
        vector = next(embeddings_generator).tolist()
        return vector
    except Exception as e:
        logger.error(f"Error generando embedding con FastEmbed: {e}")
        return None

# --- PROCESO BULK (Para correr en background o script) ---
def update_embeddings_bulk(batch_size=100):
    """
    Busca propiedades sin vector o con vector desactualizado y regenera.
    """
    db = get_db()
    collection = db[Config.COLLECTION_NAME]
    
    # Buscar docs que tengan descripcion_clean pero NO vector_descripcion
    # O que tengan descripcion pero no descripcion_clean (primero limpiamos)
    
    # 1. Asegurar limpieza primero
    pending_clean = collection.count_documents({
        "descripcion": {"$exists": True},
        "descripcion_clean": {"$exists": False}
    })
    
    if pending_clean > 0:
        logger.info(f"Limpiando {pending_clean} descripciones pendientes...")
        cursor = collection.find({"descripcion": {"$exists": True}, "descripcion_clean": {"$exists": False}}).limit(1000)
        ops_clean = []
        for doc in cursor:
            clean_text = clean_desc_for_embedding(doc.get("descripcion"))
            if clean_text:
                ops_clean.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"descripcion_clean": clean_text}}))
        
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
    
    # Cargar modelo explícitamente antes del loop
    if not get_model():
        return 0
        
    for doc in cursor:
        # Mejora Profesional: Concatenar metadata para que el vector sea más rico
        desc = doc.get("descripcion_clean", "")
        amenities = doc.get("amenities", "")
        tipo = doc.get("tipo", "")
        comuna = doc.get("comuna", "")
        
        # Texto técnico enriquecido
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
