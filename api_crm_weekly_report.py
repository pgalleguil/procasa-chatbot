from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import jwt

from config import Config
from chatbot.crm_weekly_report import cancel_report, get_report, list_reports, regenerate_narrative
from chatbot.storage import get_async_db

router = APIRouter(tags=["CRM Weekly Report"])
templates = Jinja2Templates(directory="templates")


async def admin_user(request):
    token = request.cookies.get("access_token")
    if not token: raise HTTPException(status_code=401, detail="SesiÃ³n requerida")
    try: username = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"]).get("sub")
    except Exception as exc: raise HTTPException(status_code=401, detail="SesiÃ³n invÃ¡lida") from exc
    user = await get_async_db()["usuarios"].find_one({"username": username})
    if not user or user.get("rol") not in {"admin", "supervisor", "jefatura"}:
        raise HTTPException(status_code=403, detail="Permiso administrativo requerido")
    return user


@router.get("/crm/weekly-reports")
async def weekly_reports_view(request: Request, report_id: str = Query(None)):
    user = await admin_user(request)
    reports = await list_reports()
    report = await get_report(report_id) if report_id else (reports[0] if reports else None)
    return templates.TemplateResponse("crm_weekly_reports.html", {"request": request, "report": report,
                                      "reports": reports, "user": user,
                                      "group_configured": bool(getattr(Config, "CRM_WEEKLY_REPORT_GROUP_ID", None))})


@router.post("/api/crm/weekly-report/{report_id}/regenerate")
async def regenerate(request: Request, report_id: str):
    user = await admin_user(request)
    return await regenerate_narrative(report_id, user.get("nombre") or user.get("username"))


@router.post("/api/crm/weekly-report/{report_id}/cancel")
async def cancel(request: Request, report_id: str):
    user = await admin_user(request)
    return await cancel_report(report_id, user.get("nombre") or user.get("username"))
