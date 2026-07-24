# chatbot/storage.py
import re
from pymongo import MongoClient
import time
import threading
import inspect
import asyncio
from datetime import datetime
import pytz
from config import Config
from typing import List, Dict, Optional

# ---- Phone redaction for logs ----
_PHONE_RE = re.compile(r"(\+?56\s*9)\s*(\d{4})\s*(\d{4})")
_PHONE_MASK = r"\1 **** \3"

def redact_phone(text: str) -> str:
    """Redact Chilean mobile numbers in log output. +56 9 XXXX 1234 -> +56 9 **** 1234"""
    if not text or not isinstance(text, str):
        return text
    return _PHONE_RE.sub(_PHONE_MASK, text)
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
            {
                "$set": {"conversation_id": conversation_id},
                "$setOnInsert": {"lead_temperature_effective": "COLD"},
            },
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


def guardar_mensaje(phone: str, role: str, content: str, metadata: dict = None, lead_id: str = None):
    from .phone_utils import normalize_phone_strict, is_synthetic_phone
    normalized = normalize_phone_strict(phone)
    phone = normalized or phone

    db = get_db()
    now = datetime.now(CHILE_TZ)
    message = {
        "role": role,
        "content": str(content),
        "timestamp": now.isoformat()
    }
    if metadata:
        message.update(metadata)

    if lead_id:
        from bson import ObjectId
        try:
            qid = ObjectId(lead_id) if isinstance(lead_id, str) else lead_id
        except Exception:
            qid = lead_id
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"_id": qid},
            {
                "$push": {"messages": {"$each": [message], "$slice": -50}},
                "$set": {
                    "last_message_at": now.isoformat(),
                    "last_message_role": role,
                    "last_message_preview": str(content)[:160],
                },
            }
        )
    else:
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {
                "$push": {"messages": {"$each": [message], "$slice": -50}},
                "$set": {
                    "last_message_at": now.isoformat(),
                    "last_message_role": role,
                    "last_message_preview": str(content)[:160],
                },
                "$setOnInsert": {
                    "created_at": now.isoformat(),
                    "lead_temperature_effective": "COLD",
                }
            },
            upsert=True
        )
    if result.modified_count or result.upserted_id:
        from .crm_updates import bump_crm_leads_version
        bump_crm_leads_version(db, reason=f"message_{role}", phone=phone)

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
    update_fields = {
        "$set": {},
        "$setOnInsert": {"lead_temperature_effective": "COLD"},
    }
    for key, value in datos.items():
        if value not in [None, "", "desconocido"]:
            if isinstance(value, (bool, int, float, list, dict)):
                update_fields["$set"][f"prospecto.{key}"] = value
            else:
                update_fields["$set"][f"prospecto.{key}"] = str(value).strip()

    if "alerts_sent" in datos:
        from .lead_temperature import derive_effective_temperature

        lead_snapshot = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone}) or {}
        prospecto_snapshot = dict(lead_snapshot.get("prospecto") or {})
        prospecto_snapshot["alerts_sent"] = datos["alerts_sent"]
        update_fields["$set"]["lead_temperature_effective"] = derive_effective_temperature(
            lead_snapshot,
            overrides={"prospecto": prospecto_snapshot},
        )
        update_fields.pop("$setOnInsert", None)

    if update_fields["$set"]:
        if trace_id:
            logger.info(f"[PROSPECT_UPDATE] trace={trace_id} phone={phone} fields={list(update_fields['$set'].keys())}")
        result = db[COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            update_fields,
            upsert=True
        )
        visible_prospect_fields = {
            "nombre", "ejecutivo", "codigo", "codigo_yapo",
            "codigo_mercadolibre", "ultimo_mensaje", "alerts_sent",
        }
        if (result.modified_count or result.upserted_id) and visible_prospect_fields.intersection(datos):
            from .crm_updates import bump_crm_leads_version
            bump_crm_leads_version(db, reason="prospect_updated", phone=phone)

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
    from .notification_identity import lead_notification_identity

    db = get_db()
    lead_phone = lead_data.get("lead_phone") or lead_data.get("phone")
    notification_key = lead_notification_identity(lead_data)

    # Un evento puede ser detectado por reglas con distinto lead_type. Para el
    # ejecutivo sigue siendo el mismo contacto interesado en la misma propiedad.
    if notification_key:
        collection = db[COLLECTION_PENDING_NOTIFICATIONS]
        existing = collection.find_one({
            "status": {"$in": ["pending", "sent"]},
            "notification_key": notification_key,
        })

        # Compatibilidad con documentos pendientes creados antes de esta clave.
        if not existing and lead_phone:
            legacy_candidates = collection.find({
                "status": {"$in": ["pending", "sent"]},
                "$or": [
                    {"lead_data.lead_phone": lead_phone},
                    {"lead_data.phone": lead_phone},
                ],
            })
            existing = next(
                (candidate for candidate in legacy_candidates
                 if lead_notification_identity(candidate) == notification_key),
                None,
            )

        if existing and existing.get("status") == "sent":
            return

        if existing:
            collection.update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        "lead_data": lead_data,
                        "notification_key": notification_key,
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
    if notification_key:
        notification["notification_key"] = notification_key
    db[COLLECTION_PENDING_NOTIFICATIONS].insert_one(notification)

