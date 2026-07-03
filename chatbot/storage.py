# chatbot/storage.py
from pymongo import MongoClient
import time
import threading
import inspect
import asyncio
from datetime import datetime
import pytz
from config import Config
from typing import List, Dict, Optional
from .constants import PipelineStage, InteractionType, EventType, CHILE_TZ
import logging
from uuid import uuid4
from collections import deque

logger = logging.getLogger(__name__)

_mongo_client = None
_mongo_forensics_patched = False
_mongo_log_last_ts = {}
_observability_lock = threading.Lock()
_observability_metrics = {"mongo_sync_on_loop": 0, "event_loop_blocked": 0}
_event_loop_blocked_ts = deque(maxlen=1000)


def record_observability_event(event_type: str, payload: dict | None = None) -> str:
    """
    Registro pasivo de eventos del flujo en la colección event_log.
    Nunca lanza error al caller.
    """
    try:
        event = {
            "id": str(uuid4()),
            "event": event_type,
            "timestamp": datetime.now(CHILE_TZ).isoformat(),
        }
        if payload:
            event.update(payload)
        db = get_db()
        db["event_log"].insert_one(event)
        return event["id"]
    except Exception:
        return ""


def ensure_conversation_id(phone: str) -> str:
    """
    Garantiza un conversation_id persistente por lead.
    Si no existe, crea uno y lo guarda en la conversación.
    """
    try:
        db = get_db()
        doc = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone}, {"conversation_id": 1})
        conversation_id = (doc or {}).get("conversation_id")
        if conversation_id:
            return str(conversation_id)

        conversation_id = str(uuid4())
        db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {"$set": {"conversation_id": conversation_id}},
            upsert=True
        )
        return conversation_id
    except Exception:
        return str(uuid4())


