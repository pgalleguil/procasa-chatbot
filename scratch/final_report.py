
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
    # Si es string, quitar puntos de miles y cambiar coma por punto decimal
    s = str(val).strip().replace('.', '')
    s = s.replace(',', '.')
    try:
        return float(s)
    except:
        return 0

def extract_suggested_price(text):
    if not text: return None
    # Patrones comunes en el análisis de Propiteq
    # Ejemplo: "valor comercial estimado de UF 15.400"
    patterns = [
        r"(?:valor comercial estimado de|valor sugerido|precio sugerido|tasación estimada|valor de)\s*(?:UF|CLP)?\s*([\d\.]+)",
        r"([\d\.]+)\s*UF",
        r"UF\s*([\d\.]+)"
    ]
    for p in patterns:
        matches = re.findall(p, text, re.IGNORECASE)
        for m in matches:
            val = clean_number(m)
            if 100 < val < 200000: # Rango razonable para UF
                return val
    return None

exitosas = list(tasaciones.find({"status": "exito_informe_completo"}))
results = []

for doc in exitosas:
    codigo = doc.get('codigo_propiedad')
    prop_orig = universo.find_one({"codigo": codigo})
    if not prop_orig: continue
    
    precio_actual = clean_number(prop_orig.get('precio_uf'))
    precio_sugerido = extract_suggested_price(doc.get('analisis_ia', ''))
    if not precio_sugerido:
        precio_sugerido = extract_suggested_price(doc.get('resumen_ejecutivo', ''))
        
    if precio_actual > 0 and precio_sugerido and precio_sugerido > 0:
        diff = precio_actual - precio_sugerido
        pct = (diff / precio_actual) * 100
        
        # Guardar si hay sobreprecio
        if pct > 2: # Más del 2%
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
                "vendedor": prop_orig.get('ejecutivo', 'N/A')
            })

results.sort(key=lambda x: x['pct'], reverse=True)

print(f"Propiedades procesadas con éxito: {len(exitosas)}")
print(f"Propiedades identificadas con sobreprecio: {len(results)}")
print("-" * 120)
print(f"{'CÓDIGO':<8} | {'COMUNA':<15} | {'ACTUAL':>8} | {'SUGERIDO':>8} | {'DIF %':>6} | {'PROPIETARIO':<25} | {'EMAIL'}")
print("-" * 120)

for r in results[:30]:
    print(f"{r['codigo']:<8} | {r['comuna'][:15]:<15} | {r['precio_actual']:>8.0f} | {r['precio_sugerido']:>8.0f} | {r['pct']:>5.1f}% | {r['propietario'][:25]:<25} | {r['email']}")
