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

from analytics.leads_service import get_leads_dashboard_overview, get_leads_operational_dashboard, get_operational_executive_performance, get_operational_portfolios

app = FastAPI(title="Leads Dashboard - Preview local (solo lectura)")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/leads-dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    template = templates.get_template("leads_dashboard.html")
    return HTMLResponse(template.render(user_role="supervisor", user_name="Preview"))


@app.get("/api/leads-dashboard/overview")
async def overview(period_start=None, period_end=None, compare=None, period_preset=None):
    return get_leads_dashboard_overview(
        period_start=period_start,
        period_end=period_end,
        compare=compare,
        period_preset=period_preset,
    )


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
