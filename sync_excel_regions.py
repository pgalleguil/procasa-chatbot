import sys
import pandas as pd
from pymongo import MongoClient
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config
import time

def main():
    print("Leyendo Excel...")
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    start = time.time()
    df = pd.read_excel(excel_path, engine='xlrd', header=1)
    
    # Clean headers
    for idx, row in df.iterrows():
        row_str = " ".join([str(x).lower() for x in row.values])
        if 'regi' in row_str and ('odigo' in row_str or 'cód' in row_str):
            df.columns = df.iloc[idx]
            df = df[idx+1:]
            break
            
    cod_col = next((c for c in df.columns if 'odigo' in str(c).lower() or 'cód' in str(c).lower()), None)
    reg_col = next((c for c in df.columns if 'regi' in str(c).lower()), None)
    
    excel_map = {}
    for _, row in df.iterrows():
        code = str(row[cod_col]).replace('.0', '').strip()
        region = str(row[reg_col]).strip()
        if code and code != 'nan':
            excel_map[code] = region
            
    print(f"Diccionario Excel construido en {time.time()-start:.1f}s. Encontradas {len(excel_map)} propiedades confirmadas.")

    client = MongoClient(Config.MONGO_URI)
    cartera = client[Config.DB_NAME]["universo_cartera"]
    
    # Queremos regularizar sólo los de SUCRE
    docs = list(cartera.find({"oficina": {"$regex": "PROCASA SUCRE", "$options": "i"}}))
    print(f"Propiedades SUCRE encontradas en DB: {len(docs)}")
    
    updated = 0
    arica_fixed = 0
    
    for doc in docs:
        c1 = str(doc.get('codigo_procasa', '')).replace('.0', '').strip()
        c2 = str(doc.get('codigo', '')).replace('.0', '').strip()
        c3 = str(doc.get('codigo_pi', '')).replace('.0', '').strip()
        
        region_excel = excel_map.get(c1) or excel_map.get(c2) or excel_map.get(c3)
        current_region = str(doc.get("region", ""))
        
        if region_excel:
            clean_region = region_excel
            if "bio" in region_excel.lower() or "bío" in region_excel.lower() or "uble" in region_excel.lower() or "ñuble" in region_excel.lower():
                clean_region = "Región Bío-Bío"
            elif "metropolitana" in region_excel.lower():
                clean_region = "Región Metropolitana"
            elif "maule" in region_excel.lower():
                clean_region = "Región del Maule"
            elif "arauc" in region_excel.lower():
                clean_region = "Región de la Araucanía"
                
            if clean_region != current_region:
                if "arica" in current_region.lower():
                    arica_fixed += 1
                cartera.update_one({"_id": doc["_id"]}, {"$set": {"region": clean_region}})
                updated += 1
                
    print(f"Total propiedades regularizadas / actualizadas: {updated}")
    print(f"De esas, {arica_fixed} estaban en Arica y fueron corregidas.")

if __name__ == '__main__':
    main()
