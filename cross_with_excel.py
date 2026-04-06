import sys
import pandas as pd
from pymongo import MongoClient
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config

def main():
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    df = pd.read_excel(excel_path, engine='xlrd', header=1)
    
    # Find columns
    for idx, row in df.iterrows():
        row_str = " ".join([str(x).lower() for x in row.values])
        if 'regi' in row_str and ('odigo' in row_str or 'cód' in row_str):
            df.columns = df.iloc[idx]
            df = df[idx+1:]
            break
            
    reg_col = next((c for c in df.columns if 'regi' in str(c).lower()), None)
    cod_col = next((c for c in df.columns if 'odigo' in str(c).lower() or 'cód' in str(c).lower()), None)
    
    # Prepare excel dictionary map
    excel_map = {}
    for _, row in df.iterrows():
        code = str(row[cod_col]).replace('.0', '').strip()
        region = str(row[reg_col]).strip()
        excel_map[code] = region
        
    client = MongoClient(Config.MONGO_URI)
    cartera = client[Config.DB_NAME]["universo_cartera"]
    
    # Get all properties currently in Arica for PROCASA SUCRE
    docs = list(cartera.find({
        "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"},
        "region": {"$regex": "Arica", "$options": "i"} 
    }))
    
    print(f"Total properties in Arica in MongoDB: {len(docs)}")
    
    found_in_excel = 0
    to_update = []
    
    for doc in docs:
        codes = [
            str(doc.get("codigo_procasa", "")),
            str(doc.get("codigo", "")),
            str(doc.get("codigo_pi", ""))
        ]
        codes = [c.replace('.0', '').strip() for c in codes if c]
        
        region_excel = None
        for c in codes:
            if c in excel_map:
                region_excel = excel_map[c]
                break
                
        if region_excel:
            found_in_excel += 1
            print(f"Prop {codes[0]} in Excel is: {region_excel}")
            
            # Map Excel region to standard if needed
            clean_region = region_excel
            if "bio" in region_excel.lower() or "bío" in region_excel.lower() or "uble" in region_excel.lower() or "ñuble" in region_excel.lower():
                clean_region = "Región Bío-Bío"
            elif "metropolitana" in region_excel.lower():
                clean_region = "Región Metropolitana"
            elif "maule" in region_excel.lower():
                clean_region = "Región del Maule"
            elif "arauc" in region_excel.lower():
                clean_region = "Región de la Araucanía"
                
            to_update.append((doc, clean_region))
            
    print(f"Encontramos {found_in_excel} de las propiedades problemáticas de Arica en el Excel provisto.")
    
    # Do the update!
    if to_update:
        print("Actualizando según Excel...")
        for doc, reg in to_update:
            cartera.update_one({"_id": doc["_id"]}, {"$set": {"region": reg}})
        print(f"Actualizadas {len(to_update)} propiedades guiadas por el Excel")

if __name__ == '__main__':
    main()
