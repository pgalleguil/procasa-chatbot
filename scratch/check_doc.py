
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

doc = universo.find_one({"codigo": "5695"}) # Vitacura
print("--- DOCUMENTO ORIGINAL 5695 ---")
for k, v in doc.items():
    if k != "_id":
        print(f"{k}: {v}")
