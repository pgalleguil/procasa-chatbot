"""Sanitized, read-only CRM review app.

This module intentionally does not import webhook.py, the production CRM service,
or chatbot.storage,
or any production service. It renders the production CRM templates with controlled
demo payloads and intercepts browser actions locally.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Procasa CRM Sanitized Review", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")

REVIEW_BOOT = r"""
<script>
window.CRM_REVIEW_MODE = true;
window.CRM_REVIEW_NOTICE = function (message) {
    let notice = document.getElementById('crmReviewNotice');
    if (!notice) {
        notice = document.createElement('div');
        notice.id = 'crmReviewNotice';
        notice.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:2000;padding:12px 16px;border:1px solid #6366f1;border-radius:10px;background:#182235;color:#f8fafc;box-shadow:0 10px 30px #0006;font:600 14px system-ui';
        document.body.appendChild(notice);
    }
    notice.textContent = message;
    clearTimeout(window.crmReviewNoticeTimer);
    window.crmReviewNoticeTimer = setTimeout(() => notice.remove(), 2600);
};
const crmReviewFetch = window.fetch.bind(window);
window.fetch = async function (resource, options) {
    const url = String(resource && resource.url ? resource.url : resource);
    if (window.CRM_REVIEW_MODE && !url.includes('/static/')) {
        return new Response(JSON.stringify({ok: true, review_mode: true, simulated: true}), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
    }
    return crmReviewFetch(resource, options);
};
document.addEventListener('click', function (event) {
    const target = event.target.closest?.('.btn-comm, a[href^="tel:"], a[href^="mailto:"], #btnMainAction, #btnSemSearch');
    if (!target) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    window.CRM_REVIEW_NOTICE('Review sanitizada: acción simulada, sin envío real.');
}, true);
</script>
"""


def _now() -> datetime:
    # Fixed fixture date keeps the public review deterministic and clearly
    # separate from any live CRM timeline.
    return datetime(2026, 8, 20, 16, 10, tzinfo=timezone.utc)


def _lead(index: int, *, temperature: str, sla_status: str, sla_label: str,
          state: str, state_label: str, managed: bool = False,
          action: str = "", relative: str = "", sent_confirmed: bool = False,
          closed: bool = False, visit: bool = False) -> dict:
    sent_at = _now() - timedelta(minutes=15 * index + 10)
    elapsed_minutes = max(1, int((_now() - sent_at).total_seconds() // 60))
    elapsed_hours, remaining_minutes = divmod(elapsed_minutes, 60)
    assigned_relative = (f"Hace {elapsed_hours} h" if elapsed_hours else "Hace")
    if remaining_minutes:
        assigned_relative += f" {remaining_minutes} min" if elapsed_hours else f" {remaining_minutes} min"
    return {
        "phone": f"+569000000{index:02d}",
        "lead_id": f"review-{index:02d}",
        "phone_is_synthetic": True,
        "sla_status": sla_status,
        "sla_label": sla_label,
        "sla_compact_label": {
            "hot_near_critical": "Próximo · 24 min",
            "near_critical": "Próximo · 1h 42m",
        }.get(sla_status, "Gestionado" if sla_status == "fulfilled" else sla_label),
        "sla_timing": {
            "critical": "Venció hace 1 h 12 min",
            "hot_near_critical": "Faltan 24 min",
            "good": "Faltan 1 h 42 min",
            "fulfilled": "Dentro de SLA · 42 min",
        }.get(sla_status, "SLA no disponible"),
        "estado": "CLOSED_WON" if closed else state,
        "estado_badge": state_label,
        "estado_resultado": action if managed else None,
        "gestionado": managed,
        "lead_temperature_effective": temperature,
        "nombre": f"Cliente Demo {index:02d}",
        "whatsapp_display": f"+56 9 0000 00{index:02d}",
        "codigo_propiedad": f"DEMO-{1000 + index}",
        "url_propiedad": "#review-property",
        "ultima_accion_titulo": action or "Sin gestión",
        "ultima_accion_nota": "",
        "tiempo_relativo": relative,
        "ejecutivo_nombre": "Ejecutivo Demo",
        "assignment_cycle_id": f"review-cycle-{index:02d}",
        "effective_sent_at": sent_at,
        "effective_sent_date": sent_at.strftime("%d/%m/%Y"),
        "effective_sent_time": sent_at.strftime("%H:%M"),
        "assigned_relative": assigned_relative,
        "effective_sent_source": "Entrega confirmada" if sent_confirmed else "Asignación",
        "effective_sent_confirmed": sent_confirmed,
    }


DEMO_LEADS = [
    _lead(1, temperature="HOT", sla_status="critical", sla_label="Vencido", state="NEW", state_label="Sin atender"),
    _lead(2, temperature="HOT", sla_status="hot_near_critical", sla_label="Próximo a vencer", state="NEW", state_label="Sin atender"),
    _lead(3, temperature="COLD", sla_status="critical", sla_label="Vencido", state="NEW", state_label="Sin atender"),
    _lead(4, temperature="COLD", sla_status="good", sla_label="En plazo", state="NEW", state_label="Sin atender", sent_confirmed=True),
    _lead(5, temperature="HOT", sla_status="fulfilled", sla_label="Gestionado", state="GRUPO_GESTION", state_label="En gestión", managed=True, action="No respondió", relative="Hace 25 min"),
    _lead(6, temperature="COLD", sla_status="fulfilled", sla_label="Gestionado", state="GRUPO_GESTION", state_label="En gestión", managed=True, action="Contactado", relative="Hoy · 11:32"),
    _lead(7, temperature="COLD", sla_status="fulfilled", sla_label="Gestionado", state="GRUPO_VISITA", state_label="Visita agendada", managed=True, action="Visita agendada", relative="Ayer · 17:10", visit=True),
    _lead(8, temperature="HOT", sla_status="fulfilled", sla_label="Gestionado", state="CLOSED_WON", state_label="Cerrado ganado", managed=True, action="Cierre ganado", relative="Ayer · 15:20", closed=True),
    _lead(9, temperature="COLD", sla_status="good", sla_label="En plazo", state="NEW", state_label="Sin atender"),
]


def _review_url(**params: str) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    return "/crm-leads-review" + (f"?{urlencode(clean)}" if clean else "")


def _list_urls(request: Request, *, temperature=None, state=None, order=None) -> dict:
    base = {key: value for key, value in request.query_params.items() if key not in {"view", "page", "temperatura", "estado", "orden", "ejecutivo", "busqueda", "property_code"}}
    base["view"] = "list"
    current = {key: value for key, value in request.query_params.items() if key in {"temperatura", "estado", "orden", "ejecutivo", "busqueda", "property_code"} and value}

    def url(**changes):
        values = {**base, **current}
        for key, value in changes.items():
            if value in (None, "", "Todos"):
                values.pop(key, None)
            else:
                values[key] = value
        return _review_url(**values)

    urls = {"clear": _review_url(view="list"), "temperature": url(temperatura=None),
            "state": url(estado=None), "executive": url(ejecutivo=None),
            "search": url(busqueda=None, property_code=None), "order": url(orden=None)}
    for key in ("total", "hot", "cold", "unassigned", "nuevo", "gestion", "visita", "cerrado",
                "nuevo_hot", "nuevo_cold", "gestion_hot", "gestion_cold", "visita_hot", "visita_cold",
                "cerrado_hot", "cerrado_cold"):
        if key == "total": urls[key] = url(temperatura=None)
        elif key == "hot": urls[key] = url(temperatura="HOT")
        elif key == "cold": urls[key] = url(temperatura="COLD")
        elif key == "unassigned": urls[key] = url(estado="UNASSIGNED")
        else:
            state_key = {"nuevo": "NEW", "gestion": "GRUPO_GESTION", "visita": "GRUPO_VISITA", "cerrado": "GRUPO_CERRADO"}.get(key.split("_")[0])
            urls[key] = url(estado=state_key) if state_key else url()
    return urls


def _list_context(request: Request) -> dict:
    params = request.query_params
    temperature = params.get("temperatura", "Todos")
    state = params.get("estado", "")
    leads = [lead for lead in DEMO_LEADS
             if temperature in ("", "Todos") or lead["lead_temperature_effective"] == temperature]
    if state == "NEW": leads = [lead for lead in leads if lead["estado"] == "NEW"]
    elif state == "GRUPO_GESTION": leads = [lead for lead in leads if lead["estado"] == "GRUPO_GESTION"]
    elif state == "GRUPO_VISITA": leads = [lead for lead in leads if lead["estado"] == "GRUPO_VISITA"]
    elif state == "GRUPO_CERRADO": leads = [lead for lead in leads if lead["estado"] == "CLOSED_WON"]
    elif state == "UNASSIGNED": leads = []
    order = params.get("orden", "recent_assigned")
    if order in ("oldest_assigned", "antiguos"):
        leads = sorted(leads, key=lambda lead: lead["effective_sent_at"])
    else:
        leads = sorted(leads, key=lambda lead: lead["effective_sent_at"], reverse=True)
    # KPI y barra representan el mismo universo base aunque se seleccione una
    # temperatura o un estado; solo las filas visibles cambian.
    scope = list(DEMO_LEADS)
    counts = {key: sum(1 for lead in scope if lead["estado"] == value) for key, value in {
        "nuevo": "NEW", "gestion": "GRUPO_GESTION", "visita": "GRUPO_VISITA", "cerrado": "CLOSED_WON"}.items()}
    total = len(scope)
    hot = sum(1 for lead in scope if lead["lead_temperature_effective"] == "HOT")
    cold = total - hot
    assignment_series = {"total": [0] * 7, "hot": [0] * 7, "cold": [0] * 7}
    for lead in DEMO_LEADS:
        day_index = min(6, max(0, int((_now() - lead["effective_sent_at"]).total_seconds() // 86400)))
        assignment_series["total"][6 - day_index] += 1
        bucket = "hot" if lead["lead_temperature_effective"] == "HOT" else "cold"
        assignment_series[bucket][6 - day_index] += 1
    def pct(value): return round(value * 100 / total, 1) if total else 0
    kpis = {"total": total, "hot": hot, "cold": cold, "hot_percent": pct(hot), "cold_percent": pct(cold),
            **{f"{key}_percent": pct(value) for key, value in counts.items()}, **counts,
            "managed": counts["gestion"] + counts["visita"] + counts["cerrado"],
            "managed_percent": pct(counts["gestion"] + counts["visita"] + counts["cerrado"]),
            "scope_total": total, "sin_asignar_global": 0,
            "nuevo_hot": sum(1 for lead in scope if lead["estado"] == "NEW" and lead["lead_temperature_effective"] == "HOT"),
            "nuevo_cold": sum(1 for lead in scope if lead["estado"] == "NEW" and lead["lead_temperature_effective"] == "COLD"),
            "gestion_hot": 0, "gestion_cold": counts["gestion"], "visita_hot": 0, "visita_cold": counts["visita"],
            "cerrado_hot": counts["cerrado"], "cerrado_cold": 0}
    kpis["assignment_series"] = assignment_series
    return {
        "request": request, "leads": leads, "kpis": kpis, "user_role": "supervisor",
        "user_name": "Supervisor Demo", "can_administer_leads": True, "executives": ["Ejecutivo Demo"],
        "current_ejecutivo": params.get("ejecutivo", "Todos"), "current_temperatura": temperature, "crm_version": 0,
        "partial": False, "review_mode": True, "card_urls": _list_urls(request), "filter_urls": _list_urls(request),
        "pagination_base_url": _review_url(view="list") + "&", "pagination": {
            "total_count": len(leads), "current_page": 1, "total_pages": 1,
            "has_more": False, "has_prev": False, "limit": 15,
        },
    }


def _detail_context(request: Request, lead: dict) -> dict:
    index = lead["lead_id"].split("-")[-1]
    property_data = {"calle": "Avenida Demo", "numeracion": index, "comuna": "Comuna Demo",
                     "codigo": f"DEMO-{1000 + int(index)}", "url": "#review-property", "tipo": "Departamento",
                     "precio_uf": "3.200", "nombre_propietario": "Propietario Demo", "movil_propietario": "+56 9 0000 0099",
                     "email_propietario": "propietario.demo@example.invalid"}
    history = [{"type_class": "success", "icon_class": "fa-solid", "icon": "fa-check",
                "timestamp": _now() - timedelta(hours=2), "user_action": "Revisión demo", "notes": "Evento ficticio", "result": "Review"}]
    detail = {**lead, "crm_estado": lead["estado"], "email": f"cliente.demo{index}@example.invalid", "rut": "99.999.999-9",
              "datos_propiedad": property_data, "ejecutivo_asignado": "Ejecutivo Demo", "crm_history": history,
              "timeline": [{"role": "chat-bot", "content": "Conversación ficticia de review."}], "sticky_notes": [],
              "next_action_date": "", "last_action_label": lead["ultima_accion_titulo"],
              "last_action_relative": lead["tiempo_relativo"], "last_crm_update": ""}
    return {"request": request, "lead": detail, "user_email": "review@example.invalid", "user_role": "agente",
            "user_name": "Ejecutivo Demo", "review_mode": True}


def _render(template_name: str, context: dict) -> HTMLResponse:
    html = templates.get_template(template_name).render(context)
    html = html.replace("</head>", REVIEW_BOOT + "</head>", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-CRM-Review": "sanitized"})


@app.get("/crm-leads-review", response_class=HTMLResponse)
async def crm_leads_review(request: Request, view: str = "list", lead: str = "review-01"):
    if view == "detail":
        selected = next((item for item in DEMO_LEADS if item["lead_id"] == lead), DEMO_LEADS[0])
        return _render("crm_lead_detail.html", _detail_context(request, selected))
    return _render("crm_leads_list.html", _list_context(request))


@app.get("/api/review-health", response_class=JSONResponse)
async def review_health() -> dict:
    return {"status": "ok", "sanitized": True, "mongo": False, "writes": False}
