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
    
    corrections = []
    
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
            obelix_region = str(o.get("region", ""))
            
            # Map obelix region to strict names
            clean_region = "Desconocida"
            lower_reg = obelix_region.lower()
            if not lower_reg.strip():
                continue
                
            if "metropolitana" in lower_reg:
                clean_region = "Región Metropolitana"
            elif "maule" in lower_reg:
                clean_region = "Región del Maule"
            elif "araucan" in lower_reg:
                clean_region = "Región de la Araucanía"
            elif "bio" in lower_reg or "bío" in lower_reg or "ñuble" in lower_reg or "nuble" in lower_reg:
                clean_region = "Región Bío-Bío"
            else:
                clean_region = obelix_region.strip()
                
            corrections.append({
                "_id": doc["_id"],
                "codigo": doc.get("codigo_procasa") or doc.get("codigo"),
                "obelix_raw": obelix_region,
                "proposed": clean_region,
                "disponible": doc.get('disponible')
            })

    print("Correcciones Propuestas:")
    for c in corrections:
        status_text = "Activo" if c["disponible"] else "Inactivo"
        print(f"Prop {c['codigo']} ({status_text}) | Obelix dice: '{c['obelix_raw']}' -> Cambiar a: '{c['proposed']}'")
        
if __name__ == '__main__':
    main()
