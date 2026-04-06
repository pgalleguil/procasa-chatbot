import sys
import os
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config
from pymongo import MongoClient

def main():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    cartera = db["universo_cartera"]
    obelix = db["universo_obelix"]

    query = {
        "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"},
        "region": {"$regex": "Arica", "$options": "i"},
        "disponible": True
    }
    
    docs = list(cartera.find(query))
    print(f"Found {len(docs)} docs")
    if docs:
        print("First doc IDs:")
        for k in ['_id', 'codigo', 'codigo_pi', 'codigo_procasa', 'codigo_original', 'url']:
            print(f"{k}: {docs[0].get(k)}")
        
        # Let's peek into obelix
        obelix_sample = obelix.find_one()
        print("Obelix keys:", obelix_sample.keys() if obelix_sample else "No docs in obelix")
        if obelix_sample:
            for k in ['_id', 'codigo', 'codigo_procasa', 'id_propiedad']:
                print(f"Obelix {k}: {obelix_sample.get(k)}")

if __name__ == '__main__':
    main()
