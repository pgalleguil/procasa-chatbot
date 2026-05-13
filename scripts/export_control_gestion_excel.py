import argparse
from datetime import datetime
from typing import Any, Dict, List, Optional
import sys
from pathlib import Path
import json
import re
import statistics
from typing import Any, Dict, List, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db
from chatbot.constants import CHILE_TZ

MANAGEMENT_EVENT_TYPES = {
    "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD",
    "CLICK_PHONE_LEAD", "CLICK_WHATSAPP_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER",
    "CLICK_PHONE_OWNER", "CLICK_WHATSAPP_OWNER", "ALERT_SENT", "alert_sent",
}
VISIT_HINT_EVENT_TYPES = {"VISIT_REQUEST", "VISIT_SCHEDULED", "ASK_VISIT"}
CAPTADO_STATES = {"CAPTADO", "captado", "cerrado_ganado", "CERRADO_GANADO"}
ACTIVE_CAPTACION_STATES = {
    "GESTION", "gestion", "NUEVO", "nuevo", "DETECTADO", "detectado",
    "INTENTO DE CONTACTO", "intento de contacto", "POR CONTACTAR", "por contactar",
    "Sin respuesta", "sin respuesta", "Contacto exitoso", "contacto exitoso",
    "Reunión agendada", "reunión agendada",
}
SLA_CRITICAL_MINUTES = 180