async def run_in_threadpool(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def observability_mark(kind: str, **kwargs):
    with _observability_lock:
        if kind in _observability_metrics:
            _observability_metrics[kind] += 1
        if kind == "event_loop_blocked":
            _event_loop_blocked_ts.append(time.time())
    return True


def observability_snapshot_and_reset():
    with _observability_lock:
        snap = dict(_observability_metrics)
        _observability_metrics["mongo_sync_on_loop"] = 0
        _observability_metrics["event_loop_blocked"] = 0
        return snap


def observability_event_loop_blocked_recent(count_seconds: int = 10) -> int:
    now = time.time()
    with _observability_lock:
        while _event_loop_blocked_ts and (now - _event_loop_blocked_ts[0]) > count_seconds:
            _event_loop_blocked_ts.popleft()
        return len(_event_loop_blocked_ts)

def _should_rate_log(key: str, every_seconds: float) -> bool:
    now = time.time()
    last = _mongo_log_last_ts.get(key, 0.0)
    if now - last >= every_seconds:
        _mongo_log_last_ts[key] = now
        return True
    return False

def _patch_mongo_forensics():
    """Instrumentación temporal forense para operaciones sync de PyMongo."""
    global _mongo_forensics_patched
    if _mongo_forensics_patched:
        return
    try:
        from pymongo.collection import Collection
        op_names = ["find", "find_one", "aggregate", "update_one", "find_one_and_update", "insert_one"]
        for op in op_names:
            original = getattr(Collection, op, None)
            if not original or getattr(original, "__forensics_wrapped__", False):
                continue

            def _make_wrapper(name, fn):
                def _wrapped(self, *args, **kwargs):
                    t0 = time.perf_counter()
                    thread_id = threading.get_ident()
                    thread_name = threading.current_thread().name
                    stack = inspect.stack()
                    caller = stack[1].function if len(stack) > 1 else "unknown"
                    stack_hint = " > ".join(f"{s.function}:{s.lineno}" for s in stack[1:5])
                    file_hint = " > ".join((s.filename or "") for s in stack[1:8])
                    from_motor = ("\\motor\\" in file_hint) or ("/motor/" in file_hint)
                    in_event_loop = False
                    try:
                        asyncio.get_running_loop()
                        in_event_loop = True
                    except RuntimeError:
                        in_event_loop = False
                    if in_event_loop and (not from_motor):
                        observability_mark("mongo_sync_on_loop")
                        logger.error(
                            f"[MONGO_SYNC_ON_EVENT_LOOP] op={name} col={self.name} caller={caller} "
                            f"thread={thread_name}:{thread_id} stack={stack_hint}"
                        )
                        logger.critical(
                            f"[CRITICAL] [ASYNC_VIOLATION] type=mongo_sync_on_event_loop "
                            f"event_loop_blocked=none lag_ms=none op={name} collection={self.name} "
                            f"caller={caller} trace=none impact=HIGH action_required=true "
                            f"async_context=true thread_type=main safe=false"
                        )
                    try:
                        return fn(self, *args, **kwargs)
                    finally:
                        dt_ms = (time.perf_counter() - t0) * 1000
                        # Reducir ruido: loggear siempre lo anómalo, y muestrear lo normal.
                        # 1) Siempre: operaciones lentas >=400ms
                        # 2) Siempre: sync real en event loop (no motor)
                        # 3) Muestreo: 1 log/30s por firma para rápidas normales
                        is_anomalous = (dt_ms >= 400.0) or (in_event_loop and not from_motor)
                        key = f"{name}:{self.name}:{caller}:{thread_name}:{str(in_event_loop).lower()}:{str(from_motor).lower()}"
                        if is_anomalous or _should_rate_log(key, 30.0):
                            logger.debug(
                                f"[MONGO_OP] op={name} col={self.name} dur={dt_ms:.1f}ms "
                                f"thread={thread_name}:{thread_id} caller={caller} stack={stack_hint} "
                                f"in_event_loop={str(in_event_loop).lower()} from_motor={str(from_motor).lower()}"
                            )
                            logger.debug(
                                f"[MONGO_OP_META] async_context={str(in_event_loop).lower()} "
                                f"thread_type={'main' if thread_name.lower().startswith('main') else 'threadpool'} "
                                f"safe={str((not in_event_loop) or from_motor).lower()} from_motor={str(from_motor).lower()}"
                            )
                _wrapped.__forensics_wrapped__ = True
                return _wrapped

            setattr(Collection, op, _make_wrapper(op, original))
        _mongo_forensics_patched = True
        logger.info("[MONGO_FORENSICS] wrappers activos para operaciones sync")
    except Exception as e:
        logger.warning(f"[MONGO_FORENSICS] no se pudo activar wrappers: {e}")

def get_db():
    global _mongo_client
    if _mongo_client is None:
        # Timeouts defensivos para Render: evita freezes de 60s por conexiones TCP muertas
        # socketTimeoutMS: max wait para respuesta de una operacion en progreso
        # connectTimeoutMS: max espera al establecer una nueva conexion
        # serverSelectionTimeoutMS: max espera para encontrar un servidor Mongo disponible
        _mongo_client = MongoClient(
            Config.MONGO_URI,
            socketTimeoutMS=8000,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=10000,
            maxIdleTimeMS=45000,
        )
        _patch_mongo_forensics()
    return _mongo_client[Config.DB_NAME]

# --- ASYNC MOTOR INFRASTRUCTURE ---
try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    pass

_async_mongo_client = None

def get_async_db():
    """Retorna la base de datos asíncrona usando motor. Solo debe llamarse en rutas async."""
    global _async_mongo_client
    if _async_mongo_client is None:
        from motor.motor_asyncio import AsyncIOMotorClient
        _async_mongo_client = AsyncIOMotorClient(
            Config.MONGO_URI,
            socketTimeoutMS=8000,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=10000,
            maxIdleTimeMS=45000,
        )
    return _async_mongo_client[Config.DB_NAME]

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

def actualizar_prospecto(phone: str, datos: dict, trace_id: str = None):
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
            if isinstance(value, (bool, int, float, list, dict)):
                update_fields["$set"][f"prospecto.{key}"] = value
            else:
                update_fields["$set"][f"prospecto.{key}"] = str(value).strip()

    if update_fields["$set"]:
        if trace_id:
            logger.info(f"[PROSPECT_UPDATE] trace={trace_id} phone={phone} fields={list(update_fields['$set'].keys())}")
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
    lead_phone = lead_data.get("lead_phone") or lead_data.get("phone")
    lead_type = lead_data.get("lead_type")

    # Evitar duplicados del mismo lead/tipo: si ya existe un pendiente, se actualiza.
    if lead_phone and lead_type:
        existing = db[COLLECTION_PENDING_NOTIFICATIONS].find_one({
            "status": "pending",
            "$or": [
                {"lead_data.lead_phone": lead_phone},
                {"lead_data.phone": lead_phone},
            ],
            "lead_data.lead_type": lead_type,
        })
        if existing:
            db[COLLECTION_PENDING_NOTIFICATIONS].update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        "lead_data": lead_data,
                        "created_at": datetime.now(CHILE_TZ).isoformat(),
                        "status": "pending",
                    }
                }
            )
            return

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
