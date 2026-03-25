# chatbot/storage.py
from pymongo import MongoClient
from datetime import datetime
import pytz
from config import Config
from typing import List, Dict, Optional
from .constants import PipelineStage, InteractionType, EventType, CHILE_TZ
import logging

logger = logging.getLogger(__name__)

_mongo_client = None

def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000)
    return _mongo_client[Config.DB_NAME]

COLLECTION_CONVERSATIONS = "leads"
COLLECTION_PENDING_NOTIFICATIONS = "pending_notifications"

def guardar_mensaje(phone: str, role: str, content: str, metadata: dict = None):
    db = get_db()
    # Usamos hora de Chile para consistencia visual en DB
    now = datetime.now(CHILE_TZ)
    message = {
        "role": role,
        "content": str(content),
        "timestamp": now.isoformat()
    }
    if metadata:
        message.update(metadata)

    db[COLLECTION_CONVERSATIONS].update_one(
        {"phone": phone},
        {
            "$push": {"messages": {"$each": [message], "$slice": -50}}, # Historial más largo
            "$setOnInsert": {"created_at": now.isoformat()}
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
            del datos["nombre"]
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

def obtener_bot_pausado(phone: str) -> bool:
    db = get_db()
    doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone}, {"is_paused": 1})
    if not doc:
        return False
    return doc.get("is_paused", False)

def toggle_bot_pausado(phone: str) -> bool:
    """Toggles and returns the new state"""
    db = get_db()
    current_state = obtener_bot_pausado(phone)
    new_state = not current_state
    db[COLLECTION_CONVERSATIONS].update_one(
        {"phone": phone},
        {"$set": {"is_paused": new_state}}
    )
    return new_state

def registrar_propiedades_vistas(phone: str, nuevos_codigos: List[str]):
    if not nuevos_codigos: return
    db = get_db()
    nuevos_codigos = [str(c).strip() for c in nuevos_codigos if c]
    
    try:
        db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {"$addToSet": {"prospecto.propiedades_vistas": {"$each": nuevos_codigos}}},
            upsert=True
        )
    except Exception as e:
        # Si falla porque el campo no es un array (ej: es un string), lo convertimos
        logger.warning(f"[STORAGE] Re-intentando registro de propiedades por conflicto de tipo: {e}")
        doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone}, {"prospecto.propiedades_vistas": 1})
        current = (doc or {}).get("prospecto", {}).get("propiedades_vistas")
        
        if isinstance(current, str):
            final_list = list(set([current] + nuevos_codigos))
        elif isinstance(current, list):
            final_list = list(set(current + nuevos_codigos))
        else:
            final_list = nuevos_codigos
            
        db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {"$set": {"prospecto.propiedades_vistas": final_list}},
            upsert=True
        )

def obtener_propiedades_vistas(phone: str) -> List[str]:
    p = obtener_prospecto(phone)
    return p.get("propiedades_vistas", [])

# ==========================================
# NOTIFICACIONES PENDIENTES
# ==========================================

def save_pending_notification(lead_data: dict):
    db = get_db()
    notification = {
        "lead_data": lead_data,
        "created_at": datetime.now(CHILE_TZ).isoformat(),
        "status": "pending",
        "attempts": 0
    }
    db[COLLECTION_PENDING_NOTIFICATIONS].insert_one(notification)

def get_pending_notifications():
    db = get_db()
    return list(db[COLLECTION_PENDING_NOTIFICATIONS].find({"status": "pending"}))

def mark_notification_sent(notification_id):
    db = get_db()
    db[COLLECTION_PENDING_NOTIFICATIONS].update_one(
        {"_id": notification_id},
        {"$set": {"status": "sent", "sent_at": datetime.now(CHILE_TZ).isoformat()}}
    )

def delete_pending_notification(notification_id):
    db = get_db()
    db[COLLECTION_PENDING_NOTIFICATIONS].delete_one({"_id": notification_id})

# ==========================================
# EVENT LOG & PIPELINE
# ==========================================

def log_event(phone: str, event_type: str, actor: str = "system", meta: dict = None):
    db = get_db()
    event = {
        "phone": str(phone).replace("+", "").strip(),
        "timestamp": datetime.now(CHILE_TZ).isoformat(),
        "type": event_type,
        "actor": actor,
        "meta": meta or {}
    }
    db["crm_events"].insert_one(event)
    
    # Precomputación SaaS: Actualizar métricas del lead atómicamente
    try:
        from .metrics import update_lead_metrics
        update_lead_metrics(db, phone, event_at=event["timestamp"], event_type=event_type)
    except Exception as e:
        logger.error(f"Error triggering metrics update in log_event: {e}")

def update_lead_state(phone: str, stage: str = None, metadata: dict = None):
    db = get_db()
    update_data = {}
    if stage:
        update_data["stage"] = stage
    
    ts = datetime.now(CHILE_TZ).isoformat()
    # Mapeo de timestamps automáticos por stage
    if stage == PipelineStage.CONTACTED:
        update_data["lifecycle.first_response_at"] = ts
    elif stage == PipelineStage.VISIT_SCHEDULED:
        update_data["lifecycle.visit_scheduled_at"] = ts
    elif stage == PipelineStage.CLOSED_WON:
        update_data["lifecycle.closed_at"] = ts
    
    if metadata:
        for k, v in metadata.items():
            update_data[k] = v

    if update_data:
        db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {"$set": update_data, "$setOnInsert": {"lifecycle.created_at": ts}},
            upsert=True
        )
        if stage:
            log_event(phone, InteractionType.STATUS_CHANGE, "system", {"to": stage})
        else:
            # Si no hay cambio de estado pero hay metadatos, forzamos refresco de métricas
            try:
                from .metrics import update_lead_metrics
                update_lead_metrics(db, phone)
            except: pass

def get_user_by_phone(phone: str) -> Optional[dict]:
    """Busca un usuario en la colección 'usuarios' por su teléfono (normalizado)."""
    if not phone:
        return None
    db = get_db()
    # Normalizamos el teléfono para la búsqueda (quitamos + y espacios)
    phone_clean = str(phone).replace("+", "").replace(" ", "").strip()
    
    # Buscamos por teléfono con regex para ser flexibles
    return db["usuarios"].find_one({
        "telefono": {"$regex": phone_clean}
    })
