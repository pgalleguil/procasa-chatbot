import sys
import pandas as pd

def main():
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    try:
        with open(excel_path, 'r', encoding='latin-1', errors='replace') as f:
            html_content = f.read()
            
        dfs = pd.read_html(html_content)
        df = dfs[0]
        
        # Find the row that contains 'Región' or 'codigo'
        header_idx = -1
        for idx, row in df.iterrows():
            row_str = " ".join([str(x).lower() for x in row.values])
            if 'regi' in row_str and ('odigo' in row_str or 'cód' in row_str):
                header_idx = idx
                break
                
        if header_idx != -1:
            df.columns = df.iloc[header_idx]
            df = df[header_idx+1:].reset_index(drop=True)
            
            print("Columns:", len(df.columns), df.columns.tolist()[:10])
            
            reg_col = next((c for c in df.columns if 'regi' in str(c).lower()), None)
            cod_col = next((c for c in df.columns if 'códig' in str(c).lower() or 'codigo' in str(c).lower()), None)
            
            if reg_col and cod_col:
                # Find Bio Bio ones in Excel
                bio_df = df[df[reg_col].astype(str).str.contains("bio|bío", case=False, na=False)]
                nuble_df = df[df[reg_col].astype(str).str.contains("uble|ñuble", case=False, na=False)]
                arica_df = df[df[reg_col].astype(str).str.contains("Arica", case=False, na=False)]
                
                print(f"Bio Bio in excel: {len(bio_df)}")
                print(f"Nuble in excel: {len(nuble_df)}")
                print(f"Arica in excel: {len(arica_df)}")
                
                if len(bio_df) > 0:
                    bio_codes = bio_df[cod_col].dropna().astype(str).tolist()
                    print(f"CÓDIGOS REGION BIO BIO (primeros 30): {bio_codes[:30]}")
                    
                    # Also cross check with universo_cartera to see if they exist
                    from pymongo import MongoClient
                    from config import Config
                    client = MongoClient(Config.MONGO_URI)
                    cartera = client[Config.DB_NAME]["universo_cartera"]
                    
                    to_update = []
                    for code in bio_codes:
                        code = str(code).replace('.0', '').strip()
                        # Buscar en cartera
                        doc = cartera.find_one({
                            "$or": [
                                {"codigo_procasa": code}, 
                                {"codigo_procasa": int(code) if code.isdigit() else code},
                                {"codigo": code},
                                {"codigo": int(code) if code.isdigit() else code}
                            ],
                            "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"}
                        })
                        if doc:
                            to_update.append(doc)
                            
                    print(f"De esos {len(bio_df)}, encontré {len(to_update)} en 'universo_cartera' para PROCASA SUCRE")
                    if to_update:
                        print("Muestra de códigos a actualizar:")
                        for d in to_update[:5]:
                            print(f"- {d.get('codigo_procasa')} (Región actual: {d.get('region')})")
                            
                        # Here we would do the actual update if we want to preview it for the user first.
    except Exception as e:
        print("Error:", e)

if __name__ == '__main__':
    main()
