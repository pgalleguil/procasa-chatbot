# chatbot/captacion_report.py
"""
Reporte de Meta Diaria de Captaciones para Procasa.
Formato optimizado para WhatsApp: SIMPLE, CLARO y COMPACTO.
Se envía de Lunes a Viernes a las 09:00 AM sobre el día hábil anterior.
"""
import logging
from datetime import datetime, timedelta
import pytz
from .storage import get_db
from .constants import CHILE_TZ
from .whatsapp_client import send_whatsapp_message
from .lead_router import ROUND_ROBIN_TEAM, EXECUTIVES_ON_VACATION
from config import Config

logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN ---
META_DIARIA = 10
MINIMO_ESPERADO = 5

def is_executive_unavailable_on_date(name: str, dt: datetime) -> bool:
    if not name: return False
    if name in EXECUTIVES_ON_VACATION:
        return True
    if "Raquel" in name and dt.weekday() in [0, 2]: # Lunes y Miércoles
        return True
    return False

async def get_daily_progress_stats(target_date: datetime) -> dict:
    """Calcula estadísticas con formato optimizado."""
    db = get_db()
    
    start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
    start_utc = start_of_day.astimezone(pytz.utc).replace(tzinfo=None)
    end_utc = end_of_day.astimezone(pytz.utc).replace(tzinfo=None)

    # 1. Ejecutivos configurados (Stock activo)
    active_stock_execs = db[Config.CAPTACION_COLLECTION_NAME].distinct("gestion.ejecutivo_asignado", {"details.es_propietario_directo": True})
    active_stock_names = {name.strip().title() for name in active_stock_execs if name and name.strip()}
    
    # Lista maestra de captación — defensiva: None guard en ambas fuentes
    round_robin = list(ROUND_ROBIN_TEAM) if ROUND_ROBIN_TEAM else []
    stock_names_list = list(active_stock_names) if active_stock_names else []
    master_team = list(set(stock_names_list + round_robin))
    # Ejecutivos "En configuración" (los que tú mencionaste/otros nuevos sin stock)
    en_configuracion = ["Paula Morales", "Rocío Aliaga"]
    
    avance_list = []
    sin_turno_list = []
    
    for exec_name in master_team:
        name_title = exec_name.strip().title()
        if name_title in en_configuracion: continue
        if name_title == "Sin Asignar": continue

        if is_executive_unavailable_on_date(exec_name, target_date):
            sin_turno_list.append(name_title)
        else:
            avance_list.append(name_title)

    # Contar gestiones desde crm_events (legacy / genérico)
    gestion_event_types = {
        "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
        "CLICK_PHONE_LEAD", "GESTION_LOG", "SEND_WA_OWNER", 
        "msg_out", "register_phone", "gestion_captacion", "stage_change"
    }
    
    # Query robusta que acepta strings (ISO) y objetos Datetime
    iso_start = start_utc.isoformat()
    iso_end = end_utc.isoformat()
    
    query = {
        "$or": [
            {"timestamp": {"$gte": start_utc, "$lte": end_utc}},
            {"timestamp": {"$gte": iso_start, "$lte": iso_end}}
        ],
        "type": {"$in": list(gestion_event_types)},
        "actor": {"$exists": True, "$ne": "system"}
    }
    
    cursor = db["crm_events"].find(query, {"actor": 1, "phone": 1})
    counts = {} # {actor: {total: X, phones: Y}}
    users_cache = {}
    
    for event in cursor:
        actor = event.get("actor", "").strip().title()
        phone = event.get("phone")
        ev_type = event.get("type")
        
        if actor in ["Agent", "Supervisor", "Sistema"] and phone:
            if phone in users_cache:
                actor = users_cache[phone]
            else:
                user = db["usuarios"].find_one({"telefono": {"$regex": phone}})
                if user:
                    actor = user.get("nombre", "").strip().title()
                    users_cache[phone] = actor
        
        if actor and actor not in ["Bot", "User", "System"]:
            if actor not in counts: counts[actor] = {"total": 0, "phones": 0}
            counts[actor]["total"] += 1
            if ev_type == "register_phone":
                counts[actor]["phones"] += 1

    # --- NUEVO: Extraer gestiones reales documentadas en yapo_propiedades ---
    # Convertimos start/end a BSON datetime compatibles pasándolas a UTC explícito o tz-aware.
    # Dado que los eventos de captacion se guardan con `get_chile_now()`, en Mongo se guardan como BSON datetime (UTC bajo el capó).
    
    yapo_start = start_of_day.astimezone(pytz.utc)
    yapo_end = end_of_day.astimezone(pytz.utc)
    
    # Buscamos propiedades que hayan tenido ALGUN cambio recientemente
    yapo_cursor = db[Config.CAPTACION_COLLECTION_NAME].find({
        "$or": [
            {"gestion.fecha_ultima_gestion": {"$gte": yapo_start, "$lte": yapo_end}},
            {"gestion.notas.timestamp": {"$gte": yapo_start, "$lte": yapo_end}},
            {"audit.contact_changes.timestamp": {"$gte": yapo_start, "$lte": yapo_end}}
        ]
    }, {"gestion.notas": 1, "audit.contact_changes": 1})
    
    for doc in yapo_cursor:
        # Analizar notas/gestiones
        gestion_data = doc.get("gestion") or {}
        notas = gestion_data.get("notas") or []
        for n in notas:
            ts = n.get("timestamp")
            actor = n.get("usuario", "").strip().title()
            
            # Chequear si cae en la fecha
            if ts and isinstance(ts, datetime):
                # Pymongo devuelve naive UTC, la localizamos
                if ts.tzinfo is None: ts = ts.replace(tzinfo=pytz.utc)
                if yapo_start <= ts <= yapo_end:
                    if actor and actor not in ["Bot", "User", "Sistema", "System"]:
                        if actor not in counts: counts[actor] = {"total": 0, "phones": 0}
                        # Como los clics a veces graban nota y cambian estado, esto podría duplicar en el futuro si hay 2 notas a la vez, 
                        # pero por simplicidad se cuenta cada log explícito como 1
                        counts[actor]["total"] += 1
                        
        # Analizar cambios de contacto
        audit_data = doc.get("audit") or {}
        audits = audit_data.get("contact_changes") or []
        for a in audits:
            ts = a.get("timestamp")
            actor = a.get("user", "").strip().title()
            
            if ts and isinstance(ts, datetime):
                if ts.tzinfo is None: ts = ts.replace(tzinfo=pytz.utc)
                if yapo_start <= ts <= yapo_end:
                    if actor and actor not in ["Bot", "User", "Sistema", "System"]:
                        if actor not in counts: counts[actor] = {"total": 0, "phones": 0}
                        counts[actor]["total"] += 1
                        if a.get("field") == "telefono":
                            counts[actor]["phones"] += 1

    results = []
    for name in avance_list:
        stats = counts.get(name, {"total": 0, "phones": 0})
        count = stats["total"]
        results.append({
            "name": name, 
            "count": count
        })
    
    # Ordenar por conteo
    results.sort(key=lambda x: x["count"], reverse=True)

    return {
        "avance": results,
        "sin_turno": sorted(sin_turno_list),
        "en_configuracion": sorted(en_configuracion),
        "date_label": target_date.strftime("%d/%m/%Y")
    }

