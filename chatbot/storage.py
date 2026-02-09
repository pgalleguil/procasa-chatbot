# chatbot/storage.py
from pymongo import MongoClient
from datetime import datetime
from config import Config
from typing import List, Dict, Optional
from .constants import PipelineStage, InteractionType

_mongo_client = None

def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000)
    return _mongo_client[Config.DB_NAME]

COLLECTION_CONVERSATIONS = "leads"

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

# ==========================================
# NUEVAS FUNCIONES: NOTIFICACIONES PENDIENTES
# ==========================================
COLLECTION_PENDING_NOTIFICATIONS = "pending_notifications"

def save_pending_notification(lead_data: dict):
    """Guarda una notificación pendiente para ser procesada en horario hábil."""
    db = get_db()
    notification = {
        "lead_data": lead_data,
        "created_at": datetime.utcnow().isoformat(),
        "status": "pending",
        "attempts": 0
    }
    db[COLLECTION_PENDING_NOTIFICATIONS].insert_one(notification)

def get_pending_notifications():
    """Obtiene notificaciones pendientes."""
    db = get_db()
    return list(db[COLLECTION_PENDING_NOTIFICATIONS].find({"status": "pending"}))

def mark_notification_sent(notification_id):
    """Marca una notificación como enviada (o eliminada)."""
    db = get_db()
    db[COLLECTION_PENDING_NOTIFICATIONS].update_one(
        {"_id": notification_id},
        {"$set": {"status": "sent", "sent_at": datetime.utcnow().isoformat()}}
    )

def delete_pending_notification(notification_id):
    """Elimina una notificación (si falló o ya no es necesaria)."""
    db = get_db()
    db[COLLECTION_PENDING_NOTIFICATIONS].delete_one({"_id": notification_id})

# ==========================================
# EVENT LOG MANAGEMENT
# ==========================================

def log_event(phone: str, event_type: str, actor: str = "system", metadata: dict = None):
    """Registra un evento estructurado e inmutable en crm_events."""
    db = get_db()
    event = {
        "phone": str(phone).replace("+", "").strip(),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": event_type,
        "actor": actor,
        "metadata": metadata or {}
    }
    db["crm_events"].insert_one(event)
from datetime import datetime
from config import Config
from typing import List, Dict, Optional

_mongo_client = None

def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000)
    return _mongo_client[Config.DB_NAME]

COLLECTION_CONVERSATIONS = "leads"

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

# ==========================================
# NUEVAS FUNCIONES: NOTIFICACIONES PENDIENTES
# ==========================================
COLLECTION_PENDING_NOTIFICATIONS = "pending_notifications"

def save_pending_notification(lead_data: dict):
    """Guarda una notificación pendiente para ser procesada en horario hábil."""
    db = get_db()
    notification = {
        "lead_data": lead_data,
        "created_at": datetime.utcnow().isoformat(),
        "status": "pending",
        "attempts": 0
    }
    db[COLLECTION_PENDING_NOTIFICATIONS].insert_one(notification)

def get_pending_notifications():
    """Obtiene notificaciones pendientes."""
    db = get_db()
    return list(db[COLLECTION_PENDING_NOTIFICATIONS].find({"status": "pending"}))

def mark_notification_sent(notification_id):
    """Marca una notificación como enviada (o eliminada)."""
    db = get_db()
    db[COLLECTION_PENDING_NOTIFICATIONS].update_one(
        {"_id": notification_id},
        {"$set": {"status": "sent", "sent_at": datetime.utcnow().isoformat()}}
    )

# ==========================================
# EVENT LOG & PIPELINE MANAGEMENT
# ==========================================

def log_event(phone: str, event_type: str, actor: str = "system", metadata: dict = None):
    """Registra un evento estructurado e inmutable en crm_events."""
    db = get_db()
    event = {
        "phone": str(phone).replace("+", "").strip(),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": event_type,
        "actor": actor,
        "metadata": metadata or {}
    }
    db["crm_events"].insert_one(event)

def update_lead_state(phone: str, stage: str = None, metadata: dict = None):
    """
    Actualiza el estado (pipeline stage) y los timestamps de ciclo de vida.
    """
    db = get_db()
    update_data = {}
    
    if stage:
        update_data["stage"] = stage
    
    # Marcamos timestamps automáticos según el stage
    ts = datetime.utcnow().isoformat() + "Z"
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
        
        # Registrar el cambio de estado si aplica
        if stage:
            log_event(phone, EventType.STAGE_CHANGE, "system", {"new_stage": stage})

def delete_pending_notification(notification_id):
    """Elimina una notificación (si falló o ya no es necesaria)."""
    db = get_db()

# ==========================================
# CONSTANTES DE NEGOCIO (Pipeline & Eventos)
# ==========================================
class PipelineStage:
    NEW = "new"  # Recién llegado, sin procesar
    CONTACTED = "contacted" # Bot o Humano respondió
    CONVERSING = "conversing" # Intercambio activo
    VISIT_SCHEDULED = "visit_scheduled"
    VISIT_DONE = "visit_done"
    OFFER = "offer"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"

class EventType:
    MSG_IN = "msg_in"
    MSG_OUT = "msg_out"
    ASSIGNMENT = "assignment"
    ASSIGNMENT_FAIL = "assignment_fail"
    STAGE_CHANGE = "stage_change"
    NOTE = "note"
    ALERT_SENT = "alert_sent"
    BOT_PAUSE = "bot_pause"
    BOT_RESUME = "bot_resume"

class LeadSource:
    WHATSAPP = "whatsapp"
    PORTAL = "portal"
    MANUAL = "manual"
