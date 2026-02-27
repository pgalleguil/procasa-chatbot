import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from bson import ObjectId

async def diagnose_mismatch():
    load_dotenv()
    uri = os.getenv("MONGO_URI")
    client = AsyncIOMotorClient(uri)
    db = client["URLS"]
    queue = db["yapo_queue"]
    props = db["yapo_propiedades"]
    
    print("--- DIAGNÓSTICO DE DATOS ---")
    
    # 1. Verificar tipos de ID en yapo_queue
    q_sample = await queue.find_one({})
    if q_sample:
        print(f"ID en yapo_queue: {q_sample['_id']} (Tipo: {type(q_sample['_id'])})")
    
    # 2. Verificar tipos de ID en yapo_propiedades
    p_sample = await props.find_one({})
    if p_sample:
        print(f"ID en yapo_propiedades: {p_sample['_id']} (Tipo: {type(p_sample['_id'])})")
    
    # 3. Mismatch por tipo de ID
    # Si yapo_queue tiene Strings y yapo_propiedades tiene ObjectIds, el $in fallará.
    str_ids_in_props = await props.count_documents({"_id": {"$type": "string"}})
    obj_ids_in_props = await props.count_documents({"_id": {"$type": "objectId"}})
    print(f"Propiedades con ID String: {str_ids_in_props}")
    print(f"Propiedades con ID ObjectId: {obj_ids_in_props}")

    # 4. Verificar colisiones de Content Hash
    cursor = props.aggregate([
        {"$group": {"_id": "$details.content_hash", "unique_urls": {"$addToSet": "$url"}, "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$limit": 5}
    ])
    collisions = await cursor.to_list(length=5)
    if collisions:
        print("\n--- COLISIONES DE CONTENT HASH (Mismo contenido, distinta URL) ---")
        for c in collisions:
            print(f"Hash: {c['_id']} | URLs: {len(c['unique_urls'])}")
            for url in c['unique_urls']:
                print(f"  -> {url}")
    else:
        print("\nNo hay colisiones de content_hash (Cada hash es una url única).")

    # 5. URLs en cola que SI están en propiedades (con conversión de tipo si es necesario)
    all_q_ids = await queue.distinct("_id")
    # Intentar buscar un subconjunto
    sample_q_ids = all_q_ids[:100]
    # Convertir a ObjectId los que parezcan serlo
    potential_obj_ids = []
    for qid in sample_q_ids:
        if isinstance(qid, str) and len(qid) == 24:
            try: potential_obj_ids.append(ObjectId(qid))
            except: pass
            
    found_as_str = await props.count_documents({"_id": {"$in": sample_q_ids}})
    found_as_obj = await props.count_documents({"_id": {"$in": potential_obj_ids}})
    
    print(f"\nMuestra de 100 IDs de la cola:")
    print(f"  - Encontrados en propiedades como String: {found_as_str}")
    print(f"  - Encontrados en propiedades como ObjectId: {found_as_obj}")

    client.close()

if __name__ == "__main__":
    asyncio.run(diagnose_mismatch())
