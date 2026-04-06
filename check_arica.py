import sys
import os
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config
from pymongo import MongoClient

def main():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    cartera = db["universo_cartera"]

    # Filter PROCASA SUCRE and Arica y Parinacota, active
    query = {
        "oficina": {"$regex": "SUCRE", "$options": "i"},
        "region": {"$regex": "Arica y Parinacota|Arica", "$options": "i"},
        # guessing we should look for active ones, maybe `status` or we just output the whole list
    }
    docs = list(cartera.find(query))
    print(f"Found {len(docs)} documents matching basic query")
    if docs:
        print("Example document keys:", docs[0].keys())
        print("Example document region:", docs[0].get("region"))
        print("Example document office:", docs[0].get("oficina"))
        print("Example document status fields:")
        for k in ["estado", "status", "disponible", "vigente", "activo"]:
            if k in docs[0]:
                print(f"  {k}: {docs[0][k]}")

if __name__ == '__main__':
    main()
