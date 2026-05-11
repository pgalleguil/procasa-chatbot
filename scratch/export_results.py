
import os
import sys
import re
from pymongo import MongoClient
from dotenv import load_dotenv

if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE_DIR = r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok"
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "URLS")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
tasaciones = db["tasaciones"]
universo = db["universo_cartera"]

def clean_number(val):
    if val is None: return 0
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip()
    if not s: return 0
    if ',' in s and '.' in s: s = s.replace('.', '').replace(',', '.')
    elif ',' in s: s = s.replace(',', '.')
    try: return float(s)
    except: return 0

def extract_suggested_price(text):
    if not text: return None
    patterns = [r"(?:valor comercial estimado de|valor sugerido|precio sugerido|tasación estimada|valor de)\s*(?:UF|CLP)?\s*([\d\.,]+)", r"([\d\.,]+)\s*UF", r"UF\s*([\d\.,]+)"]
    for p in patterns:
        matches = re.findall(p, text, re.IGNORECASE)
        for m in matches:
            val = clean_number(m)
            if 500 < val < 200000: return val
    return None

exitosas = list(tasaciones.find({"status": "exito_informe_completo"}))
results = []

for doc in exitosas:
    codigo = doc.get('codigo_propiedad')
    prop_orig = universo.find_one({"codigo": codigo})
    if not prop_orig: continue
    
    precio_actual = clean_number(prop_orig.get('precio_uf'))
    if precio_actual < 100: precio_actual = clean_number(prop_orig.get('precio_ppal'))
    
    precio_sugerido = extract_suggested_price(doc.get('analisis_ia', ''))
    if not precio_sugerido: precio_sugerido = extract_suggested_price(doc.get('resumen_ejecutivo', ''))
        
    if precio_actual > 100 and precio_sugerido and precio_sugerido > 100:
        diff = precio_actual - precio_sugerido
        pct = (diff / precio_actual) * 100
        
        if pct > 0: # Guardar todos los que tienen algún sobreprecio
            results.append({
                "codigo": codigo,
                "comuna": doc.get('comuna'),
                "precio_actual": precio_actual,
                "precio_sugerido": precio_sugerido,
                "diff_uf": diff,
                "pct": pct,
                "propietario": prop_orig.get('nombre_propietario', 'N/A'),
                "email": prop_orig.get('email', prop_orig.get('mail', 'N/A')),
                "telefono": prop_orig.get('telefono', 'N/A'),
                "vendedor": prop_orig.get('ejecutivo', 'N/A')
            })

results.sort(key=lambda x: x['pct'], reverse=True)

import json
with open(os.path.join(BASE_DIR, 'scratch', 'full_results.json'), 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=4)

print(f"Total procesados: {len(exitosas)}")
print(f"Total con sobreprecio: {len(results)}")
