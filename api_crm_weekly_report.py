from fastapi import APIRouter, HTTPException, Request
from jose import jwt

from config import Config
from chatbot.crm_weekly_report import approve_and_send, cancel_report, regenerate_narrative
from chatbot.storage import get_async_db

router = APIRouter(tags=["CRM Weekly Report"])


async def admin_user(request):
    token = request.cookies.get("access_token")
    if not token: raise HTTPException(status_code=401, detail="SesiÃ³n requerida")
    try: username = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"]).get("sub")
    except Exception as exc: raise HTTPException(status_code=401, detail="SesiÃ³n invÃ¡lida") from exc
    user = await get_async_db()["usuarios"].find_one({"username": username})
    if not user or user.get("rol") not in {"admin", "supervisor", "jefatura"}:
        raise HTTPException(status_code=403, detail="Permiso administrativo requerido")
    return user


@router.post("/api/crm/weekly-report/{report_id}/regenerate")
async def regenerate(request: Request, report_id: str):
    user = await admin_user(request)
    return await regenerate_narrative(report_id, user.get("nombre") or user.get("username"))


@router.post("/api/crm/weekly-report/{report_id}/cancel")
async def cancel(request: Request, report_id: str):
    user = await admin_user(request)
    return await cancel_report(report_id, user.get("nombre") or user.get("username"))


@router.post("/api/crm/weekly-report/{report_id}/approve-send")
async def approve_send(request: Request, report_id: str):
    user = await admin_user(request)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return await approve_and_send(report_id, user.get("nombre") or user.get("username"), body.get("final_text"))
