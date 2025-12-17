# chatbot/storage.py
from pymongo import MongoClient
from datetime import datetime
from config import Config
from typing import List, Dict, Optional

_mongo_client = None

def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000)
    return _mongo_client[Config.DB_NAME]

COLLECTION_CONVERSATIONS = "conversaciones_whatsapp"

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
            "$push": {"messages": {"$each": [message], "$slice": -30}}, # Aumenté un poco el historial
            "$setOnInsert": {"created_at": datetime.utcnow().isoformat() + "Z"}
        },
        upsert=True
    )

def obtener_conversacion(phone: str) -> List[Dict]:
    db = get_db()
    doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone}, {"messages": 1})
    if not doc:
        return []
    return doc.get("messages", [])

def obtener_prospecto(phone: str) -> dict:
    db = get_db()
    doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone})
    if not doc:
        return {}
    return doc.get("prospecto", {})

def actualizar_prospecto(phone: str, datos: dict):
    if not datos:
        return

    # Validación defensiva de nombre
    if "nombre" in datos:
        nombre = str(datos.get("nombre", "")).strip()
        if len(nombre.split()) > 5 or len(nombre) < 2:
            del datos["nombre"] # No guardar si parece basura
        else:
            datos["nombre"] = nombre.title()

    db = get_db()
    update_fields = {"$set": {}}

    for key, value in datos.items():
        if value not in [None, "", "desconocido"]:
            update_fields["$set"][f"prospecto.{key}"] = str(value).strip()

    if update_fields["$set"]:
        db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            update_fields,
            upsert=True
        )

def establecer_nombre_usuario(phone: str, nombre: str):
    actualizar_prospecto(phone, {"nombre": nombre})

# ==========================================
# NUEVA FUNCIÓN: REGISTRAR PROPIEDADES VISTAS
# ==========================================
def registrar_propiedades_vistas(phone: str, nuevos_codigos: List[str]):
    """
    Agrega códigos de propiedades a la lista 'vistas' para no repetirlas.
    Usa $addToSet para evitar duplicados en la lista.
    """
    if not nuevos_codigos:
        return
    
    db = get_db()
    db[COLLECTION_CONVERSATIONS].update_one(
        {"phone": phone},
        {"$addToSet": {"prospecto.propiedades_vistas": {"$each": nuevos_codigos}}},
        upsert=True
    )

def obtener_propiedades_vistas(phone: str) -> List[str]:
    """Retorna la lista de códigos que ya se le recomendaron al usuario."""
    p = obtener_prospecto(phone)
    return p.get("propiedades_vistas", [])