import asyncio
import os
import re
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from bson import ObjectId

def extract_id_from_url(url: str) -> str:
    if not url: return ""
    match = re.search(r'(\d{7,11})', url)
    return match.group(1) if match else ""

async def migrate_and_reset():
    load_dotenv()
    uri = os.getenv("MONGO_URI")
    client = AsyncIOMotorClient(uri)
    db = client["URLS"]
    queue = db["yapo_queue"]
    props = db["yapo_propiedades"]
    
    print("--- INICIANDO MIGRACIÓN DE DATOS ---")
    
    # 1. Migrar yapo_propiedades: ObjectId -> String ID
    migrated = 0
    deleted_orphans = 0
    
    cursor = props.find({"_id": {"$type": "objectId"}})
    async for doc in cursor:
        url = doc.get("url")
        prop_id = extract_id_from_url(url)
        
        if prop_id:
            try:
                # Upsert con el ID correcto
                old_id = doc.pop("_id")
                await props.update_one(
                    {"_id": prop_id},
                    {"$set": doc},
                    upsert=True
                )
                # Eliminar el registro viejo con ObjectId
                await props.delete_one({"_id": old_id})
                migrated += 1
            except Exception as e:
                # Si falla por duplicado, es que ya existe la versión corregida
                # o hay colisión de URL. En cualquier caso, borramos la versión ObjectId
                await props.delete_one({"_id": doc.get("_id", old_id)})
                deleted_orphans += 1
        else:
            # Si no tiene ID de Yapo (raro), borrarlo para limpiar
            await props.delete_one({"_id": doc["_id"]})
            deleted_orphans += 1

    print(f"  - Propiedades migradas a ID String: {migrated}")
    print(f"  - Registros huérfanos eliminados: {deleted_orphans}")

    # 2. Corregir IDs en yapo_queue si hubiera ObjectIds (aunque el diagnóstico dijo que son Strings)
    res_q = await queue.delete_many({"_id": {"$type": "objectId"}})
    print(f"  - Registros ObjectId eliminados de yapo_queue: {res_q.deleted_count}")

    # 3. RESET DE COLA: Volver todo a 'pending'
    # Solo reseteamos los que NO fallaron por error crítico (opcional, aquí reseteamos todo para limpieza total)
    res_reset = await queue.update_many(
        {}, 
        {"$set": {"status": "pending", "retries": 0}}
    )
    print(f"  - Cola reseteada: {res_reset.modified_count} URLs listas para re-scrapear.")

    print("\n✅ MIGRACIÓN COMPLETADA. El scraper ahora debería encontrar todo correctamente.")
    client.close()

if __name__ == "__main__":
    asyncio.run(migrate_and_reset())
