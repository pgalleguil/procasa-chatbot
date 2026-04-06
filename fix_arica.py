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

    # Filter PROCASA SUCRE and Arica y Parinacota, active
    query = {
        "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"},
        "region": {"$regex": "Arica", "$options": "i"},
        "disponible": True
    }
    
    docs = list(cartera.find(query))
    found_count = len(docs)
    print(f"Total propiedades activas encontradas en universo_cartera (SUCRE, Arica): {found_count}")
    
    modificadas = 0
    obelix_not_found = 0
    not_nuble = 0

    for doc in docs:
        codigo_procasa = doc.get("codigo_procasa")
        if not codigo_procasa:
            # fallback to 'codigo_pi' or 'codigo' just in case?
            codigo_procasa = doc.get("codigo_pi")
            
        if not codigo_procasa:
            continue

        # buscar en obelix
        # obelix might have the field as `codigo_procasa`, `codigo`, etc. Let's try `codigo` or `codigo_procasa`.
        obelix_doc = obelix.find_one({"$or": [{"codigo_procasa": codigo_procasa}, {"codigo": codigo_procasa}]})
        
        if obelix_doc:
            obelix_region = obelix_doc.get("region", "")
            if isinstance(obelix_region, str) and re.search(r"ñuble|nuble", obelix_region, re.IGNORECASE):
                # Es de ñuble en obelix! Modificar la región en universo_cartera
                cartera.update_one({"_id": doc["_id"]}, {"$set": {"region": "Región Bío-Bío"}})
                modificadas += 1
            else:
                not_nuble += 1
                # print(f"Obelix doc {codigo_procasa} tiene región diferente: {obelix_region}")
        else:
            obelix_not_found += 1
            # print(f"No se encontró el código {codigo_procasa} en universo_obelix")

    print(f"Propiedades encontradas inicialmente (activas en Arica): {found_count}")
    print(f"Propiedades modificadas exitosamente a 'Región Bío-Bío': {modificadas}")
    print(f"Propiedades en Obelix pero sin región Ñuble: {not_nuble}")
    print(f"Propiedades no encontradas en Obelix: {obelix_not_found}")

if __name__ == '__main__':
    main()
