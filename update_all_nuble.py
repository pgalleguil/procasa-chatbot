import sys
import os
import re
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
    print(f"Total en universo_cartera en Arica y Parinacota: {len(docs)}")
    
    found_in_obelix = 0
    modificadas = 0

    for doc in docs:
        cp = doc.get("codigo_procasa")
        if not cp:
            cp = doc.get("codigo_pi")
            
        if not cp:
            continue
            
        o = obelix.find_one({"codigo": cp})
        if not o:
            o = obelix.find_one({"codigo": int(cp) if str(cp).isdigit() else cp})
        if not o:
            o = obelix.find_one({"codigo": str(cp)})
            
        if o:
            found_in_obelix += 1
            region = str(o.get('region', '')).lower()
            
            # Check if region is ñuble or biobio or anything else to confirm it's not Arica.
            # "buscar su region si dice ñuble confirmamos que no es de arica y cambiaras..."
            # Let's count how many say Nuble or Biobio
            if "ñuble" in region or "nuble" in region or "bio" in region or "bío" in region:
                cartera.update_one({"_id": doc["_id"]}, {"$set": {"region": "Región Bío-Bío"}})
                modificadas += 1
            else:
                pass
                # The user specifically said "si dice ñuble confirmamos... pero no podras ñuble sino Región Bío-Bío"
                # What if the user meant: "Arica is totally wrong, if you confirm that in Obelix they are Nuble/BioBio, update them"
                
    print(f"Propiedades encontradas en Obelix: {found_in_obelix}")
    print(f"Propiedades con region Nuble/Biobio modificadas: {modificadas}")

if __name__ == '__main__':
    main()
