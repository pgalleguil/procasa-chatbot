
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
    
    # Caso 1: Formato "13.677,01" (Chileno típico)
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    # Caso 2: Formato "13677.01" (Americano/Python)
    elif '.' in s and ',' not in s:
        # No hacemos nada, el float() lo entenderá
        pass
    # Caso 3: Formato "13.677" (Sin decimales, punto como miles?)
    elif '.' in s and ',' not in s:
        # Si el punto está cerca del final (2 o 1 dígito), es decimal.
        # Pero si es una tasación de UF, 13.677 es más probable que sea 13 mil que 13 pesos.
        # Para UF, si el valor es > 1000 y tiene un punto, es probable que sea miles si no tiene más de 3 decimales?
        # En realidad, si viene de Python str(), 13.677 es trece punto algo.
        pass
    # Caso 4: Formato "13,677" (Coma como decimal)
    elif ',' in s and '.' not in s:
        s = s.replace(',', '.')

    try:
        return float(s)
    except:
        return 0

def extract_suggested_price(text):
    if not text: return None
    # Patrones específicos para Propiteq
    patterns = [
        r"(?:valor comercial estimado de|valor sugerido|precio sugerido|tasación estimada|valor de)\s*(?:UF|CLP)?\s*([\d\.,]+)",
        r"([\d\.,]+)\s*UF",
        r"UF\s*([\d\.,]+)"
    ]
    for p in patterns:
        matches = re.findall(p, text, re.IGNORECASE)
        for m in matches:
            val = clean_number(m)
            if 500 < val < 200000: # Filtro razonable para propiedades en UF
                return val
    return None

exitosas = list(tasaciones.find({"status": "exito_informe_completo"}))
results = []

for doc in exitosas:
    codigo = doc.get('codigo_propiedad')
    prop_orig = universo.find_one({"codigo": codigo})
    if not prop_orig: continue
    
    precio_actual = clean_number(prop_orig.get('precio_uf'))
    # Si el precio actual es muy bajo (<100), intentamos precio_ppal
    if precio_actual < 100:
        precio_actual = clean_number(prop_orig.get('precio_ppal'))
    
    precio_sugerido = extract_suggested_price(doc.get('analisis_ia', ''))
    if not precio_sugerido:
        precio_sugerido = extract_suggested_price(doc.get('resumen_ejecutivo', ''))
        
    if precio_actual > 100 and precio_sugerido and precio_sugerido > 100:
        diff = precio_actual - precio_sugerido
        pct = (diff / precio_actual) * 100
        
        if pct > 5: # Más del 5% de sobreprecio
            results.append({
                "codigo": codigo,
                "comuna": doc.get('comuna'),
                "precio_actual": precio_actual,
                "precio_sugerido": precio_sugerido,
                "diff_uf": diff,
                "pct": pct,
                "propietario": prop_orig.get('nombre_propietario', 'N/A'),
                "email": prop_orig.get('email', 'N/A'),
                "telefono": prop_orig.get('telefono', 'N/A'),
                "ejecutivo": prop_orig.get('ejecutivo', 'N/A')
            })

results.sort(key=lambda x: x['pct'], reverse=True)

print(f"Propiedades analizadas: {len(exitosas)}")
print(f"Propiedades con sobreprecio (>5%): {len(results)}")
print("-" * 130)
print(f"{'CÓDIGO':<8} | {'COMUNA':<15} | {'ACTUAL (UF)':>11} | {'SUGERIDO':>10} | {'DIF %':>6} | {'PROPIETARIO':<25} | {'EJECUTIVO'}")
print("-" * 130)

for r in results:
    print(f"{r['codigo']:<8} | {r['comuna'][:15]:<15} | {r['precio_actual']:>11.0f} | {r['precio_sugerido']:>10.0f} | {r['pct']:>5.1f}% | {r['propietario'][:25]:<25} | {r['ejecutivo']}")
