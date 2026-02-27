from pymongo import MongoClient
import os
import sys

# Agregamos la ruta principal para importar Config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]

# Find duplicated phones
pipeline = [
    {"$group": {"_id": "$phone", "count": {"$sum": 1}, "docs": {"$push": "$_id"}}},
    {"$match": {"count": {"$gt": 1}, "_id": {"$ne": None}}}
]

dupes = list(db["leads"].aggregate(pipeline))
print(f"Buscando duplicados... Encontrados {len(dupes)} números telefónicos con múltiples tarjetas.")

merged_count = 0
deleted_count = 0

for dupe in dupes:
    phone = dupe["_id"]
    docs = list(db["leads"].find({"phone": phone}).sort("created_at", 1)) # Del más antiguo al más nuevo
    
    if len(docs) <= 1:
        continue
    
    print(f"\nProcesando {phone} con {len(docs)} tarjetas:")
    
    # El principal será el último (el más reciente, que suele tener la última intención/propiedad)
    # O el que tenga más mensajes. Vamos a juntar los mensajes.
    
    target_doc = docs[-1]
    target_id = target_doc["_id"]
    
    all_messages = []
    propiedades_vistas_set = set()
    
    for doc in docs:
        all_messages.extend(doc.get("messages", []))
        vistas = doc.get("prospecto", {}).get("propiedades_vistas", [])
        if isinstance(vistas, list):
            for v in vistas: propiedades_vistas_set.add(str(v))
            
    # Ordenar todos los mensajes por timestamp
    def get_msg_time(m):
        return m.get("timestamp", "")
        
    all_messages.sort(key=get_msg_time)
    
    # Quitar duplicados
    unique_messages = []
    seen = set()
    for m in all_messages:
        hashable = (m.get("role"), m.get("content"), m.get("timestamp"))
        if hashable not in seen:
            seen.add(hashable)
            unique_messages.append(m)
            
    # Actualizar target
    db["leads"].update_one(
        {"_id": target_id},
        {
            "$set": {
                "messages": unique_messages,
                "prospecto.propiedades_vistas": list(propiedades_vistas_set)
            }
        }
    )
    
    # Borrar los demás
    ids_to_delete = [d["_id"] for d in docs if d["_id"] != target_id]
    if ids_to_delete:
        db["leads"].delete_many({"_id": {"$in": ids_to_delete}})
        deleted_count += len(ids_to_delete)
        merged_count += 1
        print(f"  -> Fusionado en {target_id}. {len(ids_to_delete)} tarjetas antiguas eliminadas.")

print(f"\n--- HOMOGENEIZACIÓN COMPLETA ---")
print(f"Contactos unificados exitosamente: {merged_count}")
print(f"Tarjetas sobrantes eliminadas: {deleted_count}")
