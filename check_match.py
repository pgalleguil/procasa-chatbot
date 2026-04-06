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
                print(f"Match for {cp}! Obelix region: {o.get('region')}")
                # check if nuble
                if "nuble" in str(o.get('region')).lower() or "ñuble" in str(o.get('region')).lower():
                    print("--> IT IS NUBLE")
            else:
                pass
                # print(f"No match in obelix for {cp}")

if __name__ == '__main__':
    main()
