"""Servidor local de PREVIEW del Leads Dashboard (solo lectura, SIN tareas de fondo).

Sirve unicamente la pagina /leads-dashboard y su endpoint de datos.
No inicia loops de WhatsApp, reportes, reasignaciones ni nada que escriba datos.

Uso:
    python preview_dashboard.py

Luego abre:  http://localhost:8001/leads-dashboard
"""
import sys
import asyncio
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

templates = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
crm_templates = Jinja2Templates(directory=str(ROOT / "templates"))

from analytics.leads_service import get_leads_dashboard_overview, get_leads_operational_dashboard, get_leads_control_breakdown, get_monthly_leads_control, get_monthly_control_executives, get_monthly_control_portfolio_demand, get_monthly_control_property_leads, get_monthly_control_lead_conversation, get_operational_executive_performance, get_operational_portfolios, get_properties_inventory_dashboard, get_capture_simulation
app = FastAPI(title="Leads Dashboard - Preview local (solo lectura)")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/leads-dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    template = templates.get_template("leads_dashboard.html")
    return HTMLResponse(template.render(user_role="supervisor", user_name="Preview", preview_mode=True))


@app.get("/api/leads-dashboard/overview")
async def overview(period_start=None, period_end=None, compare=None, period_preset=None):
    return get_leads_dashboard_overview(
        period_start=period_start,
        period_end=period_end,
        compare=compare,
        period_preset=period_preset,
    )


@app.get("/api/leads-dashboard/properties-inventory")
async def properties_inventory(period_start=None, period_end=None, operation=None,
                               property_type=None, commune=None, responsible=None):
    filters = {key: value for key, value in {
        "operation": operation, "property_type": property_type,
        "commune": commune, "responsible": responsible,
    }.items() if value}
    return await asyncio.to_thread(
        get_properties_inventory_dashboard,
        period_start=period_start, period_end=period_end, filters=filters,
    )


@app.get("/api/leads-dashboard/capture-simulator")
async def capture_simulator(operation=None, property_type=None, commune=None, price=None, bedrooms=None, bathrooms=None, surface=None, period_end=None):
    params = {"operation": operation, "type": property_type, "commune": commune, "price": price, "bedrooms": bedrooms, "bathrooms": bathrooms, "surface": surface}
    return await asyncio.to_thread(get_capture_simulation, params=params, period_end=period_end)


@app.get("/api/leads-dashboard/operations")
async def operations(period_start=None, period_end=None, compare="auto", period_preset=None, executive=None,
                     temperature=None, stage=None, priority=None,
                     assignment=None, search=None, portfolio=None):
    filters = {key: value for key, value in {
        "executive": executive, "temperature": temperature, "stage": stage,
        "priority": priority, "assignment": assignment, "search": search,
        "portfolio": portfolio,
    }.items() if value}
    timing = {}
    payload = await asyncio.to_thread(
        get_leads_operational_dashboard,
        period_start=period_start, period_end=period_end,
        compare=compare, period_preset=period_preset,
        role="supervisor", user_name="Preview", filters=filters, timing=timing,
    )
    response = JSONResponse(payload)
    response.headers["Server-Timing"] = ", ".join(
        item for item in (
            f'cache;desc="{timing.get("cache")}"' if timing.get("cache") else None,
            f'current;dur={timing.get("current_query_ms")}' if timing.get("current_query_ms") is not None else None,
            f'period;dur={timing.get("period_query_ms")}' if timing.get("period_query_ms") is not None else None,
            f'cycles;dur={timing.get("assignment_cycles_ms")}' if timing.get("assignment_cycles_ms") is not None else None,
            f'activity;dur={timing.get("activity_results_ms")}' if timing.get("activity_results_ms") is not None else None,
            f'python;dur={timing.get("transform_ms")}' if timing.get("transform_ms") is not None else None,
            f'compare;dur={(timing.get("comparable") or {}).get("total_ms")}' if (timing.get("comparable") or {}).get("total_ms") is not None else None,
            f'backend;dur={timing.get("total_ms")}' if timing.get("total_ms") is not None else None,
        ) if item
    )
    response.headers["X-Analytics-Mongo-Calls"] = str(timing.get("mongo_calls_total", timing.get("mongo_calls", 0)))
    return response


@app.get("/api/leads-dashboard/operations/portfolios")
async def operations_portfolios():
    return await asyncio.to_thread(get_operational_portfolios)


@app.get("/api/leads-dashboard/operations/executives")
async def operations_executives(period_start=None, period_end=None):
    return await asyncio.to_thread(
        get_operational_executive_performance,
        period_start=period_start,
        period_end=period_end,
    )


