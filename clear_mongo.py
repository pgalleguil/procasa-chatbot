from motor.motor_asyncio import AsyncIOMotorClient
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def run():
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = AsyncIOMotorClient(mongo_uri)
    db = client["URLS"] # Corregido para coincidir con el scraper
    coll = db["yapo_propiedades"]
    
    # Eliminar registros donde comuna o precio sean N/A o nulos dentro de 'details'
    query = {
        "$or": [
            {"details.comuna": "N/A"},
            {"details.precio": "N/A"},
            {"details.comuna": None},
            {"details.precio": None}
        ]
    }
    
    count = await coll.count_documents(query)
    if count > 0:
        res = await coll.delete_many(query)
        print(f"OK: Se eliminaron {res.deleted_count} registros invalidos.")
    else:
        print("INFO: No hay registros invalidos para eliminar.")

if __name__ == "__main__":
    asyncio.run(run())
