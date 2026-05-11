
import os
from pymongo import MongoClient
from dotenv import load_dotenv

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

print(f"--- RESUMEN DE TASACIONES ---")
total_tasaciones = tasaciones.count_documents({})
exitosas = tasaciones.count_documents({"status": "exito_informe_completo"})
no_tasables = tasaciones.count_documents({"status": "no_tasable"})
errores = tasaciones.count_documents({"status": {"$regex": "error"}})

print(f"Total registros: {total_tasaciones}")
print(f"Exitosas: {exitosas}")
print(f"No tasables: {no_tasables}")
print(f"Errores: {errores}")

print("\n--- MUESTRA DE RESULTADOS EXITOSOS (ÚLTIMOS 3) ---")
for doc in tasaciones.find({"status": "exito_informe_completo"}).sort("timestamp", -1).limit(3):
    print(f"\nPropiedad: {doc.get('codigo_propiedad')}")
    print(f"Comuna: {doc.get('comuna')}")
    # Mostrar una parte del análisis IA para ver cómo extraer el precio
    analisis = doc.get('analisis_ia', '')
    print(f"Resumen IA (primeros 200 caps): {analisis[:200]}...")
    
    # Buscar info en el universo para comparar precio
    prop_orig = universo.find_one({"codigo": doc.get('codigo_propiedad')})
    if prop_orig:
        precio_actual = prop_orig.get('precio_uf')
        print(f"Precio Actual (UF): {precio_actual}")
