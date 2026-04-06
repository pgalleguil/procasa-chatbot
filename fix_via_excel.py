import sys
from bs4 import BeautifulSoup

def main():
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    try:
        with open(excel_path, 'r', encoding='latin-1', errors='replace') as f:
            html_content = f.read()
            
        soup = BeautifulSoup(html_content, 'html.parser')
        rows = soup.find_all('tr')
        print(f"Total rows found: {len(rows)}")
        
        if not rows:
            return
            
        # Find header index
        header_row = None
        headers = []
        for r in rows:
            cells = r.find_all(['th', 'td'])
            text_cells = [c.get_text(strip=True).lower() for c in cells]
            if 'región' in text_cells and ('código' in text_cells or 'codigo' in text_cells):
                header_row = r
                headers = [c.get_text(strip=True) for c in cells]
                break
                
        if not headers:
            print("Could not find headers in file")
            return
            
        reg_idx = -1
        cod_idx = -1
        
        for i, h in enumerate(headers):
            if 'regi' in h.lower(): reg_idx = i
            if 'odigo' in h.lower() or 'cód' in h.lower(): cod_idx = i
            
        print(f"Indices -> Code: {cod_idx}, Region: {reg_idx}")
        
        # Parse data
        bio_codes = []
        nuble_codes = []
        
        for r in rows:
            if r == header_row:
                continue
            cells = r.find_all(['th', 'td'])
            if len(cells) > max(reg_idx, cod_idx):
                code = cells[cod_idx].get_text(strip=True)
                region = cells[reg_idx].get_text(strip=True)
                
                if "bio" in region.lower() or "bío" in region.lower():
                    bio_codes.append(code)
                if "uble" in region.lower() or "ñuble" in region.lower():
                    nuble_codes.append(code)
                    
        print(f"Found {len(bio_codes)} Bio Bio codes in excel")
        print(f"Found {len(nuble_codes)} Nuble codes in excel")
        
        from pymongo import MongoClient
        from config import Config
        client = MongoClient(Config.MONGO_URI)
        cartera = client[Config.DB_NAME]["universo_cartera"]
        
        target_codes = bio_codes + nuble_codes
        
        if target_codes:
            # Check currently in "Arica" for SUCRE
            to_update = []
            for code in set(target_codes):
                if not code.strip() or code.strip().lower() == 'nan':
                    continue
                doc = cartera.find_one({
                    "$or": [
                        {"codigo_procasa": code}, 
                        {"codigo_procasa": int(code) if code.isdigit() else code},
                        {"codigo": code},
                        {"codigo": int(code) if code.isdigit() else code}
                    ],
                    "oficina": {"$regex": "PROCASA SUCRE", "$options": "i"},
                    "region": {"$regex": "Arica", "$options": "i"} 
                })
                if doc:
                    to_update.append(doc)
            
            print(f"De los códigos BioBio/Nuble en el Excel, {len(to_update)} están actualmente marcados como 'Arica y Parinacota' en la base de datos (SUCRE).")
            
            if to_update:
                print("Actualizando a 'Región Bío-Bío' guiados por la confirmación del Excel...")
                for d in to_update:
                    cartera.update_one({"_id": d["_id"]}, {"$set": {"region": "Región Bío-Bío"}})
                print(f"Se actualizaron exitosamente {len(to_update)} propiedades.")
            else:
                # Si no encontró ninguna para actualizar que dijera Arica... tal vez buscar TODAS las que el excel dice que son Bio Bio de Arica
                pass
                
    except Exception as e:
        print("Error:", e)

if __name__ == '__main__':
    main()
