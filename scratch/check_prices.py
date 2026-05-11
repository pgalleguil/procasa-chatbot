
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
universo = db["universo_cartera"]

doc = universo.find_one({"codigo": "5695"})
print("--- CAMPOS DE PRECIO 5695 ---")
if doc:
    for k, v in doc.items():
        if "precio" in k.lower() or "valor" in k.lower() or "uf" in k.lower():
            print(f"{k}: {v} ({type(v)})")
else:
    print("No se encontró el documento")