@app.get("/api/leads-dashboard/operations/control-breakdown")
async def operations_control_breakdown(period_start=None, period_end=None, executive=None):
    return await asyncio.to_thread(
        get_leads_control_breakdown,
        period_start=period_start,
        period_end=period_end,
        executive=executive,
    )


@app.get("/api/leads-dashboard/operations/control-monthly")
async def operations_control_monthly(period_start=None, period_end=None, executive=None,
                                     compare="auto", period_preset="custom"):
    return await asyncio.to_thread(
        get_monthly_leads_control,
        period_start=period_start,
        period_end=period_end,
        executive=executive,
        compare=compare,
        period_preset=period_preset,
    )


@app.get("/api/leads-dashboard/operations/control-portfolio-demand")
async def operations_control_portfolio_demand(period_start=None, period_end=None, executive=None):
    return await asyncio.to_thread(
        get_monthly_control_portfolio_demand,
        period_start=period_start, period_end=period_end, executive=executive,
    )


@app.get("/api/leads-dashboard/operations/control-portfolio-property-leads")
async def operations_control_portfolio_property_leads(period_start=None, period_end=None, executive=None, property_code=None):
    return await asyncio.to_thread(
        get_monthly_control_property_leads,
        period_start=period_start, period_end=period_end,
        executive=executive, property_code=property_code,
    )


@app.get("/api/leads-dashboard/operations/control-portfolio-conversation")
async def operations_control_portfolio_conversation(period_start=None, period_end=None, executive=None,
                                                    property_code=None, lead_id=None, assignment_cycle_id=None):
    # This preview binds only to loopback and impersonates the dashboard's
    # existing supervisor preview role; production endpoints enforce session roles.
    return await asyncio.to_thread(
        get_monthly_control_lead_conversation,
        period_start=period_start, period_end=period_end, executive=executive,
        property_code=property_code, lead_id=lead_id,
        assignment_cycle_id=assignment_cycle_id,
    )


@app.get("/api/leads-dashboard/operations/control-monthly/executives")
async def operations_control_monthly_executives(period_start=None, period_end=None):
    return await asyncio.to_thread(
        get_monthly_control_executives,
        period_start=period_start,
        period_end=period_end,
    )


