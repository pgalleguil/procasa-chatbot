"""Servidor local de PREVIEW del Leads Dashboard (solo lectura, SIN tareas de fondo).

Sirve unicamente la pagina /leads-dashboard y su endpoint de datos.
No inicia loops de WhatsApp, reportes, reasignaciones ni nada que escriba datos.

Uso:
    python preview_dashboard.py

Luego abre:  http://localhost:8001/leads-dashboard
"""
import sys
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

templates = Environment(loader=FileSystemLoader(str(ROOT / "templates")))

from analytics.leads_service import get_leads_dashboard_overview, get_leads_operational_dashboard, get_operational_executive_performance, get_operational_portfolios, get_properties_inventory_dashboard, get_capture_simulation
from review_fixtures import territorial_review_payload

app = FastAPI(title="Leads Dashboard - Preview local (solo lectura)")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/leads-dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    template = templates.get_template("leads_dashboard.html")
    review = request.query_params.get("territorial_review") == "1"
    return HTMLResponse(template.render(user_role="supervisor", user_name="Preview", territorial_review=review))


def _territorial_review_payload():
    regions = [
        ("Arica y Parinacota", 2), ("Tarapacá", 3), ("Antofagasta", 4), ("Atacama", 2),
        ("Coquimbo", 17), ("Valparaíso", 53), ("Metropolitana", 175), ("Bernardo O'Higgins", 3),
        ("Maule", 24), ("Ñuble", 0), ("Biobío", 14), ("La Araucanía", 0),
        ("Los Ríos", 0), ("Los Lagos", 0), ("Aysén", 0), ("Magallanes", 0),
    ]
    total = sum(value for _, value in regions)
    geography = [{
        "name": name, "geo_key": name.lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u").replace("ñ", "n").replace(" ", " "),
        "leads": value, "demand_share_pct": round(value / total * 100, 1) if total else 0,
        "properties_with_demand": max(0, round(value * .44)), "leads_per_property": round(value / max(1, round(value * .44)), 2),
        "stock_sucre": max(0, round(value * .55)), "supply_share_pct": round(value / total * 100 * .8, 1) if total else 0,
        "gap_pp": round(value / total * 100 - value / total * 100 * .8, 1) if total else 0,
        "top_segments": [],
    } for name, value in regions]
    return {
        "inventory": {"active": 442}, "demand": {"leads": total, "properties_with_demand": 119, "coverage_pct": 26.9, "qualified_signals": {"contact_effective": 74}},
        "meta": {"period_start": "2026-07-19", "period_end": "2026-08-17"},
        "data_quality": {"price_dimension": {}, "bedrooms_dimension": {}}, "attribution": {"coverage_pct": 97.4, "leads_with_identifiable_office": total, "leads_total": total},
        "demand_intelligence": {"dimensions": {key: [] for key in ("operation", "type", "zone_rm", "price_range", "bedrooms")}, "geography": {"region": geography, "commune": [], "metric_rule": {"default": "leads"}, "matching": {}}},
        "opportunities": [], "benchmark": {}, "simulator_options": {"types": [], "communes": []}, "forecast": {"available": False},
    }


@app.get("/api/leads-dashboard/territorial-review")
async def territorial_review():
    return _territorial_review_payload()


@app.get("/api/review/leads-dashboard")
async def territorial_review_public():
    return territorial_review_payload()


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
    return get_operational_portfolios()


@app.get("/api/leads-dashboard/operations/executives")
async def operations_executives(period_start=None, period_end=None):
    return get_operational_executive_performance(
        period_start=period_start, period_end=period_end,
    )


if __name__ == "__main__":
    import threading
    import webbrowser

    url = "http://localhost:8001/leads-dashboard"
    print("PREVIEW LOCAL: " + url)
    print("Abriendo el navegador en 2 segundos... (si no se abre, pega la URL manualmente)")
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
