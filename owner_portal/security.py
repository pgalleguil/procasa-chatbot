"""Fail-closed access gate for the local/internal preview route."""

from __future__ import annotations

import inspect
import os
from typing import Any

from fastapi import HTTPException, Request


def _is_local_request(request: Request) -> bool:
    host = (request.client.host if request.client else "").split(":", 1)[0].lower()
    return host in {"127.0.0.1", "::1", "localhost"}


async def _existing_crm_user(request: Request) -> Any:
    """Reuse the application's existing CRM auth without importing it at load time."""

    try:
        import webhook

        dependency = getattr(webhook, "get_current_user_doc", None)
        if dependency is None:
            raise RuntimeError("CRM auth dependency is unavailable")
        result = dependency(request)
        return await result if inspect.isawaitable(result) else result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="CRM authentication required") from exc


async def require_internal_preview(request: Request) -> Any:
    """Allow an explicit local dev gate or an already authenticated CRM user."""

    dev_mode = os.getenv("OWNER_PORTAL_PREVIEW_DEV_MODE", "false").strip().lower() == "true"
    if dev_mode and _is_local_request(request):
        return {"auth": "local_dev_gate"}
    return await _existing_crm_user(request)
