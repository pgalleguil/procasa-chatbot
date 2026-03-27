import logging
import asyncio
from datetime import datetime, timedelta
from .storage import get_db
from .constants import CHILE_TZ, PipelineStage, UNASSIGNED_LABEL
from .utils import calculate_business_minutes
from .whatsapp_client import send_whatsapp_message
from config import Config

logger = logging.getLogger(__name__)

# Configuración de SLA
CRITICAL_THRESHOLD_MINUTES = 180  # 3 horas según feedback

# Mapeo de estados para normalización
STAGE_MAPPING = {
    "nuevo": PipelineStage.NEW,
    "new": PipelineStage.NEW,
    "gestion": PipelineStage.CONTACTED,
    "contacted": PipelineStage.CONTACTED,
    "CONTACT_PENDING": PipelineStage.CONTACTED
}

def format_sla_time(minutes):
    """Formatea el tiempo de SLA de manera amigable (min/h/d)."""
    if minutes < 60:
        return f"{int(minutes)} min"
    elif minutes < 1440:
        hours = minutes / 60
        return f"{hours:.1f}h"
    else:
        days = minutes / 1440
        return f"{days:.1f}d"

async def get_critical_leads_summary():
    """
    Busca leads críticos (>3h sin primera atención) y los agrupa por ejecutivo.
    Considera únicamente la etapa PipelineStage.NEW.
    """
    db = get_db()
    now_cl = datetime.now(CHILE_TZ)
    
    # Solo buscamos leads que podrían estar en etapa NEW
    query = {
        "$or": [
            {"pipeline_stage": PipelineStage.NEW},
            {"pipeline_stage": "NEW"},
            {"stage": "nuevo"},
            {"stage": "new"},
            {"pipeline_stage": None},
            {"stage": None}
        ]
    }
    
    leads = list(db["leads"].find(query))
    if not leads:
        return []
    
    summary = {} # { executive_name: { count: X, max_antiquity: Y } }

    for lead in leads:
        # Determinar la etapa actual del lead
        stage = lead.get("pipeline_stage") or STAGE_MAPPING.get(str(lead.get("stage", "")).lower())
        
        # FILTRO ESTRICTO: Solo leads en etapa "NUEVO" (Sin Atender)
        if stage != PipelineStage.NEW:
            continue

        # --- DETERMINAR PUNTO DE INICIO PARA SLA ---
        # Si está asignado, el SLA corre desde la asignación. Si no, desde la creación.
        start_time = lead.get("lifecycle", {}).get("assigned_at") or lead.get("created_at")
        if not start_time:
            continue
            
        try:
            if isinstance(start_time, str):
                # Manejar formatos ISO con 'Z' o '+HH:MM'
                dt_start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            else:
                dt_start = start_time
            
            # Asegurar que sea aware (Chile/Continental por defecto si no tiene tz)
            if dt_start.tzinfo is None:
                dt_start = CHILE_TZ.localize(dt_start)
            else:
                dt_start = dt_start.astimezone(CHILE_TZ)
                
            # Calcular minutos de negocio transcurridos
            minutes_diff = calculate_business_minutes(dt_start, now_cl)
            
            # Solo reportamos los que están en ESTADO CRÍTICO (> 3 horas hábiles sin atención)
            if minutes_diff < CRITICAL_THRESHOLD_MINUTES:
                continue
            
            # --- REFUERZO DE SEGURIDAD (CRM_EVENTS / STATUS) ---
            # Si el lead tiene eventos de gestión humana reciente o una acción manual reciente, lo saltamos
            phone_clean = str(lead.get("phone", "")).replace("+", "").strip()
            
            # Excluir si hubo una actualización manual o cambio de estado muy reciente (ej: reasignación)
            # El dashboard dice "Acción registrada Hace 28m", esto se mapea a last_event_at
            last_event_at = lead.get("last_event_at")
            if last_event_at:
                if isinstance(last_event_at, str):
                    dt_event = datetime.fromisoformat(last_event_at.replace('Z', '+00:00'))
                else: 
                    dt_event = last_event_at
                
                # Sincronizar timezone
                if dt_event.tzinfo is None: dt_event = CHILE_TZ.localize(dt_event)
                
                # Si hubo CUALQUIER evento en los últimos 30 min, no es "crítico" para el reporte
                if (now_cl - dt_event.astimezone(CHILE_TZ)).total_seconds() < 1800:
                    continue

            # Búsqueda en eventos (Mismo criterio que antes)
            recent_management = db["crm_events"].find_one({
                "phone": {"$regex": f"^{phone_clean}"},
                "type": {"$in": [
                    "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
                    "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", 
                    "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER"
                ]}
            })
            
            if recent_management:
                continue

            exec_name = lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo")
            
            # Normalizar nombres de ejecutivos
            if not exec_name or str(exec_name).strip() in [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", ""]:
                exec_name = UNASSIGNED_LABEL
            else:
                exec_name = str(exec_name).strip().title()

            if exec_name not in summary:
                summary[exec_name] = {"count": 0, "max_antiquity": 0}
                
            summary[exec_name]["count"] += 1
            if minutes_diff > summary[exec_name]["max_antiquity"]:
                summary[exec_name]["max_antiquity"] = minutes_diff
                
        except Exception as e:
            # logger.error(f"Error procesando lead {lead.get('phone')} para reporte: {e}")
            continue

    # Filtrar ejecutivos sin leads críticos y ordenar por cantidad descendente
    sorted_summary = []
    for exec_name, data in summary.items():
        if data["count"] > 0 and exec_name != UNASSIGNED_LABEL:
            sorted_summary.append({
                "name": exec_name,
                "count": data["count"],
                "max_antiquity": data["max_antiquity"]
            })
            
    # Ordenar por cantidad de leads críticos
    sorted_summary.sort(key=lambda x: x["count"], reverse=True)

    return sorted_summary

async def send_daily_sla_report(group_id: str):
    """Genera y envía el reporte de SLA al grupo especificado."""
    if not group_id:
        return False

    try:
        sorted_summary = await get_critical_leads_summary()
        
        if not sorted_summary:
            # Si no hay pendientes críticos, NO enviamos nada (tal como pidió el usuario)
            logger.info("[DAILY_REPORT] Sin leads críticos. No se enviará reporte hoy.")
            return True # Retornamos True para marcar que la revisión del día se completó

        # --- CONSTRUIR MENSAJE ---
        message_lines = [
            "📢 *REPORTE CRÍTICO SLA – CRM PROCASA*",
            "",
            "🚨 *Leads en estado CRÍTICO* 🔴",
            "_(Sin atención por más de 3 horas)_",
            "",
            "Resumen por ejecutivo:"
        ]
        
        for item in sorted_summary:
            antiquity_str = format_sla_time(item['max_antiquity'])
            if item['max_antiquity'] > 1440: # Más de 24h
                 antiquity_str = ">24h"
                 
            message_lines.append(f"👤 *{item['name']}*: {item['count']} leads 🔴 ({antiquity_str})")

        message_lines.extend([
            "",
            "⚠️ *Acción inmediata requerida*",
            "Estos leads se encuentran en estado crítico (fuera de SLA) y requieren gestión urgente.",
            "",
            "👉 *Prioridad:* contactar de inmediato",
            "",
            "🔗 *Acceder al CRM:*",
            f"{Config.CRM_BASE_URL}/crm"
        ])

        message = "\n".join(message_lines)
        sent = await send_whatsapp_message(group_id, message)
        return sent

    except Exception as e:
        logger.error(f"[DAILY_REPORT] Error enviando el reporte: {e}", exc_info=True)
        return False

async def check_and_run_daily_report(force: bool = False):
    """Lógica de scheduler que verifica si toca enviar el reporte hoy."""
    db = get_db()
    now_cl = datetime.now(CHILE_TZ)
    
    # 1. Filtro de Días de Semana (Lunes a Viernes)
    if not force and now_cl.weekday() >= 5: # 5=Sábado, 6=Domingo
        return

    # 2. Filtro de Horario (No antes de las 9:30 AM)
    if not force:
        if now_cl.hour < 9 or (now_cl.hour == 9 and now_cl.minute < 30):
            return

    today_str = now_cl.strftime("%Y-%m-%d")
    
    # Evitar duplicados
    state = db["system_state"].find_one({"type": "daily_report"})
    if state and state.get("last_run") == today_str:
        return

    group_id = getattr(Config, "DAILY_REPORT_GROUP_ID", None)
    if not group_id:
        return

    success = await send_daily_sla_report(group_id)
    if success:
        db["system_state"].update_one(
            {"type": "daily_report"},
            {"$set": {"last_run": today_str}},
            upsert=True
        )