def get_pending_notifications():
    db = get_db()
    _reconcile_missing_hot_notifications(db)
    return list(db[COLLECTION_PENDING_NOTIFICATIONS].find({"status": "pending"}))


_HOT_RECONCILIATION_LAST_RUN = None
_HOT_RECONCILIATION_CUTOVER = "2026-07-20T00:00:00-04:00"


def _reconcile_missing_hot_notifications(db):
    """Recover post-cutover HOT assignments that never reached the durable queue.

    Uses the canonical path (``assign_and_enqueue_hot()`` → ``crm_notifications_v1``).
    No new documents are created in ``pending_notifications``.
    The scan is throttled and runs only when the normal business-hours consumer
    asks for pending work.
    """
    global _HOT_RECONCILIATION_LAST_RUN
    if not Config.LEAD_HOT_RECONCILIATION_ENABLED:
        return
    if not Config.LEAD_HOT_NOTIFICATIONS_ENABLED:
        return
    from datetime import timezone
    from .crm_metrics import coerce_utc_datetime, active_assignment_cycle
    from .crm_hot_delivery import assign_and_enqueue_hot
    from .crm_notifications import individual_identity, COLLECTION as NOTIF_COLL

    now = datetime.now(timezone.utc)
    if _HOT_RECONCILIATION_LAST_RUN and (now - _HOT_RECONCILIATION_LAST_RUN).total_seconds() < 300:
        return
    _HOT_RECONCILIATION_LAST_RUN = now
    cutover = coerce_utc_datetime(_HOT_RECONCILIATION_CUTOVER)
    recovered = 0
    closed = {"ARCHIVED", "REJECTED", "CLOSED_LOST", "CLOSED_WON"}

    projection = {
        "_id": 1, "phone": 1, "created_at": 1, "ejecutivo_asignado": 1,
        "lead_temperature_effective": 1, "pipeline_stage": 1, "stage": 1,
        "prospecto": 1, "last_message_preview": 1, "lifecycle.first_valid_management_at": 1,
        "lifecycle.assigned_at": 1,
    }
    for lead_doc in db["leads"].find({"lead_temperature_effective": "HOT"}, projection):
        created_at = coerce_utc_datetime(lead_doc.get("created_at"))
        if not created_at or created_at < cutover:
            continue
        if str(lead_doc.get("pipeline_stage") or lead_doc.get("stage") or "").upper() in closed:
            continue
        if (lead_doc.get("lifecycle") or {}).get("first_valid_management_at"):
            continue
        executive = lead_doc.get("ejecutivo_asignado")
        if not executive:
            continue
        user = db["usuarios"].find_one({"nombre": executive, "is_active": True})
        if not user:
            continue
        recipient_user_id = str(user.get("_id") or "")
        target_phone = user.get("telefono") or user.get("tel") or user.get("movil")
        if not target_phone:
            continue

        # Resolve or create active cycle
        cycle = active_assignment_cycle(db, lead_doc["_id"])
        if not cycle:
            from .crm_metrics import create_assignment_cycle
            assigned_at = coerce_utc_datetime(
                (lead_doc.get("lifecycle") or {}).get("assigned_at")
            ) or created_at
            cycle = create_assignment_cycle(
                db, lead=lead_doc, assigned_to_user_id=recipient_user_id,
                assigned_by="system", reason="reconciliation",
                assigned_at=assigned_at,
                assigned_to_display_name=executive,
            )
        if not cycle:
            continue

        cycle_id = str(cycle.get("assignment_cycle_id", ""))
        if not cycle_id:
            continue

        # Check canonical dedup
        identity = individual_identity(
            lead_id=lead_doc["_id"], assignment_cycle_id=cycle_id,
            notification_type="lead_assignment_hot", recipient_user_id=recipient_user_id,
        )
        existing = db[NOTIF_COLL].find_one({
            "individual_identity": identity,
            "state": {"$in": ["pending", "sending", "sent"]},
        })
        if existing:
            continue

        prospect = lead_doc.get("prospecto") or {}
        property_code = (
            prospect.get("codigo") or prospect.get("codigo_interno")
            or prospect.get("codigo_propiedad") or prospect.get("codigo_mercadolibre")
        )
        payload = {
            "phone": lead_doc.get("phone"), "lead_phone": lead_doc.get("phone"),
            "property_code": property_code,
            "target_name": executive, "target_phone": target_phone,
            "nombre": prospect.get("nombre"),
            "comuna": prospect.get("comuna"), "operacion": prospect.get("operacion"),
            "canal": prospect.get("origen"), "source": prospect.get("origen"),
            "last_message": prospect.get("ultimo_mensaje") or lead_doc.get("last_message_preview"),
            "lead_type": "CRMLead", "notification_type": "lead_assignment_reconciled",
            "reconciled": True,
        }

        assign_and_enqueue_hot(
            db, lead=lead_doc, recipient_user_id=recipient_user_id,
            recipient_phone=target_phone, payload=payload,
            assigned_by="system", reason="reconciliation",
            recipient_name=executive,
        )
        recovered += 1

    if recovered:
        logger.warning("[NOTIFICATION_RECONCILIATION] Recuperados %s leads HOT via ruta canónica", recovered)

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

