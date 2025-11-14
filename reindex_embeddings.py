# reindex_embeddings.py
from sentence_transformers import SentenceTransformer
from db import PROPERTIES_COLLECTION, Config  # Importa tu Config y colección
import numpy as np
from tqdm import tqdm  # Para barra de progreso (pip install tqdm si no tienes)
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

config = Config()
model = SentenceTransformer(config.EMBEDDING_MODEL)
print(f"[INFO] Modelo cargado: {config.EMBEDDING_MODEL} (dims: {config.EMBEDDING_DIM})")

# Función para embed un texto (con fallback vacío)
def embed_field(text: str) -> list:
    if not text or text.strip() == "":
        return [0.0] * config.EMBEDDING_DIM  # Vector cero si vacío
    return model.encode(text).tolist()  # Lista para Mongo (serializable)

# Itera sobre todas las propiedades
total_props = PROPERTIES_COLLECTION.count_documents({})
print(f"[INFO] Total propiedades a procesar: {total_props}")

updated = 0
for prop in tqdm(PROPERTIES_COLLECTION.find({}), total=total_props, desc="Re-indexando"):
    prop_id = prop['_id']
    codigo = prop.get('codigo', 'N/A')

    # Chequea si necesita update (basado en vector_descripcion como proxy; asume los 3 tienen misma dims)
    current_vec = prop.get('vector_descripcion', [])
    needs_update = not current_vec or len(current_vec) != config.EMBEDDING_DIM

    # Genera nuevos vectores solo si necesita
    if needs_update:
        # Campo 1: vector_descripcion (de 'descripcion')
        desc_text = prop.get('descripcion', '')
        new_vec_desc = embed_field(desc_text)

        # Campo 2: vector_images (de 'image_descriptions' como array; concatena en string)
        image_descs = prop.get('image_descriptions', [])
        if isinstance(image_descs, list):
            imgs_text = ' '.join([str(desc) for desc in image_descs if desc])  # Concatena descripciones de imgs
        else:
            imgs_text = str(image_descs)
        new_vec_imgs = embed_field(imgs_text)

        # Campo 3: vector_amenities (de 'amenities_text')
        amens_text = prop.get('amenities_text', '') or prop.get('amenities_cercanos', '')  # Fallback a amenities_cercanos si no hay text
        new_vec_amens = embed_field(amens_text)

        # Update en DB (solo estos 3 campos)
        PROPERTIES_COLLECTION.update_one(
            {'_id': prop_id},
            {'$set': {
                'vector_descripcion': new_vec_desc,
                'vector_images': new_vec_imgs,
                'vector_amenities': new_vec_amens
            }}
        )
        updated += 1
        logger.info(f"Actualizado {codigo} (ID: {prop_id}) con nuevos vectores de {config.EMBEDDING_DIM} dims")

print(f"[INFO] Re-index completado: {updated}/{total_props} propiedades actualizadas.")
print(f"[INFO] Verifica en Mongo: db.universo_obelix.findOne({{'vector_descripcion':1, 'vector_images':1, 'vector_amenities':1}}). Cada array debe tener {config.EMBEDDING_DIM} elementos.")