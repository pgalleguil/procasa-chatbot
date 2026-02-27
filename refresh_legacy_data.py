import asyncio
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from config import Config

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)

async def refresh_legacy_data():
    """
    Identifica registros en 'yapo_propiedades' que no tienen los nuevos campos de BI
    (lat, lon, sector, etc.) y los re-encola en 'yapo_queue' para su re-procesamiento.
    """
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]
    queue_coll = db["yapo_queue"]

    logging.info("🔍 Buscando registros antiguos sin datos de BI...")

    # Buscamos registros donde detalles.lat no existe o es "N/A"
    # (Usamos 'lat' como indicador de que el registro es antiguo/incompleto)
    cursor = coll.find({
        "$or": [
            {"details.lat": {"$exists": False}},
            {"details.lat": "N/A"},
            {"details.vendedor_id": {"$exists": False}}
        ]
    })

    count_new = 0
    count_already_queued = 0
    async for doc in cursor:
        url = doc.get("url")
        if not url:
            continue

        # Verificar si ya está en la cola y en qué estado
        q_doc = await queue_coll.find_one({"url": url})
        
        if q_doc and q_doc.get("status") == "pending":
            count_already_queued += 1
            continue

        # Resetear el estado en la cola para que el scraper principal lo recoja
        result = await queue_coll.update_one(
            {"url": url},
            {
                "$set": {
                    "status": "pending",
                    "retries": 0,
                    "refresh_reason": "missing_bi_fields"
                }
            },
            upsert=True
        )
        
        if result.modified_count > 0 or result.upserted_id:
            count_new += 1
            if count_new % 50 == 0:
                logging.info(f"🔄 Re-encolando registros antiguos... ({count_new})")

    logging.info(f"✅ Análisis de cola finalizado.")
    logging.info(f"   - {count_new} registros fueron movidos a 'pending'.")
    logging.info(f"   - {count_already_queued} registros ya estaban en espera (desde la ejecución anterior).")
    logging.info("🚀 Ahora puedes ejecutar 'scraping_yapo_proxys.py' para actualizar los datos.")
    
    client.close()

if __name__ == "__main__":
    asyncio.run(refresh_legacy_data())
