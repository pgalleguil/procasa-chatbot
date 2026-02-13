import os
import sys

# Add parent directory to path since config is there
sys.path.append(os.getcwd())

from pymongo import MongoClient
from config import Config

def investigate_erika():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    print("--- Searching in 'usuarios' ---")
    users = list(db["usuarios"].find({"nombre": {"$regex": "Erika", "$options": "i"}}))
    for u in users:
        print(f"User: {u.get('username')}, Name: {u.get('nombre')}, Role: {u.get('rol')}")

    print("\n--- Searching in 'universo_obelix' (Executives) ---")
    execs = db["universo_obelix"].distinct("ejecutivo")
    erikas = [e for e in execs if e and "Erika" in e]
    print(f"Erika variations found: {erikas}")
    
    for e in erikas:
        count = db["universo_obelix"].count_documents({"ejecutivo": e})
        print(f"Properties assigned to '{e}': {count}")

    print("\n--- Searching in leads ---")
    lead_counts = db["leads"].aggregate([
        {"$match": {"ejecutivo_asignado": {"$regex": "Erika", "$options": "i"}}},
        {"$group": {"_id": "$ejecutivo_asignado", "count": {"$sum": 1}}}
    ])
    for lc in lead_counts:
        print(f"Leads assigned to '{lc['_id']}': {lc['count']}")

if __name__ == "__main__":
    investigate_erika()
