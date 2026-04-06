import sys
import pandas as pd
import time
from pymongo import MongoClient
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config

def main():
    print("Starting reading excel via xlrd...")
    start = time.time()
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    
    try:
        # Load the file skipping the first few lines to get the real header
        # Based on previous HTML experience, maybe header is row 1
        df = pd.read_excel(excel_path, engine='xlrd', header=1)
        print(f"Read Excel in {time.time()-start:.1f}s. Shape: {df.shape}")
        
        # Check columns
        reg_col = next((c for c in df.columns if 'regi' in str(c).lower()), None)
        cod_col = next((c for c in df.columns if 'odigo' in str(c).lower() or 'cód' in str(c).lower()), None)
        
        if not reg_col or not cod_col:
            print(f"Could not find exact columns, let's search via index")
            print("Columns are:", df.columns.tolist()[:10])
            for idx, row in df.iterrows():
                row_str = " ".join([str(x).lower() for x in row.values])
                if 'regi' in row_str and ('odigo' in row_str or 'cód' in row_str):
                    df.columns = df.iloc[idx]
                    df = df[idx+1:]
                    break
            reg_col = next((c for c in df.columns if 'regi' in str(c).lower()), None)
            cod_col = next((c for c in df.columns if 'odigo' in str(c).lower() or 'cód' in str(c).lower()), None)
            
        print(f"Using {cod_col} for Code, and {reg_col} for Region")
        
        bio_codes = df[df[reg_col].astype(str).str.contains("bio|bío", case=False, na=False)][cod_col].dropna().tolist()
        nuble_codes = df[df[reg_col].astype(str).str.contains("uble|ñuble", case=False, na=False)][cod_col].dropna().tolist()
        
        target_codes = [str(c).replace('.0', '') for c in bio_codes + nuble_codes]
        print(f"Total Nuble/BioBio codes in Excel = {len(target_codes)}")
        
        client = MongoClient(Config.MONGO_URI)
        cartera = client[Config.DB_NAME]["universo_cartera"]
        
        to_update = []
        for code in set(target_codes):
            doc = cartera.find_one({
                "$or": [
                    {"codigo_procasa": code}, 
                    {"codigo_procasa": int(code) if str(code).isdigit() else code},
                    {"codigo": code},
                    {"codigo": int(code) if str(code).isdigit() else code}
                ],
                "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"},
                "region": {"$regex": "Arica", "$options": "i"} 
            })
            if doc:
                to_update.append(doc)
                
        print(f"De los {len(target_codes)} códigos de Ñuble/BioBio en Excel, encontramos {len(to_update)} actualmente erróneamente en Arica.")
        
        for d in to_update:
            cartera.update_one({"_id": d["_id"]}, {"$set": {"region": "Región Bío-Bío"}})
        
        print(f"Propiedades actualizadas a 'Región Bío-Bío': {len(to_update)}")
        
    except Exception as e:
        print("Exception occurred:", e)

if __name__ == '__main__':
    main()
