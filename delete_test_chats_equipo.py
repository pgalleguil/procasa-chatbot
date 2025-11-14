#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Elimina mensajes de prueba (equipo) de la colección "chats"

from pymongo import MongoClient
from dotenv import load_dotenv
import os

# === Cargar configuración ===
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

if not MONGO_URI or not DB_NAME:
    raise ValueError("Faltan variables MONGO_URI o DB_NAME en el archivo .env")

# === Conexión a la base ===
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
chats = db["chats"]

print(f"[LOG] Conectado a base: {DB_NAME} / colección: chats")

# === Números del equipo (con nombres) ===
MANUAL_NUMEROS = {
    "+56983219804": "Pablo",
    "+56990152481": "Pablo",
    "+56940904971": "Jorge Pablo",
    "+56940474465": "María Paz",
    "+56961892120": "Raquel",
    "+56991951317": "Erika",
    "+56941829185": "Marcela",
    "+56991788250": "Mariela",
    "+56939125978": "Susana",
}

def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    phone = str(phone).replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone
    return phone

manual_normalizados = [normalize_phone(n) for n in MANUAL_NUMEROS.keys()]

# === Verificar coincidencias ===
count = chats.count_documents({"phone": {"$in": manual_normalizados}})
print(f"[CHECK] Se encontraron {count} chats con teléfonos del equipo en 'chats'.")

if count > 0:
    print("\nDetalle de números a eliminar:")
    for num in manual_normalizados:
        print(f" - {num}: {MANUAL_NUMEROS[num]}")
    
    confirm = input("\n¿Eliminar esos registros? (s/n): ").strip().lower()
    if confirm in ["s", "si", "y", "yes"]:
        result = chats.delete_many({"phone": {"$in": manual_normalizados}})
        print(f"\n[CLEANUP] ✅ Eliminados {result.deleted_count} documentos de prueba.")
    else:
        print("\n❌ Operación cancelada.")
else:
    print("[INFO] No hay coincidencias.")
