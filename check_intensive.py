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

    query_all = {
        "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"},
        "region": {"$regex": "Arica", "$options": "i"}
    }
    
    docs = list(cartera.find(query_all))
    
    found_obelix = 0
    # cross using different fields
    for doc in docs:
        ids_to_try = [
            doc.get("codigo_procasa"),
            doc.get("codigo_pi"),
            doc.get("codigo"),
            doc.get("codigo_original")
        ]
        
        o = None
        for id_val in ids_to_try:
            if not id_val: continue
            # find by exact matching int or str on `codigo`, `codigo_procasa`, `id_propiedad`
            o = obelix.find_one({
                "$or": [
                    {"codigo": id_val},
                    {"codigo": int(id_val) if str(id_val).isdigit() else id_val},
                    {"codigo": str(id_val)},
                    {"codigo_procasa": id_val},
                    {"codigo_pi": id_val}
                ]
            })
            if o:
                break
                
        if o:
            found_obelix += 1
            region = str(o.get("region", ""))
            print(f"Matched Obelix for doc {doc.get('codigo_procasa')} / {doc.get('codigo')}, region in Obelix: {region}")
    
    print(f"Found {found_obelix} out of {len(docs)}")

if __name__ == '__main__':
    main()
