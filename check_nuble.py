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
    
    match_obelix = 0
    nuble_count = 0
    bio_bio_count = 0
    
    for doc in docs:
        cp = doc.get("codigo_procasa")
        if cp:
            # Let's search in obelix by int or str
            o = obelix.find_one({"codigo": cp})
            if not o:
                o = obelix.find_one({"codigo": int(cp) if str(cp).isdigit() else cp})
            if not o:
                o = obelix.find_one({"codigo": str(cp)})
            
            if o:
                match_obelix += 1
                region = str(o.get('region', '')).lower()
                print(f"Prop {cp} matches Obelix region: {region}")
                if "ñuble" in region or "nuble" in region:
                    nuble_count += 1
                if "bio" in region or "bío" in region:
                    bio_bio_count += 1

    print(f"Total: {len(docs)}")
    print(f"Matched with Obelix: {match_obelix}")
    print(f"Region Nuble in Obelix: {nuble_count}")
    print(f"Region Bio Bio in Obelix: {bio_bio_count}")

if __name__ == '__main__':
    main()
