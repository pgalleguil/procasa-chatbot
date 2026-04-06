import sys
import pandas as pd
from pymongo import MongoClient
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config

def main():
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    df = pd.read_excel(excel_path, engine='xlrd', header=1)
    
    for idx, row in df.iterrows():
        row_str = " ".join([str(x).lower() for x in row.values])
        if 'regi' in row_str and ('odigo' in row_str or 'cód' in row_str):
            df.columns = df.iloc[idx]
            df = df[idx+1:]
            break
            
    cod_col = next((c for c in df.columns if 'odigo' in str(c).lower() or 'cód' in str(c).lower()), None)
    
    if not cod_col:
        print("Couldn't find codigo")
        return
        
    codes = df[cod_col].dropna().astype(str).tolist()
    codes = [c.replace('.0', '').strip() for c in codes if c.strip() and c.strip().lower() != 'nan']
    print(f"Total codes in excel: {len(codes)}")
    
    client = MongoClient(Config.MONGO_URI)
    cartera = client[Config.DB_NAME]["universo_cartera"]
    
    # Let's count how many we find in cartera across ALL offices
    docs = list(cartera.find({"codigo_procasa": {"$in": codes}}))
    print(f"Found {len(docs)} matching properties by codigo_procasa in universo_cartera")
    
    # Are there any Nuble / BioBio that we need to update?
    reg_col = next((c for c in df.columns if 'regi' in str(c).lower()), None)
    
    corrections = 0
    for _, row in df.iterrows():
        code = str(row[cod_col]).replace('.0', '').strip()
        region_excel = str(row[reg_col]).strip()
        
        doc = cartera.find_one({"codigo_procasa": code, "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"}})
        if doc:
            current_region = doc.get("region", "")
            # If they don't match, or if excel says BioBio
            if region_excel != current_region:
                # check Nuble/BioBio
                if "bio" in region_excel.lower() or "bío" in region_excel.lower() or "uble" in region_excel.lower():
                    corrections += 1
                    
    print(f"Propiedades en Excel que necesitan corrección a BioBio en SUCRE: {corrections}")

if __name__ == '__main__':
    main()
