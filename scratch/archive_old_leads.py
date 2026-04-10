import sys
import os
sys.path.append(os.getcwd())
from pymongo import MongoClient
from config import Config
from bson import ObjectId
from datetime import datetime
from chatbot.constants import CHILE_TZ

def archive_problematic_leads():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    leads_col = db["leads"]
    
    target_ids = [
        "695873fc3bfdb6de3d842708",
        "695c09cb3bfdb6de3d8439b4",
        "695c29173bfdb6de3d843a99"
    ]
    
    print(f"Iniciando limpieza manual de leads...")
    
    for lid in target_ids:
        try:
            query_id = ObjectId(lid)
            res = leads_col.update_one(
                {"_id": query_id},
                {"$set": {
                    "stage": "ARCHIVED",
                    "archive_reason": "Limpieza manual: Lead antiguo y propiedad no disponible",
                    "last_processed_at": datetime.now(CHILE_TZ).isoformat()
                }}
            )
            if res.modified_count > 0:
                print(f"  [OK] Lead {lid} archivado.")
            else:
                print(f"  [SKIP] Lead {lid} ya estaba procesado o no se encontró.")
        except Exception as e:
            print(f"  [ERROR] Error procesando lead {lid}: {e}")

    print("\nBuscando otros leads antiguos (>90 días) sin asignar...")
    # Opcional: Limpieza masiva de leads muy viejos que podrían estar causando ruido
    # (Similar a la lógica que agregamos al service, pero para ejecución inmediata)
    from datetime import timedelta
    limit_date = datetime.now(CHILE_TZ) - timedelta(days=90)
    
    # Buscamos leads creados antes de limit_date que no tengan ejecutivo o cluster_id
    query = {
        "stage": {"$ne": "ARCHIVED"},
        "$or": [
            {"created_at": {"$lt": limit_date.isoformat()}},
            {"timestamp": {"$lt": limit_date.isoformat()}}
        ],
        "$or": [
            {"ejecutivo_asignado": {"$in": [None, "", "No Asignado"]}},
            {"cluster_id": {"$in": [None, ""]}}
        ]
    }
    
    others = leads_col.update_many(
        query,
        {"$set": {
            "stage": "ARCHIVED",
            "archive_reason": "Limpieza automática masiva: Antigüedad > 90 días"
        }}
    )
    print(f"  [DONE] Otros leads archivados masivamente: {others.modified_count}")

if __name__ == "__main__":
    archive_problematic_leads()