def log_event(phone: str, event_type: str, actor: str = "system", meta: dict = None,
              *, lead_id=None, actor_type=None, result=None, confirmed=False,
              timestamp=None):
    db = get_db()
    from .crm_metrics import active_assignment_cycle, coerce_utc_datetime, event_evidence, resolve_canonical_lead, utc_now
    resolution = resolve_canonical_lead(db, lead_id=lead_id, phone=phone)
    lead = resolution.lead
    event_at = coerce_utc_datetime(timestamp) or utc_now()
    cycle = active_assignment_cycle(db, lead["_id"]) if lead else None
    event = {
        "phone": str(phone).replace("+", "").strip(),
        "timestamp": event_at,
        "type": event_type,
        "actor": actor,
        "actor_type": actor_type or ("system" if str(actor).lower() in {"system", "bot", "sistema"} else "human"),
        "lead_id": lead.get("_id") if lead else None,
        "assignment_cycle_id": cycle.get("assignment_cycle_id") if cycle else None,
        "result": result,
        "confirmed": bool(confirmed),
        "identity_status": resolution.status,
        "meta": meta or {}
    }
    event["evidence"] = event_evidence(event)
    db["crm_events"].insert_one(event)
    if lead and event["evidence"]["management"]:
        first_fields = {"lifecycle.first_valid_management_at": event_at}
        if event["evidence"]["contact_attempt"]:
            first_fields["lifecycle.first_contact_attempt_at"] = event_at
        if event["evidence"]["effective_contact"]:
            first_fields["lifecycle.first_effective_contact_at"] = event_at
        for key, value in first_fields.items():
            db["leads"].update_one({"_id": lead["_id"], key: {"$exists": False}}, {"$set": {key: value}})
        if cycle:
            db["crm_assignment_cycles"].update_one(
                {"_id": cycle["_id"], "first_valid_management_at": {"$exists": False}},
                {"$set": {"first_valid_management_at": event_at, "first_valid_management_actor": actor}},
            )
        # Any canonical management evidence must stop the lead from remaining
        # unattended.  Click/send/call events are excluded by event_evidence()
        # and never reach this block.
        db[COLLECTION_CONVERSATIONS].update_one(
            {
                "_id": lead["_id"],
                "$or": [
                    {"pipeline_stage": {"$in": ["NEW", "new", "nuevo", ""]}},
                    {
                        "pipeline_stage": {"$exists": False},
                        "stage": {"$in": ["NEW", "new", "nuevo", "", None]},
                    },
                    {
                        "pipeline_stage": None,
                        "stage": {"$in": ["NEW", "new", "nuevo", "", None]},
                    },
                ],
            },
            {"$set": {
                "pipeline_stage": "CONTACTED",
                "stage": "CONTACTED",
                "last_crm_update": event_at,
            }},
        )
    
    # Precomputación SaaS: Actualizar métricas del lead atómicamente
    try:
        from .metrics import update_lead_metrics
        update_lead_metrics(db, phone, event_at=event["timestamp"], event_type=event_type,
                            lead_id=event.get("lead_id"))
    except Exception as e:
        logger.error(f"Error triggering metrics update in log_event: {e}")
    finally:
        from .crm_updates import bump_crm_leads_version
        bump_crm_leads_version(db, reason=f"event_{event_type}", phone=phone)

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