async def send_meta_diaria_report(group_id: str, target_date: datetime) -> bool:
    logger.info("[CAPTACION_REPORT] Envío al grupo desactivado temporalmente.")
    return False
    data = await get_daily_progress_stats(target_date)
    if not data: return False
    
    lines = [
        "🏠 *REPORTE DE CAPTACIÓN*",
        f"🗓️ {data['date_label']}",
        "",
        "━━━━━━━━━━━━",
        "👥 *Avance*",
        ""
    ]
    
    for r in data['avance']:
        lines.append(f"{r['name']}: {r['count']} contactos")

    lines.extend(["", "━━━━━━━━━━━━", "⚪ *Sin turno*", ""])
    if data['sin_turno']:
        for n in data['sin_turno']: lines.append(f"{n}")
    else: lines.append("(vacío)")

    lines.extend(["", "━━━━━━━━━━━━", "🆕 *En configuración*", ""])
    if data['en_configuracion']:
        for n in data['en_configuracion']: lines.append(f"{n}")
    else: lines.append("(vacío)")

    lines.extend([
        "",
        "━━━━━━━━━━━━",
        "🎯 *Meta*",
        "10 contactos por ejecutivo"
    ])
    
    return await send_whatsapp_message(group_id, "\n".join(lines))

async def check_and_run_meta_diaria_report(force: bool = False):
    db = get_db()
    now_cl = datetime.now(CHILE_TZ)
    logger.info("[CAPTACION_REPORT] Scheduler desactivado temporalmente.")
    return
    
    days_back = 3 if now_cl.weekday() == 0 else 1
    target_date = now_cl - timedelta(days=days_back)
    today_str = now_cl.strftime("%Y-%m-%d")
    
    if db["system_state"].find_one({"type": "meta_diaria_report", "last_run": today_str}): return
    group_id = getattr(Config, "DAILY_REPORT_GROUP_ID", "56990152481-1598919271@g.us")
    
    if await send_meta_diaria_report(group_id, target_date):
        db["system_state"].update_one({"type": "meta_diaria_report"}, {"$set": {"last_run": today_str}}, upsert=True)
