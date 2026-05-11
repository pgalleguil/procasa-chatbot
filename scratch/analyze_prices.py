
import os
import sys
import re
from pymongo import MongoClient
from dotenv import load_dotenv

# Asegurar salida en UTF-8 para evitar errores en Windows
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Configuración manual de rutas
BASE_DIR = r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok"
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "URLS")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
tasaciones = db["tasaciones"]
universo = db["universo_cartera"]

def extract_price(text):
    if not text: return None
    # Buscar patrones como "UF 15.000", "15.000 UF", "valor de UF 12.500"
    # Buscamos números asociados a UF
    match = re.search(r'(?:UF\s*|unidades de fomento\s*|valor de\s*)(\d{1,3}(?:\.\d{3})*(?:,\d+)?)', text, re.IGNORECASE)
    if not match:
        match = re.search(r'(\d{1,3}(?:\.\d{3})*(?:,\d+)?)\s*UF', text, re.IGNORECASE)
    
    if match:
        val_str = match.group(1).replace('.', '').replace(',', '.')
        try:
            return float(val_str)
        except:
            return None
    return None

print(f"--- ANÁLISIS DE RESULTADOS ---")
exitosas = list(tasaciones.find({"status": "exito_informe_completo"}))
print(f"Propiedades con tasación exitosa: {len(exitosas)}")

reporte_candidatos = []

for doc in exitosas:
    codigo = doc.get('codigo_propiedad')
    analisis = doc.get('analisis_ia', '')
    resumen = doc.get('resumen_ejecutivo', '')
    
    # Intentar extraer precio sugerido del análisis de la IA
    precio_sugerido = extract_price(analisis)
    if not precio_sugerido:
        precio_sugerido = extract_price(resumen)
        
    prop_orig = universo.find_one({"codigo": codigo})
    if prop_orig:
        precio_actual = prop_orig.get('precio_uf')
        # Limpiar precio actual si es string
        if isinstance(precio_actual, str):
             precio_actual = float(precio_actual.replace('.', '').replace(',', '.')) if precio_actual else 0
        
        diff = 0
        if precio_actual and precio_sugerido:
            diff = precio_actual - precio_sugerido
            percentage = (diff / precio_actual) * 100 if precio_actual > 0 else 0
            
            # Si el precio actual es mayor al sugerido por más de un 5%, es candidato
            if percentage > 5:
                reporte_candidatos.append({
                    "codigo": codigo,
                    "comuna": doc.get('comuna'),
                    "precio_actual": precio_actual,
                    "precio_sugerido": precio_sugerido,
                    "sobreprecio_uf": diff,
                    "porcentaje": percentage,
                    "contacto": prop_orig.get('nombre_propietario', 'N/A'),
                    "telefono": prop_orig.get('telefono', 'N/A'),
                    "email": prop_orig.get('email', 'N/A')
                })

# Ordenar por mayor porcentaje de sobreprecio
reporte_candidatos.sort(key=lambda x: x['porcentaje'], reverse=True)

print(f"\nSe encontraron {len(reporte_candidatos)} propiedades con sobreprecio significativo (>5%).")
print("-" * 100)
print(f"{'CÓDIGO':<8} | {'COMUNA':<15} | {'ACTUAL':<8} | {'SUGERIDO':<8} | {'DIF %':<6} | {'CONTACTO'}")
print("-" * 100)

for c in reporte_candidatos[:20]: # Mostrar top 20
    print(f"{c['codigo']:<8} | {c['comuna'][:15]:<15} | {c['precio_actual']:>8.0f} | {c['precio_sugerido']:>8.0f} | {c['porcentaje']:>5.1f}% | {c['contacto']}")

# También contar errores por tipo
print("\n--- RESUMEN DE ERRORES ---")
pipeline = [
    {"$match": {"status": {"$regex": "error"}}},
    {"$group": {"_id": "$status", "count": {"$sum": 1}}}
]
for res in tasaciones.aggregate(pipeline):
    print(f"{res['_id']}: {res['count']}")