@app.get("/api/leads-dashboard/captacion-management")
async def operations_control_captacion_management(executive=None, period_start=None, period_end=None):
    """Resumen de solo lectura de Captación para la vista local de Control."""
    from captacion_goals import (
        _name_key,
        _as_chile_datetime,
        get_captacion_goal_dashboard,
        get_captacion_management_rows,
    )
    from captacion_management import summarize_grouped_outcomes
    from chatbot.storage import get_async_db, get_db

    db = get_db()
    payload = await asyncio.to_thread(
        get_captacion_goal_dashboard,
        db,
        selected_executive=executive or None,
        period_start=period_start or None,
        period_end=period_end or None,
        excluded_executives=("Control Owner",),
        include_control=True,
        ensure_indexes=False,
    )

    # Reutiliza el mismo snapshot de las cuatro tarjetas superiores del módulo
    # Captación. Los KPI de cobertura/contactabilidad describen cartera actual;
    # no se deben reconstruir con los eventos del período de Control.
    try:
        from captacion_kpis import VISIBLE_CLASSIFICATION_STATES
        from webhook import _load_captacion_kpi_snapshot

        card_query = {
            "origen": {"$exists": True, "$nin": [None, ""]},
            "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)},
        }
        if executive:
            selected_id = str(payload.get("user_id") or "").strip()
            selected_name = str(payload.get("name") or executive).strip()
            if selected_id:
                card_query["$or"] = [
                    {"gestion.ejecutivo_id": selected_id},
                    {"$and": [
                        {"gestion.ejecutivo_id": {"$exists": False}},
                        {"gestion.ejecutivo_asignado": selected_name},
                    ]},
                ]
            else:
                card_query["gestion.ejecutivo_asignado"] = selected_name
        payload["module_cards"] = await _load_captacion_kpi_snapshot(get_async_db(), card_query)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("No fue posible cargar el snapshot de tarjetas de Captación")
        payload["module_cards"] = None

    # El desglose inferior es histórico acumulado, independiente de las fechas
    # del filtro. Se reutiliza el ledger acreditado y su deduplicación canónica
    # por propiedad, ejecutivo y día; nunca se cuentan eventos crudos.
    try:
        history_end = _as_chile_datetime(None).date().isoformat()
        historical_rows = await asyncio.to_thread(
            get_captacion_management_rows,
            db,
            period_start="2000-01-01",
            period_end=history_end,
        )
        if payload.get("mode") == "individual":
            selected_id = str(payload.get("user_id") or "").strip()
            selected_name = _name_key(payload.get("name") or executive)
            historical_scope = [
                row for row in historical_rows
                if (selected_id and str(row.get("actor_user_id") or "").strip() == selected_id)
                or (selected_name and _name_key(row.get("actor")) == selected_name)
            ]
        else:
            executives = payload.get("executives") or []
            active_ids = {str(row.get("user_id") or "").strip() for row in executives if row.get("user_id")}
            active_names = {_name_key(row.get("name")) for row in executives if row.get("name")}
            historical_scope = [
                row for row in historical_rows
                if str(row.get("actor_user_id") or "").strip() in active_ids
                or _name_key(row.get("actor")) in active_names
            ]
        historical_summary = summarize_grouped_outcomes(historical_scope)
        historical_details = historical_summary.get("detailed_outcomes") or {}
        historical_labels = historical_summary.get("detail_labels") or {}
        payload["historical_response_breakdown"] = [
            {"key": key, "label": historical_labels.get(key, key), "count": int(value or 0)}
            for key, value in historical_details.items()
            if int(value or 0) > 0
        ]
        payload["historical_response_total"] = sum(
            item["count"] for item in payload["historical_response_breakdown"]
        )
        activity_by_day = {}
        for unit in historical_summary.get("units") or []:
            day_key = str(unit.get("local_date") or "")
            if day_key:
                activity_by_day[day_key] = activity_by_day.get(day_key, 0) + 1
        payload["historical_activity_daily"] = [
            {"date": day_key, "count": value}
            for day_key, value in sorted(activity_by_day.items())
        ]
        activity_by_week = {}
        for day_key, value in activity_by_day.items():
            day = date.fromisoformat(day_key)
            week_start = day - timedelta(days=day.weekday())
            week_key = week_start.isoformat()
            iso_calendar = day.isocalendar()
            if week_key not in activity_by_week:
                activity_by_week[week_key] = {
                    "week_start": week_key,
                    "week_number": int(iso_calendar.week),
                    "week_year": int(iso_calendar.year),
                    "count": 0,
                }
            activity_by_week[week_key]["count"] += value
        payload["historical_activity_weekly"] = [
            activity_by_week[week_key]
            for week_key in sorted(activity_by_week)
        ]
        payload["historical_response_available"] = True
    except Exception:
        import logging
        logging.getLogger(__name__).exception("No fue posible cargar el desglose histórico de gestión de Captación")
        payload["historical_response_breakdown"] = None
        payload["historical_response_total"] = None
        payload["historical_activity_daily"] = None
        payload["historical_activity_weekly"] = None
        payload["historical_response_available"] = False
    return payload


@app.get("/crm/lead-id/{lead_id}", response_class=HTMLResponse)
async def preview_crm_lead_detail(request: Request, lead_id: str):
    """Render the existing Leads module detail in a read-only local preview."""
    from bson import ObjectId
    from bson.errors import InvalidId
    from chatbot.storage import get_db
    from api_crm import get_lead_detail_data

    try:
        lead_oid = ObjectId(lead_id)
    except (InvalidId, TypeError):
        return HTMLResponse("Lead no encontrado", status_code=404)

    db = get_db()
    lead = await asyncio.to_thread(lambda: db["leads"].find_one({"_id": lead_oid}))
    if not lead:
        return HTMLResponse("Lead no encontrado", status_code=404)
    detail = await asyncio.to_thread(get_lead_detail_data, lead.get("phone") or "", lead_doc=lead)
    if not detail:
        return HTMLResponse("Lead no encontrado", status_code=404)
    detail["lead_id"] = str(lead.get("_id") or "")
    detail["phone_visibility"] = "full"

    response = crm_templates.TemplateResponse(request, "crm_lead_detail.html", {
        "request": request,
        "lead": detail,
        "user_email": "Vista previa local",
        "user_role": "supervisor",
        "user_name": "Preview",
        "crm_sla_security_layer_enabled": False,
        "lead_id": str(lead.get("_id") or ""),
    })
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


if __name__ == "__main__":
    import threading
    import webbrowser

    url = "http://localhost:8001/leads-dashboard"
    print("PREVIEW LOCAL: " + url)
    print("Abriendo el navegador en 2 segundos... (si no se abre, pega la URL manualmente)")
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
