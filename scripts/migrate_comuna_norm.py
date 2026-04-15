import sys
import os
import time
from datetime import datetime

# Añadir directorio raíz al path para importar módulos locales
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from api_captacion import normalize_commune
from chatbot.storage import get_db

def migrate_comunas():
    """
    Script de migración incremental para normalizar comunas en yapo_propiedades.
    Procesa en lotes para evitar saturar la base de datos.
    """
    db = get_db()
    collection = db["yapo_propiedades"]
    
    # Buscar registros que no tengan comuna_norm o donde sea nulo
    query = {
        "$or": [
            {"details.comuna_norm": {"$exists": False}},
            {"details.comuna_norm": None},
            {"details.comuna_norm": ""}
        ]
    }
    
    total_to_process = collection.count_documents(query)
    print(f"[*] Iniciando migración de {total_to_process} documentos...")
    
    processed = 0
    batch_size = 500
    
    while True:
        # Traer un lote de documentos
        batch = list(collection.find(query, {"details.comuna": 1}).limit(batch_size))
        
        if not batch:
            break
            
        print(f"[>] Procesando lote de {len(batch)} documentos... ({processed}/{total_to_process})")
        
        for doc in batch:
            details = doc.get("details", {})
            comuna_raw = details.get("comuna")
            
            if comuna_raw:
                comuna_norm = normalize_commune(comuna_raw)
                collection.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"details.comuna_norm": comuna_norm}}
                )
        
        processed += len(batch)
        print(f"[OK] Lote completado. Esperando 1s para throttling...")
        time.sleep(1) # Throttling para no saturar IO/CPU en planes básicos
        
    print(f"[FIN] Migración finalizada. Se procesaron {processed} documentos.")

if __name__ == "__main__":
    try:
        migrate_comunas()
    except Exception as e:
        print(f"[ERROR] Error durante la migración: {e}")
