import logging
import sys
import os

# Add parent directory to path to import chatbot modules if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.storage import get_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def cleanup_property_codes():
    """
    Busca documentos en universo_obelix donde el codigo tiene comillas literales
    (ej: "'64342'") y las limpia.
    """
    db = get_db()
    collection = db["universo_obelix"]
    
    # Buscar documentos que empiezan y terminan con comilla simple
    # Nota: En MongoDB, esto se puede hacer con regex
    query = {"codigo": {"$regex": "^'.*'$"}}
    
    docs = list(collection.find(query))
    logger.info(f"Encontrados {len(docs)} documentos con comillas literales en 'codigo'.")
    
    updates = 0
    for doc in docs:
        old_code = doc["codigo"]
        new_code = old_code.strip("'")
        
        # Verificar si quitando las comillas ya existe otro documento con ese código
        # para evitar duplicados si la DB ya tiene ambos (poco probable pero posible)
        exists = collection.find_one({"codigo": new_code, "_id": {"$ne": doc["_id"]}})
        
        if exists:
            logger.warning(f"No se puede renombrar '{old_code}' a '{new_code}' porque ya existe otro documento con ese código.")
            continue
            
        result = collection.update_one(
            {"_id": doc["_id"]},
            {"$set": {"codigo": new_code}}
        )
        
        if result.modified_count > 0:
            logger.info(f"Actualizado: {old_code} -> {new_code}")
            updates += 1
            
    logger.info(f"Limpieza completada. Total actualizados: {updates}")

if __name__ == "__main__":
    cleanup_property_codes()
