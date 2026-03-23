
from pymongo import MongoClient
from config import Config

def check_assignments():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    pipeline = [
        {"$match": {"gestion.ejecutivo_asignado": {"$ne": None}}},
        {"$group": {"_id": "$gestion.ejecutivo_asignado", "count": {"$sum": 1}}}
    ]
    res = list(db["yapo_propiedades"].aggregate(pipeline))
    print("ASIGNACIONES ACTUALES:")
    for r in res:
        print(f"- {r['_id']}: {r['count']}")

if __name__ == "__main__":
    check_assignments()
