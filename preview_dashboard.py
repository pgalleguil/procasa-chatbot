"""Servidor local de PREVIEW del Leads Dashboard (solo lectura, SIN tareas de fondo).

Sirve unicamente la pagina /leads-dashboard y su endpoint de datos.
No inicia loops de WhatsApp, reportes, reasignaciones ni nada que escriba datos.

Uso:
    python preview_dashboard.py

Luego abre:  http://localhost:8001/leads-dashboard
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

templates = Environment(loader=FileSystemLoader(str(ROOT / "templates")))

from analytics.leads_service import get_leads_dashboard_overview, get_leads_operational_dashboard

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
async def operations(period_start=None, period_end=None, executive=None,
                     temperature=None, stage=None, priority=None,
                     assignment=None, search=None):
    filters = {key: value for key, value in {
        "executive": executive, "temperature": temperature, "stage": stage,
        "priority": priority, "assignment": assignment, "search": search,
    }.items() if value}
    return get_leads_operational_dashboard(
        period_start=period_start, period_end=period_end,
        role="supervisor", user_name="Preview", filters=filters,
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