def _format_mins(mins: Optional[float]) -> str:
    if mins is None:
        return ""
    try:
        h = int(mins // 60)
        m = int(mins % 60)
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return str(mins)


def _extract_prop_code(text: Any) -> Optional[str]:
    s = str(text or "").strip()
    if not s:
        return None
    # Priorizar paréntesis (12345)
    match = re.search(r'\((\d{4,6})\)', s)
    if match:
        return match.group(1)
    # Brackets [12345]
    match = re.search(r'\[([\w-]+)\]', s)
    if match:
        return match.group(1)
    # Si es solo un número de 5-6 dígitos
    match = re.search(r'\b(\d{5,6})\b', s)
    if match:
        return match.group(1)
    return None


def _to_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return CHILE_TZ.localize(dt)
    return dt.astimezone(CHILE_TZ)


import unicodedata

def _norm_text(v: Any) -> str:
    """Normaliza texto: minúsculas, sin espacios extra y sin acentos."""
    s = str(v or "").strip().lower()
    # Eliminar acentos
    s = unicodedata.normalize('NFKD', s).encode('ASCII', 'ignore').decode('ASCII')
    return s


def _norm_exec_name(name: str) -> str:
    if not name: return "Sin Asignar"
    
    # Mapeos específicos de la sucursal para evitar confusiones
    MAPPINGS = {
        "paula cristina": "Paula Morales",
        "paula cristina morales": "Paula Morales",
        "jorge pablo caro": "Jorge Pablo",
    }
    
    name_clean = " ".join(name.strip().lower().split())
    if name_clean in MAPPINGS:
        return MAPPINGS[name_clean]
    
    parts = name.strip().split()
    if len(parts) >= 3:
        # Heurística: Si tiene 3 o más partes, y no es un caso especial,
        # preferir Nombre + Apellido (saltando el segundo nombre si es común)
        second = parts[1].lower()
        if second in ["maria", "cristina", "paz", "andrea", "del", "de"]:
            return f"{parts[0]} {parts[2]}".title()
        return f"{parts[0]} {parts[1]}".title()
    elif len(parts) == 2:
        return f"{parts[0]} {parts[1]}".title()
    
    return name.strip().title()


def _safe_minutes(diff: Optional[datetime], base: Optional[datetime]) -> Optional[float]:
    if not diff or not base:
        return None
    try:
        return round(max(0.0, (diff - base).total_seconds() / 60.0), 2)
    except Exception:
        return None


def _business_minutes(start_dt: Optional[datetime], end_dt: Optional[datetime]) -> Optional[float]:
    """
    Calcula los minutos hábiles entre dos fechas.
    Horario: Lunes a Viernes, 09:00 a 19:00.
    Fines de semana no cuentan.
    """
    if not start_dt or not end_dt or start_dt > end_dt:
        return None

    from datetime import timedelta
    
    # Asegurar zona horaria
    if start_dt.tzinfo is None: start_dt = CHILE_TZ.localize(start_dt)
    if end_dt.tzinfo is None: end_dt = CHILE_TZ.localize(end_dt)
    
    total_minutes = 0.0
    curr = start_dt
    
    while curr.date() <= end_dt.date():
        if curr.weekday() < 5:  # Lunes a Viernes
            # Definir límites del día hábil (09:00 - 19:00)
            day_start = curr.replace(hour=9, minute=0, second=0, microsecond=0)
            day_end = curr.replace(hour=19, minute=0, second=0, microsecond=0)
            
            # El inicio efectivo es el máximo entre el inicio del día hábil y el curr (si es el primer día)
            # o simplemente el inicio del día hábil (si es un día intermedio)
            actual_start = max(day_start, curr if curr.date() == start_dt.date() else day_start)
            
            # El fin efectivo es el mínimo entre el fin del día hábil y el end_dt (si es el último día)
            actual_end = min(day_end, end_dt if curr.date() == end_dt.date() else day_end)
            
            if actual_end > actual_start:
                total_minutes += (actual_end - actual_start).total_seconds() / 60.0
        
        # Saltar al inicio del día siguiente
        curr = (curr + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        
    return round(total_minutes, 2)


def _month_key(dt: Optional[datetime]) -> str:
    if not dt:
        return "SIN_FECHA"
    return dt.strftime("%Y-%m")


def build_report(days_back: int = 0) -> Dict[str, pd.DataFrame]:
    db = get_db()
    now = datetime.now(CHILE_TZ)
    since = now - pd.Timedelta(days=days_back)

    # Cartera Maestra (Filtrada por Oficina y Disponibilidad)
    cartera_docs = list(db["universo_cartera"].find(
        {"oficina": "PROCASA SUCRE", "disponible": True}, 
        {
            "ejecutivo": 1, "codigo": 1, "titulo": 1, "comuna": 1, "precio_uf": 1, "operacion": 1, "tipo": 1, "url": 1
        }
    ))
    
    sucre_execs = set()
    jorge_pablo_codes = set()
    jorge_pablo_norm = _norm_text("Jorge Pablo Caro")
    
    for d in cartera_docs:
        raw_name = d.get("ejecutivo")
        norm_name = _norm_text(raw_name)
        sucre_execs.add(_norm_exec_name(raw_name))
        if norm_name == jorge_pablo_norm:
            c = str(d.get("codigo") or "").strip()
            if c:
                jorge_pablo_codes.add(c)
    # ---------------------------------------------------------

    leads_query: Dict[str, Any] = {
        "$or": [
            {"ejecutivo_asignado": {"$exists": True, "$nin": ["", None]}},
            {"prospecto.ejecutivo": {"$exists": True, "$nin": ["", None]}},
        ]
    }
    if days_back and days_back > 0:
        leads_query = {
            "$and": [
                leads_query,
                {"$or": [{"created_at": {"$gte": since.isoformat()}}, {"created_at": {"$gte": since}}]},
            ]
        }

    leads_projection = {
        "phone": 1, "created_at": 1, "ejecutivo_asignado": 1, "prospecto.ejecutivo": 1,
        "prospecto.nombre": 1, "prospecto.codigo": 1, "prospecto.origen": 1,
        "prospecto.intencion": 1, "last_intent": 1, "pipeline_stage": 1, "stage": 1,
        "lifecycle.assigned_at": 1, "lifecycle.first_response_at": 1,
    }
    leads_docs = list(db["leads"].find(leads_query, leads_projection))

    phones = list({str(d.get("phone") or "").replace("+", "").strip() for d in leads_docs if d.get("phone")})
    events_docs = list(
        db["crm_events"].find({"phone": {"$in": phones}}, {"phone": 1, "timestamp": 1, "type": 1, "actor": 1, "meta": 1}).sort("timestamp", 1)
    ) if phones else []

    events_by_phone: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events_docs:
        ph = str(ev.get("phone") or "").replace("+", "").strip()
        if ph:
            events_by_phone.setdefault(ph, []).append(ev)

    lead_rows: List[Dict[str, Any]] = []
    per_exec: Dict[str, Dict[str, Any]] = {}
    monthly: Dict[tuple, Dict[str, Any]] = {}

    for lead in leads_docs:
        phone = str(lead.get("phone") or "").replace("+", "").strip()
        if not phone:
            continue

        ejecutivo = _norm_exec_name(lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo") or "SIN_ASIGNAR")
        exec_key = _norm_text(ejecutivo)

        created_at = _to_dt(lead.get("created_at"))
        assigned_at = _to_dt((lead.get("lifecycle") or {}).get("assigned_at")) or created_at
        first_response_at = _to_dt((lead.get("lifecycle") or {}).get("first_response_at"))
        stage = lead.get("pipeline_stage") or lead.get("stage") or "NEW"

        evs = events_by_phone.get(phone, [])
        mgmt_events = [e for e in evs if e.get("type") in MANAGEMENT_EVENT_TYPES]
        first_mgmt_dt = _to_dt(mgmt_events[0].get("timestamp")) if mgmt_events else None
        first_response_calc = first_response_at or first_mgmt_dt
        response_min = _safe_minutes(first_response_calc, assigned_at)

        visit_requested = (
            _norm_text(lead.get("last_intent")) in {"ask_visit", "agendar_visita"}
            or _norm_text((lead.get("prospecto") or {}).get("intencion")) in {"ask_visit", "agendar_visita"}
            or any(e.get("type") in VISIT_HINT_EVENT_TYPES for e in evs)
            or any("visita" in _norm_text((e.get("meta") or {}).get("note")) for e in evs)
        )

        manual_lead = _norm_text((lead.get("prospecto") or {}).get("origen")) == "manual" or any(e.get("type") == "MANUAL_ENTRY" for e in evs)
        exec_actions = [e for e in mgmt_events if _norm_text(e.get("actor")) == exec_key]
        last_action_dt = _to_dt(mgmt_events[-1].get("timestamp")) if mgmt_events else None

        lead_rows.append({
            "Ejecutivo": ejecutivo,
            "Telefono": phone,
            "Nombre Lead": (lead.get("prospecto") or {}).get("nombre", ""),
            "Codigo Propiedad": (lead.get("prospecto") or {}).get("codigo", ""),
            "Origen Lead": (lead.get("prospecto") or {}).get("origen", ""),
            "Lead Manual": "SI" if manual_lead else "NO",
            "Solicito Visita Chatbot": "SI" if visit_requested else "NO",
            "Etapa Actual": str(stage),
            "Fecha Creacion": created_at.isoformat() if created_at else "",
            "Fecha Asignacion": assigned_at.isoformat() if assigned_at else "",
            "Fecha Primera Respuesta": first_response_calc.isoformat() if first_response_calc else "",
            "Tiempo Primera Respuesta (min)": response_min,
            "SLA Critico >3h": "SI" if (response_min is not None and response_min > SLA_CRITICAL_MINUTES) else "NO",
            "Acciones Gestion (Total)": len(mgmt_events),
            "Acciones Gestion (Actor=Ejecutivo)": len(exec_actions),
            "Ultima Gestion": last_action_dt.isoformat() if last_action_dt else "",
        })

        b = per_exec.setdefault(ejecutivo, {
            "ejecutivo": ejecutivo,
            "leads_asignados": 0,
            "leads_con_gestion": 0,
            "leads_con_visita_chatbot": 0,
            "leads_manuales": 0,
            "acciones_plataforma_total": 0,
            "acciones_plataforma_ejecutivo": 0,
            "resp_minutes": [],
            "sla_critical": 0,
        })
        b["leads_asignados"] += 1
        b["leads_con_gestion"] += 1 if mgmt_events else 0
        b["leads_con_visita_chatbot"] += 1 if visit_requested else 0
        b["leads_manuales"] += 1 if manual_lead else 0
        b["acciones_plataforma_total"] += len(mgmt_events)
        b["acciones_plataforma_ejecutivo"] += len(exec_actions)
        if response_min is not None:
            b["resp_minutes"].append(response_min)
            if response_min > SLA_CRITICAL_MINUTES:
                b["sla_critical"] += 1

        mk = _month_key(created_at)
        mb = monthly.setdefault((ejecutivo, mk), {
            "Mes": mk, "Ejecutivo": ejecutivo,
            "Leads Nuevos": 0, "Leads con Gestion": 0, "Resp": [], "SLA Criticos >3h": 0,
            "Captaciones Asignadas": 0, "Captaciones Activas": 0, "Captaciones Captadas": 0, "Captaciones con Actividad": 0,
        })
        mb["Leads Nuevos"] += 1
    # Pre-calcular consultas por código de propiedad (Master)
    prop_consultas: Dict[str, int] = {}
    for lead in leads_docs:
        c = str(lead.get("prospecto", {}).get("codigo") or "").strip()
        if c:
            prop_consultas[c] = prop_consultas.get(c, 0) + 1

    lead_rows: List[Dict[str, Any]] = []
    per_exec: Dict[str, Dict[str, Any]] = {}
    monthly: Dict[tuple, Dict[str, Any]] = {}

    for lead in leads_docs:
        phone = str(lead.get("phone") or "").replace("+", "").strip()
        if not phone:
            continue

        ejecutivo = _norm_exec_name(lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo") or "SIN_ASIGNAR")
        exec_key = _norm_text(ejecutivo)

        created_at = _to_dt(lead.get("created_at"))
        assigned_at = _to_dt((lead.get("lifecycle") or {}).get("assigned_at")) or created_at
        first_response_at = _to_dt((lead.get("lifecycle") or {}).get("first_response_at"))
        stage = lead.get("pipeline_stage") or lead.get("stage") or "NEW"

        evs = events_by_phone.get(phone, [])
        mgmt_events = [e for e in evs if e.get("type") in MANAGEMENT_EVENT_TYPES]
        first_mgmt_dt = _to_dt(mgmt_events[0].get("timestamp")) if mgmt_events else None
        first_response_calc = first_response_at or first_mgmt_dt
        
        # SLA Realista (Business Hours vs Real)
        if first_response_calc:
            response_min_bus = _business_minutes(assigned_at, first_response_calc)
            response_min_real = _safe_minutes(first_response_calc, assigned_at)
        else:
            response_min_bus = _business_minutes(assigned_at, now)
            response_min_real = _safe_minutes(now, assigned_at)

        visit_requested = (
            _norm_text(lead.get("last_intent")) in {"ask_visit", "agendar_visita"}
            or _norm_text((lead.get("prospecto") or {}).get("intencion")) in {"ask_visit", "agendar_visita"}
            or any(e.get("type") in VISIT_HINT_EVENT_TYPES for e in evs)
            or any("visita" in _norm_text((e.get("meta") or {}).get("note")) for e in evs)
        )

        manual_lead = _norm_text((lead.get("prospecto") or {}).get("origen")) == "manual" or any(e.get("type") == "MANUAL_ENTRY" for e in evs)
        exec_actions = [e for e in mgmt_events if _norm_text(e.get("actor")) == exec_key]
        last_action_dt = _to_dt(mgmt_events[-1].get("timestamp")) if mgmt_events else None

        b = per_exec.setdefault(exec_key, {
            "ejecutivo": ejecutivo,
            "leads_asignados": 0,
            "leads_con_gestion": 0,
            "leads_con_visita_chatbot": 0,
            "leads_manuales": 0,
            "acciones_plataforma_total": 0,
            "acciones_plataforma_ejecutivo": 0,
            "resp_minutes": [],
            "resp_minutes_visita": [],
            "sla_critical": 0,
            "leads_pendientes": 0,
            "leads_vencidos_sin_resp": 0,
            "props_con_consulta": set(),
            "leads_jorge_pablo": 0,
        })
        b["leads_asignados"] += 1
        
        # Track dependencia de Jorge Pablo
        l_code = str(lead.get("prospecto", {}).get("codigo") or "").strip()
        if l_code and l_code in jorge_pablo_codes:
            b["leads_jorge_pablo"] += 1
            
        b["leads_con_gestion"] += 1 if mgmt_events else 0
        b["leads_con_visita_chatbot"] += 1 if visit_requested else 0
        b["leads_manuales"] += 1 if manual_lead else 0
        b["acciones_plataforma_total"] += len(mgmt_events)
        b["acciones_plataforma_ejecutivo"] += len(exec_actions)

        # Nuevos KPIs de Respuesta (Basados en SLA Operacional)
        is_pendiente = first_response_calc is None
        if is_pendiente:
            b["leads_pendientes"] += 1
            if response_min_bus and response_min_bus > SLA_CRITICAL_MINUTES:
                b["leads_vencidos_sin_resp"] += 1

        if response_min_bus is not None:
            b["resp_minutes"].append(response_min_bus)
            if visit_requested:
                b["resp_minutes_visita"].append(response_min_bus)
            if response_min_bus > SLA_CRITICAL_MINUTES:
                b["sla_critical"] += 1

        lead_rows.append({
            "Ejecutivo": ejecutivo,
            "Telefono": phone,
            "Nombre Lead": (lead.get("prospecto") or {}).get("nombre", ""),
            "Codigo Propiedad": (lead.get("prospecto") or {}).get("codigo", ""),
            "Origen Lead": (lead.get("prospecto") or {}).get("origen", ""),
            "Solicito Visita Chatbot": "SI" if visit_requested else "NO",
            "Pendiente Respuesta": "SI" if is_pendiente else "NO",
            "SLA Vencido Sin Respuesta": "SI" if (is_pendiente and response_min_bus and response_min_bus > SLA_CRITICAL_MINUTES) else "NO",
            "SLA Critico >3h (Op)": "SI" if (response_min_bus is not None and response_min_bus > SLA_CRITICAL_MINUTES) else "NO",
            "Tiempo Respuesta Operacional": _format_mins(response_min_bus),
            "Tiempo Respuesta Real": _format_mins(response_min_real),
            "Etapa Actual": str(stage),
            "Fecha Creacion": created_at.isoformat() if created_at else "",
            "Fecha Asignacion": assigned_at.isoformat() if assigned_at else "",
            "Fecha Primera Respuesta": first_response_calc.isoformat() if first_response_calc else ("PENDIENTE" if response_min_bus and response_min_bus > SLA_CRITICAL_MINUTES else ""),
            "Acciones Gestion (Total)": len(mgmt_events),
            "Acciones Gestion (Actor=Ejecutivo)": len(exec_actions),
            "Ultima Gestion": last_action_dt.isoformat() if last_action_dt else "",
        })
        
        # Track prop consultada
        l_code = _extract_prop_code(lead.get("prospecto", {}).get("codigo"))
        if l_code:
            b["props_con_consulta"].add(l_code)

        # Consistencia: Agrupar por fecha de ASIGNACION
        mk = _month_key(assigned_at)
        mb = monthly.setdefault((ejecutivo, mk), {
            "Mes": mk, "Ejecutivo": ejecutivo,
            "Leads Nuevos": 0, "Leads con Gestion": 0, "Resp": [], "SLA Criticos >3h": 0,
            "Captaciones Asignadas": 0, "Captaciones Activas": 0, "Captaciones Captadas": 0, "Captaciones con Actividad": 0,
        })
        mb["Leads Nuevos"] += 1
        mb["Leads con Gestion"] += 1 if mgmt_events else 0
        if response_min_bus is not None:
            mb["Resp"].append(response_min_bus)
            if response_min_bus > SLA_CRITICAL_MINUTES:
                mb["SLA Criticos >3h"] += 1
    
    # 2. Captaciones Recientes (Yapo)
    capt_rows: List[Dict[str, Any]] = []
    captacion_docs = list(db["yapo_propiedades"].find(
        {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}},
        {
            "details.titulo": 1, "details.comuna": 1, "details.telefono": 1,
            "gestion.ejecutivo_asignado": 1, "gestion.estado": 1, "gestion.estado_captacion": 1,
            "gestion.fecha_ultima_gestion": 1, "gestion.actividades": 1, "gestion.fecha_asignacion": 1,
            "score_captacion": 1, "created_at": 1, "url": 1,
        }
    ))
    for doc in captacion_docs:
        g = doc.get("gestion") or {}
        ejecutivo = _norm_exec_name(g.get("ejecutivo_asignado") or "")
        state = g.get("estado") or g.get("estado_captacion") or "N/D"
        activities = g.get("actividades") or []
        act_n = len(activities)
        last_gestion = _to_dt(g.get("fecha_ultima_gestion"))
        
        # Consistencia: Fecha de asignación real
        fecha_asig = _to_dt(g.get("fecha_asignacion"))
        primary_dt = fecha_asig or last_gestion or _to_dt(doc.get("created_at"))

        title = (doc.get("details") or {}).get("titulo", "")
        p_code = _extract_prop_code(title)
        if not p_code:
            # Fallback a URL
            url = doc.get("url", "")
            u_match = re.search(r'/(\d+)$', url.strip("/"))
            if u_match: p_code = u_match.group(1)

        consultas_n = prop_consultas.get(p_code, 0) if p_code else 0

        capt_rows.append({
            "Ejecutivo": ejecutivo,
            "Titulo Propiedad": title,
            "Codigo Interno (Detectado)": p_code or "",
            "Consultas (Leads)": consultas_n,
            "¿Tiene Consultas?": "SI" if consultas_n > 0 else "NO",
            "Comuna": (doc.get("details") or {}).get("comuna", ""),
            "Telefono Propietario": (doc.get("details") or {}).get("telefono", ""),
            "Estado Captacion": str(state),
            "Score Captacion": doc.get("score_captacion", 0),
            "Actividades Gestion": act_n,
            "Ultima Gestion": last_gestion.isoformat() if last_gestion else "",
            "Fecha Asignacion": fecha_asig.isoformat() if fecha_asig else "",
            "Fecha Creacion (Doc)": (_to_dt(doc.get("created_at")).isoformat() if _to_dt(doc.get("created_at")) else ""),
        })

        b = per_exec.setdefault(_norm_text(ejecutivo), {
            "ejecutivo": ejecutivo,
            "leads_asignados": 0, "leads_con_gestion": 0, "leads_con_visita_chatbot": 0,
            "leads_manuales": 0, "acciones_plataforma_total": 0, "acciones_plataforma_ejecutivo": 0,
            "resp_minutes": [], "resp_minutes_visita": [], "sla_critical": 0,
            "leads_pendientes": 0, "leads_vencidos_sin_resp": 0,
            "props_con_consulta": set(),
        })
        b["capt_asignadas"] = b.get("capt_asignadas", 0) + 1
        b["capt_en_gestion"] = b.get("capt_en_gestion", 0) + (1 if _norm_text(state) == "gestion" else 0)
        b["capt_activas"] = b.get("capt_activas", 0) + (1 if str(state) in ACTIVE_CAPTACION_STATES else 0)
        b["capt_captadas"] = b.get("capt_captadas", 0) + (1 if str(state) in CAPTADO_STATES else 0)
        b["capt_con_actividad"] = b.get("capt_con_actividad", 0) + (1 if act_n > 0 else 0)
        b["capt_acciones_total"] = b.get("capt_acciones_total", 0) + act_n
        
        if p_code:
            b.setdefault("all_prop_codes", set()).add(p_code)

        mk = _month_key(primary_dt)
        mb = monthly.setdefault((ejecutivo, mk), {
            "Mes": mk, "Ejecutivo": ejecutivo,
            "Leads Nuevos": 0, "Leads con Gestion": 0, "Resp": [], "SLA Criticos >3h": 0,
            "Captaciones Asignadas": 0, "Captaciones Activas": 0, "Captaciones Captadas": 0, "Captaciones con Actividad": 0,
        })
        mb["Captaciones Asignadas"] += 1
        mb["Captaciones Activas"] += 1 if str(state) in ACTIVE_CAPTACION_STATES else 0
        mb["Captaciones Captadas"] += 1 if str(state) in CAPTADO_STATES else 0
        mb["Captaciones con Actividad"] += 1 if act_n > 0 else 0

    # 3. Cartera Maestra
    cartera_rows: List[Dict[str, Any]] = []
    code_to_title: Dict[str, str] = {}
    for doc in cartera_docs:
        ejecutivo = _norm_exec_name(doc.get("ejecutivo") or "SIN_ASIGNAR")
        code = str(doc.get("codigo") or "").strip()
        consultas_n = prop_consultas.get(code, 0) if code else 0
        if code and doc.get("titulo"):
            code_to_title[code] = str(doc.get("titulo"))
        
        cartera_rows.append({
            "Ejecutivo": ejecutivo,
            "Codigo": code,
            "Titulo": doc.get("titulo", ""),
            "Consultas (Leads)": consultas_n,
            "Salud": "ACTIVA (Con Consultas)" if consultas_n > 0 else "ESTANCADA (Sin Consultas)",
            "Comuna": doc.get("comuna", ""),
            "Precio UF": doc.get("precio_uf", ""),
            "Operacion": doc.get("operacion", ""),
            "Tipo": doc.get("tipo", ""),
            "URL": doc.get("url", ""),
        })
        
        b = per_exec.setdefault(_norm_text(ejecutivo), {
            "ejecutivo": ejecutivo,
            "leads_asignados": 0, "leads_con_gestion": 0, "leads_con_visita_chatbot": 0,
            "leads_manuales": 0, "acciones_plataforma_total": 0, "acciones_plataforma_ejecutivo": 0,
            "resp_minutes": [], "resp_minutes_visita": [], "sla_critical": 0,
            "leads_pendientes": 0, "leads_vencidos_sin_resp": 0,
            "props_con_consulta": set(),
            "cartera_total": 0,
            "cartera_consultada": 0,
        })
        b["cartera_total"] = b.get("cartera_total", 0) + 1
        if consultas_n > 0:
            b["cartera_consultada"] = b.get("cartera_consultada", 0) + 1

    summary_rows: List[Dict[str, Any]] = []
    for ejecutivo, m in sorted(per_exec.items(), key=lambda kv: kv[0].lower()):
        regex_exec = re.compile(re.escape(ejecutivo), re.IGNORECASE)
        crm_total = db["leads"].count_documents({"$or": [{"prospecto.ejecutivo": regex_exec}, {"ejecutivo_asignado": regex_exec}]})
        
        resp_list = m.get("resp_minutes", [])
        avg_resp = sum(resp_list) / len(resp_list) if resp_list else None
        median_resp = statistics.median(resp_list) if resp_list else None
        
        resp_visita = m.get("resp_minutes_visita", [])
        avg_resp_visita = sum(resp_visita) / len(resp_visita) if resp_visita else None
        
        resp_under_60 = len([x for x in resp_list if x <= 60])
        pct_under_60 = round((resp_under_60 / max(len(resp_list), 1)) * 100, 1) if resp_list else 0

        summary_rows.append({
            "Ejecutivo": m["ejecutivo"],
            "Leads Asignados": m.get("leads_asignados", 0),
            "Leads Vencidos SIN Respuesta": m.get("leads_vencidos_sin_resp", 0),
            "Leads Pendientes Respuesta": m.get("leads_pendientes", 0),
            "% Respuesta <= 60m": pct_under_60,
            "Mediana Primera Respuesta": _format_mins(median_resp),
            "Promedio Primera Respuesta": _format_mins(avg_resp),
            "Promedio Resp Leads Visita": _format_mins(avg_resp_visita),
            "SLA Criticos >3h": m.get("sla_critical", 0),
            "Leads con Visita Chatbot": m.get("leads_con_visita_chatbot", 0),
            "% Dependencia Jorge Pablo": round((m.get("leads_jorge_pablo", 0) / m.get("leads_asignados", 1)) * 100, 1) if m.get("leads_asignados", 0) > 0 else 0,
            "Cartera Total (Propiedades)": m.get("cartera_total", 0),
            "Cartera con Consultas": m.get("cartera_consultada", 0),
            "Salud de Cartera (% Consultada)": round((m.get("cartera_consultada", 0) / m.get("cartera_total", 1)) * 100, 1) if m.get("cartera_total", 0) > 0 else 0,
            "Captaciones Asignadas (Yapo)": m.get("capt_asignadas", 0),
            "Acciones Plataforma (Total)": m.get("acciones_plataforma_total", 0),
        })

    monthly_rows: List[Dict[str, Any]] = []
    for _, v in sorted(monthly.items(), key=lambda x: (x[0][1], x[0][0]), reverse=True):
        resp = v.get("Resp", [])
        avg_resp = sum(resp) / len(resp) if resp else None
        monthly_rows.append({
            "Mes": v["Mes"], "Ejecutivo": v["Ejecutivo"],
            "Leads Nuevos": v.get("Leads Nuevos", 0),
            "Leads con Gestion": v.get("Leads con Gestion", 0),
            "% Leads Gestionados": round((v.get("Leads con Gestion", 0) / v.get("Leads Nuevos", 1)) * 100, 1) if v.get("Leads Nuevos", 0) > 0 else 0,
            "Promedio Primera Respuesta": _format_mins(avg_resp),
            "SLA Criticos >3h": v.get("SLA Criticos >3h", 0),
            "Captaciones Asignadas": v.get("Captaciones Asignadas", 0),
            "Captaciones Activas": v.get("Captaciones Activas", 0),
            "Captaciones Captadas": v.get("Captaciones Captadas", 0),
            "Captaciones con Actividad": v.get("Captaciones con Actividad", 0),
        })
        
    props_top_rows: List[Dict[str, Any]] = []
    props_by_exec: Dict[str, List[Dict[str, Any]]] = {}
    for row in cartera_rows:
        ejecutivo = str(row.get("Ejecutivo") or "")
        code = str(row.get("Codigo") or "").strip()
        qty = int(row.get("Consultas (Leads)") or 0)
        if not ejecutivo or not code or qty <= 0:
            continue
        props_by_exec.setdefault(ejecutivo, []).append({
            "Codigo Propiedad": code,
            "Titulo Propiedad": str(row.get("Titulo") or ""),
            "Solicitudes (Leads)": qty,
        })

    for ejecutivo in sorted(props_by_exec.keys(), key=lambda x: x.lower()):
        ordered = sorted(
            props_by_exec[ejecutivo],
            key=lambda r: (-int(r["Solicitudes (Leads)"]), str(r["Codigo Propiedad"]))
        )
        for pos, item in enumerate(ordered[:10], start=1):
            props_top_rows.append({
                "Ejecutivo": ejecutivo,
                "Ranking": pos,
                "Codigo Propiedad": item["Codigo Propiedad"],
                "Titulo Propiedad": item["Titulo Propiedad"],
                "Solicitudes (Leads)": item["Solicitudes (Leads)"],
            })

    summary_df = pd.DataFrame(summary_rows)
    leads_df = pd.DataFrame(lead_rows)
    cartera_df = pd.DataFrame(cartera_rows)
    capt_df = pd.DataFrame(capt_rows)
    monthly_df = pd.DataFrame(monthly_rows)
    props_top_df = pd.DataFrame(props_top_rows)

    if not summary_df.empty:
        summary_df = summary_df.sort_values(by=["Leads Asignados", "Acciones Plataforma (Total)"], ascending=False)
    if not leads_df.empty:
        leads_df = leads_df.sort_values(by=["Fecha Creacion"], ascending=False)
    if not capt_df.empty:
        capt_df = capt_df.sort_values(by=["Ultima Gestion"], ascending=False)
    if not monthly_df.empty:
        monthly_df = monthly_df.sort_values(by=["Mes", "Ejecutivo"], ascending=[False, True])
    if not props_top_df.empty:
        props_top_df = props_top_df.sort_values(by=["Ejecutivo", "Ranking"], ascending=[True, True])

    audit_captacion = pd.DataFrame(capt_rows)
    if not audit_captacion.empty:
        audit_captacion = (
            audit_captacion.groupby(["Ejecutivo", "Estado Captacion"], dropna=False)
            .size().reset_index(name="Cantidad")
            .sort_values(["Ejecutivo", "Cantidad"], ascending=[True, False])
        )

    return {
        "resumen_ejecutivos": summary_df,
        "control_mensual": monthly_df,
        "detalle_leads": leads_df,
        "detalle_cartera": cartera_df,
        "top_propiedades_por_ejecutivo": props_top_df,
        "detalle_captacion": capt_df,
        "auditoria_captacion_estados": audit_captacion,
    }


def export_excel(output_path: str, days_back: int = 0) -> str:
    output = Path(output_path)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)

    report = build_report(days_back=days_back)
    glossary_df = pd.DataFrame([
        {"Campo": "Control CRM Leads (mismo filtro)", "Definicion": "Conteo con el mismo criterio del CRM: prospecto.ejecutivo OR ejecutivo_asignado."},
        {"Campo": "SLA Criticos >3h", "Definicion": f"Leads con primera respuesta sobre {SLA_CRITICAL_MINUTES} minutos."},
        {"Campo": "Captaciones En Gestion (CRM)", "Definicion": "Solo estado exacto GESTION."},
        {"Campo": "Captaciones Activas", "Definicion": "Cartera activa operacional (NUEVO, POR CONTACTAR, GESTION y estados intermedios)."},
        {"Campo": "Captaciones con Actividad", "Definicion": "Propiedades con al menos una actividad en gestion.actividades."},
    ])

    with pd.ExcelWriter(str(output), engine="xlsxwriter") as writer:
        for sheet_name, df in report.items():
            if df.empty:
                df = pd.DataFrame([{"info": "Sin datos para el período seleccionado"}])
            sn = sheet_name[:31]
            df.to_excel(writer, sheet_name=sn, index=False)
            wb = writer.book
            ws = writer.sheets[sn]
            hfmt = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "border": 1})
            red_fmt = wb.add_format({"bg_color": "#FFC7CE", "font_color": "#9C0006"})
            green_fmt = wb.add_format({"bg_color": "#C6EFCE", "font_color": "#006100"})
            
            for i, c in enumerate(df.columns):
                ws.write(0, i, c, hfmt)
                max_len = max(len(str(c)), *(len(str(v)) for v in df[c].head(500).tolist())) if len(df.index) else len(str(c))
                ws.set_column(i, i, min(max_len + 2, 46))
            
            # Formatos Condicionales
            rows_n = len(df.index)
            if rows_n > 0:
                # Rojo para alertas críticas
                for i, col in enumerate(df.columns):
                    if col in ["SLA Vencido Sin Respuesta", "SLA Critico >3h (Op)", "Pendiente Respuesta"]:
                        ws.conditional_format(1, i, rows_n, i, {
                            "type": "cell", "criteria": "equal to", "value": '"SI"', "format": red_fmt
                        })
                    if col == "% Respuesta <= 60m":
                        ws.conditional_format(1, i, rows_n, i, {
                            "type": "cell", "criteria": "less than", "value": 30, "format": red_fmt
                        })
                        ws.conditional_format(1, i, rows_n, i, {
                            "type": "cell", "criteria": "greater than", "value": 70, "format": green_fmt
                        })
                    if col == "Leads Vencidos SIN Respuesta":
                        ws.conditional_format(1, i, rows_n, i, {
                            "type": "cell", "criteria": "greater than", "value": 0, "format": red_fmt
                        })

        glossary_df.to_excel(writer, sheet_name="diccionario", index=False)

    # HTML local filtrable
    html_path = output.with_suffix(".html")
    summary = report["resumen_ejecutivos"].fillna("").to_dict(orient="records")
    monthly = report["control_mensual"].fillna("").to_dict(orient="records")
    html = f"""<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Control de Gestión Comercial</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;background:#f5f7fb;margin:24px;color:#111827}}.card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin-bottom:16px}}table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid #e5e7eb;padding:8px;font-size:13px}}th{{background:#eef2ff}}select{{padding:8px;margin-right:8px}}</style>
</head><body><h1>Control de Gestión Comercial</h1>
<div class='card'><label>Ejecutivo:</label><select id='exec'></select><label>Mes:</label><select id='mes'></select></div>
<div class='card'><h3>Resumen Ejecutivo</h3><table id='t1'></table></div>
<div class='card'><h3>Control Mensual</h3><table id='t2'></table></div>
<script>
const summary={json.dumps(summary, ensure_ascii=False)};
const monthly={json.dumps(monthly, ensure_ascii=False)};
const execSel=document.getElementById('exec');const mesSel=document.getElementById('mes');
function uniq(arr,key){{return ['Todos',...new Set(arr.map(x=>x[key]).filter(Boolean))];}}
function fill(sel,vals){{sel.innerHTML='';vals.forEach(v=>{{const o=document.createElement('option');o.value=v;o.textContent=v;sel.appendChild(o);}})}}
function draw(id,rows){{const t=document.getElementById(id);if(!rows.length){{t.innerHTML='<tr><td>Sin datos</td></tr>';return;}}const cols=Object.keys(rows[0]);t.innerHTML='<tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+String(r[c]??'')+'</td>').join('')+'</tr>').join('');}}
function run(){{const ex=execSel.value,ms=mesSel.value;draw('t1',summary.filter(r=>ex==='Todos'||r['Ejecutivo']===ex));draw('t2',monthly.filter(r=>(ex==='Todos'||r['Ejecutivo']===ex)&&(ms==='Todos'||r['Mes']===ms)));}}
fill(execSel,uniq(summary,'Ejecutivo'));fill(mesSel,uniq(monthly,'Mes'));execSel.onchange=run;mesSel.onchange=run;run();
</script></body></html>"""
    html_path.write_text(html, encoding="utf-8")

    return str(output)


def main():
    parser = argparse.ArgumentParser(description="Exporta reporte profesional de control de gestión por ejecutivo.")
    parser.add_argument("--output", default="tmp/reporte_control_ejecutivos.xlsx", help="Ruta salida xlsx")
    parser.add_argument("--days", type=int, default=0, help="0 = sin recorte por fecha")
    args = parser.parse_args()

    out = export_excel(args.output, days_back=args.days)
    print(f"OK: reporte generado en {out}")
    print(f"OK: dashboard generado en {Path(out).with_suffix('.html')}")


if __name__ == "__main__":
    main()
