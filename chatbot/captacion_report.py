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
    """Verifica si el ejecutivo no tiene turno por vacaciones o reglas específicas."""
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
    active_stock_execs = db["yapo_propiedades"].distinct("gestion.ejecutivo_asignado", {"details.es_propietario_directo": True})
    active_stock_names = {name.strip().title() for name in active_stock_execs if name and name.strip()}
    
    # Lista maestra de captación
    master_team = list(set(list(active_stock_names) + ROUND_ROBIN_TEAM))
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

    # Contar gestiones
    gestion_event_types = {"HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", "CLICK_PHONE_LEAD", "GESTION_LOG", "SEND_WA_OWNER"}
    query = {"timestamp": {"$gte": start_utc, "$lte": end_utc}, "type": {"$in": list(gestion_event_types)}, "actor": {"$exists": True, "$ne": "system"}}
    
    cursor = db["crm_events"].find(query, {"actor": 1})
    counts = {}
    for event in cursor:
        actor = event.get("actor", "").strip().title()
        if actor: counts[actor] = counts.get(actor, 0) + 1

    results = []
    for name in avance_list:
        count = counts.get(name, 0)
        percent = int((count / META_DIARIA) * 100)
        results.append({"name": name, "count": count, "percent": percent})

    results.sort(key=lambda x: x["count"], reverse=True)
    best = results[0] if results else None

    return {
        "avance": results,
        "sin_turno": sorted(sin_turno_list),
        "en_configuracion": sorted(en_configuracion),
        "date_label": target_date.strftime("%d/%m/%Y"),
        "best_name": best["name"] if best and best["count"] >= META_DIARIA else None,
        "under_min": all(r["count"] < MINIMO_ESPERADO for r in results) if results else False
    }

async def send_meta_diaria_report(group_id: str, target_date: datetime) -> bool:
    data = await get_daily_progress_stats(target_date)
    if not data: return False
    
    lines = [
        "🏠 *REPORTE DE CAPTACIÓN*",
        f"📅 {data['date_label']}",
        "",
        "━━━━━━━━━━━━",
        "👥 *Avance*",
        ""
    ]
    
    medal_icons = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(data['avance']):
        icon = medal_icons[i] if i < 3 else ""
        sp = " " if icon else ""
        # Semáforo: 🔴 <50%, 🟡 50-99%, 🟢 >=100%
        semaforo = "🟢" if r['count'] >= META_DIARIA else "🟡" if r['count'] >= MINIMO_ESPERADO else "🔴"
        lines.append(f"{icon}{sp}{r['name']}: {r['count']}/{META_DIARIA} ({r['percent']}%) {semaforo}")

    lines.extend(["", "━━━━━━━━━━━━", "⚪ *Sin turno*"])
    if data['sin_turno']:
        for n in data['sin_turno']: lines.append(f"- {n}")
    else: lines.append("- (vacío)")

    lines.extend(["", "━━━━━━━━━━━━", "🆕 *En configuración*"])
    for n in data['en_configuracion']: lines.append(f"- {n}")

    msg_dinamico = f"🔥 {data['best_name']} cumplió la meta" if data['best_name'] else "📌 Bajo mínimo esperado" if data['under_min'] else ""
    
    lines.extend([
        "",
        "━━━━━━━━━━━━",
        "🎯 *Meta por ejecutivo*",
        f"5 mínimo | 10 objetivo",
        f"\n{msg_dinamico}" if msg_dinamico else "",
        "",
        "━━━━━━━━━━━━",
        "⚡ *Hoy*",
        "Meta: 10 contactos"
    ])
    
    return await send_whatsapp_message(group_id, "\n".join(lines))

async def check_and_run_meta_diaria_report(force: bool = False):
    db = get_db()
    now_cl = datetime.now(CHILE_TZ)
    if not force and (now_cl.weekday() >= 5 or now_cl.hour != 9 or now_cl.minute > 45): return
    
    days_back = 3 if now_cl.weekday() == 0 else 1
    target_date = now_cl - timedelta(days=days_back)
    today_str = now_cl.strftime("%Y-%m-%d")
    
    if db["system_state"].find_one({"type": "meta_diaria_report", "last_run": today_str}): return
    group_id = getattr(Config, "DAILY_REPORT_GROUP_ID", "56990152481-1598919271@g.us")
    
    if await send_meta_diaria_report(group_id, target_date):
        db["system_state"].update_one({"type": "meta_diaria_report"}, {"$set": {"last_run": today_str}}, upsert=True)