# ── Pending response (visit confirmation) ──

PENDING_RESPONSE_TTL_MINUTES = 60  # Default; override via env var at startup


def set_pending_response(phone: str, response_type: str, property_code: str, conversation_id: str):
    """Persist a pending response state on the lead document."""
    db = get_db()
    now = datetime.now(CHILE_TZ)
    db[COLLECTION_CONVERSATIONS].update_one(
        {"phone": phone},
        {"$set": {
            "pending_response.type": response_type,
            "pending_response.created_at": now.isoformat(),
            "pending_response.property_code": property_code,
            "pending_response.conversation_id": conversation_id,
            "pending_response.status": "waiting",
        }},
    )


def get_pending_response(phone: str, response_type: str = "VISIT_CONFIRMATION") -> Optional[dict]:
    """Return pending response state if it exists and hasn't expired."""
    db = get_db()
    lead = db[COLLECTION_CONVERSATIONS].find_one({"phone": phone}, {"pending_response": 1})
    if not lead:
        return None
    pr = lead.get("pending_response")
    if not pr or pr.get("type") != response_type:
        return None
    if pr.get("status") != "waiting":
        return None
    created = pr.get("created_at")
    if not created:
        return None
    try:
        created_dt = datetime.fromisoformat(created)
        if created_dt.tzinfo is None:
            created_dt = CHILE_TZ.localize(created_dt)
        elapsed = datetime.now(CHILE_TZ) - created_dt
        ttl = timedelta(minutes=int(os.getenv("PENDING_RESPONSE_TTL_MINUTES", "60")))
        if elapsed > ttl:
            return None
    except (ValueError, TypeError):
        return None
    return pr


def resolve_pending_response(phone: str, status: str):
    """Mark a pending response as confirmed, rejected, or expired."""
    db = get_db()
    db[COLLECTION_CONVERSATIONS].update_one(
        {"phone": phone},
        {"$set": {"pending_response.status": status, "pending_response.resolved_at": datetime.now(CHILE_TZ).isoformat()}},
    )


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
