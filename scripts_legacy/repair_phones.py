import asyncio
import logging
from yapo_contact_extractor import main as extractor_main
from motor.motor_asyncio import AsyncIOMotorClient
import sys
import os

# Add parent dir to path for config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

async def run_repair():
    logging.info("🚀 Iniciando REPARACIÓN de 1,601 leads en riesgo...")
    
    # 1. Marcar los leads en riesgo para que el extractor los tome (limpiando el teléfono corrupto)
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db['yapo_propiedades']
    
    query = {
        "details.whatsapp_phone": {"$exists": True, "$ne": ""},
        "metadata.phone_extraction_source": {"$exists": False}
    }
    
    # IMPORTANTE: Reseteamos el teléfono a vacío para que 'yapo_contact_extractor.py' los vea como pendientes
    result = await coll.update_many(
        query,
        {"$set": {"details.whatsapp_phone": "", "status": "pending_repair"}}
    )
    
    logging.info(f"🧹 Reseteados {result.modified_count} leads para re-extracción.")
    client.close()
    
    # 2. Ejecutar el extractor con límites específicos
    if result.modified_count > 0:
        # Definimos argumentos para el extractor: 
        # --concurrency 2 (más seguro para evitar bloqueos)
        # --limit 100 (podemos ir por partes para no quemar proxies si el usuario prefiere)
        # Aquí permitimos que corra sobre los que acabamos de resetear
        sys.argv = [sys.argv[0], "--concurrency", "2", "--limit", str(result.modified_count)]
        await extractor_main()
    else:
        logging.info("✨ No hay leads que requieran reparación.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    asyncio.run(run_repair())
