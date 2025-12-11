# chatbot/storage.py → VERSIÓN FINAL CORREGIDA (sin errores)
from pymongo import MongoClient
from datetime import datetime
from config import Config
from typing import List, Dict, Optional

# Cliente Mongo con conexión persistente
_mongo_client = None
def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000)
    return _mongo_client[Config.DB_NAME]

# ← AQUÍ ESTABA EL ERROR (acento)
COLLECTION_CONVERSATIONS = "conversaciones_whatsapp"   # ← SIN ACENTO

def guardar_mensaje(phone: str, role: str, content: str, metadata: dict = None):
    db = get_db()
    message = {
        "role": role,
        "content": str(content),
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    if metadata:
        message.update(metadata)

    db[COLLECTION_CONVERSATIONS].update_one(
        {"phone": phone},
        {
            "$push": {"messages": {"$each": [message], "$slice": -20}},
            "$setOnInsert": {"created_at": datetime.utcnow().isoformat() + "Z"}
        },
        upsert=True
    )

def obtener_conversacion(phone: str) -> List[Dict]:
    db = get_db()
    doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone})
    if not doc or "messages" not in doc:
        return []
    safe_messages = []
    for msg in doc["messages"]:
        safe_msg = {
            "role": msg["role"],
            "content": str(msg["content"])
        }
        safe_messages.append(safe_msg)
    return safe_messages

def obtener_nombre_usuario(phone: str) -> Optional[str]:
    db = get_db()
    doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone})
    return doc.get("user_name") if doc else None

def establecer_nombre_usuario(phone: str, nombre: str):
    db = get_db()
    db[COLLECTION_CONVERSATIONS].update_one(
        {"phone": phone},
        {"$set": {"user_name": nombre.strip().title()}},
        upsert=True
    )

def actualizar_datos_prospecto(phone: str, datos: dict):
    """
    Actualiza campos estructurados del prospecto de forma incremental.
    datos: dict con claves como rut, nombre, email, codigo_procasa, portal_origen, etc.
    """
    if not datos:
        return
    db = get_db()
    update_fields = {}
    for key, value in datos.items():
        if value not in [None, "", "desconocido"]:
            update_fields[f"prospecto.{key}"] = str(value).strip()
    
    if update_fields:
        db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {"$set": update_fields},
            upsert=True
        )

def obtener_datos_prospecto(phone: str) -> dict:
    """Devuelve los datos estructurados del prospecto"""
    db = get_db()
    doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone})
    return doc.get("prospecto", {}) if doc else {}


###################################

def actualizar_prospecto(phone: str, datos: dict):
    """Almacena datos estructurados del prospecto FUERA de messages para análisis fáciles"""
    if not datos:
        return
    db = get_db()
    update_fields = {"$set": {}}
    for key, value in datos.items():
        if value:
            update_fields["$set"][f"prospecto.{key}"] = str(value).strip()
    
    if update_fields["$set"]:
        db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            update_fields,
            upsert=True
        )

def obtener_prospecto(phone: str) -> dict:
    db = get_db()
    doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone})
    return doc.get("prospecto", {}) if doc else {}