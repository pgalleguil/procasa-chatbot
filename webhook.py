# --- START OF FILE webhook.py ---

# webhook.py → BOT PRO 2025 CON LOGIN REAL + DASHBOARD + CAMPAÑAS 100% ORIGINALES
import asyncio
import logging
import os
import time
import hmac
import hashlib
from typing import Dict, Any
import re
import secrets
import traceback
import threading
import subprocess
import concurrent.futures
import inspect
from concurrent.futures import ThreadPoolExecutor
from pymongo import MongoClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import uvicorn
import json
import pytz # Importante para la hora local
from chatbot.storage import observability_mark, observability_snapshot_and_reset, observability_event_loop_blocked_recent, run_in_threadpool

# ========================= THREAD POOL CONTROLADO =========================
# Pool separado para request web (evita que tareas batch bloqueen respuestas HTTP).
_WEB_THREAD_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="procasa_web")
# Pool separado para workers de procesamiento de leads.
_WORKER_THREAD_POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix="procasa_worker")
# Strictly limited PROCESS_SERVICE pool; it cannot consume chatbot/delivery capacity.
_PROCESS_THREAD_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="procasa_process")

# Instrumentación de diagnóstico solamente: identifica la primera entrada al
# módulo dentro del proceso sin alterar el flujo de la solicitud.
_CAPTACION_DIAG_FIRST_VISIT_LOCK = threading.Lock()
_CAPTACION_DIAG_FIRST_VISIT_SEEN = False


def _captacion_diag_is_first_module_visit() -> bool:
    global _CAPTACION_DIAG_FIRST_VISIT_SEEN
    with _CAPTACION_DIAG_FIRST_VISIT_LOCK:
        if _CAPTACION_DIAG_FIRST_VISIT_SEEN:
            return False
        _CAPTACION_DIAG_FIRST_VISIT_SEEN = True
        return True
# Pool dedicado para tareas periódicas (cache warmer) para evitar competir con workers.
_WARMER_THREAD_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="procasa_warmer")

# === NUEVAS IMPORTACIONES PARA GOOGLE ===
import httpx 
from urllib.parse import urlencode, urlsplit

import requests
from fastapi import FastAPI, Cookie, Request, HTTPException, Depends, status, Form, Header, Query, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.encoders import jsonable_encoder
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

# === TUS MÓDULOS PROPIOS ===
from campanas.handler import handle_campana_respuesta
from retiro.handler import handle_retiro_confirmacion, handle_solicitud_contacto
from api_leads_intelligence import get_leads_executive_report, get_specific_lead_chat
from api_crm import get_crm_leads_list, get_lead_detail_data, update_lead_crm_data, log_crm_event, manage_crm_notes, get_unique_executives, get_semantic_recommendations, log_recommendation_sent, normalize_crm_temperature

# ---- ANALYTICS (READ-ONLY) ----
from analytics.leads_service import (
    get_summary, get_trends, get_distributions, get_table as analytics_get_table,
    get_detail as analytics_get_detail, get_filters, get_field_coverage,
    get_dashboard, get_commercial_dashboard, get_commercial_filter_options,
    get_leads_dashboard_overview, get_leads_operational_dashboard,
    get_operational_executive_performance, get_operational_portfolios,
    get_properties_inventory_dashboard, get_capture_simulation,
)

from api_captacion import (
    get_captacion_list, get_captacion_detail, update_captacion_status, update_contact_info,
    distribute_sourced_leads, release_stale_captaciones, redistribute_inactive_agent_captaciones,
    format_relative_time as format_captacion_time, format_captacion_portal_label,
    get_personal_templates, save_personal_template, delete_personal_template,
    warm_captacion_shared_catalogs,
)
from captacion_kpis import VISIBLE_CLASSIFICATION_STATES, build_kpi_queries
from captacion_goals import (
    CAPTACION_PRIVILEGED_ROLES,
    CAPTACION_GOAL_SNAPSHOT_COLLECTION,
    can_manage_captacion,
    get_captacion_goal_dashboard,
    load_captacion_goal_snapshot,
    save_captacion_goal_snapshot,
)
from captacion_workforce import (
    create_work_exception,
    upsert_calendar_day,
    upsert_membership,
)
from chatbot.followup_tracking import (
    FollowupTokenError,
    build_followup_open_url,
    find_tracked_task,
    record_captacion_detail_open,
    record_followup_event,
    record_followup_open,
    verify_followup_token,
)
from chatbot.manual_entry import create_manual_lead, check_lead_duplicate, resolve_property_code
from chatbot.processing_service import LeadProcessingService
from chatbot.crm_permissions import (
    can_administer_leads,
    lead_is_assigned_to_user,
    payload_attempts_reassignment,
)
from chatbot.crm_service import CrmService
from chatbot.captacion_weekly_report import (
    REPORT_COLLECTION as CAPTACION_WEEKLY_REPORT_COLLECTION,
    acknowledge_outcome_review,
    approve_and_send_report,
    cancel_report,
    create_weekly_report,
    record_delivery_status_webhook,
    regenerate_report_narrative,
    send_test_report,
)

# ========================= CONFIGURACIÓN =========================
from config import Config
from review_fixtures import territorial_review_payload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("procasa-full")

# La tarjeta de rendimiento por portal depende de un campo que no existía en
# snapshots anteriores. La versión forma parte de la key y del payload para
# que un proceso caliente nunca reutilice silenciosamente un esquema viejo.
# Incremento de versión para que cualquier proceso recargado invalide snapshots KPI antiguos.
CAPTACION_KPI_CACHE_VERSION = "v13"
CAPTACION_KPI_CACHE_SCHEMA = 4
CAPTACION_GOAL_EXCLUDED_EXECUTIVES = ("Pablo Galleguillos",)

# --- BLOCKING DETECTOR (temporal forensics) ---
_ORIG_TIME_SLEEP = time.sleep
_ORIG_SUBPROCESS_RUN = subprocess.run
_ORIG_FUTURE_RESULT = concurrent.futures.Future.result

def _in_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False

def _forensic_sleep(seconds):
    if _in_event_loop_thread():
        logger.warning(f"[BLOCKING_DETECTOR] time.sleep({seconds}) llamado dentro de async/event loop")
    return _ORIG_TIME_SLEEP(seconds)

def _forensic_subprocess_run(*args, **kwargs):
    if _in_event_loop_thread():
        try:
            stack = inspect.stack()
            project_frames = [
                fr for fr in stack
                if fr.filename and ("\\ChatBot_v4_Grok\\" in fr.filename or "/ChatBot_v4_Grok/" in fr.filename)
            ]
            short = " > ".join(
                f"{os.path.basename(fr.filename)}:{fr.function}:{fr.lineno}" for fr in project_frames[:5]
            ) if project_frames else "stack_no_disponible"
            logger.warning(f"[BLOCKING_DETECTOR] subprocess.run llamado dentro de async/event loop stack={short}")
        except Exception:
            logger.warning("[BLOCKING_DETECTOR] subprocess.run llamado dentro de async/event loop")
        logger.info("[ASYNC_FIX] subprocess forced to threadpool")
        result_box = {}
        error_box = {}

        def _runner():
            try:
                result_box["value"] = asyncio.run(asyncio.to_thread(_ORIG_SUBPROCESS_RUN, *args, **kwargs))
            except Exception as exc:
                error_box["error"] = exc

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        if "error" in error_box:
            raise error_box["error"]
        return result_box.get("value")
    return _ORIG_SUBPROCESS_RUN(*args, **kwargs)

def _forensic_future_result(self, *args, **kwargs):
    if _in_event_loop_thread():
        try:
            stack = inspect.stack()
            project_frames = [
                fr for fr in stack
                if fr.filename and ("\\ChatBot_v4_Grok\\" in fr.filename or "/ChatBot_v4_Grok/" in fr.filename)
            ]
            if project_frames:
                short = " > ".join(
                    f"{os.path.basename(fr.filename)}:{fr.function}:{fr.lineno}" for fr in project_frames[:4]
                )
                logger.warning(f"[BLOCKING_DETECTOR] Future.result() llamado dentro de async/event loop stack={short}")
        except Exception:
            logger.warning("[BLOCKING_DETECTOR] Future.result() llamado dentro de async/event loop")
    return _ORIG_FUTURE_RESULT(self, *args, **kwargs)

time.sleep = _forensic_sleep
subprocess.run = _forensic_subprocess_run
concurrent.futures.Future.result = _forensic_future_result

# CONFIGURACIÓN ZONA HORARIA CHILE
from chatbot.constants import CHILE_TZ

# --- CONFIGURACIÓN DE DIRECTORIOS ---
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Global state for background tasks monitoring
background_tasks_status = {
    "notifications_loop": {"status": "starting", "last_heartbeat": None},
    "sla_monitor": {"status": "starting", "last_heartbeat": None},
    "task_monitor": {"status": "starting", "last_heartbeat": None},
    "captacion_reminder": {"status": "starting", "last_heartbeat": None},
    "prop360_poll": {"status": "starting", "last_heartbeat": None},
    "ficha_sync": {"status": "starting", "last_heartbeat": None},
    "captacion_distributor": {"status": "post_scrape_trigger", "last_heartbeat": None},
    "lead_processing": {"status": "starting", "last_heartbeat": None}
}
_OAUTH_HTTP_CLIENT = None

def _fetch_captacion_executives_catalog():
    """Carga el catálogo reutilizable de ejecutivos fuera del request."""
    from chatbot.storage import get_db
    db = get_db()
    result = list(db["usuarios"].find(
        {"is_active": True, "rol": "agente", "comunas_interes_norm": {"$exists": True, "$ne": []}},
        {"nombre": 1}
    ).sort("nombre", 1))
    names = [item.get("nombre", "") for item in result if item.get("nombre")]
    names.insert(0, "Sin asignar")
    return names


async def _prewarm_captacion_shared_data():
    """Precalienta datos compartidos sin bloquear el arranque HTTP."""
    warm_started = time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        catalog_task = loop.run_in_executor(
            _WARMER_THREAD_POOL,
            warm_captacion_shared_catalogs,
        )
        executives_task = loop.run_in_executor(
            _WEB_THREAD_POOL,
            _fetch_captacion_executives_catalog,
        )
        catalog_result, executives = await asyncio.gather(catalog_task, executives_task)
        app.state.captacion_executives_cache = {
            "time": time.time(),
            "data": executives,
        }
        logger.info(
            "[CAPTACION_WARMER] shared caches ready total_ms=%.0f catalogs_ms=%s executives=%s",
            (time.perf_counter() - warm_started) * 1000,
            catalog_result.get("elapsed_ms", "n/a"),
            len(executives),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[CAPTACION_WARMER] shared cache prewarm failed")


def _put_captacion_goal_cache(cache_key, data):
    cache = getattr(app.state, "captacion_goal_cache", {})
    cache[cache_key] = {"time": time.time(), "data": data}
    app.state.captacion_goal_cache = cache


def _captacion_goal_cache_key(selected_executive=None, period_start=None, today=None):
    key = f"goal_v2_{selected_executive or '_none'}"
    if period_start:
        return f"{key}_{period_start}"
    current_day = today or datetime.now(pytz.timezone("America/Santiago")).date().isoformat()
    return f"{key}_{current_day}"


def _captacion_goal_snapshot_is_current(snapshot):
    """Evita servir un snapshot del día anterior a una gestión confirmada."""
    invalidated_at = getattr(app.state, "captacion_goal_snapshot_invalidated_at", 0)
    if not invalidated_at:
        return True

    snapshot_timestamp = snapshot.get("timestamp") if snapshot else None
    if isinstance(snapshot_timestamp, datetime):
        if snapshot_timestamp.tzinfo is None:
            snapshot_timestamp = snapshot_timestamp.replace(tzinfo=timezone.utc)
        snapshot_epoch = snapshot_timestamp.timestamp()
    elif isinstance(snapshot_timestamp, str):
        try:
            parsed_timestamp = datetime.fromisoformat(snapshot_timestamp.replace("Z", "+00:00"))
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
            snapshot_epoch = parsed_timestamp.timestamp()
        except ValueError:
            return False
    else:
        return False
    return snapshot_epoch >= invalidated_at


def _invalidate_captacion_goal_cache():
    """Invalida la caché local y marca obsoleto el snapshot actual."""
    app.state.captacion_goal_cache = {}
    app.state.captacion_goal_snapshot_invalidated_at = time.time()


def _delete_current_captacion_goal_snapshots():
    """Elimina snapshots del día actual para no restaurar datos anteriores tras reinicio."""
    try:
        from chatbot.storage import get_db

        current_day = datetime.now(pytz.timezone("America/Santiago")).date().isoformat()
        result = get_db()[CAPTACION_GOAL_SNAPSHOT_COLLECTION].delete_many({
            "_id": {"$regex": rf":{re.escape(current_day)}$"}
        })
        return result.deleted_count
    except Exception:
        logger.exception("[CAPTACION_GOAL_SNAPSHOT] invalidación persistente fallida")
        return 0


async def _refresh_captacion_goal_snapshot(
    cache_key,
    *,
    selected_executive=None,
    period_start=None,
    period_end=None,
    excluded_executives=None,
    perf_context=None,
):
    """Calcula una meta y la persiste; el cálculo pesado vive en un hilo."""
    started = time.perf_counter()

    def _load():
        from chatbot.storage import get_db

        db = get_db()
        data = get_captacion_goal_dashboard(
            db,
            selected_executive=selected_executive,
            period_start=period_start,
            period_end=period_end,
            excluded_executives=excluded_executives,
            perf_context=perf_context,
            include_control=False,
            # Los índices de metas ya se preparan una vez durante startup.
            ensure_indexes=False,
        )
        try:
            save_captacion_goal_snapshot(
                db,
                data,
                selected_executive=selected_executive,
                period_start=period_start,
                period_end=period_end,
                excluded_executives=excluded_executives,
            )
        except Exception:
            # El snapshot es una aceleración; nunca debe impedir una respuesta
            # correcta si Mongo rechaza su persistencia.
            logger.exception("[CAPTACION_GOAL_SNAPSHOT] no se pudo guardar")
        return data

    data = await asyncio.get_running_loop().run_in_executor(_WEB_THREAD_POOL, _load)
    _put_captacion_goal_cache(cache_key, data)
    logger.info(
        "[CAPTACION_WARMER] goal refresh ready total_ms=%.0f key=%s",
        (time.perf_counter() - started) * 1000,
        cache_key,
    )
    return data


def _start_captacion_goal_refresh(
    cache_key,
    *,
    selected_executive=None,
    period_start=None,
    period_end=None,
    excluded_executives=None,
    perf_context=None,
):
    """Single-flight por equipo/ejecutivo y período, sin cálculos duplicados."""
    inflight = getattr(app.state, "captacion_goal_inflight", {})
    existing = inflight.get(cache_key)
    if existing is not None and not existing.done():
        return existing

    task = asyncio.create_task(
        _refresh_captacion_goal_snapshot(
            cache_key,
            selected_executive=selected_executive,
            period_start=period_start,
            period_end=period_end,
            excluded_executives=excluded_executives,
            perf_context=perf_context,
        )
    )
    inflight[cache_key] = task
    app.state.captacion_goal_inflight = inflight

    def _finish(done_task):
        if inflight.get(cache_key) is done_task:
            inflight.pop(cache_key, None)
        try:
            done_task.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[CAPTACION_GOAL_SINGLE_FLIGHT] refresh failed")

    task.add_done_callback(_finish)
    return task


async def _read_captacion_goal_snapshot(
    *,
    selected_executive=None,
    period_start=None,
    period_end=None,
    excluded_executives=None,
):
    def _read():
        from chatbot.storage import get_db

        return load_captacion_goal_snapshot(
            get_db(),
            selected_executive=selected_executive,
            period_start=period_start,
            period_end=period_end,
            excluded_executives=excluded_executives,
        )

    return await asyncio.get_running_loop().run_in_executor(_WEB_THREAD_POOL, _read)


async def _prewarm_captacion_default_goal():
    """Carga snapshot rápido y refresca la meta sin bloquear readiness."""
    started = time.perf_counter()
    try:
        excluded_executives = CAPTACION_GOAL_EXCLUDED_EXECUTIVES
        cache_key = _captacion_goal_cache_key()
        snapshot = await _read_captacion_goal_snapshot(
            excluded_executives=excluded_executives,
        )
        if snapshot:
            _put_captacion_goal_cache(cache_key, snapshot["data"])
            _start_captacion_goal_refresh(
                cache_key,
                excluded_executives=excluded_executives,
            )
            logger.info(
                "[CAPTACION_WARMER] default goal snapshot ready total_ms=%.0f",
                (time.perf_counter() - started) * 1000,
            )
            return True

        refresh_task = _start_captacion_goal_refresh(
            cache_key,
            excluded_executives=excluded_executives,
        )
        await refresh_task
        logger.info(
            "[CAPTACION_WARMER] default goal cold ready total_ms=%.0f",
            (time.perf_counter() - started) * 1000,
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[CAPTACION_WARMER] default goal prewarm failed")
        return False


def _build_captacion_worked_portal_breakdown(rows, contactability_by_portal=None, broker_by_portal=None):
    """Resume resultados por portal usando solo propiedades ya trabajadas."""
    contactability_by_portal = contactability_by_portal or {}
    broker_by_portal = broker_by_portal or {}
    portal_rows = []
    for row in rows:
        value = str(row.get("_id") or "").strip() or "sin_origen"
        worked = int(row.get("worked") or 0)
        corredor = int(broker_by_portal.get(value, row.get("corredor") or 0) or 0)
        captadas = int(row.get("captadas") or 0)
        contactability = contactability_by_portal.get(value, {})
        portal_rows.append({
            "value": value,
            "label": "Sin origen" if value.casefold() == "sin_origen" else format_captacion_portal_label(value),
            "worked": worked,
            "corredor": corredor,
            "captadas": captadas,
            "contact_attempts": int(contactability.get("contact_attempts") or 0),
            "effective_contacts": int(contactability.get("effective_contacts") or 0),
        })
    portal_rows.sort(key=lambda row: (-row["worked"], row["label"].casefold()))
    if len(portal_rows) > 2:
        top_rows = portal_rows[:2]
        other_rows = portal_rows[2:]
        top_rows.append({
            "value": "otros",
            "label": "Otros",
            "worked": sum(row["worked"] for row in other_rows),
            "corredor": sum(row["corredor"] for row in other_rows),
            "captadas": sum(row["captadas"] for row in other_rows),
            "contact_attempts": sum(row["contact_attempts"] for row in other_rows),
            "effective_contacts": sum(row["effective_contacts"] for row in other_rows),
        })
        portal_rows = top_rows
    for row in portal_rows:
        worked = row["worked"]
        row["corredor_rate"] = round(row["corredor"] * 100 / worked, 1) if worked else None
        row["captura_rate"] = round(row["captadas"] * 100 / worked, 1) if worked else None
        attempts = row["contact_attempts"]
        row["contactability_pct"] = round(row["effective_contacts"] * 100 / attempts, 1) if attempts else None
    return portal_rows


def _captacion_kpi_cache_is_compatible(record):
    """Valida el contrato mínimo requerido por las cuatro cards superiores."""
    if not isinstance(record, dict):
        return False
    required_keys = {
        "time",
        "in_gestion_count",
        "captados_count",
        "descartados_count",
        "available_count",
        "comunas_clean",
        "source_counts",
        "contact_type_counts",
        "worked_portal_counts",
        "contact_attempts",
        "effective_contacts",
        "contactability_pct",
        "contactability_result_buckets",
        "contactability_insight",
        "pending_count",
        "ready_to_contact_count",
        "kpi_revision",
    }
    if record.get("schema_version") != CAPTACION_KPI_CACHE_SCHEMA:
        return False
    if not required_keys.issubset(record):
        return False
    portal_rows = record.get("worked_portal_counts")
    if not isinstance(portal_rows, list):
        return False
    worked_total = sum(
        int(record.get(key) or 0)
        for key in ("in_gestion_count", "captados_count", "descartados_count")
    )
    portal_total = sum(int(row.get("worked") or 0) for row in portal_rows if isinstance(row, dict))
    return (
        (worked_total == 0 or portal_total > 0)
        and all(
            isinstance(row, dict)
            and {"contact_attempts", "effective_contacts", "contactability_pct"}.issubset(row)
            for row in portal_rows
        )
    )


def _log_captacion_portal_card_consistency(worked_total, portal_rows):
    portal_total = sum(int(row.get("worked") or 0) for row in (portal_rows or []) if isinstance(row, dict))
    if worked_total > 0 and portal_total == 0:
        logger.error(
            "[CAPTACION_PORTAL_CARD_INCONSISTENT] worked_total=%s portal_total=%s",
            worked_total,
            portal_total,
        )


def _contactability_result_buckets(active_events):
    """Asigna exactamente un resultado sin contacto a cada propiedad única."""
    from captacion_management import CONTACT_RESULT_VALUES

    attempted = {
        str(row.get("property_id")) for row in active_events
        if row.get("event_type") == "management_confirmed" or row.get("contact_attempt")
    }
    effective = {str(row.get("property_id")) for row in active_events if row.get("contact_effective")}
    latest = {}
    for row in active_events:
        property_id = str(row.get("property_id"))
        if property_id not in attempted or property_id in effective:
            continue
        result = str(row.get("contact_result") or row.get("result") or "").strip().lower()
        if result not in CONTACT_RESULT_VALUES:
            continue
        occurred_at = str(row.get("occurred_at") or "")
        if property_id not in latest or occurred_at >= latest[property_id][0]:
            latest[property_id] = (occurred_at, result)

    labels = {
        "no_answer": "No respondió",
        "message_sent": "Mensaje enviado",
        "invalid_number": "Número inválido",
        "busy": "Ocupado",
    }
    counts = {}
    for property_id in attempted - effective:
        result = latest.get(property_id, ("", "other"))[1]
        key = result if result in labels else "other"
        counts[key] = counts.get(key, 0) + 1
    buckets = [
        {"key": key, "label": labels.get(key, "Otro resultado sin contacto"), "count": counts[key]}
        for key in ("no_answer", "message_sent", "invalid_number", "busy", "other")
        if counts.get(key)
    ]
    if not attempted:
        insight = "Aún no hay intentos suficientes para analizar la contactabilidad"
    elif not (attempted - effective):
        insight = "Todos los intentos registrados lograron contacto efectivo"
    else:
        highest = max((row["count"] for row in buckets), default=0)
        leaders = [row["label"] for row in buckets if row["count"] == highest]
        insight = (
            f"{leaders[0]} concentra la mayor parte de los resultados sin contacto"
            if len(leaders) == 1
            else "Los resultados sin contacto se concentran en " + " y ".join(leaders)
        )
    return buckets, insight


async def _load_captacion_canonical_contactability(adb, worked_property_rows):
    """Calcula contactabilidad desde el ledger canónico de Captaciones."""
    from captacion_management import LEDGER_COLLECTION, VALID_CREDIT_EVENT_TYPES, summarize_management_metrics

    property_origin = {
        str(row.get("_id")): str(row.get("origen") or "sin_origen").strip() or "sin_origen"
        for row in (worked_property_rows or [])
        if row.get("_id") is not None
    }
    empty = {"contact_attempts": 0, "effective_contacts": 0, "contactability_pct": None}
    if not property_origin:
        buckets, insight = _contactability_result_buckets([])
        return {
            "overall": empty,
            "by_portal": {},
            "active_events": [],
            "result_buckets": buckets,
            "result_insight": insight,
        }

    event_projection = {
        "event_id": 1,
        "event_type": 1,
        "property_id": 1,
        "contact_attempt": 1,
        "contact_effective": 1,
        "credited": 1,
        "commercially_valid": 1,
        "result": 1,
        "contact_result": 1,
        "occurred_at": 1,
    }
    events = await adb[LEDGER_COLLECTION].find(
        {
            "property_id": {"$in": list(property_origin)},
            "event_type": {"$in": list(VALID_CREDIT_EVENT_TYPES)},
            "$or": [{"credited": True}, {"commercially_valid": True}],
        },
        event_projection,
    ).to_list(None)
    event_ids = [event.get("event_id") for event in events if event.get("event_id")]
    reversed_ids = {
        str(row.get("original_event_id"))
        for row in await adb[LEDGER_COLLECTION].find(
            {"event_type": "management_reversed", "original_event_id": {"$in": event_ids}},
            {"original_event_id": 1},
        ).to_list(None)
    }
    active_events = [event for event in events if str(event.get("event_id")) not in reversed_ids]

    def summary(rows):
        values = summarize_management_metrics(rows)
        attempts = values["contact_attempts"]
        effective = values["effective_contacts"]
        return {
            "contact_attempts": attempts,
            "effective_contacts": effective,
            "contactability_pct": round(effective * 100 / attempts, 1) if attempts else None,
        }

    by_portal = {}
    for portal in set(property_origin.values()):
        by_portal[portal] = summary([
            event for event in active_events
            if property_origin.get(str(event.get("property_id"))) == portal
        ])
    buckets, insight = _contactability_result_buckets(active_events)
    return {
        "overall": summary(active_events),
        "by_portal": by_portal,
        "active_events": active_events,
        "result_buckets": buckets,
        "result_insight": insight,
    }


def _broker_counts_by_portal(active_events, property_origin):
    grouped = {}
    for event in active_events or []:
        if str(event.get("result") or event.get("commercial_result") or "").lower() != "broker_identified":
            continue
        property_id = str(event.get("property_id"))
        portal = property_origin.get(property_id)
        if portal:
            grouped.setdefault(portal, set()).add(property_id)
    return {portal: len(properties) for portal, properties in grouped.items()}


async def _load_captacion_kpi_snapshot(adb, base_query):
    """Calcula el snapshot de KPI sin depender del listado paginado."""
    from captacion_kpis import AVAILABLE_STATES, MANAGEMENT_STATES, KPI_MANAGEMENT_STATES, CAPTURED_STATES, DISCARDED_STATES, KPI_WORKED_STATES
    worked_states = list(KPI_WORKED_STATES)
    kpi_facet = [
        {"$match": base_query},
        {"$facet": {
            "available": [{"$match": {"gestion.estado": {"$in": list(AVAILABLE_STATES)}}}, {"$count": "count"}],
            "ready_to_contact": [{"$match": {"gestion.estado": "Por contactar"}}, {"$count": "count"}],
            "management": [{"$match": {"gestion.estado": {"$in": list(KPI_MANAGEMENT_STATES)}}}, {"$count": "count"}],
            "captured": [{"$match": {"gestion.estado": {"$in": list(CAPTURED_STATES)}}}, {"$count": "count"}],
            "discarded": [{"$match": {"gestion.estado": {"$in": list(DISCARDED_STATES)}}}, {"$count": "count"}],
            "sources": [
                {"$group": {"_id": {"$ifNull": ["$origen", "sin_origen"]}, "count": {"$sum": 1}}},
                {"$sort": {"count": -1, "_id": 1}},
            ],
            "contact_type": [
                {"$match": {"gestion.estado": {"$in": list(MANAGEMENT_STATES + CAPTURED_STATES + DISCARDED_STATES)}}},
                {"$project": {
                    "contact_bucket": {
                        "$cond": [
                            {"$eq": ["$gestion.estado", "Corredor"]},
                            "corredor",
                            "otros",
                        ]
                    }
                }},
                {"$group": {"_id": "$contact_bucket", "count": {"$sum": 1}}},
            ],
        }}
    ]
    kpi_result, comunas_list = await asyncio.gather(
        adb[Config.CAPTACION_COLLECTION_NAME].aggregate(kpi_facet).to_list(1),
        adb[Config.CAPTACION_COLLECTION_NAME].distinct("comuna", base_query),
    )
    counts = (kpi_result[0] if kpi_result else {})

    def _count(key):
        rows = counts.get(key) or []
        return int((rows[0] or {}).get("count") or 0) if rows else 0

    source_counts = []
    for row in (counts.get("sources") or []):
        source_value = str(row.get("_id") or "").strip() or "sin_origen"
        source_label = (
            "Sin origen"
            if source_value.casefold() in {"sin_origen", "otro"}
            else format_captacion_portal_label(source_value)
        )
        source_counts.append({
            "value": source_value,
            "label": source_label,
            "count": int(row.get("count") or 0),
        })
    source_counts.sort(key=lambda row: (-row["count"], row["label"].casefold()))
    contact_type_counts = {"corredor": 0, "otros": 0}
    for row in (counts.get("contact_type") or []):
        bucket = str(row.get("_id") or "otros")
        if bucket not in contact_type_counts:
            bucket = "otros"
        contact_type_counts[bucket] += int(row.get("count") or 0)
    canonical_contactability = await _load_captacion_canonical_contactability(
        adb,
        counts.get("worked_properties") or [],
    )
    worked_portal_counts = _build_captacion_worked_portal_breakdown(
        counts.get("worked_portal") or [],
        canonical_contactability["by_portal"],
        _broker_counts_by_portal(
            canonical_contactability.get("active_events"),
            {
                str(row.get("_id")): str(row.get("origen") or "sin_origen").strip() or "sin_origen"
                for row in (counts.get("worked_properties") or [])
            },
        ),
    )
    comunas_clean = sorted(
        {str(value).strip() for value in comunas_list if value and str(value).strip()},
        key=lambda value: value.casefold(),
    )
    return {
        "in_gestion_count": _count("management"),
        "captados_count": _count("captured"),
        "descartados_count": _count("discarded"),
        "available_count": _count("available"),
        "comunas_clean": comunas_clean,
        "source_counts": source_counts,
        "contact_type_counts": contact_type_counts,
    }


async def _prewarm_captacion_default_kpi():
    """Precalienta el snapshot global compartido por roles privilegiados."""
    started = time.perf_counter()
    try:
        from chatbot.storage import get_async_db
        adb = get_async_db()
        base_query = {
            # Las tarjetas deben incorporar cualquier portal nuevo con origen
            # válido, sin tener que modificar esta consulta cada vez.
            "origen": {"$exists": True, "$nin": [None, ""]},
            "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)},
        }
        snapshot = await _load_captacion_kpi_snapshot(adb, base_query)
        record = {
            "time": time.time(),
            "schema_version": CAPTACION_KPI_CACHE_SCHEMA,
            "kpi_revision": int(((await adb["captacion_kpi_revision"].find_one({"_id": "captacion_kpi_revision"}) or {}).get("revision") or 0)),
            **snapshot,
        }
        cache = getattr(app.state, "captacion_stats_cache", {})
        for role in ("admin", "supervisor"):
            cache[f"stats_{CAPTACION_KPI_CACHE_VERSION}_global_{role}"] = record
        app.state.captacion_stats_cache = cache
        logger.info(
            "[CAPTACION_WARMER] default KPI ready total_ms=%.0f",
            (time.perf_counter() - started) * 1000,
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[CAPTACION_WARMER] default KPI prewarm failed")
        return False


async def _ensure_captacion_indexes_background():
    """Verifica índices una vez, fuera del camino crítico de readiness."""
    started = time.perf_counter()

    def _ensure():
        from api_captacion import ensure_leads_indexes
        from captacion_goals import ensure_captacion_goal_indexes
        from chatbot.storage import get_db

        ensure_leads_indexes()
        ensure_captacion_goal_indexes(get_db())

    try:
        await asyncio.get_running_loop().run_in_executor(_WEB_THREAD_POOL, _ensure)
        logger.info(
            "[STARTUP_PERF] indexes_ms=%.0f status=ready",
            (time.perf_counter() - started) * 1000,
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[STARTUP_PERF] indexes background check failed")
        return False


# --- NUEVA ARQUITECTURA DE COLA (PRODUCER/CONSUMER) ---
lead_processing_queue = None  # Se inicializará en lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("Bot PRO Iniciando (Lifespan Startup)...")
    
    # Install phone redaction on all loggers
    from chatbot.storage import install_log_redaction
    install_log_redaction()
    logger.info("[LOG] Phone redaction installed on root + chatbot + uvicorn loggers")

    logger.info("ThreadPoolExecutor configurado: web=8, worker=5, process=2, warmer=1")
    logger.info("[CONFIG] non_hot_digest_shadow_mode=%s configuration_source=code",
                Config.CRM_NON_HOT_DIGEST_SHADOW_MODE)
    
    global lead_processing_queue, _OAUTH_HTTP_CLIENT
    lead_processing_queue = asyncio.Queue(maxsize=4)
    captacion_prewarm_task = asyncio.create_task(_prewarm_captacion_shared_data())
    captacion_goal_prewarm_task = asyncio.create_task(_prewarm_captacion_default_goal())
    captacion_kpi_prewarm_task = asyncio.create_task(_prewarm_captacion_default_kpi())
    app.state.captacion_goal_prewarm_task = captacion_goal_prewarm_task

    # Preconectar DB para reducir latencia del primer login/request.
    try:
        from chatbot.storage import get_db, get_async_db
        import time as _ping_time
        _p0 = _ping_time.perf_counter()
        get_db().command("ping")
        _p1 = _ping_time.perf_counter()
        logger.info(f"MongoDB ping: {(_p1-_p0)*1000:.0f}ms preconnect OK")
        await get_async_db().command("ping")
        logger.info("MongoDB preconnect: OK")
    except Exception as e:
        logger.warning(f"MongoDB preconnect warning: {e}")

    # Pre-crear índices de captación para evitar que se ejecuten dentro de requests
    try:
        from api_captacion import ensure_leads_indexes
        ensure_leads_indexes()
        from captacion_goals import ensure_captacion_goal_indexes
        from chatbot.storage import get_db
        ensure_captacion_goal_indexes(get_db())
        logger.info("Captacion indexes: OK")
    except Exception as e:
        logger.warning(f"Captacion indexes warning: {e}")

    # Cliente HTTP compartido para OAuth (evita crear conexión por callback).
    _OAUTH_HTTP_CLIENT = httpx.AsyncClient(timeout=10.0)

    # Iniciar tareas de fondo
    logger.info("[CRM_FLAGS] %s", {
        "lead_hot_notifications": Config.LEAD_HOT_NOTIFICATIONS_ENABLED,
        "lead_hot_reconciliation": Config.LEAD_HOT_RECONCILIATION_ENABLED,
        "lead_cold_digest": Config.LEAD_COLD_DIGEST_ENABLED,
        "sla_shadow": Config.CRM_SLA_SHADOW_ENABLED,
        "sla_alerts": Config.CRM_SLA_ALERTS_ENABLED,
        "weekly_generation": Config.CRM_WEEKLY_REPORT_GENERATION_ENABLED,
        "weekly_send": Config.CRM_WEEKLY_REPORT_SEND_ENABLED,
        "legacy_daily_report": Config.CRM_LEGACY_DAILY_REPORT_ENABLED,
        "inactive_nudge": Config.CRM_INACTIVE_NUDGE_ENABLED,
        "non_hot_digest_enabled": Config.CRM_NON_HOT_DIGEST_ENABLED,
        "non_hot_digest_shadow": Config.CRM_NON_HOT_DIGEST_SHADOW_MODE,
        "non_hot_digest_window_minutes": Config.CRM_NON_HOT_DIGEST_WINDOW_MINUTES,
    })

    # Iniciar tareas de fondo
    n_task = asyncio.create_task(process_pending_leads_loop())
    # The canonical CRM SLA orchestrator below is the only SLA background
    # pipeline.  The legacy monitor and worker are intentionally not started:
    # running both would create duplicate alerts and bypass the fixed policy.
    s_task = None
    t_task = asyncio.create_task(check_scheduled_tasks_loop())
    cr_task = asyncio.create_task(captacion_reminder_loop())
    # La distribucion de captaciones ya NO corre en loop horario: se dispara
    # al terminar el scraping (ver scripts/run_distribution_after_scrape.py).
    sla_c_task = asyncio.create_task(captacion_sla_release_loop())
    r_task = asyncio.create_task(reassign_unassigned_leads_loop()) # Ahora es Productor
    d_task = asyncio.create_task(daily_report_loop())
    captacion_daily_production_task = asyncio.create_task(captacion_daily_production_scheduler_loop())
    nudge_task = asyncio.create_task(inactive_lead_nudge_loop())
    w_task = asyncio.create_task(cache_prewarmer_loop())  # PRE-WARMING de cache
    el_task = asyncio.create_task(event_loop_monitor_loop()) # MONITOR EVENT LOOP
    tp_task = asyncio.create_task(threadpool_forensics_loop()) # MONITOR THREAD POOLS
    from chatbot.crm_weekly_report import crm_weekly_scheduler_loop
    crm_weekly_task = asyncio.create_task(crm_weekly_scheduler_loop())
    non_hot_digest_task = asyncio.create_task(non_hot_digest_worker_loop())
    sla_alert_task = None

    # CRM SLA Alert orchestrator — exclusive domain, behind feature flag.
    # The wrapper below publishes liveness + last-cycle stats to
    # background_tasks_status["sla_alert"] so the /health endpoint shows
    # whether the loop is actually running, disabled or failing (previously
    # a disabled/dead loop was completely invisible).
    async def _sla_alert_orchestrator_runner():
        while True:
            entry = background_tasks_status.setdefault("sla_alert", {})
            entry["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            try:
                from chatbot.crm_sla_alert_orchestrator import run_sla_alert_cycle
                from chatbot.crm_sla_alert_settings import sla_alerts_enabled
                if not sla_alerts_enabled():
                    entry.update({"status": "disabled",
                                  "reason": "CRM_SLA_ALERTS_ENABLED != true"})
                    logger.warning(
                        "[SLA_ALERT] loop deshabilitado: CRM_SLA_ALERTS_ENABLED != true "
                        "(heartbeat=%s)", entry["last_heartbeat"])
                else:
                    entry["status"] = "running"
                    report = await run_sla_alert_cycle()
                    entry.update({"status": "running", "last_report": report})
                    logger.info("[SLA_ALERT] ciclo ok: %s", report)
            except Exception as exc:
                logger.exception("[SLA_ALERT] Orchestrator cycle failed")
                entry.update({"status": f"error: {type(exc).__name__}",
                              "last_error": str(exc)})
            await asyncio.sleep(60)

    sla_orch_task = None
    try:
        sla_orch_task = asyncio.create_task(_sla_alert_orchestrator_runner())
    except Exception:
        logger.warning("[SLA_ALERT] Orchestrator import failed — disabled", exc_info=True)

    # Chatbot response worker — durable inbound → batch → LLM → WASender
    from chatbot.chatbot_queue import chatbot_response_worker_loop as _crwl
    cb_task = asyncio.create_task(_crwl())
    
    # Prop360 (Convecta) periodic ingestion — feature-flagged, self-contained
    prop360_task = None
    try:
        from chatbot.prop360_poll_loop import prop360_poll_loop as _ppl
        prop360_task = asyncio.create_task(_ppl())
        logger.info(
            "[PROP360_POLL] Loop scheduled. enabled=%s interval=%s window_hours=%s",
            Config.PROP360_POLL_ENABLED,
            Config.PROP360_POLL_INTERVAL_SECONDS,
            Config.PROP360_POLL_WINDOW_HOURS,
        )
    except Exception:
        logger.warning("[PROP360_POLL] Loop import failed — disabled", exc_info=True)
    
    # PROCASA SUCRE ficha sync — feature-flagged, self-contained
    ficha_task = None
    try:
        from chatbot.ficha_sync_loop import ficha_sync_loop as _fsl
        ficha_task = asyncio.create_task(_fsl())
        import os as _os
        logger.info(
            "[FICHA_SYNC] Loop scheduled. enabled=%s",
            _os.getenv("FICHA_SYNC_ENABLED", "false"),
        )
    except Exception:
        logger.warning("[FICHA_SYNC] Loop import failed — disabled", exc_info=True)

    # UF sync diario — actualiza uf_cache y derivados de precio (BUG E)
    uf_sync_task = None
    try:
        from chatbot.uf_sync_loop import uf_sync_loop as _usl
        uf_sync_task = asyncio.create_task(_usl())
        import os as _os
        logger.info(
            "[UF_SYNC] Loop scheduled. enabled=%s hour=%s",
            _os.getenv("UF_SYNC_ENABLED", "false"),
            _os.getenv("UF_SYNC_HOUR", "4"),
        )
    except Exception:
        logger.warning("[UF_SYNC] Loop import failed — disabled", exc_info=True)
    
    # Iniciar Consumers
    c1_task = asyncio.create_task(lead_consumer_worker(1))
    c2_task = asyncio.create_task(lead_consumer_worker(2))
    
    # Crear admin y asegurar índices
    crear_admin_si_no_existe()
    asegurar_indices_db()
    
    # Invalidar cachés antiguos de captación (versiones pre-migración)
    try:
        from chatbot.storage import get_db
        _db_clean = get_db()
        result = _db_clean["system_cache"].delete_many({
            "_id": {"$regex": "^(captacion_resp_|captacion_count_|all_raw_comunas_)"}
        })
        if result.deleted_count > 0:
            logger.info(f"Cachés de captación antiguos invalidados: {result.deleted_count}")
    except Exception as e:
        logger.warning(f"No se pudieron invalidar cachés antiguos: {e}")
    
    # Tarea de fondo: otorgar permisos y copiar expedientes existentes a carpeta raiz de Drive
    def _fix_existing_drive_permissions():
        try:
            from chatbot.storage import get_db
            from services.gdrive_sync import GDriveSync
            from config import Config
            db = get_db()
            g_visitas = GDriveSync(parent_folder_id=Config.GDRIVE_VISITAS_FOLDER_ID)
            g_contracts = GDriveSync(parent_folder_id=Config.GDRIVE_CONVENIOS_FOLDER_ID)
            if not g_visitas.service:
                return
            for col_name, g_inst in [("visitas", g_visitas), ("contracts", g_contracts)]:
                docs = list(db[col_name].find({
                    "$or": [
                        {"security.gdrive_folder_id": {"$exists": True, "$ne": None}},
                        {"security.original_pdf_drive_id": {"$exists": True, "$ne": None}}
                    ]
                }))
                for doc in docs:
                    sec = doc.get("security") or {}
                    f_id = sec.get("gdrive_folder_id")
                    d_id = sec.get("original_pdf_drive_id")
                    code = doc.get("visita_code") or doc.get("contract_code")
                    if f_id:
                        g_inst.share_item(f_id)
                    if d_id:
                        g_inst.share_item(d_id)
            logger.info("[GDRIVE] Permisos y copias en carpeta principal actualizados correctamente.")
        except Exception as e:
            logger.warning(f"[GDRIVE] Error reparando permisos antiguos de Drive: {e}")

    try:
        _WORKER_THREAD_POOL.submit(_fix_existing_drive_permissions)
    except Exception as e:
        logger.warning(f"[GDRIVE] No se pudo lanzar worker de permisos Drive: {e}")

    # El modelo de embeddings se cargará bajo demanda para ahorrar RAM en el arranque
    logger.info("Startup completo. Modelo de embeddings se cargará en el primer uso.")
    
    # Instrumentación [MEM_DIAG] - app completamente inicializada (solo diagnóstico)
    try:
        from chatbot.semantic_engine import log_memory_diagnostics
        log_memory_diagnostics("startup_complete")
    except Exception as _e:
        logger.warning(f"[MEM_DIAG] fallback startup_complete: {_e}")
    
    yield
    
    # Shutdown logic
    logger.info("Bot PRO Apagando (Lifespan Shutdown)...")
    n_task.cancel()
    if s_task is not None:
        s_task.cancel()
    t_task.cancel()
    r_task.cancel()
    d_task.cancel()
    nudge_task.cancel()
    w_task.cancel()
    el_task.cancel()
    tp_task.cancel()
    crm_weekly_task.cancel()
    sla_c_task.cancel()
    c1_task.cancel()
    c2_task.cancel()
    cb_task.cancel()
    if sla_orch_task is not None:
        sla_orch_task.cancel()
    try:
        await asyncio.gather(
            n_task, t_task, r_task, d_task, nudge_task, w_task, el_task, tp_task, crm_weekly_task, sla_c_task, c1_task, c2_task,
            *([sla_orch_task] if sla_orch_task is not None else []),
            return_exceptions=True
        )
    except Exception as e:
        logger.error(f"Error apagando tareas: {e}")
    finally:
        for _captacion_task in (captacion_prewarm_task, captacion_goal_prewarm_task, captacion_kpi_prewarm_task):
            if _captacion_task is not None:
                _captacion_task.cancel()
        if _OAUTH_HTTP_CLIENT is not None:
            try:
                await _OAUTH_HTTP_CLIENT.aclose()
            except Exception:
                pass
            _OAUTH_HTTP_CLIENT = None
        _WEB_THREAD_POOL.shutdown(wait=False)
        _WORKER_THREAD_POOL.shutdown(wait=False)
        _PROCESS_THREAD_POOL.shutdown(wait=False)
        _WARMER_THREAD_POOL.shutdown(wait=False)
        logger.info("ThreadPoolExecutors cerrados.")

app = FastAPI(title="Procasa WhatsApp Bot - PRO PAGADO 2025", lifespan=lifespan)

# ========================= MIDDLEWARE DE OBSERVABILIDAD =========================
import time
import uuid

@app.middleware("http")
async def advanced_perf_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    start_time = time.time()
    request.state.trace_id = request_id
    
    # Extract user if available (lightweight estimation)
    user = "anon"
    token = request.cookies.get("procasa_token")
    if token:
        try:
            from jose import jwt
            # Only decode without verifying signature to save CPU in middleware, just for logging
            payload = jwt.decode(token, options={"verify_signature": False})
            user = payload.get("sub", "anon").split("@")[0] # keep it short
        except:
            pass

    try:
        response = await call_next(request)
        duration_ms = (time.time() - start_time) * 1000
        
        if not request.url.path.startswith("/static/") and not request.url.path.startswith("/contracts_pdf/"):
            content_length = response.headers.get("content-length", "unknown")
            log_str = f"[HTTP_PERF] request_id={request_id} user={user} method={request.method} path={request.url.path} total={duration_ms:.0f}ms status={response.status_code} size={content_length}"
            logger.debug(log_str)
            
            if duration_ms > 3000:
                logger.error(f"[SLOW_REQUEST] ERROR: request_id={request_id} path={request.url.path} duration={duration_ms:.0f}ms")
            elif duration_ms > 1000:
                logger.warning(f"[SLOW_REQUEST] WARNING: request_id={request_id} path={request.url.path} duration={duration_ms:.0f}ms")
                
        response.headers["X-Process-Time"] = str(duration_ms)
        response.headers["X-Request-ID"] = request_id
        try:
            snap = observability_snapshot_and_reset()
            status_level = "OK" if snap["mongo_sync_on_loop"] == 0 and snap["event_loop_blocked"] == 0 else "DEGRADED"
            if snap["mongo_sync_on_loop"] > 5 or snap["event_loop_blocked"] > 5:
                status_level = "CRITICAL"
                
            _captacion_diag = getattr(request.state, "captacion_perf", None) or {}
            _mongo_calls = _captacion_diag.get("list_mongo_calls")
            _mongo_call_text = str(_mongo_calls) if _mongo_calls is not None else "n/a"
            _stage_text = ""
            if _captacion_diag:
                _stage_text = (
                    f"\nauth_ms={_captacion_diag.get('auth_ms', 'n/a')}"
                    f"\nlist_ms={_captacion_diag.get('list_stage_ms', 'n/a')}"
                    f"\nlist_query_ms={_captacion_diag.get('query_ms', 'n/a')}"
                    f"\nlist_count_ms={_captacion_diag.get('count_ms', 'n/a')}"
                    f"\nlist_catalog_ms={_captacion_diag.get('portal_ms', 'n/a')}"
                    f"\nkpi_ms={_captacion_diag.get('kpi_ms', 'n/a')}"
                    f"\ngoal_ms={_captacion_diag.get('goal_ms', 'n/a')}"
                    f"\nrender_ms={_captacion_diag.get('render_ms', 'n/a')}"
                    f"\ncache_hits={_captacion_diag.get('cache_hits', 'n/a')}"
                    f"\ncache_misses={_captacion_diag.get('cache_misses', 'n/a')}"
                    f"\ncache_stale={_captacion_diag.get('cache_stale', 'n/a')}"
                    f"\ncold_total_ms={_captacion_diag.get('cold_total_ms', 'n/a')}"
                    f"\nwarm_total_ms={_captacion_diag.get('warm_total_ms', 'n/a')}"
                )
            summary_msg = (
                f"[REQUEST_SUMMARY]\ntrace={request_id}\n"
                f"mongo_calls={_mongo_call_text}\n"
                f"mongo_sync_violations={snap['mongo_sync_on_loop']}\n"
                f"event_loop_blocked={snap['event_loop_blocked']}\n"
                f"duration_ms={duration_ms:.0f}\nstatus={status_level}"
                f"{_stage_text}"
            )
            if status_level == "CRITICAL":
                logger.error(summary_msg)
            elif status_level == "DEGRADED":
                logger.warning(summary_msg)
            else:
                logger.debug(summary_msg)
        except Exception:
            logger.exception(f"[REQUEST_SUMMARY] trace={request_id} error=summary_failed")
        return response
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(f"[HTTP_PERF] request_id={request_id} user={user} method={request.method} path={request.url.path} ERROR={str(e)} total={duration_ms:.0f}ms")
        raise e

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Mount contracts_pdf to serve PDFs statically and fast
CONTRACTS_PDF_DIR = BASE_DIR / "contracts_pdf"
CONTRACTS_PDF_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/contracts_pdf", StaticFiles(directory=CONTRACTS_PDF_DIR), name="contracts_pdf")

# Mount visitas_pdf
VISITAS_PDF_DIR = BASE_DIR / "visitas_pdf"
VISITAS_PDF_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/visitas_pdf", StaticFiles(directory=VISITAS_PDF_DIR), name="visitas_pdf")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

from api_contracts import router as contracts_router
app.include_router(contracts_router)

from api_visitas import router as visitas_router
app.include_router(visitas_router)

from api_crm_weekly_report import router as crm_weekly_router
app.include_router(crm_weekly_router)

from chatbot.lead_router import should_send_now, format_whatsapp_template
from chatbot.storage import (
    get_db,
    get_pending_notifications, 
    mark_notification_sent, 
    save_pending_notification,
    get_user_by_phone
)
from chatbot.whatsapp_client import send_whatsapp_message

# Función auxiliar para imágenes (necesaria globalmente)
def get_images():
    prop_dir = STATIC_DIR / "propiedades"
    if not prop_dir.exists() or not prop_dir.is_dir():
        return ["propiedades/default.jpg"]
    images = [f"propiedades/{f.name}" for f in prop_dir.iterdir() if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}]
    return images or ["propiedades/default.jpg"]

# ========================= 2. SEGURIDAD, JWT Y MIDDLEWARE DE SESIÓN =========================
from jose import JWTError, jwt
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

if not hasattr(Config, "SECRET_KEY") or not Config.SECRET_KEY:
    # Si no hay clave en Config, usamos una por defecto PERO estable para evitar que cada worker tenga una distinta
    Config.SECRET_KEY = "procasa_secret_default_key_2025"
    logger.warning("ATENCIÓN: Usando SECRET_KEY por defecto. Se recomienda configurar una en variables de entorno para máxima seguridad.")
else:
    logger.info("SECRET_KEY cargada correctamente desde Config.")

def get_password_hash(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str):
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict):
    to_encode = data.copy()
    # Expiración aumentada a 120 minutos (2 horas) según plan de estabilidad
    expire = datetime.now(pytz.utc) + timedelta(minutes=120)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, Config.SECRET_KEY, algorithm="HS256")

def crear_admin_si_no_existe():
    """Asegura usuario admin de emergencia para acceso operativo."""
    try:
        from chatbot.storage import get_db
        db = get_db()
        usuarios = db["usuarios"]

        admin_user = os.getenv("ADMIN_USERNAME", "admin")
        admin_pass = os.getenv("ADMIN_PASSWORD", "admin12345")
        admin_email = os.getenv("ADMIN_EMAIL", "admin@procasa.cl")
        admin_nombre = os.getenv("ADMIN_NOMBRE", "Administrador")

        exists = usuarios.find_one({"username": admin_user}, {"_id": 1})
        if exists:
            logger.info("Usuario 'admin' ya existe")
            return

        usuarios.insert_one({
            "username": admin_user,
            "email": admin_email,
            "nombre": admin_nombre,
            "rol": "admin",
            "hashed_password": get_password_hash(admin_pass),
            "activo": True,
            "created_at": datetime.now(CHILE_TZ).isoformat()
        })
        logger.info("Usuario 'admin' creado correctamente")
    except Exception as e:
        logger.error(f"Error creando admin: {e}")

# --- MIDDLEWARE DE SESION SLIDING (SOLUCION TIMEOUT) ---
@app.middleware("http")
async def slide_session_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)

    process_time = time.time() - start_time
    if process_time > 1.0:
        logger.warning(f"[LATENCY_ALERT] {request.method} {request.url.path} tardo {process_time:.3f}s")

    if request.url.path.startswith("/static") or request.url.path in ["/logout", "/webhook", "/auth/google/callback"]:
        return response

    token = request.cookies.get("access_token")
    if token:
        try:
            payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
            username = payload.get("sub")
            if username:
                exp_ts = payload.get("exp")
                should_renew = True
                if exp_ts:
                    try:
                        now_ts = datetime.now(pytz.utc).timestamp()
                        should_renew = (float(exp_ts) - now_ts) <= 5400
                    except Exception:
                        should_renew = True
                if should_renew:
                    new_token = create_access_token({"sub": username})
                    response.set_cookie(
                        key="access_token",
                        value=new_token,
                        httponly=True,
                        secure=True,
                        samesite="lax",
                        max_age=7200,
                        path="/"
                    )
        except JWTError:
            pass
    return response

async def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    if not token:
        logger.warning("Intento de acceso sin token")
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Token sin usuario")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalido o expirado")

async def get_current_user_doc(request: Request):
    cached = getattr(request.state, "current_user_doc", None)
    if cached is not None:
        return cached
    username = await get_current_user(request)
    from chatbot.storage import get_async_db
    adb = get_async_db()
    user = await adb["usuarios"].find_one(
        {"username": username},
        {"username": 1, "nombre": 1, "rol": 1, "email": 1}
    )
    request.state.current_user_doc = user
    return user

@app.post("/api/session/renew")
async def renew_session(user_name: str = Depends(get_current_user)):
    return {"status": "ok", "user": user_name}

# ========================= 3. LOGIN CON GOOGLE =========================

def _safe_login_next(value: str | None) -> str | None:
    """Accept only local CRM paths as post-login destinations."""
    if not value or not str(value).startswith("/") or str(value).startswith("//"):
        return None
    parsed = urlsplit(str(value))
    if parsed.scheme or parsed.netloc:
        return None
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _default_post_login_url(user_role: str | None) -> str:
    """Return the first module shown after login for each role."""
    role = str(user_role or "").strip().lower()
    return "/leads-dashboard" if role in {"admin", "supervisor", "administrador"} else "/crm"


def _is_local_auth_request(request: Request) -> bool:
    """Identify the local development host without affecting Render redirects."""
    return (request.url.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


def _post_login_target(request: Request, user_role: str | None, explicit_next: str | None = None) -> str:
    """Choose a role default, avoiding stale local redirect cookies after a restart."""
    default_url = _default_post_login_url(user_role)
    requested_url = _safe_login_next(explicit_next)
    if _is_local_auth_request(request):
        return requested_url or default_url
    return requested_url or _safe_login_next(request.cookies.get("login_next")) or default_url

@app.get("/login/google")
async def login_google(request: Request, next: str = Query(None)):
    params = {
        "client_id": Config.GOOGLE_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": Config.GOOGLE_REDIRECT_URI,
        "state": secrets.token_hex(16),
        "access_type": "offline",
        "prompt": "select_account"
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    response = RedirectResponse(url)
    next_url = _safe_login_next(next)
    if not next_url and not _is_local_auth_request(request):
        next_url = _safe_login_next(request.cookies.get("login_next"))
    if next_url:
        response.set_cookie("login_next", next_url, httponly=True, secure=True, samesite="lax", max_age=600)
    return response

@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str):
    try:
        token_url = "https://oauth2.googleapis.com/token"
        client = _OAUTH_HTTP_CLIENT
        owns_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=10.0)
            owns_client = True
        try:
            token_resp = await client.post(token_url, data={
                "client_id": Config.GOOGLE_CLIENT_ID,
                "client_secret": Config.GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": Config.GOOGLE_REDIRECT_URI,
            })
            token_data = token_resp.json()
            if "error" in token_data:
                logger.error(f"Error Token Google: {token_data}")
                return templates.TemplateResponse(request, "login.html", {"request": request, "images": get_images(), "error": "Error al conectar con Google (Token)"})

            access_token = token_data.get("access_token")
            user_info_url = "https://www.googleapis.com/oauth2/v1/userinfo"
            user_resp = await client.get(user_info_url, headers={"Authorization": f"Bearer {access_token}"})
            user_info = user_resp.json()
        finally:
            if owns_client:
                await client.aclose()

        email = user_info.get("email")
        from chatbot.storage import get_async_db as _gadb
        _adb = _gadb()
        usuarios = _adb["usuarios"]
        user = await usuarios.find_one({"$or": [{"email": email}, {"username": email}]}, {"username": 1, "rol": 1, "email": 1})
        if not user:
            logger.warning(f"Intento de acceso denegado: {email}")
            return templates.TemplateResponse(request, "login.html", {"request": request, "images": get_images(), "error": f"Acceso Denegado: El correo {email} no tiene permisos."})

        user_sub = user["username"]
        user_rol = user.get("rol", "agente")
        target_url = _post_login_target(request, user_rol)
        access_token_jwt = create_access_token({"sub": user_sub})
        response = RedirectResponse(target_url, status_code=303)
        response.set_cookie(key="access_token", value=access_token_jwt, httponly=True, secure=True, samesite="lax", max_age=7200)
        response.delete_cookie("login_next")
        logger.info("Conexion a MongoDB exitosa")
        logger.info(f"Sesion iniciada para {email} (Rol: {user_rol})")
        return response
    except Exception as e:
        logger.error(f"Error Google Auth Critical: {e}")
        return templates.TemplateResponse(request, "login.html", {"request": request, "images": get_images(), "error": f"Error interno: {str(e)}"})

# ========================= 4. RUTAS DE LOGIN TRADICIONAL =========================

@app.head("/")
@app.get("/")
@app.get("/login")
async def login_get(request: Request):
    next_url = _safe_login_next(request.query_params.get("next"))
    if not next_url and not _is_local_auth_request(request):
        next_url = _safe_login_next(request.cookies.get("login_next"))
    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "images": get_images(),
            "error": request.query_params.get("error"),
            "next_url": next_url or "",
        }
    )
    if next_url:
        response.set_cookie("login_next", next_url, httponly=True, secure=True, samesite="lax", max_age=600)
    return response

@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form(None)):
    try:
        from chatbot.storage import get_async_db
        db = get_async_db()
        usuarios = db["usuarios"]
        user = await usuarios.find_one({"username": username})
        
        if user and verify_password(password, user.get("hashed_password", "")):
            user_rol = user.get("rol", "agente")
            target_url = _post_login_target(request, user_rol, next)

            token = create_access_token({"sub": username})
            
            response = RedirectResponse(target_url, status_code=303) 
            response.set_cookie(
                "access_token", 
                token,
                httponly=True, 
                secure=True,   # Cambiado a True para Render (HTTPS)
                samesite="lax", 
                max_age=7200
            )
            response.delete_cookie("login_next")
            return response
        
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "images": get_images(), "error": "Usuario o contraseña incorrectos"
        })
    except Exception as e:
        logger.error(f"Error en login tradicional: {e}")
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "images": get_images(), "error": "Error del servidor"
        })

@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response

@app.get("/forgot-password")
async def forgot_password(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", {"request": request})

@app.get("/reset-password/{token}")
async def reset_password(request: Request, token: str):
    return templates.TemplateResponse(request, "reset_password.html", {"request": request, "token": token})

# ========================= 5. DASHBOARD & REPORTES =========================

@app.get("/dashboard", response_class=HTMLResponse)
async def ver_campanas(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"request": request})

@app.get("/api/leads_reporte")
async def api_leads_reporte(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="No autorizado")
    # FIX: run_in_executor evita bloquear el event loop durante cache miss
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WEB_THREAD_POOL, get_leads_executive_report)

@app.get("/api/leads-intelligence")
async def leads_intelligence_endpoint():
    # FIX: get_leads_executive_report() es síncrona (pymongo). Sin executor bloqueaba
    # el event loop completo durante ~1.6s en cada cache miss (cada 5 min).
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WEB_THREAD_POOL, get_leads_executive_report)

@app.get("/leads-dashboard", response_class=HTMLResponse)
async def ver_leads(request: Request):
    user = await get_current_user_doc(request)
    
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        return RedirectResponse(url="/crm?error=acceso_denegado")
    
    return templates.TemplateResponse(request, "leads_dashboard.html", {
        "request": request,
        "user_role": user.get("rol", "agente"),
        "user_name": user.get("nombre", "")
    })


@app.get("/leads-dashboard-review", response_class=HTMLResponse)
async def ver_leads_review(request: Request):
    """Public, read-only visual review; never resolves a user or touches Mongo."""
    response = templates.TemplateResponse(request, "leads_dashboard.html", {
        "request": request,
        "user_role": "review",
        "user_name": "Visual Review",
        "territorial_review": True,
    })
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/review/leads-dashboard")
async def leads_dashboard_review_data():
    """Sanitized fixture only. No request parameters and no database access."""
    response = JSONResponse(territorial_review_payload())
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


def _public_executive_overview(payload: dict) -> dict:
    """Allowlist only the aggregated fields needed by the public demo.

    The private overview also contains executive rows and internal rescue data.
    They are deliberately omitted from this public read-only surface.
    """
    allowed = ("period", "demand", "conversion", "pipeline", "funnel", "sla", "sources", "insights", "meta")
    return {key: payload.get(key) for key in allowed if key in payload}


def _overview_server_timing(timing: dict) -> str:
    parts = []
    cache = timing.get("cache")
    if cache:
        parts.append(f'cache;desc="{cache}"')
    for name, item in (timing.get("components") or {}).items():
        duration = item.get("duration_ms")
        if duration is not None:
            parts.append(f"{name};dur={duration}")
    # Diagnóstico agregado y temporal: permite distinguir los round trips
    # internos del Funnel sin exponer documentos ni información de leads.
    for component, detail in (timing.get("component_details") or {}).items():
        for mongo_item in detail.get("mongo", []) if isinstance(detail, dict) else []:
            operation = str(mongo_item.get("operation") or "mongo").replace(" ", "_")
            duration = mongo_item.get("duration_ms")
            if duration is not None:
                parts.append(f"{component}.{operation};dur={duration}")
        wait_ms = detail.get("signed_orders_wait_ms") if isinstance(detail, dict) else None
        if wait_ms is not None:
            parts.append(f"{component}.shared_orders_wait;dur={wait_ms}")
    for name in ("executor_wait_ms", "concurrent_block_ms", "total_ms"):
        duration = timing.get(name)
        if duration is not None:
            parts.append(f"{name};dur={duration}")
    return ", ".join(parts)


@app.get("/demo/leads-intelligence", response_class=HTMLResponse)
async def public_leads_intelligence_demo(request: Request):
    """Temporary, read-only public shell for the Executive Summary only."""
    return templates.TemplateResponse(request, "leads_dashboard.html", {
        "user_role": "public_demo",
        "user_name": "",
        "public_demo": True,
    })


@app.get("/api/demo/leads-intelligence/overview")
async def public_leads_intelligence_overview(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    compare: str = Query(None),
    period_preset: str = Query(None),
):
    """Aggregated read-only Overview API for external performance audits."""
    from analytics.commercial_periods import VALID_COMPARISONS, VALID_PRESETS, validate_explicit_range

    for key in ("period_start", "period_end", "compare", "period_preset"):
        if len(request.query_params.getlist(key)) > 1:
            raise HTTPException(status_code=422, detail=f"Parámetro duplicado: {key}")
    if compare is not None and compare not in VALID_COMPARISONS:
        raise HTTPException(status_code=422, detail="Comparación inválida")
    if period_preset is not None and period_preset not in VALID_PRESETS:
        raise HTTPException(status_code=422, detail="Preset inválido")
    try:
        _, _, period_preset = validate_explicit_range(period_start, period_end, period_preset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    timing = {}
    loop = asyncio.get_running_loop()
    submitted_at = time.perf_counter()

    def load_overview():
        timing["executor_wait_ms"] = round((time.perf_counter() - submitted_at) * 1000, 1)
        return get_leads_dashboard_overview(
            period_start=period_start,
            period_end=period_end,
            compare=compare,
            period_preset=period_preset,
            timing=timing,
        )

    payload = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        load_overview,
    )
    response = JSONResponse(_public_executive_overview(payload))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Overview-Cache"] = str(timing.get("cache", "UNKNOWN"))
    response.headers["Server-Timing"] = _overview_server_timing(timing)
    return response


@app.get("/api/demo/leads-intelligence/mongo-latency")
async def public_leads_intelligence_mongo_latency():
    """Temporary aggregate-only Mongo latency probe for infrastructure audit."""
    def percentile(values, pct):
        ordered = sorted(values)
        if not ordered:
            return None
        index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
        return round(ordered[index], 1)

    def probe():
        from chatbot.storage import get_db

        db = get_db()
        operations = {"ping": [], "find_one": []}
        collection = "visitas"
        for _ in range(10):
            started = time.perf_counter()
            db.command("ping")
            operations["ping"].append((time.perf_counter() - started) * 1000)
        for _ in range(10):
            started = time.perf_counter()
            db[collection].find_one({}, {"_id": 1})
            operations["find_one"].append((time.perf_counter() - started) * 1000)
        return {
            "collection": collection,
            "client_elapsed_ms": {
                name: {
                    "min": round(min(values), 1),
                    "p50": percentile(values, 50),
                    "p90": percentile(values, 90),
                    "max": round(max(values), 1),
                    "samples": len(values),
                }
                for name, values in operations.items()
            },
            "mongo_client_reused": True,
            "thread": threading.current_thread().name,
        }

    loop = asyncio.get_running_loop()
    submitted = time.perf_counter()
    started = time.perf_counter()
    result = await loop.run_in_executor(_WEB_THREAD_POOL, probe)
    result["executor_wait_ms"] = round((started - submitted) * 1000, 1)
    result["request_elapsed_ms"] = round((time.perf_counter() - submitted) * 1000, 1)
    response = JSONResponse(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/leads-dashboard/overview")
async def api_leads_dashboard_overview(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    compare: str = Query(None),
    period_preset: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    from analytics.commercial_periods import VALID_COMPARISONS, VALID_PRESETS, validate_explicit_range
    for key in ("period_start", "period_end", "compare", "period_preset"):
        if len(request.query_params.getlist(key)) > 1:
            raise HTTPException(status_code=422, detail=f"Parámetro duplicado: {key}")
    if compare is not None and compare not in VALID_COMPARISONS:
        raise HTTPException(status_code=422, detail="Comparación inválida")
    if period_preset is not None and period_preset not in VALID_PRESETS:
        raise HTTPException(status_code=422, detail="Preset inválido")
    try:
        _, _, period_preset = validate_explicit_range(period_start, period_end, period_preset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_leads_dashboard_overview(
            period_start=period_start,
            period_end=period_end,
            compare=compare,
            period_preset=period_preset,
        ),
    )


@app.get("/api/leads-dashboard/properties-inventory")
async def api_leads_dashboard_properties_inventory(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    operation: str = Query(None),
    property_type: str = Query(None),
    commune: str = Query(None),
    responsible: str = Query(None),
):
    """Lazy, read-only inventory snapshot for the third dashboard tab."""
    user = await get_current_user_doc(request)
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    filters = {key: value for key, value in {
        "operation": operation, "property_type": property_type,
        "commune": commune, "responsible": responsible,
    }.items() if value}
    timing = {}
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_properties_inventory_dashboard(
            period_start=period_start, period_end=period_end,
            filters=filters, timing=timing,
        ),
    )
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "private, max-age=120"
    response.headers["X-Analytics-Mongo-Calls"] = str(timing.get("mongo_calls", 0))
    return response


@app.get("/api/leads-dashboard/capture-simulator")
async def api_leads_dashboard_capture_simulator(
    request: Request,
    operation: str = Query(None), property_type: str = Query(None), commune: str = Query(None),
    price: str = Query(None), bedrooms: str = Query(None), bathrooms: str = Query(None),
    surface: str = Query(None), period_end: str = Query(None),
):
    """Read-only what-if capture simulation; no CRM or Mongo writes."""
    user = await get_current_user_doc(request)
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    params = {"operation": operation, "type": property_type, "commune": commune, "price": price, "bedrooms": bedrooms, "bathrooms": bathrooms, "surface": surface}
    timing = {}
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_capture_simulation(params=params, period_end=period_end, timing=timing))
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "private, max-age=120"
    response.headers["X-Analytics-Mongo-Calls"] = str(timing.get("mongo_calls", 0))
    response.headers["X-Analytics-N-Plus-One"] = "false"
    return response


@app.get("/api/leads-dashboard/operations")
async def api_leads_dashboard_operations(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    compare: str = Query("auto"),
    period_preset: str = Query(None),
    executive: str = Query(None),
    temperature: str = Query(None),
    stage: str = Query(None),
    priority: str = Query(None),
    assignment: str = Query(None),
    search: str = Query(None),
    portfolio: str = Query(None),
):
    """Datos operativos para la segunda pestaña del dashboard ejecutivo."""
    user = await get_current_user_doc(request)
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    filters = {key: value for key, value in {
        "executive": executive, "temperature": temperature, "stage": stage,
        "priority": priority, "assignment": assignment, "search": search,
        "portfolio": portfolio,
    }.items() if value}
    timing = {}
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_leads_operational_dashboard(
            period_start=period_start, period_end=period_end, compare=compare,
            period_preset=period_preset,
            role=user.get("rol"), user_name=user.get("nombre"), filters=filters, timing=timing,
        ),
    )
    response = JSONResponse(payload)
    response.headers["Server-Timing"] = ", ".join(
        item for item in (
            f'cache;desc="{timing.get("cache")}"' if timing.get("cache") else None,
            f'mongo;dur={timing.get("current_query_ms", 0) + timing.get("period_query_ms", 0):.1f}' if timing.get("mongo_calls") else None,
            f'current;dur={timing.get("current_query_ms")}' if timing.get("current_query_ms") is not None else None,
            f'period;dur={timing.get("period_query_ms")}' if timing.get("period_query_ms") is not None else None,
            f'backend;dur={timing.get("total_ms")}' if timing.get("total_ms") is not None else None,
        ) if item
    )
    response.headers["X-Analytics-Mongo-Calls"] = str(timing.get("mongo_calls", 0))
    return response


@app.get("/api/leads-dashboard/operations/portfolios")
async def api_leads_dashboard_operations_portfolios(request: Request):
    """Opciones dinámicas de cartera/captador para la vista supervisiva."""
    user = await get_current_user_doc(request)
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WEB_THREAD_POOL, get_operational_portfolios)


@app.get("/api/leads-dashboard/operations/executives")
async def api_leads_dashboard_operations_executives(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
):
    """Rendimiento comercial lazy; nunca forma parte del Overview."""
    user = await get_current_user_doc(request)
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_operational_executive_performance(
            period_start=period_start, period_end=period_end,
        ),
    )

@app.get("/api/leads-dashboard/captacion-management")
async def api_leads_dashboard_captacion_management(
    request: Request,
    executive: str = Query(None),
    period_start: str = Query(None),
    period_end: str = Query(None),
):
    """Resumen de actividad del equipo usando el período seleccionado del dashboard."""
    user = await get_current_user_doc(request)
    if not user or user.get("rol") not in CAPTACION_PRIVILEGED_ROLES:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    from chatbot.storage import get_db

    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_captacion_goal_dashboard(
            get_db(),
            selected_executive=executive or None,
            period_start=period_start or None,
            period_end=period_end or None,
            excluded_executives=CAPTACION_GOAL_EXCLUDED_EXECUTIVES,
        ),
    )
    response = JSONResponse(jsonable_encoder(payload))
    response.headers["Cache-Control"] = "private, no-store"
    return response

# ========================= COMMERCIAL DASHBOARD (READ-ONLY) =========================

async def _optional_commercial_user(request: Request):
    try:
        return await get_current_user_doc(request)
    except HTTPException:
        return None

@app.get("/analytics/commercial", response_class=HTMLResponse)
async def commercial_dashboard_page(request: Request):
    user = await _optional_commercial_user(request)
    privileged = bool(user and user.get("rol") in ("admin", "supervisor"))
    return templates.TemplateResponse(
        request=request,
        name="analytics/commercial_dashboard.html",
        context={
            "user_role": user.get("rol", "admin") if privileged else "admin",
            "user_name": user.get("nombre", "") if privileged else "",
        },
    )


@app.get("/api/analytics/commercial-dashboard")
async def api_commercial_dashboard(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    executive: str = Query(None),
    source: str = Query(None),
    operation: str = Query(None),
    property_type: str = Query(None),
    commune: str = Query(None),
    temperature: str = Query(None),
    property_code: str = Query(None),
    assignment: str = Query(None),
    stage: str = Query(None),
    compare: str = Query(None),
    period_preset: str = Query(None),
):
    from analytics.commercial_periods import VALID_COMPARISONS, VALID_PRESETS, validate_explicit_range
    for key in ("period_start", "period_end", "compare", "period_preset"):
        if len(request.query_params.getlist(key)) > 1:
            raise HTTPException(status_code=422, detail=f"Parámetro duplicado: {key}")
    if compare is not None and compare not in VALID_COMPARISONS:
        raise HTTPException(status_code=422, detail="Comparación inválida")
    if period_preset is not None and period_preset not in VALID_PRESETS:
        raise HTTPException(status_code=422, detail="Preset inválido")
    try:
        _, _, period_preset = validate_explicit_range(period_start, period_end, period_preset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    user = await _optional_commercial_user(request)
    privileged = bool(user and user.get("rol") in ("admin", "supervisor"))
    filters = {}
    if source: filters["source"] = source
    if operation: filters["operation"] = operation
    if property_type: filters["property_type"] = property_type
    if commune: filters["commune"] = commune
    if temperature: filters["temperature"] = temperature
    if property_code: filters["property_code"] = property_code
    if assignment: filters["assignment"] = assignment
    if stage: filters["stage"] = stage

    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_commercial_dashboard(
            period_start=period_start,
            period_end=period_end,
            executive=executive,
            role=user.get("rol") if privileged else "admin",
            user_name=user.get("nombre", "") if privileged else "",
            period_preset=period_preset,
            filters=filters or None,
            compare=compare,
        ),
    )
    return payload

# ========================= ANALYTICS DASHBOARD (READ-ONLY) =========================

@app.get("/analytics/leads", response_class=HTMLResponse)
async def analytics_leads_page(request: Request):
    user = await get_current_user_doc(request)
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")
    return templates.TemplateResponse(request, "analytics/leads_dashboard.html", {
        "request": request,
        "user_role": user.get("rol", "agente"),
        "user_name": user.get("nombre", ""),
    })


@app.get("/api/analytics/leads/summary")
async def api_analytics_leads_summary(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    executive: str = Query(None),
    stage: str = Query(None),
    temperature: str = Query(None),
    source: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    filters = {}
    if stage:
        filters["stage"] = stage
    if temperature:
        filters["temperature"] = temperature
    if source:
        filters["source"] = source
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_summary(
            period_start=period_start,
            period_end=period_end,
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
            filters=filters or None,
        ),
    )


@app.get("/api/analytics/leads/trends")
async def api_analytics_leads_trends(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_trends(period_start=period_start, period_end=period_end),
    )


@app.get("/api/analytics/leads/distributions")
async def api_analytics_leads_distributions(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    executive: str = Query(None),
    universe: str = Query("current_active"),
    stage: str = Query(None),
    temperature: str = Query(None),
    source: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    filters = {}
    if stage:
        filters["stage"] = stage
    if temperature:
        filters["temperature"] = temperature
    if source:
        filters["source"] = source
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_distributions(
            period_start=period_start,
            period_end=period_end,
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
            universe=universe,
            filters=filters or None,
        ),
    )


@app.get("/api/analytics/leads/table")
async def api_analytics_leads_table(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("desc"),
    executive: str = Query(None),
    stage: str = Query(None),
    temperature: str = Query(None),
    source: str = Query(None),
    search: str = Query(None),
    universe: str = Query("current_active"),
    period_start: str = Query(None),
    period_end: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    filters = {}
    if stage:
        filters["stage"] = stage
    if temperature:
        filters["temperature"] = temperature
    if source:
        filters["source"] = source
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: analytics_get_table(
            page=page,
            limit=limit,
            sort_by=sort_by,
            sort_dir=sort_dir,
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
            filters=filters or None,
            search=search,
            universe=universe,
            period_start=period_start,
            period_end=period_end,
        ),
    )


@app.get("/api/analytics/leads/filters")
async def api_analytics_leads_filters(
    request: Request,
    executive: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_filters(
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
        ),
    )


@app.get("/api/analytics/commercial/filters")
async def api_commercial_filters(request: Request):
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_commercial_filter_options(),
    )
    return payload


@app.get("/api/analytics/leads/coverage")
async def api_analytics_leads_coverage(
    request: Request,
    executive: str = Query(None),
    universe: str = Query("current_active"),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_field_coverage(
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
            universe=universe,
        ),
    )


@app.get("/api/analytics/leads/dashboard")
async def api_analytics_leads_dashboard(
    request: Request,
    period_start: str = Query(None),
    period_end: str = Query(None),
    executive: str = Query(None),
):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: get_dashboard(
            period_start=period_start,
            period_end=period_end,
            executive=executive,
            role=user.get("rol"),
            user_name=user.get("nombre"),
        ),
    )


@app.get("/api/analytics/leads/{lead_id}/detail")
async def api_analytics_leads_detail(request: Request, lead_id: str):
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")

    if not lead_id or len(lead_id) < 12:
        raise HTTPException(status_code=400, detail="ID inv\u00e1lido")

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: analytics_get_detail(lead_id),
    )
    if not data:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    # Verificar propiedad: agente solo ve sus leads
    if user.get("rol") not in ("admin", "supervisor"):
        exec_name = user.get("nombre", "")
        lead_exec = (data.get("public") or {}).get("ejecutivo", "")
        if exec_name and lead_exec and exec_name.strip() != lead_exec.strip():
            raise HTTPException(status_code=403, detail="El lead no est\u00e1 asignado a este ejecutivo")

    # Enmascarar datos sensibles
    if user.get("rol") not in ("admin", "supervisor"):
        phone = (data.get("public") or {}).get("phone", "")
        if phone:
            data["public"]["phone_masked"] = _mask_phone(phone)
            data["public"].pop("phone", None)
    return data


def _mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) >= 8:
        return f"+{digits[:2]}****{digits[-4:]}"
    return "****"

# ========================= FIN ANALYTICS =========================

@app.get("/chat-detail/{phone}", response_class=HTMLResponse)
async def ver_detalle_chat(request: Request, phone: str):
    phone_clean = phone.replace(" ", "").replace("+", "")
    loop = asyncio.get_running_loop()
    chat_data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_specific_lead_chat(phone_clean))
    
    if not chat_data:
        chat_data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_specific_lead_chat(phone))
        
    return templates.TemplateResponse(request, "chat_detail.html", {
        "request": request, 
        "chat": chat_data,
        "phone": phone
    })

# --- RUTAS DE INGRESO MANUAL ---
@app.get("/manual-lead-entry", response_class=HTMLResponse)
async def view_manual_lead_entry(request: Request):
    user = await get_current_user_doc(request)
    
    if not user or user.get("rol") not in ["admin", "supervisor"]:
        return RedirectResponse(url="/crm?error=acceso_denegado")
    
    # LÓGICA FINAL SIMPLE: Usar estrictamente el correo/usuario con el que se identificó.
    email = user.get("email") or user.get("username")
    if email: email = email.strip()

    return templates.TemplateResponse(request, "manual_lead_entry.html", {
        "request": request,
        "user_email": email,
        "user_role": user.get("rol", "agente"),
        "user_name": user.get("nombre", "")
    })

@app.get("/api/leads/check-duplicate")
async def api_check_duplicate(request: Request, phone: str = Query(None), property_code: str = Query(...), email: str = Query(None)):
    # Seguridad básica
    await get_current_user(request)
    loop = asyncio.get_running_loop()
    status, executive = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: check_lead_duplicate(phone, property_code, email)
    )
    return {"status": status, "exists": status != "not_found", "assigned_to": executive}

@app.get("/api/leads/resolve-property-code")
async def api_resolve_property_code(request: Request, code: str = Query(...)):
    await get_current_user(request)
    logger.info("[API_RESOLVE] incoming code=%r user=%s", code, getattr(request.state, "user", None) and getattr(request.state.user, "get", lambda *_: None)("email"))
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: resolve_property_code(code)
    )
    logger.info("[API_RESOLVE] result=%s", result)
    return result

@app.post("/api/leads/manual")
async def api_create_manual_lead(request: Request):
    import time as _time
    _t0 = _time.perf_counter()
    # [PERF] user lookup
    _tu = _time.perf_counter()
    user = await get_current_user_doc(request)
    logger.info(f"[PERF] /api/leads/manual user_lookup: {(_time.perf_counter()-_tu)*1000:.1f}ms")

    if not user or user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    # [PERF] json parse
    _tj = _time.perf_counter()
    data = await request.json()
    logger.info(f"[PERF] /api/leads/manual json_parse: {(_time.perf_counter()-_tj)*1000:.1f}ms")

    # [PERF] create_manual_lead (sync: duplicate check + DB insert + executive lookup)
    _tc = _time.perf_counter()
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: create_manual_lead(data))
    logger.info(f"[PERF] /api/leads/manual create_manual_lead: {(_time.perf_counter()-_tc)*1000:.1f}ms")

    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("message"))

    logger.info(
        f"[PERF] /api/leads/manual TOTAL_BEFORE_RESPONSE: {(_time.perf_counter()-_t0)*1000:.1f}ms "
        f"lead_id={result.get('lead_id')} assigned_to={result.get('assigned_to')}"
    )
    return result


# ========================= 11. DETALLE Y GESTIÓN CRM =========================


async def _get_authorized_crm_lead(
    request: Request,
    phone: str,
    *,
    administrative: bool = False,
):
    """Resolve the authenticated user and enforce CRM role/ownership access."""
    user = await get_current_user_doc(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    if administrative and not can_administer_leads(user.get("rol")):
        raise HTTPException(status_code=403, detail="Acción reservada a administración")

    loop = asyncio.get_running_loop()
    lead = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: CrmService.get_lead(phone))
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    if not can_administer_leads(user.get("rol")) and not lead_is_assigned_to_user(lead, user):
        raise HTTPException(status_code=403, detail="El lead no está asignado a este ejecutivo")
    return user, lead



@app.get("/crm/lead/{phone}", response_class=HTMLResponse)
async def view_crm_detail(request: Request, phone: str, codigo: str = Query(None)):
    """DEPRECATED: phone-based lead detail. 302 redirect to ObjectId route.
    
    Auth is checked before any redirection. Phone is redacted in logs.
    If the user is not authorized, returns 403 without revealing lead existence.
    """
    from chatbot.storage import redact_phone
    logger.info("[DEPRECATED] /crm/lead/%s accessed — redirecting to secure route",
                redact_phone(phone))
    
    user = await get_current_user_doc(request)
    from chatbot.storage import get_db as _sync_db
    from chatbot.lead_router import build_secure_crm_url

    def _resolve():
        db = _sync_db()
        from chatbot.crm_metrics import resolve_canonical_lead
        resolution = resolve_canonical_lead(db, phone=phone)
        if not resolution.lead and str(phone).startswith("no-phone-"):
            legacy_lead = db["leads"].find_one({"phone": str(phone)})
            if legacy_lead:
                resolution = type(resolution)(legacy_lead, "resolved_synthetic_phone", 1)
        if resolution and resolution.lead:
            lead = resolution.lead
            phone_norm = lead.get("phone") or ""
            detail = get_lead_detail_data(phone_norm, lead_doc=lead)
            return lead, detail
        return None, None

    loop = asyncio.get_running_loop()
    lead, lead_data = await loop.run_in_executor(_WEB_THREAD_POOL, _resolve)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    if not can_administer_leads(user.get("rol")) and not lead_is_assigned_to_user(lead_data or {}, user):
        raise HTTPException(status_code=403, detail="No autorizado")

    secure_url = build_secure_crm_url(lead)
    response = RedirectResponse(url=secure_url, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/crm/lead-id/{lead_id}", response_class=HTMLResponse)
async def view_crm_detail_by_id(request: Request, lead_id: str):
    """Secure lead detail by ObjectId. No phone in URL. No query-string data trusted."""
    from bson import ObjectId as BsonObjectId
    from bson.errors import InvalidId

    user = await get_current_user_doc(request)
    try:
        oid = BsonObjectId(lead_id)
    except InvalidId:
        return HTMLResponse("ID de lead invalido", status_code=400)

    from chatbot.storage import get_db as _sync_db

    def _resolve():
        db = _sync_db()
        lead = db["leads"].find_one({"_id": oid})
        if lead:
            phone = lead.get("phone") or ""
            detail = get_lead_detail_data(phone, lead_doc=lead)
            return lead, detail
        return None, None

    loop = asyncio.get_running_loop()
    lead, data = await loop.run_in_executor(_WEB_THREAD_POOL, _resolve)
    if not lead or not data:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    full_phone_access = can_administer_leads(user.get("rol")) or lead_is_assigned_to_user(data, user)
    if not full_phone_access:
        # Keep the current ownership gate for lead access, but never leak a raw
        # phone if this route is reused by a read-only authenticated view.
        raise HTTPException(status_code=403, detail="El lead no esta asignado a este ejecutivo")
    raw_phone = str(data.get("phone") or "").strip()
    phone_synthetic = bool(lead.get("phone_is_synthetic")) or raw_phone.startswith("no-phone-")
    if phone_synthetic:
        data["phone"] = raw_phone
        data["phone_masked"] = "Sin teléfono"
        data["whatsapp_display"] = "Sin teléfono"
        data["phone_is_synthetic"] = True
    else:
        data["phone"] = raw_phone
        data["phone_masked"] = _mask_phone(raw_phone)
        display = raw_phone if raw_phone else "Sin teléfono"
        if display != "Sin teléfono" and not display.startswith("+"):
            display = f"+{display}"
        data["whatsapp_display"] = display
        data["phone_is_synthetic"] = False
    data.pop("phone_raw", None)
    data["phone_visibility"] = "full"

    email = user.get("email") or user.get("username")

    return templates.TemplateResponse(request, "crm_lead_detail.html", {
        "request": request, 
        "lead": data,
        "user_email": email,
        "user_role": user.get("rol", "agente"),
        "user_name": user.get("nombre", "")
    })


# ---- Phone masking helper ----

def _mask_phone(phone: str) -> str:
    """Mask a phone number for display: +56 9 XXXX 1234 -> +56 9 **** 1234"""
    import re
    p = str(phone or "").strip()
    m = re.match(r"(\+?56\s*9)\s*(\d{4})\s*(\d{4})", p)
    if m:
        return f"{m.group(1)} **** {m.group(3)}"
    return p[:6] + "****" + p[-4:] if len(p) > 8 else p


# ---- Contact actions (secure, no phone in URL or frontend) ----

@app.post("/crm/lead-id/{lead_id}/contact/whatsapp")
async def contact_whatsapp(request: Request, lead_id: str):
    """Open WhatsApp for a lead. Auth required. Telemetry first, phone from DB only."""
    from bson import ObjectId as BsonObjectId
    from bson.errors import InvalidId
    try:
        oid = BsonObjectId(lead_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="ID invalido")
    
    user = await get_current_user_doc(request)
    from chatbot.storage import get_db as _sync_db
    
    loop = asyncio.get_running_loop()
    lead, detail = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: _resolve_lead_by_id(_sync_db(), oid))
    if not lead or not detail:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    if not can_administer_leads(user.get("rol")) and not lead_is_assigned_to_user(detail, user):
        raise HTTPException(status_code=403, detail="No autorizado")
    
    def _log():
        from chatbot.storage import get_db, log_event
        db = get_db()
        log_event(lead.get("phone", ""), "CLICK_WHATSAPP_LEAD",
                  actor=str(user.get("nombre", "")), lead_id=lead["_id"])
    await loop.run_in_executor(_WEB_THREAD_POOL, _log)
    
    raw_phone = str(lead.get("phone", "")).strip()
    clean = "".join(c for c in raw_phone if c.isdigit() or c == "+")
    return JSONResponse({
        "action": "whatsapp",
        "url": f"https://wa.me/{clean.replace('+', '')}",
        "disclaimer": "Contactar no constituye gestion. Registra el resultado en el CRM."
    })


@app.post("/crm/lead-id/{lead_id}/contact/call")
async def contact_call(request: Request, lead_id: str):
    """Initiate a phone call. Auth required. Telemetry first."""
    from bson import ObjectId as BsonObjectId
    from bson.errors import InvalidId
    try:
        oid = BsonObjectId(lead_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="ID invalido")
    
    user = await get_current_user_doc(request)
    from chatbot.storage import get_db as _sync_db
    
    loop = asyncio.get_running_loop()
    lead, detail = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: _resolve_lead_by_id(_sync_db(), oid))
    if not lead or not detail:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    if not can_administer_leads(user.get("rol")) and not lead_is_assigned_to_user(detail, user):
        raise HTTPException(status_code=403, detail="No autorizado")
    
    def _log():
        from chatbot.storage import get_db, log_event
        db = get_db()
        log_event(lead.get("phone", ""), "CLICK_CALL_LEAD",
                  actor=str(user.get("nombre", "")), lead_id=lead["_id"])
    await loop.run_in_executor(_WEB_THREAD_POOL, _log)
    
    raw_phone = str(lead.get("phone", "")).strip()
    clean = "".join(c for c in raw_phone if c.isdigit() or c == "+")
    return JSONResponse({
        "action": "call",
        "url": f"tel:{clean}",
        "disclaimer": "Llamar no constituye gestion. Registra el resultado en el CRM."
    })


def _resolve_lead_by_id(db, oid):
    lead = db["leads"].find_one({"_id": oid})
    if lead:
        detail = get_lead_detail_data(lead.get("phone", ""), lead_doc=lead)
        return lead, detail
    return None, None


@app.post("/api/crm/log_action")
async def api_crm_log_action(request: Request):
    try:
        data = await request.json()
        phone = data.get("phone")
        payload = data.get("data", {})
        event_type = payload.get("type")
        user, authorized_lead = await _get_authorized_crm_lead(request, phone)
        actor_user_id = str(user.get("_id") or "")
        actor_name = user.get("nombre") or user.get("username") or actor_user_id

        now_cl = datetime.now(CHILE_TZ)
        if "meta" not in payload:
            payload["meta"] = {}
        payload["meta"]["server_time_cl"] = now_cl.strftime("%Y-%m-%d %H:%M:%S")

        def _sync_log_action():
            from chatbot.storage import get_db
            db = get_db()
            lead = db["leads"].find_one({"_id": authorized_lead["_id"]})

            # All click/send/call actions are telemetry only — they never write
            # first_valid_management_at, stop SLA, or count as gestion valida.
            # Only a complete management result from /api/crm/update or
            # /api/crm/management-result can acreditar gestion.
            log_crm_event(phone=phone, event_type=event_type, agent=actor_name,
                          meta_data=payload.get("meta"))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_WEB_THREAD_POOL, _sync_log_action)
        return {"status": "ok"}
    except Exception as e:
        logger.error("Error logging CRM action: phone=%s type=%s -> %s",
                     data.get("phone") if data else None,
                     (data.get("data") or {}).get("type") if data else None,
                     e, exc_info=True)
        return {"status": "error"}


@app.post("/api/crm/management-result")
async def api_crm_management_result(request: Request):
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Falta teléfono")
    user, lead = await _get_authorized_crm_lead(request, phone)
    actor_user_id = str(user.get("_id") or "")
    if not actor_user_id:
        raise HTTPException(status_code=400, detail="Usuario sin identidad canónica")

    def _record():
        from chatbot.crm_management import (
            record_management_result,
            ScheduledTimeTooSoonError,
            StaleAssignmentCycleError,
        )
        from chatbot.crm_metrics import active_assignment_cycle
        from chatbot.storage import get_db
        db = get_db()
        requested_cycle_id = str(data.get("assignment_cycle_id") or "").strip()
        cycle = (
            db["crm_assignment_cycles"].find_one({
                "lead_id": lead["_id"], "assignment_cycle_id": requested_cycle_id,
                "cycle_status": "active", "unassigned_at": None,
            })
            if requested_cycle_id else active_assignment_cycle(db, lead["_id"])
        )
        if not cycle:
            if requested_cycle_id and active_assignment_cycle(db, lead["_id"]):
                raise StaleAssignmentCycleError(StaleAssignmentCycleError.code)
            raise ValueError("El lead no tiene un ciclo de asignación activo")
        return record_management_result(
            db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
            actor_user_id=actor_user_id, result_type=data.get("result_type"),
            occurred_at=None, source="crm_quick_action",
            idempotency_key=str(data.get("management_request_id") or data.get("idempotency_key") or ""),
            next_follow_up_at=data.get("next_follow_up_at"),
            details_json=data.get("details_json") if isinstance(data.get("details_json"), dict) else {},
            actor_can_manage_any_cycle=can_administer_leads(user.get("rol")),
        )
    try:
        result = await asyncio.get_running_loop().run_in_executor(_WEB_THREAD_POOL, _record)
        return {"status": "ok", "result_type": result.get("result_type"),
                "follow_up_required": result.get("follow_up_required")}
    except Exception as exc:
        from chatbot.crm_management import ScheduledTimeTooSoonError, StaleAssignmentCycleError
        if isinstance(exc, StaleAssignmentCycleError):
            raise HTTPException(status_code=409, detail=StaleAssignmentCycleError.code)
        if isinstance(exc, ScheduledTimeTooSoonError):
            raise HTTPException(status_code=400, detail=ScheduledTimeTooSoonError.code)
        if isinstance(exc, ValueError) and str(exc) == "active assignment cycle not found":
            raise HTTPException(status_code=409, detail="closed_lead")
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc))
        logger.error("CRM management-result error: phone=%s lead=%s result_type=%s -> %s",
                     phone, lead.get("_id"), data.get("result_type"), exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/api/crm/detail-state")
async def api_crm_detail_state(request: Request, phone: str):
    """Return the minimal current state needed after a stale detail submit."""
    phone = str(phone or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Falta teléfono")
    _user, lead = await _get_authorized_crm_lead(request, phone)
    detail = await asyncio.get_running_loop().run_in_executor(
        _WEB_THREAD_POOL, lambda: get_lead_detail_data(phone, lead_doc=lead)
    )
    return {
        "assignment_cycle_id": detail.get("assignment_cycle_id") or "",
        "crm_estado": detail.get("crm_estado") or "",
        "next_action_date": detail.get("next_action_date") or "",
        "last_action_label": detail.get("last_action_label") or "",
        "last_action_relative": detail.get("last_action_relative") or "",
        "ejecutivo_asignado": detail.get("ejecutivo_asignado") or "",
        "sla_status": detail.get("sla_status") or "",
        "sla_label": detail.get("sla_label") or "",
    }

@app.post("/api/crm/update")
async def api_crm_update_lead(request: Request):
    data = None
    try:
        data = await request.json()
        phone = data.get("phone")
        if not phone:
            raise HTTPException(status_code=400, detail="Falta teléfono")

        user, _lead = await _get_authorized_crm_lead(request, phone)
        if (
            not can_administer_leads(user.get("rol"))
            and payload_attempts_reassignment(data)
        ):
            raise HTTPException(status_code=403, detail="El ejecutivo no puede reasignar leads")

        # Aseguramos que se guarde la hora de actualización en CL
        data["updated_at_cl"] = datetime.now(CHILE_TZ).isoformat()
        data["_actor_name"] = user.get("nombre") or user.get("username") or ""
        data["_actor_user_id"] = str(user.get("_id") or "")
        data["_actor_can_manage_any_cycle"] = can_administer_leads(user.get("rol"))

        # CRITICO: update_lead_crm_data usa PyMongo sync + log_event/update_metrics sync.
        # Debe ejecutarse fuera del event loop para evitar bloqueos y MONGO_SYNC_ON_EVENT_LOOP.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: update_lead_crm_data(phone, data)
        )
        if result and isinstance(result, dict) and result.get("status") == "ok":
            return result
        elif result is True: # Fallback just in case
            return {"status": "ok"}
        else:
            logger.warning(
                "CRM update rejected: phone=%s actor=%s result=%s interaction=%s next_date=%s",
                phone, data.get("_actor_name"), data.get("resultado_gestion"),
                data.get("interaction_type"), data.get("next_action_date"),
            )
            raise HTTPException(status_code=500, detail="No se pudo actualizar")
    except HTTPException as exc:
        logger.error(
            "CRM update failed: status=%s detail=%s phone=%s actor=%s result=%s",
            exc.status_code, exc.detail,
            (data or {}).get("phone"), (data or {}).get("_actor_name"),
            (data or {}).get("resultado_gestion"),
        )
        raise
    except Exception as e:
        from chatbot.crm_management import StaleAssignmentCycleError
        if isinstance(e, StaleAssignmentCycleError):
            raise HTTPException(status_code=409, detail=StaleAssignmentCycleError.code)
        if isinstance(e, PermissionError):
            raise HTTPException(status_code=403, detail=str(e))
        if isinstance(e, ValueError):
            raise HTTPException(status_code=400, detail=str(e))
        logger.error(
            "CRM Update Error: phone=%s actor=%s result=%s -> %s",
            (data or {}).get("phone"), (data or {}).get("_actor_name"),
            (data or {}).get("resultado_gestion"), e,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/crm/admin/reassign")
async def api_crm_admin_reassign(request: Request):
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    executive = str(data.get("executive") or "").strip()
    if not phone or not executive:
        raise HTTPException(status_code=400, detail="Faltan teléfono o ejecutivo")

    user, _lead = await _get_authorized_crm_lead(request, phone, administrative=True)
    from chatbot.storage import get_async_db

    targets = await get_async_db()["usuarios"].find(
        {
            "nombre": re.compile(rf"^{re.escape(executive)}(?:\s|$)", re.IGNORECASE),
            "rol": {"$in": ["agente", "supervisor", "admin", "jefatura"]},
            "is_active": {"$ne": False},
        },
        {"nombre": 1},
    ).to_list(length=2)
    if len(targets) != 1:
        raise HTTPException(status_code=400, detail="Ejecutivo no válido o inactivo")
    target = targets[0]

    actor = user.get("nombre") or user.get("username") or "Administración"
    loop = asyncio.get_running_loop()
    changed = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: CrmService.assign_executive(
            phone,
            target["nombre"],
            method="crm_list_admin",
            actor=actor,
        ),
    )
    if not changed:
        raise HTTPException(status_code=409, detail="No fue posible reasignar el lead")
    return {"status": "ok", "executive": target["nombre"]}


@app.post("/api/crm/admin/mark-duplicate")
async def api_crm_admin_mark_duplicate(request: Request):
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Falta teléfono")

    user, _lead = await _get_authorized_crm_lead(request, phone, administrative=True)
    actor = user.get("nombre") or user.get("username") or "Administración"
    loop = asyncio.get_running_loop()
    changed = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: CrmService.mark_duplicate(phone, actor=actor),
    )
    if not changed:
        raise HTTPException(status_code=409, detail="No fue posible marcar el lead como duplicado")
    return {"status": "ok"}


@app.post("/api/crm/admin/archive")
async def api_crm_admin_archive(request: Request):
    data = await request.json()
    phone = str(data.get("phone") or "").strip()
    reason = str(data.get("reason") or "Archivo administrativo").strip()[:300]
    if not phone:
        raise HTTPException(status_code=400, detail="Falta teléfono")

    user, _lead = await _get_authorized_crm_lead(request, phone, administrative=True)
    actor = user.get("nombre") or user.get("username") or "Administración"
    loop = asyncio.get_running_loop()
    changed = await loop.run_in_executor(
        _WEB_THREAD_POOL,
        lambda: CrmService.archive_lead(phone, actor=actor, reason=reason),
    )
    if not changed:
        raise HTTPException(status_code=409, detail="No fue posible archivar el lead")
    return {"status": "ok"}

@app.post("/api/crm/notes")
async def api_crm_notes(request: Request):
    data = None
    try:
        data = await request.json()
        action = data.get("action", "add")
        phone = data.get("phone")
        note_data = data.get("note", {})
        if not isinstance(note_data, dict):
            raise HTTPException(status_code=400, detail="Datos de nota inválidos")
        if action not in {"add", "delete"}:
            raise HTTPException(status_code=400, detail="Acción de nota inválida")
        user, lead = await _get_authorized_crm_lead(request, phone)
        from chatbot.crm_metrics import active_assignment_cycle
        from chatbot.storage import get_db
        cycle = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: active_assignment_cycle(get_db(), lead["_id"]),
        )

        # SOLUCIÓN HORA NOTAS: Forzar la hora de Chile en la creación
        if action == "add":
            now_cl = datetime.now(CHILE_TZ)
            # Sobreescribimos/Agregamos fecha formateada con HORA
            note_data["created_at_str"] = now_cl.strftime("%d/%m/%Y %H:%M")
            # Añadimos timestamp ISO para ordenamiento backend
            note_data["timestamp_iso"] = now_cl.isoformat()

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: manage_crm_notes(
                phone, note_data, action,
                lead_id=lead["_id"],
                actor_user_id=str(user.get("_id") or ""),
                assignment_cycle_id=(cycle or {}).get("assignment_cycle_id"),
            ),
        )
        if result:
            return {"status": "ok", "note": result}
        logger.warning("CRM notes rejected: action=%s phone=%s", action, phone)
        raise HTTPException(status_code=404, detail="Nota o lead no encontrado")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("CRM notes error: phone=%s action=%s -> %s", data.get("phone") if data else None, (data or {}).get("action"), e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# --- BÚSQUEDA SEMÁNTICA ---
@app.post("/api/crm/recommendations")
async def api_crm_recommendations(request: Request):
    try:
        data = await request.json()
        query = data.get("query", "")
        exclude = data.get("exclude", [])
        limit = data.get("limit", 3)
        scope = data.get("scope", "local")
        include_neighbors = data.get("include_neighbors", False)

        if not query or len(query.strip()) < 5:
            raise HTTPException(status_code=400, detail="Query muy corta")
        
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: get_semantic_recommendations(query, exclude_codes=exclude, limit=limit, scope=scope, include_neighbors=include_neighbors)
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SEMANTIC] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- ENVÍO DE RECOMENDACIÓN ---
@app.post("/api/crm/send_recommendation")
async def api_crm_send_recommendation(request: Request):
    try:
        data = await request.json()
        phone = data.get("phone", "")
        properties = data.get("properties", [])
        user_email = data.get("user_email", "")
        
        if not phone or not properties:
            raise HTTPException(status_code=400, detail="Faltan datos")
        
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: log_recommendation_sent(phone, properties, user_email))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SEMANTIC] Error send_recommendation: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ========================= 7. WEBHOOK & API ENDPOINTS =========================


def _normalize_webhook_phone(value):
    digits = "".join(filter(str.isdigit, str(value or "")))
    if not digits:
        return None
    if digits.startswith("56") and len(digits) >= 11:
        return "+" + digits
    if len(digits) == 9 and digits.startswith("9"):
        return "+56" + digits
    return "+" + digits


def _remote_peer_phone(key, msg_obj):
    """Return the remote PN peer; never treat a LID as a phone number."""
    candidates = [
        key.get("remoteJid"), key.get("remoteJidAlt"),
        msg_obj.get("from"), msg_obj.get("chatId"),
    ]
    for value in candidates:
        raw = str(value or "")
        if "@s.whatsapp.net" in raw:
            return _normalize_webhook_phone(raw.split("@", 1)[0])
    return None

@app.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_signature: str = Header(None, alias="X-Webhook-Signature")
):
    raw_body = await request.body()
    if Config.WASENDER_WEBHOOK_SECRET:
        expected = hmac.new(
            Config.WASENDER_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_webhook_signature or ""):
            logger.warning("Firma inválida en webhook")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error(f"JSON inválido: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if data.get("event") == "webhook.test":
        logger.info("TEST WEBHOOK EXITOSO")
        return JSONResponse({"ok": True}, status_code=200)

    if data.get("event") == "messages.update":
        updated = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: record_delivery_status_webhook(data)
        )
        return JSONResponse({"status": "delivery_updated" if updated else "delivery_not_tracked"}, status_code=200)

    # --- LOG AGRESIVO PARA DEBUG ---
    logger.info(f"Incoming Webhook Event: {data.get('event')} | Payload size: {len(raw_body)}")
    if "@g.us" in str(data):
        logger.info(f"🎯 Grupo detectado en el payload! Raw: {json.dumps(data)[:500]}")


    messages_data = data.get("data", {}).get("messages", {}) or {}
    if not messages_data:
        return JSONResponse({"status": "no messages"}, status_code=200)

    msg_obj = messages_data if isinstance(messages_data, dict) else messages_data[0]
    key = msg_obj.get("key", {})
    from_me = key.get("fromMe", False)
    provider_message_id = (
        key.get("id") or msg_obj.get("id") or msg_obj.get("messageId")
        or data.get("message_id") or data.get("id")
    )
    loop = asyncio.get_running_loop()
    remote_peer_phone = _remote_peer_phone(key, msg_obj)
    sender_phone = _normalize_webhook_phone(key.get("cleanedSenderPn") or key.get("senderPn"))
    bot_outbound = None
    if from_me and provider_message_id:
        try:
            from chatbot.storage import find_bot_outbound_by_provider_id
            bot_outbound = await loop.run_in_executor(
                _WEB_THREAD_POOL,
                lambda: find_bot_outbound_by_provider_id(str(provider_message_id)),
            )
        except Exception:
            logger.exception("[CHATBOT_QUEUE] no se pudo resolver el outbound por provider ID")

    # A client is the remote peer. For our outbound messages the provider
    # sender/session number is not the lead; the canonical provider ID wins.
    if from_me and bot_outbound:
        phone = bot_outbound.get("phone") or remote_peer_phone
    elif from_me:
        phone = remote_peer_phone
    else:
        phone = remote_peer_phone or sender_phone

    # --- SEGURIDAD: FILTRO DE RECENCIA (ANTI-BURST) ---
    msg_ts = msg_obj.get("messageTimestamp")
    if msg_ts:
        try:
            # Robust conversion for cases where Baileys/WASender sends Int64 as a dict {low, high}
            if isinstance(msg_ts, dict):
                ts_int = int(msg_ts.get("low", msg_ts.get("seconds", 0)))
            else:
                ts_int = int(msg_ts)
                
            now_ts = int(time.time())
            diff = now_ts - ts_int
            
            # Logger de diagnóstico
            logger.info(f"[DEBUG TIMESTAMP] Msg TS: {ts_int} | Now: {now_ts} | Diff: {diff}s")

            # Aumentado a 5 días (432000s) para permitir procesar mensajes acumulados del fin de semana
            if diff > 432000: 
                logger.warning(f"[SAFETY] Ignorando mensaje MUY antiguo de {diff}s (Remitente: {phone}).")
                return JSONResponse({"status": "very old message ignored", "diff": diff}, status_code=200)
            
            if diff > 60:
                logger.info(f"[SAFETY] Procesando mensaje con retraso detectado de {diff}s...")
        except Exception as te:
            logger.error(f"Error parseando timestamp ({type(msg_ts)}): {te}")

    # --- DEBUG CRÍTICO: VER EL PAYLOAD COMPLETO ---
    logger.info(f"[DEBUG PAYLOAD] Key: {key}")
    logger.info(f"[DEBUG PAYLOAD] From: {msg_obj.get('from')} | SenderPn: {key.get('senderPn')} | Cleaned: {key.get('cleanedSenderPn')}")
    
    # --- DISCOVERY: CAPTURAR ID DE GRUPO ---
    remote_jid = key.get("remoteJid", "")
    if "@g.us" in remote_jid:
        group_name = msg_obj.get("pushName") or "Grupo Desconocido"
        logger.info(f"🔍 [GROUP_DISCOVERY] ID: {remote_jid} | Name: {group_name}")
    
    phone = str(phone or "").strip()

    # --- FILTRO DE EJECUTIVOS (Solicitado por usuario) ---
    # Si quien escribe es un ejecutivo (excepto Pablo Galleguillos), 
    # forzamos from_me=True para que el bot no responda.
    user_lookup_phone = sender_phone if from_me else (sender_phone or phone)
    user_found = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_user_by_phone(user_lookup_phone))
    if not from_me and user_found and user_found.get("rol") in ["agente", "supervisor"]:
        if user_found.get("nombre") != "Pablo Galleguillos":
            logger.info(f"[FILTER] Mensaje de EJECUTIVO ({user_found.get('nombre')}) detectado. Forzando modo manual.")
            from_me = True

    # --- GUARDA DE FIRMA DIGITAL: SOLO EL TELÉFONO DEL DOCUMENTO VIGENTE ---
    # Si detectamos que es un grupo (@g.us), lo ignoramos
    if "@g.us" in (key.get("remoteJid") or ""):
         logger.info(f"[WHATSAPP] Ignorando mensaje de grupo")
         return JSONResponse({"status": "group message ignored"}, status_code=200)

    # --- EXTRACCIÓN DEL TEXTO (RESTAURADA) ---
    text = (
        msg_obj.get("messageBody") or
        msg_obj.get("message", {}).get("conversation") or
        msg_obj.get("message", {}).get("extendedTextMessage", {}).get("text", "") or
        ""
    ).strip()
    # -----------------------------------------

    # Limpiamos el número: nos quedamos solo con dígitos
    phone_digits = "".join(filter(str.isdigit, phone))
    if from_me and not bot_outbound and not phone_digits:
        from chatbot.storage import record_observability_event
        await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: record_observability_event("human_takeover_unresolved_peer", {
                "provider_message_id": str(provider_message_id) if provider_message_id else None,
                "remote_jid": str(key.get("remoteJid") or "")[:100],
            }),
        )
        return JSONResponse({"ok": True, "status": "human_takeover_unresolved_peer"}, status_code=200)
    
    if not phone_digits:
        return JSONResponse({"status": "invalid phone"}, status_code=200)

    # Normalización para Chile (Casos comunes de entrada: 912345678, 56912345678, +56912345678)
    phone = _normalize_webhook_phone(phone)

    logger.info(f"[WHATSAPP] {'[HUMANO]' if from_me else '[CLIENTE]'} Mensaje en {phone}: {text}")
    try:
        from chatbot.storage import log_event, EventType, get_db as _sync_db
        # Para el log de eventos, usamos el número limpio sin el '+'
        phone_log = phone.replace("+", "")
        await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: log_event(
                phone_log,
                EventType.MSG_IN if not from_me else EventType.MSG_OUT,
                "user" if not from_me else "agent",
                {"text": text},
            ),
        )
    except:
        pass
    
    # Persist to the durable queue. The durable worker is the only component
    # authorized to batch, invoke the LLM and send a chatbot response.
    if not from_me:
        from chatbot.chatbot_queue import create_inbound_job
        provider_message_id = (
            key.get("id")
            or msg_obj.get("id")
            or msg_obj.get("messageId")
            or data.get("message_id")
            or data.get("id")
        )
        if not provider_message_id:
            logger.error("[CHATBOT_QUEUE] inbound rejected: provider id missing")
            raise HTTPException(status_code=422, detail="Inbound provider message id required")
        try:
            job_id = await loop.run_in_executor(
                _WEB_THREAD_POOL,
                lambda: create_inbound_job(
                    _sync_db(),
                    inbound_provider_message_id=str(provider_message_id),
                    phone=phone,
                    text=text,
                    conversation_id=None,
                ),
            )
        except ValueError as exc:
            logger.warning("[CHATBOT_QUEUE] inbound rejected: %s", exc)
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            logger.exception("[CHATBOT_QUEUE] persistence failed")
            raise HTTPException(status_code=503, detail="Durable queue unavailable")
        return JSONResponse({"ok": True, "job_id": str(job_id)}, status_code=200)

    if from_me and bot_outbound:
        return JSONResponse({"ok": True, "status": "chatbot_outbound_recorded"}, status_code=200)

    # A manual outbound message invalidates any automatic response that has not
    # reached WhatsApp yet.  The durable worker performs a second cutoff check
    # immediately before provider delivery for batches already in processing.
    if not from_me:
        return JSONResponse({"ok": True, "status": "outbound_message_recorded"}, status_code=200)

    try:
        from chatbot.storage import mark_human_takeover, record_observability_event
        from chatbot.chatbot_queue import cancel_pending_batches_for_human
        takeover_at = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: mark_human_takeover(phone, source="whatsapp_human_message"),
        )
        cancelled = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: cancel_pending_batches_for_human(_sync_db(), phone=phone),
        )
        await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: record_observability_event(
                "human_takeover_received",
                {"conversation_id": None, "takeover_at": takeover_at,
                 "cancelled_pending_batches": int(cancelled or 0)},
            ),
        )
    except Exception:
        logger.exception("[CHATBOT_QUEUE] no se pudo invalidar la respuesta automatica ante takeover humano")

    return JSONResponse({"ok": True, "status": "human_message_recorded"}, status_code=200)

@app.get("/health")
async def health_check():
    now = datetime.now(CHILE_TZ).isoformat()
    try:
        worker_status = background_tasks_status.get("chatbot_response") or {}
        queue_health = worker_status.get("health_snapshot")
        snapshot_at = worker_status.get("health_snapshot_at")
        if not queue_health or not snapshot_at:
            raise RuntimeError("queue_health_snapshot_missing")
        snapshot_dt = datetime.fromisoformat(str(snapshot_at).replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - snapshot_dt.astimezone(timezone.utc)).total_seconds() > 30:
            raise RuntimeError("queue_health_snapshot_stale")
        degraded_reasons = list(queue_health.get("degraded_reasons") or [])
    except Exception as exc:
        logger.exception("[HEALTH] chatbot queue metrics unavailable")
        queue_health = {
            "metrics_available": False,
            "degraded_reasons": ["queue_metrics_unavailable"],
            "error_type": type(exc).__name__,
        }
        degraded_reasons = ["queue_metrics_unavailable"]
    try:
        process_status = background_tasks_status.get("lead_processing") or {}
        process_health = process_status.get("health_snapshot")
        snapshot_at = process_status.get("health_snapshot_at")
        if not process_health or not snapshot_at:
            raise RuntimeError("process_health_snapshot_missing")
        snapshot_dt = datetime.fromisoformat(str(snapshot_at).replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - snapshot_dt.astimezone(timezone.utc)).total_seconds() > 30:
            raise RuntimeError("process_health_snapshot_stale")
        if process_health.get("expired_leases"):
            degraded_reasons.append("process_service_expired_leases")
    except Exception as exc:
        logger.exception("[HEALTH] PROCESS_SERVICE metrics unavailable")
        process_health = {
            "metrics_available": False,
            "error_type": type(exc).__name__,
        }
        degraded_reasons.append("process_service_metrics_unavailable")
    return {
        "status": "degraded" if degraded_reasons else "healthy",
        "deploy_commit": os.getenv("RENDER_GIT_COMMIT", "unknown"),
        "server_time": now,
        "background_tasks": background_tasks_status,
        "chatbot": {
            "worker": background_tasks_status.get("chatbot_response", {"status": "missing"}),
            "queue": queue_health,
        },
        "process_service": {
            "worker": background_tasks_status.get("lead_processing", {"status": "missing"}),
            "queue": process_health,
        },
        "uptime_now": time.strftime("%Y-%m-%d %H:%M:%S")
    }

@app.post("/api/keep-alive")
async def api_keep_alive(request: Request):
    """Endpoint ligero para renovar la cookie de sesión sin recargar"""
    return {"status": "ok", "timestamp": time.time()}

@app.get("/campana/respuesta")
async def campana_respuesta(
    request: Request,
    email: str = Query(...),
    accion: str = Query(...),
    codigos: str = Query("N/A"),
    campana: str = Query(...),
    token: str = Query(""),
    mode: str = Query("live")
):
    return await handle_campana_respuesta(request, email, accion, codigos, campana, mode, token)

@app.get("/api/reporte_real")
async def api_reporte_real():
    from api_reporte_real import get_reporte_real
    data = get_reporte_real()
    return data
    

# ========================= 10. RUTAS CAPTACIÓN (NUEVO) =========================

@app.get("/captacion", response_class=HTMLResponse)
async def view_captaciones(
    request: Request,
    comuna: list[str] = Query(None),
    estado: str = Query(None),
    ejecutivo: str = Query(None),
    operacion: str = Query(None),
    telefono: str = Query(None),
    portal: str = Query(None),
    classification: str = Query(None),
    orden: str = Query(None),
    meta_semana: str = Query(None),
    gestion_fecha: str = Query(None),
    gestion_semana: str = Query(None),
    sort_by: str = Query(None),
    sort_dir: str = Query("desc"),
    page: int = Query(1, ge=1)
):
    _perf = {}  # {stage: perf_counter_ns}
    _perf["start"] = time.perf_counter()
    _captacion_diag = {
        "request_id": getattr(request.state, "trace_id", None) or str(uuid.uuid4())[:8],
        "first_module_visit": False,
        "auth_ms": 0,
        "actor_resolution_ms": 0,
        "filter_parse_ms": 0,
        "lazy_import_ms": 0,
        "mongo_client_init_ms": 0,
        "db_first_operation_ms": 0,
        "catalogs_ms": 0,
        "catalogs_cache_hit": "not_run",
        "list_ms": 0,
        "kpi_ms": 0,
        "kpi_cache_hit": "not_run",
        "goals_ms": 0,
        "goals_cache_hit": "not_run",
        "goals_snapshot_hit": "not_run",
        "workforce_ms": 0,
        "ledger_ms": 0,
        "comparable_period_ms": 0,
        "context_build_ms": 0,
        "template_ms": 0,
        "wait_ms": 0,
        "compute_ms": 0,
        "worked_total_runtime": 0,
        "worked_portal_counts_runtime": [],
        "portal_rows_runtime": 0,
    }
    _lazy_import_started = time.perf_counter()
    from chatbot.storage import get_async_db
    _captacion_diag["lazy_import_ms"] = round((time.perf_counter() - _lazy_import_started) * 1000, 1)
    _mongo_client_started = time.perf_counter()
    adb = get_async_db()
    _captacion_diag["mongo_client_init_ms"] = round((time.perf_counter() - _mongo_client_started) * 1000, 1)
    _db_first_operation_started = time.perf_counter()
    user = await get_current_user_doc(request)
    _captacion_diag["db_first_operation_ms"] = round((time.perf_counter() - _db_first_operation_started) * 1000, 1)
    _perf["auth"] = time.perf_counter()
    _captacion_diag["auth_ms"] = round((_perf["auth"] - _perf["start"]) * 1000, 1)
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    # Marcar la primera visita solo después de autenticar correctamente. Las
    # redirecciones de sesión inválida no deben consumir el caso diagnóstico.
    _captacion_diag_first_visit = _captacion_diag_is_first_module_visit()
    _captacion_diag["first_module_visit"] = _captacion_diag_first_visit

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")
    user_id = str(user["_id"])
    user_email = user.get("email", "")
    current_ejecutivo = ejecutivo if ejecutivo and ejecutivo != "Todos" else ""
    _captacion_diag["actor_resolution_ms"] = round((time.perf_counter() - _perf["auth"]) * 1000, 1)
    
    limit = 10
    valid_orders = {"prioridad", "recientes", "probabilidad", "antiguas", "ultima_gestion"}
    # Si hay ordenamiento por columna, ese criterio tiene prioridad aunque la
    # URL conserve un `orden` antiguo.
    current_order = (
        orden
        if orden in valid_orders and not sort_by
        else ("prioridad" if not sort_by else "")
    )

    # Semana visible y filtro temporal se normalizan antes de consultar el
    # listado. Las métricas superiores siguen usando exclusivamente los
    # filtros globales y no reciben estos parámetros.
    goal_today = datetime.now(pytz.timezone("America/Santiago")).date()
    current_goal_monday = goal_today - timedelta(days=goal_today.weekday())
    selected_goal_monday = current_goal_monday
    if meta_semana:
        try:
            candidate_goal_monday = datetime.strptime(meta_semana, "%Y-%m-%d").date()
            if candidate_goal_monday.weekday() == 0 and candidate_goal_monday <= current_goal_monday:
                selected_goal_monday = candidate_goal_monday
        except (TypeError, ValueError):
            pass
    goal_is_current_week = selected_goal_monday == current_goal_monday
    goal_period_start = None if goal_is_current_week else selected_goal_monday.isoformat()
    goal_period_end = None if goal_is_current_week else (selected_goal_monday + timedelta(days=6)).isoformat()
    goal_week_query = goal_period_start or ""

    temporal_date = None
    temporal_week_start = None
    try:
        if gestion_fecha:
            temporal_date = datetime.strptime(gestion_fecha, "%Y-%m-%d").date().isoformat()
        elif gestion_semana:
            candidate_temporal_week = datetime.strptime(gestion_semana, "%Y-%m-%d").date()
            if candidate_temporal_week.weekday() == 0 and candidate_temporal_week <= current_goal_monday:
                temporal_week_start = candidate_temporal_week.isoformat()
    except (TypeError, ValueError):
        pass

    _captacion_diag["filter_parse_ms"] = round((time.perf_counter() - _perf["auth"]) * 1000, 1)

    loop = asyncio.get_running_loop()
    # El objetivo y el listado usan el cliente síncrono en hilos separados.
    # Obtenerlo antes permite que ambas cargas comiencen sin esperar al listado.
    from chatbot.storage import get_db as get_sync_db
    _sync_db_started = time.perf_counter()
    sync_db = get_sync_db()
    _captacion_diag["mongo_client_init_ms"] = round(
        _captacion_diag["mongo_client_init_ms"] + (time.perf_counter() - _sync_db_started) * 1000, 1
    )
    _perf["list_submit"] = time.perf_counter()
    _list_perf = {}
    _list_mongo_spans = []

    def _run_captacion_list():
        from chatbot.storage import set_dashboard_perf_context, reset_dashboard_perf_context
        _mongo_token = set_dashboard_perf_context(_list_mongo_spans)
        try:
            return get_captacion_list(
                user_role=user_role,
                user_name=user_name,
                user_id=user_id,
                user_email=user_email,
                page=page,
                limit=limit,
                comuna_filter=comuna,
                status_filter=estado,
                executive_filter=ejecutivo,
                operacion_filter=operacion,
                telefono_filter=telefono,
                portal_filter=portal,
                classification_filter=classification,
                sort_by=sort_by,
                sort_dir=sort_dir,
                order_filter=current_order,
                gestion_date=temporal_date,
                gestion_week_start=temporal_week_start,
                perf_context=_list_perf,
                return_portals=True,
            )
        finally:
            reset_dashboard_perf_context(_mongo_token)

    list_task = loop.run_in_executor(
        _WEB_THREAD_POOL,
        _run_captacion_list,
    )
    _executives_catalog_elapsed = {}

    def _fetch_executives():
        _catalog_started = time.perf_counter()
        from chatbot.storage import get_db
        db = get_db()
        result = list(db["usuarios"].find(
            {"is_active": True, "rol": "agente", "comunas_interes_norm": {"$exists": True, "$ne": []}},
            {"nombre": 1}
        ).sort("nombre", 1))
        names = [u.get("nombre", "") for u in result if u.get("nombre")]
        names.insert(0, "Sin asignar")
        _executives_catalog_elapsed["ms"] = round((time.perf_counter() - _catalog_started) * 1000, 1)
        return names

    executives = []
    executives_task = None
    _executives_catalog_started = time.perf_counter()
    if user_role in CAPTACION_PRIVILEGED_ROLES:
        executive_cache = getattr(app.state, "captacion_executives_cache", None)
        executive_cache_fresh = (
            executive_cache
            and time.time() - executive_cache.get("time", 0) < 300
        )
        if executive_cache_fresh:
            executives = executive_cache["data"]
            _perf["executives_cache"] = "HIT"
        else:
            loop2 = asyncio.get_running_loop()
            executives_task = loop2.run_in_executor(None, _fetch_executives)
            _perf["executives_cache"] = "MISS"
    else:
        _perf["executives_cache"] = "not_run"
    if executives_task is None:
        _captacion_diag["catalogs_ms"] = round((time.perf_counter() - _executives_catalog_started) * 1000, 1)
    # El resultado del listado se recoge después de lanzar KPI y metas. Así el
    # cold path no queda serializado como: listado -> KPI -> metas.

    nav_query_pairs = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in {"meta_semana", "gestion_fecha", "gestion_semana"}
    ]

    def goal_navigation_url(week_start):
        pairs = list(nav_query_pairs)
        if week_start != current_goal_monday:
            pairs.append(("meta_semana", week_start.isoformat()))
        if temporal_week_start:
            pairs.append(("gestion_semana", week_start.isoformat()))
        query = urlencode(pairs)
        return "/captacion" + (f"?{query}" if query else "")

    goal_week_end_date = selected_goal_monday + timedelta(days=6)
    month_labels = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
    if selected_goal_monday.month == goal_week_end_date.month:
        goal_week_range = (
            f"{selected_goal_monday.day:02d}–{goal_week_end_date.day:02d} "
            f"{month_labels[selected_goal_monday.month - 1]}"
        )
    else:
        goal_week_range = (
            f"{selected_goal_monday.day:02d} {month_labels[selected_goal_monday.month - 1]}–"
            f"{goal_week_end_date.day:02d} {month_labels[goal_week_end_date.month - 1]}"
        )
    next_goal_monday = min(selected_goal_monday + timedelta(days=7), current_goal_monday)

    temporal_filter_kind = None
    temporal_filter_label = None
    temporal_context = None
    if temporal_date:
        temporal_date_obj = date.fromisoformat(temporal_date)
        temporal_day_labels = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
        temporal_filter_label = (
            f"Día: {temporal_day_labels[temporal_date_obj.weekday()]} "
            f"{temporal_date_obj.day:02d} {month_labels[temporal_date_obj.month - 1]}"
        )
        temporal_context = f"Mostrando propiedades gestionadas el {temporal_filter_label[5:]}"
        temporal_filter_kind = "date"
    elif temporal_week_start:
        temporal_filter_label = f"Semana {selected_goal_monday.isocalendar().week} · {goal_week_range}"
        temporal_context = f"Mostrando propiedades gestionadas en la {temporal_filter_label.lower()}"
        temporal_filter_kind = "week"

    # Valores iniciales necesarios para construir los links de paginación
    # mientras el listado sigue ejecutándose en paralelo. Después de recoger
    # el listado, current_portal se valida contra su catálogo real.
    current_comunas = [c for c in (comuna or []) if c]
    current_estado = estado
    current_operacion = (operacion or "").lower()
    current_telefono = telefono or ""
    current_portal = portal or ""
    current_classification = classification or ""

    allowed_sort_keys = {"comuna", "precio", "owner_probability", "antiguedad"}
    raw_sort_keys = [s.strip() for s in str(sort_by or "").split(",") if s.strip()]
    raw_sort_dirs = [s.strip().lower() for s in str(sort_dir or "").split(",") if s.strip()]
    sort_keys = []
    sort_dirs = []
    for index, key in enumerate(raw_sort_keys):
        if key not in allowed_sort_keys or key in sort_keys:
            continue
        sort_keys.append(key)
        sort_dirs.append(
            raw_sort_dirs[index]
            if index < len(raw_sort_dirs) and raw_sort_dirs[index] in ("asc", "desc")
            else "desc"
        )
    current_sorts = {
        key: {"direction": sort_dirs[index], "priority": index + 1}
        for index, key in enumerate(sort_keys)
    }

    from urllib.parse import urlencode
    pagination_query = {
        key: value for key, value in {
            "telefono": current_telefono,
            "comuna": current_comunas,
            "operacion": current_operacion,
            "portal": current_portal,
            "orden": current_order,
            "estado": current_estado,
            "ejecutivo": current_ejecutivo,
            "classification": current_classification,
            "meta_semana": goal_week_query,
            "gestion_fecha": temporal_date,
            "gestion_semana": temporal_week_start,
            "sort_by": ",".join(sort_keys),
            "sort_dir": ",".join(sort_dirs),
        }.items() if value
    }
    pagination_base_url = "?" + urlencode(pagination_query, doseq=True) + ("&" if pagination_query else "")
    _perf["diag_done"] = time.perf_counter()
    
    # KPIs de todos los portales soportados.
    base_query = {
        # Universo dinámico: todo portal con origen válido entra en las
        # tarjetas superiores y en su desglose por portal.
        "origen": {"$exists": True, "$nin": [None, ""]},
        "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)}
    }
    if user_role not in CAPTACION_PRIVILEGED_ROLES:
        # Agente ve sus propias propiedades: matchea por ejecutivo_id (canonico)
        # con fallback a ejecutivo_asignado para documentos legacy
        base_query["$or"] = [
            {"gestion.ejecutivo_id": user_id},
            {"$and": [
                {"gestion.ejecutivo_id": {"$exists": False}},
                {"gestion.ejecutivo_asignado": user_name},
            ]},
        ]

    # Las metas no dependen del listado ni de los KPI de cartera. Se inicia
    # desde aquí para que su cold fill ocurra en paralelo con ambas consultas.
    goal_executive = current_ejecutivo if user_role in CAPTACION_PRIVILEGED_ROLES else user_name
    goal_excluded_executives = CAPTACION_GOAL_EXCLUDED_EXECUTIVES
    goal_cache_key = _captacion_goal_cache_key(
        goal_executive,
        goal_period_start,
        today=goal_today.isoformat(),
    )
    _goal_diag_context = {}
    _goal_snapshot_mode = "not_run"
    _goal_wait_ms = 0.0
    _goal_compute_ms = 0.0
    _temporal_request = bool(temporal_date or temporal_week_start)
    async def _load_goal_dashboard():
        nonlocal _goal_snapshot_mode, _goal_wait_ms, _goal_compute_ms
        goal_cache = getattr(app.state, 'captacion_goal_cache', {})
        goal_record = goal_cache.get(goal_cache_key)
        goal_now = time.time()
        goal_fresh = (
            goal_record is not None
            and goal_now - goal_record['time'] < 60
        )
        goal_stale_snapshot = (
            _temporal_request
            and goal_record is not None
            and goal_now - goal_record['time'] < 300
        )
        goal_hit = goal_fresh or goal_stale_snapshot
        goal_cache_mode = "HIT" if goal_fresh else "STALE" if goal_stale_snapshot else "MISS"
        if goal_hit:
            return goal_record['data'], goal_cache_mode
        # Si el warm-up del arranque todavía está en curso, esperar el mismo
        # trabajo compartido evita duplicar el cálculo pesado de metas.
        default_goal_prewarm = getattr(app.state, "captacion_goal_prewarm_task", None)
        if not goal_executive and not goal_period_start and default_goal_prewarm is not None:
            _prewarm_wait_started = time.perf_counter()
            await default_goal_prewarm
            _goal_wait_ms += (time.perf_counter() - _prewarm_wait_started) * 1000
            goal_record = getattr(app.state, "captacion_goal_cache", {}).get(goal_cache_key)
            if goal_record is not None:
                return goal_record['data'], "HIT"

        # Una consulta por _id recupera el último snapshot persistente después
        # de un reinicio. Se entrega como fallback y se actualiza en segundo
        # plano; nunca se confunde con un resultado calculado fresco.
        try:
            snapshot = await _read_captacion_goal_snapshot(
                selected_executive=goal_executive or None,
                period_start=goal_period_start,
                period_end=goal_period_end,
                excluded_executives=goal_excluded_executives,
            )
        except Exception:
            logger.exception("[CAPTACION_GOAL_SNAPSHOT] lectura fallida")
            snapshot = None
        # El período actual debe salir siempre del ledger fresco. Un snapshot
        # persistente puede haber sido creado antes de la última gestión (o
        # pertenecer al esquema antiguo sin timestamp), y servirlo aquí deja
        # visible un "0 de 10" hasta la siguiente recarga/refresco. Los
        # períodos históricos explícitos sí pueden reutilizar su snapshot.
        snapshot_can_be_used = snapshot and bool(goal_period_start or goal_period_end)
        if snapshot_can_be_used:
            _goal_snapshot_mode = "HIT"
            _put_captacion_goal_cache(goal_cache_key, snapshot["data"])
            _start_captacion_goal_refresh(
                goal_cache_key,
                selected_executive=goal_executive or None,
                period_start=goal_period_start,
                period_end=goal_period_end,
                excluded_executives=goal_excluded_executives,
            )
            return snapshot["data"], "STALE"

        _goal_snapshot_mode = "MISS"
        _goal_refresh_started = time.perf_counter()
        inflight_before = getattr(app.state, "captacion_goal_inflight", {}).get(goal_cache_key)
        refresh_task = _start_captacion_goal_refresh(
            goal_cache_key,
            selected_executive=goal_executive or None,
            period_start=goal_period_start,
            period_end=goal_period_end,
            excluded_executives=goal_excluded_executives,
            perf_context=_goal_diag_context if inflight_before is None else None,
        )
        goal_data = await refresh_task
        _goal_elapsed_ms = (time.perf_counter() - _goal_refresh_started) * 1000
        if inflight_before is not None and not inflight_before.done():
            _goal_wait_ms += _goal_elapsed_ms
        else:
            _goal_compute_ms += _goal_elapsed_ms
        return goal_data, goal_cache_mode

    _perf["goal_start"] = time.perf_counter()
    goal_task = asyncio.create_task(_load_goal_dashboard())

    # Para administradores sin ejecutivo seleccionado, el conjunto es global y
    # no depende del usuario autenticado. Esto permite compartir el cold fill.
    stats_scope = (
        f"global_{user_role}"
        if user_role in CAPTACION_PRIVILEGED_ROLES and not current_ejecutivo
        else f"{user_role}_{user_id}_{current_ejecutivo}"
    )
    cache_key = f"stats_{CAPTACION_KPI_CACHE_VERSION}_{stats_scope}"
    cache_store = getattr(app.state, 'captacion_stats_cache', {})
    from captacion_management import KPI_REVISION_COLLECTION
    _revision_row = await adb[KPI_REVISION_COLLECTION].find_one({"_id": "captacion_kpi_revision"})
    _current_kpi_revision = int((_revision_row or {}).get("revision") or 0)
    _cache_now = time.time()
    _stats_record = cache_store.get(cache_key)
    _worked_portal_cache_valid = _captacion_kpi_cache_is_compatible(_stats_record)
    if _stats_record is not None and not _worked_portal_cache_valid:
        logger.info(
            "[CAPTACION_KPI_CACHE_INVALID] key=%s schema=%s; recalculando snapshot",
            cache_key,
            _stats_record.get("schema_version"),
        )
        # Solo se invalida la entrada incompatible de este scope/version.
        cache_store.pop(cache_key, None)
    _kpi_fresh = (
        _stats_record is not None
        and int(_stats_record.get('kpi_revision') or 0) == _current_kpi_revision
        and _cache_now - _stats_record['time'] < 300
        and 'contact_type_counts' in _stats_record
        and _worked_portal_cache_valid
    )
    # Un cambio de día/semana solo cambia el conjunto del listado. Las cards
    # superiores siguen representando la cartera global y no deben obligar a
    # recalcularse al entrar a un histórico. Si el snapshot expiró, usamos el
    # último válido durante una ventana acotada; las peticiones normales siguen
    # respetando el TTL de 300 s y refrescan de forma habitual.
    _kpi_stale_snapshot = (
        _temporal_request
        and _stats_record is not None
        and int(_stats_record.get('kpi_revision') or 0) == _current_kpi_revision
        and _cache_now - _stats_record['time'] < 900
        and 'contact_type_counts' in _stats_record
        and _worked_portal_cache_valid
    )
    _kpi_hit = _kpi_fresh or _kpi_stale_snapshot
    _kpi_cache_mode = "HIT" if _kpi_fresh else "STALE" if _kpi_stale_snapshot else "MISS"
    _perf["kpi_start"] = time.perf_counter()
    if _kpi_hit:
        in_gestion_count = _stats_record['in_gestion_count']
        captados_count = _stats_record['captados_count']
        descartados_count = _stats_record['descartados_count']
        available_count = _stats_record['available_count']
        ready_to_contact_count = _stats_record['ready_to_contact_count']
        pending_count = _stats_record['pending_count']
        comunas_clean = _stats_record['comunas_clean']
        source_counts = _stats_record.get('source_counts', [])
        contact_type_counts = _stats_record['contact_type_counts']
        worked_portal_counts = _stats_record.get('worked_portal_counts', [])
        contact_attempts = int(_stats_record.get('contact_attempts') or 0)
        effective_contacts = int(_stats_record.get('effective_contacts') or 0)
        contactability_pct = _stats_record.get('contactability_pct')
        contactability_result_buckets = _stats_record.get('contactability_result_buckets', [])
        contactability_insight = _stats_record.get('contactability_insight', '')
    else:
        if ejecutivo and ejecutivo != "Todos" and user_role in CAPTACION_PRIVILEGED_ROLES:
            selected_exec_doc = await adb["usuarios"].find_one(
                {"nombre": ejecutivo}
            )
            if selected_exec_doc:
                exec_id = str(selected_exec_doc["_id"])
                exec_name = selected_exec_doc.get("nombre", ejecutivo)
                # Canonico: matchear por ejecutivo_id, con fallback a nombre para legacy
                base_query["$or"] = [
                    {"gestion.ejecutivo_id": exec_id},
                    {"$and": [
                        {"gestion.ejecutivo_id": {"$exists": False}},
                        {"gestion.ejecutivo_asignado": exec_name},
                    ]},
                ]
            else:
                # Fallback si no encuentra el usuario: buscar solo por nombre
                base_query["gestion.ejecutivo_asignado"] = ejecutivo
        from captacion_kpis import AVAILABLE_STATES, MANAGEMENT_STATES, KPI_MANAGEMENT_STATES, CAPTURED_STATES, DISCARDED_STATES, KPI_WORKED_STATES
        worked_states = list(KPI_WORKED_STATES)
        kpi_facet = [
            {"$match": base_query},
            {"$facet": {
                "available": [{"$match": {"gestion.estado": {"$in": list(AVAILABLE_STATES)}}}, {"$count": "count"}],
                "ready_to_contact": [{"$match": {"gestion.estado": "Por contactar"}}, {"$count": "count"}],
                "management": [{"$match": {"gestion.estado": {"$in": list(KPI_MANAGEMENT_STATES)}}}, {"$count": "count"}],
                "captured": [{"$match": {"gestion.estado": {"$in": list(CAPTURED_STATES)}}}, {"$count": "count"}],
                "discarded": [{"$match": {"gestion.estado": {"$in": list(DISCARDED_STATES)}}}, {"$count": "count"}],
                "sources": [
                    {"$group": {"_id": {"$ifNull": ["$origen", "sin_origen"]}, "count": {"$sum": 1}}},
                    {"$sort": {"count": -1, "_id": 1}},
                ],
                "contact_type": [
                    {"$match": {"gestion.estado": {"$in": list(MANAGEMENT_STATES + CAPTURED_STATES + DISCARDED_STATES)}}},
                    {"$project": {
                        "contact_bucket": {
                            "$cond": [
                                {"$eq": ["$gestion.estado", "Corredor"]},
                                "corredor",
                                "otros",
                            ]
                        }
                    }},
                    {"$group": {"_id": "$contact_bucket", "count": {"$sum": 1}}},
                ],
                "worked_portal": [
                    {"$match": {"gestion.estado": {"$in": worked_states}}},
                    {"$group": {
                        "_id": {"$ifNull": ["$origen", "sin_origen"]},
                        "worked": {"$sum": 1},
                        "corredor": {"$sum": 0},
                        "captadas": {"$sum": {"$cond": [{"$in": ["$gestion.estado", list(CAPTURED_STATES)]}, 1, 0]}},
                    }},
                ],
                "worked_properties": [
                    {"$match": {"gestion.estado": {"$in": worked_states}}},
                    {"$project": {"_id": 1, "origen": 1}},
                ],
            }}
        ]
        kpi_task = adb[Config.CAPTACION_COLLECTION_NAME].aggregate(kpi_facet).to_list(1)
        comuna_task = adb[Config.CAPTACION_COLLECTION_NAME].distinct("comuna", base_query)
        kpi_result, comunas_list = await asyncio.gather(kpi_task, comuna_task)
        counts = (kpi_result[0] if kpi_result else {})
        def _fc(key):
            rows = counts.get(key) or []
            if not rows:
                return 0
            return int((rows[0] or {}).get("count") or 0)
        available_count = _fc("available")
        ready_to_contact_count = _fc("ready_to_contact")
        pending_count = available_count + ready_to_contact_count
        in_gestion_count = _fc("management")
        captados_count = _fc("captured")
        descartados_count = _fc("discarded")
        source_counts = []
        for row in (counts.get("sources") or []):
            source_value = str(row.get("_id") or "").strip()
            source_value = source_value or "sin_origen"
            source_label = (
                "Sin origen"
                if source_value.casefold() in {"sin_origen", "otro"}
                else format_captacion_portal_label(source_value)
            )
            source_counts.append({
                "value": source_value,
                "label": source_label,
                "count": int(row.get("count") or 0),
            })
        source_counts.sort(key=lambda row: (-row["count"], row["label"].casefold()))
        contact_type_counts = {"corredor": 0, "otros": 0}
        for row in (counts.get("contact_type") or []):
            bucket = str(row.get("_id") or "otros")
            if bucket not in contact_type_counts:
                bucket = "otros"
            contact_type_counts[bucket] += int(row.get("count") or 0)
        canonical_contactability = await _load_captacion_canonical_contactability(
            adb,
            counts.get("worked_properties") or [],
        )
        worked_portal_counts = _build_captacion_worked_portal_breakdown(
            counts.get("worked_portal") or [],
            canonical_contactability["by_portal"],
            _broker_counts_by_portal(
                canonical_contactability.get("active_events"),
                {
                    str(row.get("_id")): str(row.get("origen") or "sin_origen").strip() or "sin_origen"
                    for row in (counts.get("worked_properties") or [])
                },
            ),
        )
        comunas_clean = sorted(
            {str(c).strip() for c in comunas_list if c and str(c).strip()},
            key=lambda value: value.casefold(),
        )
        cache_store[cache_key] = {
            'time': _cache_now,
            'schema_version': CAPTACION_KPI_CACHE_SCHEMA,
            'kpi_revision': _current_kpi_revision,
            'in_gestion_count': in_gestion_count,
            'captados_count': captados_count,
            'descartados_count': descartados_count,
            'available_count': available_count,
            'ready_to_contact_count': ready_to_contact_count,
            'pending_count': pending_count,
            'comunas_clean': comunas_clean,
            'source_counts': source_counts,
            'contact_type_counts': contact_type_counts,
            'worked_portal_counts': worked_portal_counts,
            'contact_attempts': canonical_contactability["overall"]["contact_attempts"],
            'effective_contacts': canonical_contactability["overall"]["effective_contacts"],
            'contactability_pct': canonical_contactability["overall"]["contactability_pct"],
            'contactability_result_buckets': canonical_contactability["result_buckets"],
            'contactability_insight': canonical_contactability["result_insight"],
        }
        app.state.captacion_stats_cache = cache_store
        contact_attempts = canonical_contactability["overall"]["contact_attempts"]
        effective_contacts = canonical_contactability["overall"]["effective_contacts"]
        contactability_pct = canonical_contactability["overall"]["contactability_pct"]
        contactability_result_buckets = canonical_contactability["result_buckets"]
        contactability_insight = canonical_contactability["result_insight"]

    # El listado ya avanzó en paralelo con KPI y metas; se recoge ahora antes
    # de construir el contexto que necesita total_count y los catálogos.
    if executives_task is not None:
        items_total, executives = await asyncio.gather(list_task, executives_task)
        app.state.captacion_executives_cache = {
            "time": time.time(),
            "data": executives,
        }
    else:
        items_total = await list_task
    items, total_count, available_ops, available_portals = items_total
    _perf["list_done"] = time.perf_counter()
    _captacion_diag["catalogs_ms"] = round(
        _executives_catalog_elapsed.get("ms", (time.perf_counter() - _executives_catalog_started) * 1000), 1
    )
    _captacion_diag["catalogs_cache_hit"] = (
        "HIT" if _perf.get("executives_cache") == "HIT" and
        str(_list_perf.get("portal_catalog_cache", "")).lower() == "hit" and
        str(_list_perf.get("operation_catalog_cache", "")).lower() == "hit"
        else "MISS"
    )
    _captacion_diag["list_ms"] = round((_perf["list_done"] - _perf["list_submit"]) * 1000, 1)
        
    total_pages = (total_count + limit - 1) // limit
    worked_count = in_gestion_count + captados_count + descartados_count
    _log_captacion_portal_card_consistency(worked_count, worked_portal_counts)
    _captacion_diag["worked_total_runtime"] = worked_count
    _captacion_diag["worked_portal_counts_runtime"] = worked_portal_counts
    _captacion_diag["portal_rows_runtime"] = sum(
        int(row.get("worked") or 0) for row in worked_portal_counts if isinstance(row, dict)
    )
    capture_rate = round((captados_count / worked_count) * 100, 1) if worked_count else 0
    portfolio_count = pending_count + worked_count
    volume_available_rate = round((pending_count / portfolio_count) * 100, 1) if portfolio_count else 0
    # Es el complemento del porcentaje disponible para que ambos valores
    # mostrados siempre reconcilien exactamente el 100% de la cartera.
    volume_worked_rate = round(100 - volume_available_rate, 1) if portfolio_count else 0
    management_rate = round((in_gestion_count / worked_count) * 100, 1) if worked_count else 0
    captured_rate = round((captados_count / worked_count) * 100, 1) if worked_count else 0
    discarded_rate = round((descartados_count / worked_count) * 100, 1) if worked_count else 0
    contact_type_total = sum(contact_type_counts.values())
    contact_corredor_rate = round((contact_type_counts["corredor"] / contact_type_total) * 100, 1) if contact_type_total else 0
    contact_corredor_share = round((contact_type_counts["corredor"] / contact_type_total) * 100, 1) if contact_type_total else 0
    contact_other_share = round(100 - contact_corredor_share, 1) if contact_type_total else 0
    source_total = sum(row.get("count", 0) for row in source_counts)
    for source in source_counts:
        source["percentage"] = round((source["count"] / source_total) * 100, 1) if source_total else 0

    # La tarjeta de origen mantiene todos los registros en la distribución:
    # cuando hay demasiadas fuentes, muestra las dos principales y agrupa el
    # resto en "Otros" para no perder propiedades ni sobrecargar la tarjeta.
    if len(source_counts) > 3:
        source_breakdown = [dict(source) for source in source_counts[:2]]
        other_count = sum(source.get("count", 0) for source in source_counts[2:])
        source_breakdown.append({
            "value": "otros",
            "label": "Otros",
            "count": other_count,
            "percentage": 0,
        })
    else:
        source_breakdown = [dict(source) for source in source_counts]

    if source_total:
        shown_percentage = 0
        for index, source in enumerate(source_breakdown):
            if index == len(source_breakdown) - 1:
                source["percentage"] = round(max(0, 100 - shown_percentage), 1)
            else:
                source["percentage"] = round((source["count"] / source_total) * 100, 1)
                shown_percentage += source["percentage"]

    source_primary = (
        dict(source_counts[0])
        if source_counts
        else {"value": "sin_origen", "label": "Sin origen", "count": 0, "percentage": 0}
    )
    _perf["kpi_done"] = time.perf_counter()
    _captacion_diag["kpi_ms"] = round((_perf["kpi_done"] - _perf["kpi_start"]) * 1000, 1)
    _captacion_diag["kpi_cache_hit"] = _kpi_cache_mode

    captacion_goal, _goal_cache_mode = await goal_task
    _perf["goal_done"] = time.perf_counter()
    _captacion_diag["goals_ms"] = round((_perf["goal_done"] - _perf["goal_start"]) * 1000, 1)
    _captacion_diag["goals_cache_hit"] = _goal_cache_mode
    _captacion_diag["goals_snapshot_hit"] = _goal_snapshot_mode
    _captacion_diag["wait_ms"] += round(_goal_wait_ms, 1)
    _captacion_diag["compute_ms"] += round(_goal_compute_ms, 1)
    _captacion_diag.update(_goal_diag_context)

    # El nombre del ejecutivo conserva los filtros globales y la semana que
    # el supervisor estaba revisando al abrir la vista individual.
    if captacion_goal.get("mode") == "team":
        executive_url_base = [(key, value) for key, value in nav_query_pairs if key != "ejecutivo"]
        if goal_week_query:
            executive_url_base.append(("meta_semana", goal_week_query))
        if temporal_date:
            executive_url_base.append(("gestion_fecha", temporal_date))
        elif temporal_week_start:
            executive_url_base.append(("gestion_semana", temporal_week_start))
        for executive in captacion_goal.get("executives") or []:
            executive_url_pairs = list(executive_url_base)
            executive_url_pairs.append(("ejecutivo", executive.get("name") or ""))
            executive["detail_url"] = "/captacion?" + urlencode(executive_url_pairs, doseq=True)

    # Alinear con nombre de variables del template original
    current_comunas = [c for c in (comuna or []) if c]
    current_comuna = current_comunas[0] if len(current_comunas) == 1 else ""
    current_estado = estado
    current_operacion = (operacion or "").lower()
    current_telefono = telefono or ""
    available_portal_values = {item["value"] for item in available_portals}
    current_portal = portal if portal in available_portal_values else ""
    current_classification = classification or ""

    _context_started = time.perf_counter()
    _template_context = {
        "request": request,
        "items": items,
        "total_count": total_count,
        "available_count": available_count,
        "ready_to_contact_count": ready_to_contact_count,
        "pending_count": pending_count,
        "in_gestion_count": in_gestion_count,
        "worked_count": worked_count,
        "captados_count": captados_count,
        "descartados_count": descartados_count,
        "capture_rate": capture_rate,
        "portfolio_count": portfolio_count,
        "volume_available_rate": volume_available_rate,
        "volume_worked_rate": volume_worked_rate,
        "management_rate": management_rate,
        "captured_rate": captured_rate,
        "discarded_rate": discarded_rate,
        "contact_type_counts": contact_type_counts,
        "contact_type_total": contact_type_total,
        "contact_corredor_rate": contact_corredor_rate,
        "contact_corredor_share": contact_corredor_share,
        "contact_other_share": contact_other_share,
        "contact_attempts": contact_attempts,
        "effective_contacts": effective_contacts,
        "contactability_pct": contactability_pct,
        "contactability_result_buckets": contactability_result_buckets,
        "contactability_insight": contactability_insight,
        "worked_portal_counts": worked_portal_counts,
        "source_counts": source_counts,
        "source_total": source_total,
        "source_primary": source_primary,
        "source_breakdown": source_breakdown,
        "captacion_goal": captacion_goal,
        "goal_week_number": selected_goal_monday.isocalendar().week,
        "goal_week_range": goal_week_range,
        "goal_week_start": selected_goal_monday.isoformat(),
        "goal_is_current_week": goal_is_current_week,
        "goal_previous_url": goal_navigation_url(selected_goal_monday - timedelta(days=7)),
        "goal_next_url": None if goal_is_current_week else goal_navigation_url(next_goal_monday),
        "goal_current_url": goal_navigation_url(current_goal_monday),
        "temporal_filter_kind": temporal_filter_kind,
        "temporal_filter_value": temporal_date or temporal_week_start or "",
        "temporal_filter_label": temporal_filter_label,
        "temporal_context": temporal_context,
        "comunas": comunas_clean,
        "available_ops": available_ops,
        "user_role": user_role,
        "user_name": user_name,
        "current_comuna": current_comuna,
        "current_comunas": current_comunas,
        "current_estado": current_estado,
        "current_ejecutivo": current_ejecutivo,
        "current_operacion": current_operacion,
        "current_telefono": current_telefono,
        "current_portal": current_portal,
        "available_portals": available_portals,
        "current_order": current_order,
        "current_classification": current_classification,
        "current_sort_by": ",".join(sort_keys),
        "current_sort_dir": ",".join(sort_dirs),
        "current_sorts": current_sorts,
        "pagination_base_url": pagination_base_url,
        "executives": executives,
        "pagination": {
            "current_page": page,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }
    }
    _context_built = time.perf_counter()
    response = templates.TemplateResponse(request, "captacion_list.html", _template_context, headers={
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
    })
    _template_created = time.perf_counter()
    _captacion_diag["context_build_ms"] = round((_context_built - _context_started) * 1000, 1)
    _captacion_diag["template_ms"] = round((_template_created - _context_built) * 1000, 1)

    _perf["render"] = time.perf_counter()
    request.state.captacion_perf = {
        **_list_perf,
        "list_mongo_calls": len(_list_mongo_spans),
        "list_mongo_spans": _list_mongo_spans,
        "kpi_cache": _kpi_cache_mode,
        "goal_cache": _goal_cache_mode,
    }
    _cache_modes = [
        _perf.get("executives_cache"),
        _list_perf.get("portal_catalog_cache"),
        _list_perf.get("operation_catalog_cache"),
        _kpi_cache_mode,
        _goal_cache_mode,
    ]
    _cache_hits = sum(str(value).upper() in {"HIT", "STALE"} for value in _cache_modes)
    _cache_misses = sum(str(value).upper() == "MISS" for value in _cache_modes)
    _cache_stale = sum(str(value).upper() == "STALE" for value in _cache_modes)
    _cold_total_ms = ((_perf["render"] - _perf["start"]) * 1000) if _cache_misses else 0
    _warm_total_ms = ((_perf["render"] - _perf["start"]) * 1000) if not _cache_misses else 0
    request.state.captacion_perf.update({
        "cache_hits": _cache_hits,
        "cache_misses": _cache_misses,
        "cache_stale": _cache_stale,
        "cold_total_ms": _cold_total_ms,
        "warm_total_ms": _warm_total_ms,
    })
    _t0 = _perf["start"]
    _stage_ms = {
        "auth": (_perf["auth"] - _perf["start"]) * 1000,
        "list": (_perf["list_done"] - _perf["list_submit"]) * 1000,
        "kpi": (_perf["kpi_done"] - _perf["kpi_start"]) * 1000,
        "goal": (_perf["goal_done"] - _perf["goal_start"]) * 1000,
        "render": (_perf["render"] - _perf["goal_done"]) * 1000,
    }
    request.state.captacion_perf.update({
        "auth_ms": round(_stage_ms["auth"], 1),
        "list_stage_ms": round(_stage_ms["list"], 1),
        "kpi_ms": round(_stage_ms["kpi"], 1),
        "goal_ms": round(_stage_ms["goal"], 1),
        "render_ms": round(_stage_ms["render"], 1),
    })
    _deltas = [f"{name}={value:.0f}" for name, value in _stage_ms.items()]
    _deltas.append(f"total={( _perf['render'] - _t0) * 1000:.0f}")
    _deltas.append(f"diag={( _perf['diag_done'] - _perf['list_submit']) * 1000:.0f}")
    _total_ms = (_perf['render'] - _t0) * 1000
    _captacion_perf = getattr(request.state, "captacion_perf", {})
    _captacion_diag.update({
        "request_id": getattr(request.state, "trace_id", None) or _captacion_diag["request_id"],
        "total_ms": round(_total_ms, 1),
        "list_ms": round((_perf["list_done"] - _perf["list_submit"]) * 1000, 1),
        "kpi_ms": round((_perf["kpi_done"] - _perf["kpi_start"]) * 1000, 1),
        "goals_ms": round((_perf["goal_done"] - _perf["goal_start"]) * 1000, 1),
        "kpi_cache_hit": _kpi_cache_mode,
        "goals_cache_hit": _goal_cache_mode,
        "goals_snapshot_hit": _goal_snapshot_mode,
        "cache_hits": _cache_hits,
        "cache_misses": _cache_misses,
        "cache_stale": _cache_stale,
        "portal_catalog_cache": _list_perf.get("portal_catalog_cache", "not_run"),
        "operation_catalog_cache": _list_perf.get("operation_catalog_cache", "not_run"),
        "list_query_ms": _list_perf.get("query_ms", "not_run"),
        "list_count_ms": _list_perf.get("count_ms", "not_run"),
        "list_mongo_calls": len(_list_mongo_spans),
    })
    request.state.captacion_perf["diagnostic"] = _captacion_diag
    logger.info("[CAPTACION_PERF_DETAIL] %s", json.dumps(_captacion_diag, ensure_ascii=False, sort_keys=True))
    response.headers["X-Captacion-Perf"] = (
        f"total={_total_ms:.0f};auth={(_perf['auth'] - _perf['start']) * 1000:.0f};"
        f"list={(_perf['list_done'] - _perf['list_submit']) * 1000:.0f};"
        f"kpi={(_perf['kpi_done'] - _perf['kpi_start']) * 1000:.0f};"
        f"goal={(_perf['goal_done'] - _perf['goal_start']) * 1000:.0f};"
        f"render={(_perf['render'] - _perf['goal_done']) * 1000:.0f};"
        f"list_query={_captacion_perf.get('query_ms', 'na')};"
        f"mongo={_captacion_perf.get('list_mongo_calls', 'na')};"
        f"cache_hits={_cache_hits};cache_misses={_cache_misses};cache_stale={_cache_stale};"
        f"cold_total_ms={_cold_total_ms:.0f};warm_total_ms={_warm_total_ms:.0f};"
        f"kpi_cache={_kpi_cache_mode};goal_cache={_goal_cache_mode}"
    )
    if _total_ms > 2000:
        logger.warning(
            f"[CAPTACION_PERF] total_ms={_total_ms:.0f} "
            f"auth_ms={(_perf['auth'] - _perf['start']) * 1000:.0f} "
            f"list_ms={(_perf['list_done'] - _perf['list_submit']) * 1000:.0f} "
            f"list_query_ms={_captacion_perf.get('query_ms', 'n/a')} "
            f"count_ms={_captacion_perf.get('count_ms', 'n/a')} "
            f"kpi_ms={(_perf['kpi_done'] - _perf['kpi_start']) * 1000:.0f} "
            f"goal_ms={(_perf['goal_done'] - _perf['goal_start']) * 1000:.0f} "
            f"render_ms={(_perf['render'] - _perf['goal_done']) * 1000:.0f} "
            f"mongo_calls={_captacion_perf.get('list_mongo_calls', 'n/a')} "
            f"cache_hits={_cache_hits} cache_misses={_cache_misses} cache_stale={_cache_stale} "
            f"cold_total_ms={_cold_total_ms:.0f} warm_total_ms={_warm_total_ms:.0f} "
            f"stages={' '.join(_deltas)} "
            f"role={user_role} page={page} sort={sort_by or 'def'} "
            f"ejec={current_ejecutivo or '-'} comuna={current_comuna or '-'} "
            f"items={len(items)} total={total_count} "
            f"kpi_cache={_kpi_cache_mode} "
            f"goal_cache={_goal_cache_mode} "
            f"exec_catalog_cache={_perf.get('executives_cache', 'N/A')} "
            f"portal_catalog_cache={_list_perf.get('portal_catalog_cache', 'N/A')} "
            f"ops_catalog_cache={_list_perf.get('operation_catalog_cache', 'N/A')}"
        )

    return response

@app.get("/followup/open/{signed_token}")
async def open_followup_link(request: Request, signed_token: str):
    """Attribute a WhatsApp follow-up click, then preserve the old detail UX."""
    try:
        payload = verify_followup_token(signed_token)
        db = get_db()
        task = find_tracked_task(db, payload["task_id"])
        if not task:
            raise FollowupTokenError("followup_task_not_found")
        if task.get("lead_type") == "captacion":
            entity_id = task.get("obj_id")
            target = f"/captacion/{entity_id}"
        else:
            entity_id = task.get("lead_id")
            target = f"/crm/lead-id/{entity_id}"
        if not entity_id:
            raise FollowupTokenError("followup_entity_missing")
        record_followup_event(
            db,
            task=task,
            event_type="reminder_clicked",
            source="whatsapp_followup",
        )
        separator = "&" if "?" in target else "?"
        target = f"{target}{separator}followup_token={quote(signed_token, safe='')}"
        return RedirectResponse(url=target, status_code=302)
    except FollowupTokenError as exc:
        raise HTTPException(status_code=410, detail=str(exc))

@app.get("/captacion/{obj_id}", response_class=HTMLResponse)
async def view_captacion_detail_route(
    request: Request,
    obj_id: str,
    followup_token: str | None = Query(None),
    source: str | None = Query(None),
):
    user = await get_current_user_doc(request)
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")



    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_captacion_detail(obj_id))
    if not data:
        return HTMLResponse("Propiedad no encontrada")

    # El acceso al detalle y a sus APIs usa una sola regla de permisos.
    if not can_manage_captacion(user, data):
        return RedirectResponse(url="/captacion?error=no_asignada")

    def _record_unattributed_open(open_source=None):
        try:
            from chatbot.storage import get_db as _sync_db
            record_captacion_detail_open(
                _sync_db(),
                property_id=obj_id,
                executive_id=str(user.get("_id") or user.get("nombre") or ""),
                source=open_source or ("captacion_list" if source == "captacion_list" else "direct"),
            )
        except Exception:
            # A telemetry failure must never block a valid authenticated detail view.
            logger.warning("[CAPTACION] no se pudo registrar apertura: obj_id=%s", obj_id, exc_info=True)

    if followup_token:
        try:
            from chatbot.storage import get_db as _sync_db
            record_followup_open(
                _sync_db(), token=followup_token, entity_id=obj_id,
                actor_user_id=str(user.get("_id") or ""),
            )
        except FollowupTokenError:
            logger.info("[FOLLOWUP] captacion open was not attributable: obj_id=%s", obj_id)
            _record_unattributed_open("direct")
    else:
        _record_unattributed_open()

    # Ya no calculamos el matching aquí (se hace vía AJAX)
    
    return templates.TemplateResponse(request, "captacion_detail.html", {
        "request": request,
        "prop": data,
        "user_name": user_name,
        "user_role": user.get("rol", "agente")
    }, headers={"Content-Type": "text/html; charset=utf-8"})

# --- PROTECCIÓN ANTI-SPAM PARA MATCHING ---
PENDING_MATCHING_REQUESTS = {} # obj_id -> timestamp

@app.get("/api/captacion/{obj_id}/matching")
async def api_get_matching_leads(request: Request, obj_id: str):
    user_doc = await get_current_user_doc(request)
    if not user_doc:
        raise HTTPException(status_code=401, detail="Sesión inválida")

    from api_captacion import get_captacion_detail, get_matching_leads_analysis, get_cached_value, set_cached_value

    loop = asyncio.get_running_loop()

    cache_key = f"matching_{obj_id}"
    cached_data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_cached_value(cache_key))
    if cached_data:
        return cached_data

    now = time.time()
    if obj_id in PENDING_MATCHING_REQUESTS:
        last_req = PENDING_MATCHING_REQUESTS[obj_id]
        if now - last_req < 5:
            return {"status": "processing", "message": "Ya se esta calculando el matching. Por favor espere."}

    PENDING_MATCHING_REQUESTS[obj_id] = now

    try:
        data = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_captacion_detail(obj_id))
        if not data:
            raise HTTPException(status_code=404, detail="Propiedad no encontrada")
        if not can_manage_captacion(user_doc, data):
            raise HTTPException(status_code=403, detail="No autorizado para gestionar esta captación")

        ma = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_matching_leads_analysis(data))

        response_data = {
            "exact": ma.get("exact", 0),
            "zone": ma.get("zone", 0),
            "broad": ma.get("broad", 0),
            "matching_analysis": ma,
            "ma": ma,
            "zone_name": ma.get("zone_name", "Sin zona"),
            "pitch_text": ma.get("pitch_text", "")
        }

        await loop.run_in_executor(_WEB_THREAD_POOL, lambda: set_cached_value(cache_key, response_data, expire_seconds=300))

        return response_data
    finally:
        if obj_id in PENDING_MATCHING_REQUESTS:
            del PENDING_MATCHING_REQUESTS[obj_id]
@app.post("/api/captacion/update")
async def api_update_captacion(request: Request):
    try:
        username_str = await get_current_user(request)
        user_doc = await get_current_user_doc(request)
        data = await request.json()
        obj_id = data.get("id")
        status = data.get("status")
        notes = data.get("notes")
        next_followup = data.get("next_followup")
        channel = data.get("channel")
        outcome = data.get("outcome")
        user_name = user_doc.get("nombre", username_str) if user_doc else username_str
        
        if not obj_id or not status:
            raise HTTPException(status_code=400, detail="Faltan datos")

        captacion_doc = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: get_captacion_detail(obj_id)
        )
        if not captacion_doc:
            raise HTTPException(status_code=404, detail="Propiedad no encontrada")
        if not user_doc or not can_manage_captacion(user_doc, captacion_doc):
            raise HTTPException(status_code=403, detail="No autorizado para gestionar esta captación")
            
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: update_captacion_status(
                obj_id,
                status,
                notes,
                channel=channel,
                outcome=outcome,
                user_name=user_name,
                next_followup=next_followup,
                user_doc=user_doc,
            )
        )
        if result:
            _up3 = time.perf_counter()
            app.state.captacion_stats_cache = {}
            _invalidate_captacion_goal_cache()
            await loop.run_in_executor(_WEB_THREAD_POOL, _delete_current_captacion_goal_snapshots)
            return {"status": "ok"}
        return {"status": "error", "message": "Operación retornó falso"}
    except HTTPException:
        # Re-lanzar 401/403/400 para que el cliente y el handler global los manejen correctamente
        raise
    except Exception as e:
        logger.error(f"Error updating captacion: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/captacion/contact")
async def api_update_captacion_contact(request: Request):
    try:
        await get_current_user(request)
        user_doc = await get_current_user_doc(request)
        data = await request.json()
        obj_id = data.get("id")
        if not obj_id:
            raise HTTPException(status_code=400, detail="Falta ID")

        captacion_doc = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: get_captacion_detail(obj_id)
        )
        if not captacion_doc:
            raise HTTPException(status_code=404, detail="Propiedad no encontrada")
        if not user_doc or not can_manage_captacion(user_doc, captacion_doc):
            raise HTTPException(status_code=403, detail="No autorizado para gestionar esta captación")
        
        # Check user name in session or payload
        user_name = data.get("user_name")
        if not user_name:
            user_name = user_doc.get("nombre", user_doc.get("username", "Sistema")) if user_doc else "Sistema"
        
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: update_contact_info(
                obj_id,
                nombre=data.get("nombre"),
                telefono=data.get("telefono"),
                email=data.get("email"),
                notas=data.get("notas"),
                user_name=user_name
            )
        )
        return {"status": "ok"} if result else {"status": "error", "message": "Operación retornó falso"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating captacion contact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/captacion/log_action")
async def api_captacion_log_action(request: Request):
    username_str = await get_current_user(request)
    user_doc = await get_current_user_doc(request)
    actual_name = user_doc.get("nombre", username_str) if user_doc else "Sistema"
    
    data = await request.json()
    obj_id = data.get("id")
    action = data.get("action")
    channel = data.get("channel")
    message = data.get("message")
    phone = data.get("phone")
    result = data.get("result")
    template_used = data.get("template_used")
    
    if not obj_id or not action:
        raise HTTPException(status_code=400, detail="Faltan datos")
        
    try:
        from api_captacion import log_captacion_activity
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(
            _WEB_THREAD_POOL,
            lambda: log_captacion_activity(
                obj_id,
                actual_name,
                action,
                channel,
                message,
                phone,
                result,
                template_used,
                user_doc=user_doc,
            )
        )
        return {
            "status": "ok",
            "credited": False,
            "attempt_id": success.get("attempt_id"),
            "attempt_status": success.get("status"),
        } if success else {"status": "error"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging captacion action: {e}")
        raise HTTPException(status_code=500, detail="No fue posible registrar la gestión")


@app.post("/api/captacion/confirm_action")
async def api_captacion_confirm_action(request: Request):
    user_doc = await get_current_user_doc(request)
    if not user_doc:
        raise HTTPException(status_code=401, detail="Sesión inválida")
    payload = await request.json()
    if not payload.get("attempt_id") or not payload.get("result"):
        raise HTTPException(status_code=400, detail="Faltan intento o resultado")
    try:
        from api_captacion import confirm_captacion_activity, _invalidate_captacion_list_cache
        result = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: confirm_captacion_activity(
                payload["attempt_id"], user_doc, payload["result"], payload.get("notes"),
                payload.get("followup_token"), payload.get("commercial_result"),
            ),
        )
        app.state.captacion_stats_cache = {}
        _invalidate_captacion_list_cache()
        _invalidate_captacion_goal_cache()
        await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, _delete_current_captacion_goal_snapshots
        )
        return {"status": "ok", **result}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _require_captacion_workforce_admin(request: Request):
    user_doc = await get_current_user_doc(request)
    if not user_doc:
        raise HTTPException(status_code=401, detail="Sesión inválida")
    if str(user_doc.get("rol") or "").lower() not in CAPTACION_PRIVILEGED_ROLES:
        raise HTTPException(status_code=403, detail="Permiso administrativo requerido")
    return user_doc


async def _require_captacion_report_admin(request: Request):
    user_doc = await get_current_user_doc(request)
    if not user_doc:
        raise HTTPException(status_code=401, detail="Sesión inválida")
    if str(user_doc.get("rol") or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Permiso de administrador requerido")
    return user_doc


@app.get("/captacion/reporte-semanal", response_class=HTMLResponse)
async def view_captacion_weekly_report(request: Request, report_id: str = Query(None)):
    await _require_captacion_report_admin(request)
    from chatbot.storage import get_db

    def load_report():
        query = {"report_id": report_id} if report_id else {"is_test": False}
        return get_db()[CAPTACION_WEEKLY_REPORT_COLLECTION].find_one(
            query, {"_id": 0, "deepseek_payload": 0}, sort=[("created_at", -1)]
        )

    report = await asyncio.get_running_loop().run_in_executor(_WEB_THREAD_POOL, load_report)
    return templates.TemplateResponse(request, "captacion_weekly_report_preview.html", {
        "request": request,
        "report": report,
        "group_recipient": (report or {}).get("group_recipient") or Config.DAILY_REPORT_GROUP_ID,
    })


@app.post("/api/captacion/weekly-report/test/generate")
async def api_generate_captacion_weekly_test(request: Request):
    user_doc = await _require_captacion_report_admin(request)
    payload = await request.json()
    try:
        report = await create_weekly_report(
            payload.get("period_start"), payload.get("period_end"), is_test=True,
            created_by=user_doc.get("_id") or user_doc.get("username"),
        )
        return {"status": "ok", "report_id": report["report_id"], "snapshot_id": report["snapshot_id"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/test-send")
async def api_send_captacion_weekly_test(request: Request, report_id: str):
    await _require_captacion_report_admin(request)
    payload = await request.json()
    try:
        delivery = await send_test_report(report_id, payload.get("recipient"))
        return {"status": "ok", "delivery": delivery}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/approve-send")
async def api_approve_captacion_weekly_report(request: Request, report_id: str):
    user_doc = await _require_captacion_report_admin(request)
    payload = await request.json()
    try:
        delivery = await approve_and_send_report(
            report_id, user_doc, edited_narrative=payload.get("narrative")
        )
        return {"status": "ok", "delivery": delivery}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/acknowledge-outcomes")
async def api_acknowledge_captacion_weekly_outcomes(request: Request, report_id: str):
    user_doc = await _require_captacion_report_admin(request)
    try:
        report = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: acknowledge_outcome_review(report_id, user_doc)
        )
        return {"status": "ok", "report_id": report["report_id"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/regenerate")
async def api_regenerate_captacion_weekly_report(request: Request, report_id: str):
    user_doc = await _require_captacion_report_admin(request)
    try:
        report = await regenerate_report_narrative(report_id, user_doc)
        return {"status": "ok", "report_id": report["report_id"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/weekly-report/{report_id}/cancel")
async def api_cancel_captacion_weekly_report(request: Request, report_id: str):
    user_doc = await _require_captacion_report_admin(request)
    try:
        report = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL, lambda: cancel_report(report_id, user_doc)
        )
        return {"status": "ok", "report_id": report["report_id"], "report_status": report["status"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/workforce/membership")
async def api_captacion_workforce_membership(request: Request):
    user_doc = await _require_captacion_workforce_admin(request)
    payload = await request.json()
    from chatbot.storage import get_db
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: upsert_membership(get_db(), payload, user_doc.get("_id")),
        )
        return {"status": "ok", "membership": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/workforce/exception")
async def api_captacion_workforce_exception(request: Request):
    user_doc = await _require_captacion_workforce_admin(request)
    payload = await request.json()
    from chatbot.storage import get_db
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: create_work_exception(get_db(), payload, user_doc.get("_id")),
        )
        return {"status": "ok", "exception": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/workforce/calendar")
async def api_captacion_workforce_calendar(request: Request):
    user_doc = await _require_captacion_workforce_admin(request)
    payload = await request.json()
    from chatbot.storage import get_db
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: upsert_calendar_day(get_db(), payload, user_doc.get("_id")),
        )
        return {"status": "ok", "calendar_day": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/captacion/management/{event_id}/reverse")
async def api_reverse_captacion_management(request: Request, event_id: str):
    user_doc = await _require_captacion_workforce_admin(request)
    payload = await request.json()
    from captacion_management import reverse_management_event
    from chatbot.storage import get_db
    try:
        reversal = await asyncio.get_running_loop().run_in_executor(
            _WEB_THREAD_POOL,
            lambda: reverse_management_event(
                get_db(), event_id=event_id, actor_user=user_doc, reason=payload.get("reason"),
                replacement_status=payload.get("replacement_status")
            ),
        )
        return {"status": "ok", "reversal_event_id": reversal["event_id"]}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/captacion/management/anomalies")
async def api_captacion_management_anomalies(request: Request, status_filter: str = "pending_review"):
    await _require_captacion_workforce_admin(request)
    from captacion_management import ANOMALY_COLLECTION
    from chatbot.storage import get_db
    rows = await asyncio.get_running_loop().run_in_executor(
        _WEB_THREAD_POOL,
        lambda: list(get_db()[ANOMALY_COLLECTION].find(
            {"status": status_filter}, {"_id": 0}
        ).sort("created_at", -1).limit(100)),
    )
    return {"status": "ok", "items": rows}

@app.get("/api/captacion/templates/personal")
async def api_get_personal_templates(request: Request):
    username_str = await get_current_user(request)
    user_doc = await get_current_user_doc(request)
    actual_name = user_doc.get("nombre", username_str) if user_doc else username_str
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WEB_THREAD_POOL, lambda: get_personal_templates(actual_name))

@app.post("/api/captacion/templates/personal")
async def api_save_personal_template(request: Request):
    username_str = await get_current_user(request)
    user_doc = await get_current_user_doc(request)
    actual_name = user_doc.get("nombre", username_str) if user_doc else username_str
    
    data = await request.json()
    loop = asyncio.get_running_loop()
    tid = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: save_personal_template(actual_name, data))
    return {"status": "ok", "id": tid}

@app.delete("/api/captacion/templates/personal")
async def api_delete_personal_template(request: Request):
    username_str = await get_current_user(request)
    user_doc = await get_current_user_doc(request)
    actual_name = user_doc.get("nombre", username_str) if user_doc else username_str
    
    data = await request.json()
    tid = data.get("id")
    if not tid:
        raise HTTPException(status_code=400, detail="Falta ID")
        
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(_WEB_THREAD_POOL, lambda: delete_personal_template(tid, actual_name))
    return {"status": "ok"} if success else {"status": "error"}

@app.post("/api/captacion/distribute")
async def api_distribute_captacion(request: Request):
    user = await get_current_user_doc(request)
    
    if user.get("rol") not in ["admin", "supervisor"]:
        raise HTTPException(status_code=403, detail="No autorizado")
        
    loop = asyncio.get_running_loop()
    reassigned = await loop.run_in_executor(
        _WORKER_THREAD_POOL, lambda: redistribute_inactive_agent_captaciones(dry_run=False)
    )
    count = await loop.run_in_executor(_WORKER_THREAD_POOL, distribute_sourced_leads)
    app.state.captacion_stats_cache = {}
    return {"status": "ok", "assigned": count, "reassigned_inactive": reassigned}

SLA_RELEASE_WEEKDAY = 6  # domingo
SLA_RELEASE_HOUR = 4  # 04:00 hora Chile


async def captacion_sla_release_loop():
    """Libera captaciones sin gestión por SLA (>=5 días) y redistribuye las de
    ejecutivos inactivos. Corre una vez por semana (domingo 04:00 hora Chile)
    como proceso nocturno, evitando la carrera horaria con la distribución."""
    logger.info("[BACKGROUND] Iniciando loop semanal de release SLA de captaciones...")
    last_run_key = "captacion_sla_release_last_run"
    while True:
        try:
            background_tasks_status.setdefault("captacion_sla_release", {"status": "starting", "last_heartbeat": None})
            background_tasks_status["captacion_sla_release"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["captacion_sla_release"]["status"] = "running"

            now_local = datetime.now(CHILE_TZ)
            due = (now_local.weekday() == SLA_RELEASE_WEEKDAY
                   and now_local.hour == SLA_RELEASE_HOUR
                   and 0 <= now_local.minute < 10)

            if due:
                loop = asyncio.get_running_loop()
                run_date = now_local.strftime("%Y-%m-%d")
                from chatbot.storage import get_db
                db = get_db()

                try:
                    already_run = db["background_runs"].find_one({"_id": f"{last_run_key}_{run_date}"})
                except Exception:
                    already_run = None
                if already_run:
                    logger.info(f"[SLA_WEEKLY] Release de {run_date} ya ejecutado. Saltando.")
                else:
                    inactive_result = await loop.run_in_executor(
                        _WORKER_THREAD_POOL, lambda: redistribute_inactive_agent_captaciones(dry_run=False)
                    )
                    released = await loop.run_in_executor(_WORKER_THREAD_POOL, release_stale_captaciones)
                    logger.info(
                        f"[SLA_WEEKLY] Reasignadas por ejecutivo inactivo: {inactive_result.get('modified', 0)} | "
                        f"liberadas por SLA: {released}"
                    )
                    try:
                        db["background_runs"].update_one(
                            {"_id": f"{last_run_key}_{run_date}"},
                            {"$set": {"run_at": datetime.now(timezone.utc).isoformat(),
                                      "inactive_reassigned": inactive_result.get("modified", 0),
                                      "released": released}},
                            upsert=True,
                        )
                    except Exception:
                        pass

            # Revisar cada hora si llegó el momento (domingo 04:00)
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            background_tasks_status["captacion_sla_release"]["status"] = "error"
            logger.error(f"[BACKGROUND] Error en loop semanal de release SLA: {e}")
            await asyncio.sleep(3600)

# ========================= 6. RUTAS CRM (MODIFICADAS PARA HORA LOCAL) =========================

async def _render_crm_list(
    request: Request, 
    estado: str = None, 
    busqueda: str = None, 
    orden: str = "recent_assigned", 
    ejecutivo: str = None,
    temperatura: str = "Todos",
    page: int = 1,
    partial: bool = False,
    property_code: str = None,
):
    username = await get_current_user(request)
    from chatbot.storage import get_async_db
    from chatbot.crm_updates import get_crm_leads_version_async
    from chatbot.crm_filters import build_crm_card_urls, build_crm_filter_urls

    # Una sola selección normalizada gobierna consulta, KPI, tarjetas y enlaces.
    temperatura = normalize_crm_temperature(temperatura)

    adb = get_async_db()
    user = await adb["usuarios"].find_one({"username": username})
    
    if not user:
        return RedirectResponse(url="/?error=sesion_invalida")

    user_role = user.get("rol", "agente")
    user_name = user.get("nombre", "")
    can_administer = can_administer_leads(user_role)
    # Se lee antes del listado. Si ocurre un cambio durante el render, el
    # siguiente polling verá una versión mayor y repetirá la actualización.
    crm_version = await get_crm_leads_version_async(adb)

    limit = 15
    leads_task = get_crm_leads_list(
        filtro_estado=estado,
        busqueda=busqueda,
        ordenar_por=orden,
        user_role=user_role,
        user_name=user_name,
        ejecutivo_filter=ejecutivo,
        temperatura_filter=temperatura,
        page=page,
        limit=limit,
        property_code=property_code,
    )
    exec_task = get_unique_executives() if can_administer else asyncio.sleep(0, result=[])
    leads_payload, executives = await asyncio.gather(leads_task, exec_task)
    leads, kpis, total_count = leads_payload

    total_pages = max(1, (total_count + limit - 1) // limit)
    page = min(max(page, 1), total_pages)
    pagination_query = {
        "temperatura": temperatura if temperatura != "Todos" else None,
        "estado": estado,
        "busqueda": busqueda,
        "property_code": property_code,
        "orden": orden,
        "ejecutivo": ejecutivo,
    }
    pagination_query = {
        key: value for key, value in pagination_query.items()
        if value not in (None, "")
    }
    pagination_base_url = "/crm?" + urlencode(pagination_query) + ("&" if pagination_query else "")

    card_query_params = dict(request.query_params)
    if temperatura == "Todos":
        card_query_params.pop("temperatura", None)
    else:
        card_query_params["temperatura"] = temperatura

    response = templates.TemplateResponse(request, "crm_leads_list.html", {
        "request": request, 
        "leads": leads, 
        "kpis": kpis,
        "user_role": user_role,
        "user_name": user_name,
        "can_administer_leads": can_administer,
        "executives": executives,
        "current_ejecutivo": (ejecutivo or "Todos") if can_administer else user_name,
        "current_temperatura": temperatura,
        "crm_version": crm_version,
        "partial": partial,
        "card_urls": build_crm_card_urls(card_query_params),
        "filter_urls": build_crm_filter_urls(card_query_params),
        "pagination_base_url": pagination_base_url,
        "pagination": {
            "total_count": total_count,
            "current_page": page,
            "total_pages": total_pages,
            "has_more": page < total_pages,
            "has_prev": page > 1,
            "limit": limit
        }
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/crm", response_class=HTMLResponse)
async def view_crm_list(
    request: Request,
    estado: str = None,
    busqueda: str = None,
    orden: str = "recent_assigned",
    ejecutivo: str = None,
    temperatura: str = "Todos",
    page: int = Query(1, ge=1),
    scope: str = Query(None),
    property_code: str = Query(None),
):
    """CRM list. scope=mine forces filter to the authenticated executive."""
    user = await get_current_user_doc(request)
    exec_filter = ejecutivo
    if scope == "mine" and user:
        exec_filter = user.get("nombre", "")
    return await _render_crm_list(
        request,
        estado=estado,
        busqueda=busqueda,
        orden=orden,
        ejecutivo=exec_filter,
        temperatura=temperatura,
        page=page,
        property_code=property_code,
    )


@app.get("/crm/check-updates")
async def check_crm_updates(request: Request, since: int = Query(0, ge=0)):
    """Consulta un solo documento de versión; no examina la colección leads."""
    await get_current_user(request)
    from chatbot.storage import get_async_db
    from chatbot.crm_updates import get_crm_leads_version_async

    version = await get_crm_leads_version_async(get_async_db())
    return JSONResponse(
        {"changed": version != since, "version": version},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/crm/partial", response_class=HTMLResponse)
async def view_crm_list_partial(
    request: Request,
    estado: str = None,
    busqueda: str = None,
    orden: str = "recent_assigned",
    ejecutivo: str = None,
    temperatura: str = "Todos",
    page: int = Query(1, ge=1),
    property_code: str = Query(None),
):
    return await _render_crm_list(
        request,
        estado=estado,
        busqueda=busqueda,
        orden=orden,
        ejecutivo=ejecutivo,
        temperatura=temperatura,
        page=page,
        partial=True,
        property_code=property_code,
    )

@app.post("/api/marcar_gestionado")
async def marcar_gestionado(request: Request):
    data = await request.json()
    email = data.get("email")
    gestionado = data.get("gestionado", False)
    if not email:
        return {"error": "Falta email"}
    from chatbot.storage import get_db as _gdb
    _db = _gdb()
    col = _db[Config.COLLECTION_CONTACTOS]
    result = col.update_one(
        {"email_propietario": email.lower()},
        {"$set": {"gestionado": gestionado}}
    )
    if result.matched_count == 0:
        col.update_one(
            {"email_propietario": {"$regex": f"^{re.escape(email.lower())}$", "$options": "i"}},
            {"$set": {"gestionado": gestionado}}
        )
    return {"status": "ok", "gestionado": gestionado}

@app.get("/retiro/confirmar")
async def retiro_confirmar(request: Request, email: str = Query(...), codigo: str = Query(...)):
    ip = request.client.host if request.client else "0.0.0.0"
    return await handle_retiro_confirmacion(email, codigo, ip)

@app.get("/retiro/contactar")
async def retiro_contactar(request: Request, email: str = Query(...), codigo: str = Query(...)):
    ip = request.client.host if request.client else "0.0.0.0"
    return await handle_solicitud_contacto(email, codigo, ip)

@app.exception_handler(401)
async def unauthorized_exception_handler(request: Request, exc: HTTPException):
    # Si el usuario intenta acceder a una ruta de la interfaz (HTML), lo mandamos al login
    logger.warning(f"Redirigiendo a login por sesión expirada en: {request.url.path}")
    if request.url.path.startswith("/api/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "error", "message": "Sesión expirada o no autenticado"}, status_code=401)
    requested_url = request.url.path
    if request.url.query:
        requested_url += f"?{request.url.query}"
    response = RedirectResponse(url="/?" + urlencode({
        "error": "sesion_expirada",
        "next": requested_url,
    }))
    response.set_cookie("login_next", requested_url, httponly=True, secure=True, samesite="lax", max_age=600)
    return response

# ========================= 9. BACKGROUND LOOPS (REFACTORED) =========================

async def process_pending_leads_loop():
    logger.info("[BACKGROUND] Iniciando loop de leads pendientes...")
    while True:
        try:
            background_tasks_status["notifications_loop"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["notifications_loop"]["status"] = "running"
            
            if should_send_now():
                # Canonical Hot worker. Fully sync — runs in threadpool.
                if Config.LEAD_HOT_NOTIFICATIONS_ENABLED:
                    from chatbot.crm_hot_delivery import process_one_hot_sync
                    from chatbot.storage import get_db as _get_sync_db
                    loop = asyncio.get_running_loop()
                    import functools
                    sync_db = _get_sync_db()
                    fn = functools.partial(process_one_hot_sync, sync_db,
                                           worker_id=f"render:{os.getpid()}")
                    result = await loop.run_in_executor(_WORKER_THREAD_POOL, fn)
                    if result and result.get("status") not in ("idle", "disabled"):
                        logger.info("[HOT] Resultado: %s", result)
                pending = await run_db("pending_notifications.find", get_pending_notifications)
                if pending:
                    logger.info(f"[BACKGROUND] Analizando {len(pending)} envíos pendientes...")
                    
                    # 1. Agrupar por ejecutivo (target_phone)
                    by_executive = {}
                    for p in pending:
                        lead_data = p.get("lead_data", {})
                        
                        # Fix Bug B: Ignore captacion notifications
                        lead_type = lead_data.get("lead_type") or p.get("lead_type", "")
                        notification_type = lead_data.get("notification_type") or p.get("notification_type", "")
                        if notification_type == "captacion" or lead_type == "AsignacionCaptacion":
                            logger.debug(f"[BACKGROUND] Skipping captacion notification para {p.get('target_phone')}")
                            await run_db("pending_notifications.mark_sent", mark_notification_sent, p["_id"])
                            continue

                        target_phone = lead_data.get("target_phone") or p.get("target_phone")
                        
                        # Fix: Si no hay teléfono o es el dummy, intentamos re-enrutar antes de descartar
                        if not target_phone or target_phone == "+56900000000":
                            from chatbot.lead_router import find_responsible_executive
                            lead_phone = lead_data.get("phone")
                            p_code = lead_data.get("property_code") or lead_data.get("prospecto", {}).get("codigo")
                            if p_code:
                                logger.info(f"[BACKGROUND] Re-enrutando lead {lead_phone} por falta de destino válido...")
                                new_exec, new_phone, assignment_type = await run_in_threadpool(
                                    find_responsible_executive,
                                    property_code=p_code,
                                    lead_phone=lead_phone,
                                    lead_name=lead_data.get("nombre")
                                )
                                if new_phone and new_phone != "+56900000000":
                                    target_phone = new_phone
                                    p["target_name"] = new_exec
                                    # Actualizamos la data para que el mensaje se mande bien
                                    lead_data["target_phone"] = new_phone
                                    lead_data["target_name"] = new_exec
                                    logger.info(f"[BACKGROUND] Re-enrutado exitosamente a {new_exec} ({new_phone})")
                        
                        if not target_phone or target_phone == "+56900000000":
                            # Si después de re-enrutar sigue mal, lo marcamos para no ciclar eternamente
                            logger.warning(f"[BACKGROUND] Skipped: No se pudo encontrar destino válido para lead {lead_data.get('phone')}")
                            await run_db("pending_notifications.mark_sent", mark_notification_sent, p["_id"])
                            continue
                            
                        if target_phone not in by_executive:
                            by_executive[target_phone] = {"name": p.get("target_name") or lead_data.get("target_name"), "items": []}
                        
                        by_executive[target_phone]["items"].append(p)

                    # 2. Procesar cada ejecutivo
                    from chatbot.lead_router import format_whatsapp_template, format_summary_whatsapp_template
                    from chatbot.notification_service import NotificationService
                    from chatbot.crm_service import CrmService
                    from chatbot.notification_identity import deduplicate_lead_notifications

                    for target_phone, data in by_executive.items():
                        raw_items = data["items"]
                        items = deduplicate_lead_notifications(raw_items)
                        target_name = data["name"]

                        if len(items) != len(raw_items):
                            logger.warning(
                                "[BACKGROUND] Deduplicadas %s notificaciones repetidas para %s",
                                len(raw_items) - len(items),
                                target_name,
                            )
                        
                        # Si tiene más de uno, enviamos resumen agrupado
                        if len(items) > 1:
                            logger.info(f"[BACKGROUND] Enviando resumen de {len(items)} leads a {target_name}")
                            msg = format_summary_whatsapp_template(items, target_name)
                            
                            # Marcamos todos como asignados en CRM (solo si no tienen ejecutivo aún)
                            for item in items:
                                lead_phone = item.get("lead_data", {}).get("phone")
                                if lead_phone:
                                    try:
                                        lead_db = await run_db(
                                            "leads.find_one",
                                            (lambda: CrmService._db()["leads"].find_one({"phone": lead_phone}))
                                        ) if hasattr(CrmService, '_db') else None
                                        existing_exec = (lead_db or {}).get("ejecutivo_asignado") if lead_db else None
                                        from chatbot.constants import UNASSIGNED_LABEL
                                        unassigned = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
                                        if not existing_exec or existing_exec in unassigned:
                                            await run_db(
                                                "crm.assign_executive",
                                                CrmService.assign_executive,
                                                lead_phone,
                                                target_name,
                                                "LeadRouter"
                                            )
                                    except: pass
                            
                            success = await NotificationService.send_notification(
                                phone=target_phone,
                                message=msg,
                                alert_type="background_notification_group",
                                meta={"to": target_name, "count": len(items)},
                                dedup_window_minutes=5
                            )
                            
                            if success:
                                for item in items:
                                    for notification_id in item.get("_notification_ids") or [item["_id"]]:
                                        await run_db("pending_notifications.mark_sent", mark_notification_sent, notification_id)

                        # Si es solo uno, enviamos el template normal
                        else:
                            p = items[0]
                            lead_data = p.get("lead_data", {})
                            lead_phone = lead_data.get("phone")
                            prop_code = lead_data.get("property_code")
                            
                            logger.info(f"[BACKGROUND] Enviando lead individual {lead_phone} a {target_name}")
                            msg = await run_db(
                                "lead_router.format_whatsapp_template",
                                format_whatsapp_template,
                                lead_data,
                                target_name,
                                prop_code,
                                True
                            )
                            
                            if lead_phone:
                                try:
                                    from chatbot.storage import get_db as _get_db
                                    _lead_db = await run_db(
                                        "leads.find_one",
                                        (lambda: _get_db()["leads"].find_one({"phone": lead_phone}))
                                    )
                                    existing_exec = (_lead_db or {}).get("ejecutivo_asignado")
                                    from chatbot.constants import UNASSIGNED_LABEL
                                    unassigned = [UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""]
                                    if not existing_exec or existing_exec in unassigned:
                                        await run_db(
                                            "crm.assign_executive",
                                            CrmService.assign_executive,
                                            lead_phone,
                                            target_name,
                                            "LeadRouter"
                                        )
                                except: pass
                                
                            success = await NotificationService.send_notification(
                                phone=target_phone,
                                message=msg,
                                alert_type="background_notification",
                                meta={"to": target_name, "lead_phone": lead_phone},
                                dedup_window_minutes=5
                            )
                            if success:
                                for notification_id in p.get("_notification_ids") or [p["_id"]]:
                                    await run_db("pending_notifications.mark_sent", mark_notification_sent, notification_id)

                        # Throttling Anti-Spam: 30 segundos entre ejecutivos (Aumentado por precaución de Meta)
                        logger.info(f"[BACKGROUND] Pausa anti-spam (30s) para siguiente destinatario...")
                        await asyncio.sleep(30)
                        
        except Exception as e:
            logger.error(f"[BACKGROUND] Error en loop de pendientes: {e}")
            background_tasks_status["notifications_loop"]["status"] = f"error: {str(e)}"
        
        await asyncio.sleep(60)

async def check_scheduled_tasks_loop():
    from chatbot.storage import get_db
    from chatbot.lead_router import get_executive_phone
    from chatbot.notification_service import NotificationService
    task_worker_id = f"crm_task_monitor_{os.getpid()}"
    
    logger.info("[TASK_MONITOR] Iniciando monitor de tareas agendadas...")
    
    while True:
        try:
            background_tasks_status["task_monitor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["task_monitor"]["status"] = "running"
            
            db = get_db()
            now = datetime.now(CHILE_TZ)
            
            tasks = await run_db(
                "crm_tasks.find_due",
                lambda: list(db["crm_tasks"].find({"$or": [
                    {"status": "pending", "execute_at": {"$lte": now}, "$or": [
                        {"next_attempt_at": {"$exists": False}},
                        {"next_attempt_at": {"$lte": now}},
                    ]},
                    {"status": "processing", "lease_until": {"$lte": now}},
                ],
                    "lead_type": {"$ne": "captacion"}}))
            )
            
            if tasks:
                logger.info(f"[TASK_MONITOR] Procesando {len(tasks)} tareas vencidas...")
                for task in tasks:
                    try:
                        claimed_task = await run_db(
                            "crm_tasks.claim_due",
                            lambda: db["crm_tasks"].find_one_and_update(
                                {"_id": task["_id"], "$or": [
                                    {"status": "pending", "$or": [
                                        {"next_attempt_at": {"$exists": False}},
                                        {"next_attempt_at": {"$lte": now}},
                                    ]},
                                    {"status": "processing", "lease_until": {"$lte": now}},
                                ]},
                                {"$set": {
                                    "status": "processing",
                                    "claimed_at": now,
                                    "lease_until": now + timedelta(minutes=10),
                                    "worker_id": task_worker_id,
                                }},
                                return_document=ReturnDocument.AFTER,
                            )
                        )
                        if not claimed_task:
                            continue
                        task = claimed_task
                        phone = task.get("phone")
                        note = task.get("note", "Sin detalles")
                        
                        is_captacion = task.get("lead_type") == "captacion"
                        if is_captacion:
                            from bson import ObjectId
                            obj_id = str(task.get("obj_id"))
                            lead = await run_db(
                                "propiedades_captacion.find_one",
                                lambda: Config.get_captacion_collection(db).find_one({"_id": ObjectId(obj_id)})
                            )
                            if not lead:
                                await run_db(
                                    "crm_tasks.update_error_captacion_not_found",
                                    lambda: db["crm_tasks"].update_one(
                                        {"_id": task["_id"]},
                                        {"$set": {"status": "error", "error": "captacion_not_found"}}
                                    )
                            )
                                continue
                            ejecutivo = lead.get("gestion", {}).get("ejecutivo_asignado")
                            lead_name = lead.get("details", {}).get("publicador", "Cliente")
                            crm_link = f"{Config.CRM_BASE_URL}/captacion/{obj_id}"
                        else:
                            lead = await run_db(
                                "leads.find_one",
                                lambda: db["leads"].find_one({"_id": task.get("lead_id")})
                                or db["leads"].find_one({"phone": phone})
                            )
                            if not lead:
                                await run_db(
                                    "crm_tasks.update_error_lead_not_found",
                                    lambda: db["crm_tasks"].update_one(
                                        {"_id": task["_id"]},
                                        {"$set": {"status": "error", "error": "lead_not_found"}}
                                    )
                                )
                                continue
                            assigned_cycle = None
                            if task.get("assignment_cycle_id"):
                                assigned_cycle = await run_db(
                                    "crm_assignment_cycles.find_for_task",
                                    lambda: db["crm_assignment_cycles"].find_one({
                                        "assignment_cycle_id": task.get("assignment_cycle_id"),
                                        "lead_id": lead.get("_id"),
                                    }),
                                )
                            recipient_user_id = (
                                task.get("recipient_user_id")
                                or task.get("target_user_id")
                                or (assigned_cycle or {}).get("assigned_to_user_id")
                            )
                            ejecutivo = (
                                task.get("recipient_name")
                                or (assigned_cycle or {}).get("assigned_to_display_name")
                                or lead.get("ejecutivo_asignado")
                            )
                            lead_name = lead.get("prospecto", {}).get("nombre", "Cliente")
                            from chatbot.lead_router import build_secure_crm_url
                            crm_link = build_secure_crm_url(lead)
                            
                        if not ejecutivo or ejecutivo in ["No asignado", "Sin Asignar"]:
                            await run_db(
                                "crm_tasks.release_no_executive",
                                lambda: db["crm_tasks"].update_one(
                                    {"_id": task["_id"], "status": "processing"},
                                    {"$set": {
                                        "status": "pending",
                                        "next_attempt_at": now + timedelta(minutes=1),
                                        "last_error": "executive_not_available",
                                    }}
                                )
                            )
                            continue
                            
                        exec_phone = None
                        if not is_captacion and recipient_user_id:
                            from bson import ObjectId
                            recipient_queries = [{"_id": recipient_user_id}]
                            try:
                                recipient_queries.append({"_id": ObjectId(str(recipient_user_id))})
                            except Exception:
                                pass
                            recipient = await run_db(
                                "usuarios.find_recipient_for_task",
                                lambda: db["usuarios"].find_one({
                                    "$or": recipient_queries,
                                    "is_active": {"$ne": False},
                                }),
                            )
                            if recipient:
                                ejecutivo = recipient.get("nombre") or ejecutivo
                                exec_phone = recipient.get("telefono") or recipient.get("tel") or recipient.get("movil")
                        if not exec_phone:
                            exec_phone = await run_in_threadpool(get_executive_phone, ejecutivo)
                        if not exec_phone or exec_phone == "+56900000000":
                            await run_db(
                                "crm_tasks.release_no_executive_phone",
                                lambda: db["crm_tasks"].update_one(
                                    {"_id": task["_id"], "status": "processing"},
                                    {"$set": {
                                        "status": "pending",
                                        "next_attempt_at": now + timedelta(minutes=1),
                                        "last_error": "executive_phone_not_available",
                                    }}
                                )
                            )
                            continue

                        if is_captacion:
                            # Si hay tareas duplicadas históricas para la misma captación,
                            # dejamos solo una activa para evitar triple envío.
                            await run_db(
                                "crm_tasks.compact_captacion_duplicates",
                                lambda: db["crm_tasks"].update_many(
                                    {
                                        "lead_type": "captacion",
                                        "obj_id": str(task.get("obj_id")),
                                        "status": "pending",
                                        "_id": {"$ne": task["_id"]}
                                    },
                                    {"$set": {"status": "completed", "resolved_at": now.isoformat(), "resolution": "superseded_duplicate"}}
                                )
                            )
                            
                        scheduled_at = task.get("execute_at")
                        if isinstance(scheduled_at, datetime):
                            if scheduled_at.tzinfo is None:
                                scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
                            scheduled_display = scheduled_at.astimezone(CHILE_TZ).strftime("%d/%m/%Y a las %H:%M")
                        else:
                            scheduled_display = "la fecha y hora programadas"
                        link_label = "Abrir captación en CRM" if is_captacion else "Abrir lead en CRM"
                        msg_text = (
                            f"*Recordatorio de seguimiento CRM*\n\n"
                            f"Hola {ejecutivo}, tienes un seguimiento programado para *{lead_name}*.\n\n"
                            f"*Fecha y hora:* {scheduled_display}\n"
                            f"*Nota:* {note}\n\n"
                            f"*{link_label}:*\n{crm_link}"
                        )
                        
                        sent = await NotificationService.send_notification(
                            phone=exec_phone,
                            message=msg_text,
                            alert_type="TASK_REMINDER",
                            meta={"task_id": str(task["_id"]), "to": ejecutivo,
                                  "lead_id": str(lead.get("_id") or "")},
                            dedup_window_minutes=60 
                        )
                        
                        if sent:
                            await run_db(
                                "crm_tasks.update_notified",
                                lambda: db["crm_tasks"].update_one(
                                    {"_id": task["_id"]},
                                    {"$set": {"status": "notified", "notified_at": now.isoformat(), "notification_sent_to": ejecutivo}}
                                )
                            )
                            if is_captacion:
                                # Clear the visible reminder only when no newer
                                # pending reminder exists for this captación.
                                has_newer_task = await run_db(
                                    "crm_tasks.has_newer_captacion_reminder",
                                    lambda: db["crm_tasks"].count_documents({
                                        "lead_type": "captacion",
                                        "obj_id": str(task.get("obj_id")),
                                        "status": "pending",
                                        "_id": {"$ne": task["_id"]},
                                    }) > 0,
                                )
                                if not has_newer_task:
                                    await run_db(
                                        "propiedades_captacion.clear_sent_followup",
                                        lambda: Config.get_captacion_collection(db).update_one(
                                            {"_id": lead["_id"]},
                                            {"$set": {
                                                "gestion.next_followup": None,
                                                "gestion.next_followup_sent_at": now,
                                            }},
                                        ),
                                    )
                            # Sleep breve para tareas
                            await asyncio.sleep(6)
                        else:
                            await run_db(
                                "crm_tasks.release_send_failure",
                                lambda: db["crm_tasks"].update_one(
                                    {"_id": task["_id"], "status": "processing"},
                                    {"$set": {
                                        "status": "pending",
                                        "next_attempt_at": now + timedelta(minutes=1),
                                        "last_error": "whatsapp_send_failed",
                                    }}
                                )
                            )
                            
                    except Exception as e:
                        logger.error(f"[TASK_MONITOR] Error procesando tarea {task.get('_id')}: {e}")
                        await run_db(
                            "crm_tasks.release_exception",
                            lambda: db["crm_tasks"].update_one(
                                {"_id": task.get("_id"), "status": "processing"},
                                {"$set": {
                                    "status": "pending",
                                    "next_attempt_at": now + timedelta(minutes=1),
                                    "last_error": type(e).__name__,
                                }}
                            )
                        )
            
        except Exception as e:
            logger.error(f"[TASK_MONITOR] Error en loop de tareas: {e}")
            background_tasks_status["task_monitor"]["status"] = f"error: {str(e)}"
            
        await asyncio.sleep(60)

async def captacion_reminder_loop():
    """Dedicated durable worker for captaci?n reminders only."""
    from chatbot.storage import get_db
    from chatbot.captacion_reminder import process_one_due_reminder
    worker_id = f"captacion_reminder_{os.getpid()}"
    while True:
        try:
            background_tasks_status["captacion_reminder"] = {
                "status": "running", "last_heartbeat": datetime.now(CHILE_TZ).isoformat()}
            # The durable reminder implementation uses the synchronous Mongo
            # client; run its complete claim/delivery transaction off-loop
            # with the delegated context so forensics does not flag it.
            from chatbot.storage import _delegated_sync_mongo
            loop = asyncio.get_running_loop()

            def _run_reminder_off_loop():
                token = _delegated_sync_mongo.set(True)
                try:
                    return asyncio.run(process_one_due_reminder(get_db(), worker_id=worker_id))
                finally:
                    _delegated_sync_mongo.reset(token)

            result = await loop.run_in_executor(_WORKER_THREAD_POOL, _run_reminder_off_loop)
            if result.get("status") not in {"idle", "notified"}:
                logger.warning("[CAPTACION_REMINDER] result=%s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[CAPTACION_REMINDER] worker error: %s", exc)
            background_tasks_status["captacion_reminder"] = {
                "status": f"error: {type(exc).__name__}",
                "last_heartbeat": datetime.now(CHILE_TZ).isoformat()}
        await asyncio.sleep(30)

async def sla_monitor_loop():
    logger.info("[SLA_MONITOR] Iniciando monitor de SLA...")
    while True:
        try:
            background_tasks_status["sla_monitor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["sla_monitor"]["status"] = "running"
            
            from chatbot.sla_service import monitor_sla_thresholds
            await monitor_sla_thresholds()
        except Exception as e:
            logger.error(f"[BACKGROUND] Error en loop de SLA: {e}")
            background_tasks_status["sla_monitor"]["status"] = f"error: {str(e)}"
            
        await asyncio.sleep(60)


async def non_hot_digest_worker_loop():
    """Periodic worker that claims and delivers due non-HOT digests in shadow mode."""
    from chatbot.crm_non_hot_digest import process_one_digest as _pod
    import inspect
    logger.info("[NON_HOT_DIGEST] Iniciando worker pid=%s sync=%s file=%s",
                os.getpid(), not inspect.iscoroutinefunction(_pod),
                inspect.getfile(_pod))
    worker_id = f"non_hot_digest_worker_{os.getpid()}"
    while True:
        try:
            background_tasks_status.setdefault("non_hot_digest", {})
            background_tasks_status["non_hot_digest"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["non_hot_digest"]["status"] = "running"

            if not Config.CRM_NON_HOT_DIGEST_ENABLED:
                await asyncio.sleep(60)
                continue

            from chatbot.crm_non_hot_digest import process_one_digest
            import inspect
            is_coro = inspect.iscoroutinefunction(process_one_digest)
            if is_coro:
                logger.error("[NON_HOT_DIGEST] CRITICAL: process_one_digest is still a coroutine! Fix not applied.")
            from chatbot.storage import get_db
            db = get_db()
            loop = asyncio.get_running_loop()

            # Safety net: every assigned non-HOT lead must be announced.  Runs
            # on a light cadence (every ~5 min via a counter) to re-enqueue any
            # lead that fell through the accumulate/send race.
            try:
                _recon_count = getattr(loop, "_nhd_recon_counter", 0) + 1
                loop._nhd_recon_counter = _recon_count
                if _recon_count >= 5:
                    loop._nhd_recon_counter = 0
                    from chatbot.crm_non_hot_digest import reconcile_missing_notifications
                    await loop.run_in_executor(
                        _WORKER_THREAD_POOL,
                        lambda: reconcile_missing_notifications(
                            db, lookback_minutes=120, dry_run=False,
                        ),
                    )
            except Exception as _recon_err:
                logger.warning("[NON_HOT_DIGEST_RECON] error: %s", _recon_err)

            try:
                result = await loop.run_in_executor(
                    _WORKER_THREAD_POOL,
                    lambda: process_one_digest(db, worker_id=worker_id),
                )
                if result["status"] not in ("idle", "shadow_sent"):
                    logger.info("[NON_HOT_DIGEST] Resultado: %s", result)
                # If we needed a live send in the future, the sender call would
                # happen here in the async context, NOT inside process_one_digest.
            except Exception as exc:
                logger.error("[NON_HOT_DIGEST] Error en digest: %s", exc)
        except Exception as e:
            logger.error(f"[NON_HOT_DIGEST] Error en loop: {e}")
            background_tasks_status["non_hot_digest"]["status"] = f"error: {str(e)}"

        await asyncio.sleep(60)


async def sla_alert_worker_loop():
    """Periodic worker that claims and delivers due SLA alerts exclusively in shadow mode."""
    logger.info("[SLA_ALERT] Iniciando worker de alertas SLA shadow...")
    worker_id = f"sla_alert_worker_{os.getpid()}"
    while True:
        try:
            background_tasks_status.setdefault("sla_alert", {})
            background_tasks_status["sla_alert"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["sla_alert"]["status"] = "running"

            # SLA delivery is intentionally disabled while the independent
            # crm_sla_alert domain is in dry-run.  Do not claim legacy shadow
            # documents or call a provider under either flag state.
            if True:
                background_tasks_status["sla_alert"].update({"status": "retired", "mode": "canonical_orchestrator"})
                await asyncio.sleep(60)
                continue

            loop = asyncio.get_running_loop()
            from chatbot.storage import get_db
            from chatbot.crm_sla_alerts import claim_due_sla_alert, send_sla_alert
            db = get_db()

            async def _try_sla():
                try:
                    notification = await loop.run_in_executor(
                        _WORKER_THREAD_POOL,
                        lambda: claim_due_sla_alert(db, worker_id=worker_id),
                    )
                    if notification:
                        result = await loop.run_in_executor(
                            _WORKER_THREAD_POOL,
                            lambda: send_sla_alert(db, notification=notification, worker_id=worker_id),
                        )
                        if result["status"] not in ("idle",):
                            logger.info("[SLA_ALERT] Resultado: %s", result)
                except Exception as exc:
                    logger.error("[SLA_ALERT] Error: %s", exc)

            await _try_sla()
        except Exception as e:
            logger.error(f"[SLA_ALERT] Error en loop: {e}")
            background_tasks_status["sla_alert"]["status"] = f"error: {str(e)}"

        await asyncio.sleep(30)


def asegurar_indices_db():
    try:
        from chatbot.storage import get_db
        db = get_db()
        db["crm_tasks"].create_index([("status", 1), ("execute_at", 1)])
        db["crm_events"].create_index([("phone", 1), ("type", 1), ("timestamp", -1)])
        db["crm_events"].create_index([("lead_id", 1)])
        db["crm_events"].create_index([("lead_id", 1), ("confirmed", 1), ("result", 1)])
        db["crm_events"].create_index([("lead_id", 1), ("result", 1)])
        db["crm_events"].create_index([("lead_id", 1), ("meta.result", 1)])
        
        # --- OPTIMIZACIÓN CAPTACIÓN ---
        # Índice Compuesto para Lista (Estado + Ejecutivo + Score)
        try:
            Config.get_captacion_collection(db).create_index([
                ("gestion.estado", 1), 
                ("gestion.ejecutivo_asignado", 1), 
                ("score_captacion", -1)
            ], name="idx_yapo_gestion_ejecutivo_score")
        except Exception as idx_e:
            if "IndexOptionsConflict" in str(idx_e):
                logger.warning("IndexOptionsConflict detectado. Eliminando índice antiguo...")
                try:
                    Config.get_captacion_collection(db).drop_index("idx_yapo_gestion_ejecutivo_score")
                    Config.get_captacion_collection(db).drop_index("gestion.estado_1_gestion.ejecutivo_asignado_1_score_captacion_-1")
                except:
                    pass
                Config.get_captacion_collection(db).create_index([
                    ("gestion.estado", 1), 
                    ("gestion.ejecutivo_asignado", 1), 
                    ("score_captacion", -1)
                ], name="idx_yapo_gestion_ejecutivo_score")
            else:
                logger.warning(f"Error creando índice propiedades_captacion: {idx_e}")
                
        # Índice para Búsqueda por Comuna Normalizada + Score
        try:
            Config.get_captacion_collection(db).create_index([
                ("details.comuna_norm", 1), 
                ("score_captacion", -1)
            ])
        except Exception as e:
            logger.warning(f"Error creando índice comuna_norm: {e}")
        # Índice compuesto cubriente para get_market_insights():
        # cubre $match(comuna, tipo, precio_uf>0, m2_total>0) → index-only scan
        try:
            db["universo_cartera_prop360"].create_index([
                ("comuna", 1), ("tipo", 1), ("precio_uf", 1), ("m2_total", 1)
            ], name="idx_uc_market_insights")
        except Exception:
            db["universo_cartera_prop360"].create_index([("comuna", 1), ("tipo", 1)])
        # Índices para respuestas de campañas por email
        db[Config.COLLECTION_CONTACTOS].create_index([("email_propietario_lc", 1)], name="idx_contactos_email_lc")
        db[Config.COLLECTION_CAMPANAS_LOG].create_index([("token", 1)], name="idx_campanas_token")
        
        logger.info("Índices de CRM y Captación asegurados.")
    except Exception as e:
        logger.warning(f"Error creando índices: {e}")

import functools

async def run_db(operation_name: str, fn, *args, **kwargs):
    """
    Ejecuta operaciones síncronas de PyMongo en el _WORKER_THREAD_POOL.
    Evita congelamientos del Event Loop de FastAPI por timeouts de red de Mongo.
    """
    loop = asyncio.get_running_loop()
    t0 = time.time()
    try:
        if kwargs or args:
            func = functools.partial(fn, *args, **kwargs)
        else:
            func = fn
            
        result = await loop.run_in_executor(_WORKER_THREAD_POOL, func)
        duration_ms = (time.time() - t0) * 1000
        
        # Log solo de operaciones lentas > 2000ms
        if duration_ms > 2000:
            logger.warning(f"[BG_MONGO] loop=background operation={operation_name} duration={duration_ms:.0f}ms")
            
        return result
    except Exception as e:
        logger.error(f"[BG_MONGO] ERROR en {operation_name}: {e}")
        raise e

async def inactive_lead_nudge_loop():
    logger.info("[NUDGE_LOOP] Iniciando monitor de reactivación (Nudge) de leads inactivos...")
    while True:
        if not Config.CRM_INACTIVE_NUDGE_ENABLED:
            background_tasks_status["nudge_loop"] = {"status": "disabled", "last_heartbeat": datetime.now(CHILE_TZ).isoformat()}
            await asyncio.sleep(300)
            continue
        try:
            background_tasks_status["nudge_loop"] = {"status": "running", "last_heartbeat": datetime.now(CHILE_TZ).isoformat()}
            
            from chatbot.storage import get_db
            from chatbot.whatsapp_client import send_whatsapp_message
            from chatbot.conversation_policy import nudge_eligibility
            db = get_db()
            
            now_utc = datetime.utcnow()
            limit_max = now_utc - timedelta(hours=12) # No revivir muertos
            limit_min = now_utc - timedelta(minutes=25) # Buffer antes de chequear el dinamismo real
            
            # INTENCIONES AVANZADAS — no molestar si el lead ya está en proceso de visita/gestión
            INTENTS_NO_NUDGE = {
                "ASK_VISIT", "VISIT_SCHEDULED", "VISIT_DONE",
                "GIVE_OFFER", "NEGOTIATION", "CLOSED_WON"
            }
            # Labels de ejecutivos "sin asignar" — si tiene ejecutivo real, no enviar nudge
            from chatbot.constants import UNASSIGNED_LABEL
            UNASSIGNED_LABELS = {
                UNASSIGNED_LABEL, "No Asignado", "No asignado",
                "Sin Asignar", "Sin asignar", "N/A", "", None
            }

            now_cl = datetime.now(CHILE_TZ)
            today_str = now_cl.strftime("%Y-%m-%d")

            # --- COOLDOWN: max 1 nudge/día, mínimo 6h entre nudges ---
            NUDGE_COOLDOWN_HOURS   = 6    # mínimo entre envios al mismo lead
            NUDGE_MAX_PER_DAY     = 1    # máximo nudges por lead por día
            NUDGE_MAX_TOTAL       = 3    # máximo nudges históricos por lead (abandona después)

            query = {
                "stage": {"$nin": ["ARCHIVED", "REJECTED", "CLOSED_LOST", "CLOSED_WON", "OFFER", "NEGOTIATION", "VISIT_DONE", "VISIT_SCHEDULED"]},
                # Solo leads que: no alcanzaron el máximo de nudges históricos
                "$or": [
                    {"nudge_count": {"$exists": False}},
                    {"nudge_count": {"$lt": NUDGE_MAX_TOTAL}}
                ],
                # Y no han tenido nudge HOY
                "nudge_last_date": {"$ne": today_str},
                "messages.0": {"$exists": True}
            }

            def _fetch_nudge_leads():
                return list(db["leads"].find(query, {
                    "phone": 1, "messages": 1, "stage": 1,
                    "ejecutivo_asignado": 1, "prospecto": 1,
                    "nudge_count": 1, "nudge_sent_at": 1, "nudge_last_date": 1,
                    "last_intent": 1, "lifecycle": 1, "conversation_status": 1,
                    "human_takeover_at": 1, "bot_pausado": 1,
                    "delivery_unknown_pending": 1, "pending_response": 1
                }))
            leads = await run_db("nudge_loop_find", _fetch_nudge_leads)
            
            for lead in leads:
                policy = nudge_eligibility(lead)
                if not policy["eligible"]:
                    _lead_id = lead["_id"]
                    await run_db(
                        "nudge_skip_policy",
                        lambda: db["leads"].update_one({"_id": _lead_id}, {"$set": {
                            "nudge_status": f"skipped_{policy['reason']}",
                            "nudge_skip_reason": policy["reason"],
                            "nudge_skip_evidence": policy["evidence"],
                        }}),
                    )
                    continue
                messages = lead.get("messages", [])
                if not messages:
                    continue

                # ─── FILTRO 1: ya tiene ejecutivo real asignado → el humano se encarga ───
                ejecutivo = (
                    lead.get("ejecutivo_asignado") or
                    lead.get("prospecto", {}).get("ejecutivo")
                )
                if ejecutivo not in UNASSIGNED_LABELS and (
                    lead.get("human_takeover_at") or (lead.get("lifecycle") or {}).get("human_takeover_at")
                ):
                    logger.debug(f"[NUDGE] Omitido {lead.get('phone')}: ya asignado a {ejecutivo}.")
                    _lead_id = lead["_id"]
                    await run_db("nudge_skip_executive", lambda: db["leads"].update_one({"_id": _lead_id}, {"$set": {"nudge_status": "skipped_has_executive"}}))
                    continue

                # ─── FILTRO 2: intención avanzada (visita, oferta, negociación) ───
                last_intent = lead.get("last_intent") or lead.get("prospecto", {}).get("last_intent", "")
                if str(last_intent).upper() in INTENTS_NO_NUDGE and (
                    lead.get("human_takeover_at") or (lead.get("lifecycle") or {}).get("human_takeover_at")
                ):
                    logger.debug(f"[NUDGE] Omitido {lead.get('phone')}: intención avanzada '{last_intent}'.")
                    _lead_id = lead["_id"]
                    await run_db("nudge_skip_intent", lambda: db["leads"].update_one({"_id": _lead_id}, {"$set": {"nudge_status": "skipped_advanced_intent"}}))
                    continue

                last_msg = messages[-1]
                # Solo si el último que habló fue el BOT
                if last_msg.get("role") != "assistant":
                    continue

                # ─── FILTRO 3: el bot ya respondió varias veces (conversación activa) ───
                bot_msgs_count  = sum(1 for m in messages if m.get("role") == "assistant")
                user_msgs_count = sum(1 for m in messages if m.get("role") == "user")

                # Si el cliente interactuó más de 1 vez y el bot respondió más de 2 veces,
                # la conversación ya está en marcha — NO mandar nudge genérico.
                if user_msgs_count >= 2 and bot_msgs_count >= 3:
                    logger.debug(f"[NUDGE] Omitido {lead.get('phone')}: conversación activa ({user_msgs_count}u/{bot_msgs_count}b msgs).")
                    _lead_id = lead["_id"]
                    await run_db("nudge_skip_active", lambda: db["leads"].update_one({"_id": _lead_id}, {"$set": {"nudge_status": "skipped_active_conversation"}}))
                    continue

                # Ver cuándo fue el último mensaje
                last_time_str = last_msg.get("timestamp") or last_msg.get("time")
                if not last_time_str:
                    continue

                try:
                    last_time = datetime.fromisoformat(str(last_time_str).replace("Z", "+00:00")).astimezone(pytz.utc).replace(tzinfo=None)
                except:
                    continue

                time_diff_mins = (now_utc - last_time).total_seconds() / 60.0

                # Descartar si es muy viejo o muy reciente
                if time_diff_mins > 720 or time_diff_mins < 30:
                    continue

                # Timing dinámico: 1 solo mensaje de usuario = esperar 60 min. Varios = 45 min.
                threshold_mins = 60 if user_msgs_count <= 1 else 45

                if time_diff_mins >= threshold_mins:
                    # --- COOLDOWN: m\u00ednimo 6h entre nudges al mismo lead ---
                    nudge_sent_at_str = lead.get("nudge_sent_at")
                    if nudge_sent_at_str:
                        try:
                            last_nudge_dt = datetime.fromisoformat(str(nudge_sent_at_str).replace("Z", "+00:00")).astimezone(pytz.utc).replace(tzinfo=None)
                            hours_since = (now_utc - last_nudge_dt).total_seconds() / 3600
                            if hours_since < NUDGE_COOLDOWN_HOURS:
                                logger.debug(f"[NUDGE] Cooldown activo {lead.get('phone')}: \u00faltimo hace {hours_since:.1f}h (m\u00edn {NUDGE_COOLDOWN_HOURS}h)")
                                continue
                        except Exception:
                            pass

                    # Re-validar en tiempo real antes de enviar (async)
                    _lead_id = lead["_id"]
                    fresh_lead = await run_db("nudge_prevalidate", lambda: db["leads"].find_one({"_id": _lead_id}))
                    fresh_msgs = (fresh_lead.get("messages", []) or []) if fresh_lead else []
                    if fresh_msgs and fresh_msgs[-1].get("role") == "user":
                        logger.info(f"[NUDGE] Cancelado para {lead.get('phone')} (Respondi\u00f3 justo ahora).")
                        continue

                    phone = lead.get("phone")
                    nudge_count = (lead.get("nudge_count") or 0) + 1
                    nudge_text = (
                        "Hola \U0001f642 Solo quer\u00eda saber si tienes alguna pregunta adicional "
                        "sobre la propiedad. Estoy aqu\u00ed para ayudarte. "
                        "Si no es buen momento, no hay problema. \U0001f44d"
                    )

                    logger.info(f"[NUDGE] Enviando reactivaci\u00f3n #{nudge_count} a {phone} (Inactivo {int(time_diff_mins)} min, Umbral: {threshold_mins})")
                    sent = await send_whatsapp_message(phone, nudge_text)

                    if sent:
                        from chatbot.storage import guardar_mensaje
                        now_cl_str = datetime.now(CHILE_TZ).isoformat()
                        guardar_mensaje(phone, "assistant", nudge_text, {
                            "tipo": "nudge_reactivacion",
                            "intencion": "reactivacion_automatica"
                        })
                        _lead_id = lead["_id"]
                        _update = {"$set": {"nudge_sent_at": now_cl_str, "nudge_last_date": today_str, "nudge_count": nudge_count}}
                        await run_db("nudge_update", lambda: db["leads"].update_one({"_id": _lead_id}, _update))
                        await asyncio.sleep(5)  # Throttling ligero
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[NUDGE_LOOP] Error global: {e}")
            if "nudge_loop" in background_tasks_status:
                background_tasks_status["nudge_loop"]["status"] = f"error: {str(e)}"
        
        await asyncio.sleep(300)  # Chequear cada 5 minutos (antes era 2min)

async def lead_consumer_worker(worker_id: int):
    """
    Consumidor aislado. Procesa exclusivamente claims ya reservados.
    Mantiene el ritmo estable y no satura el event loop ni el default executor.
    """
    logger.info(f"[CONSUMER-{worker_id}] Worker iniciado y escuchando cola...")
    loop = asyncio.get_running_loop()
    while True:
        try:
            claimed = await lead_processing_queue.get()
            try:
                status = background_tasks_status["lead_processing"]
                status["active"] = status.get("active", 0) + 1
                t0 = time.time()
                # Worker pool dedicado: evita interferencia con requests HTTP.
                await loop.run_in_executor(
                    _PROCESS_THREAD_POOL, LeadProcessingService.process_claimed, claimed
                )
                elapsed_ms = (time.time() - t0) * 1000
                background_tasks_status["lead_processing"]["last_duration_ms"] = round(elapsed_ms)
                logger.debug(f"[CONSUMER-{worker_id}] Claim procesado en {elapsed_ms:.0f}ms")
            except Exception as le:
                background_tasks_status["lead_processing"]["errors"] = (
                    background_tasks_status["lead_processing"].get("errors", 0) + 1
                )
                logger.error(f"[CONSUMER-{worker_id}] Error procesando claim: {le}")
            finally:
                status = background_tasks_status["lead_processing"]
                status["active"] = max(0, status.get("active", 1) - 1)
                lead_processing_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[CONSUMER-{worker_id}] Error crítico en worker: {e}")
            await asyncio.sleep(5)

async def reassign_unassigned_leads_loop():
    """Reserva trabajo explícito atómicamente y lo entrega al pool aislado."""
    logger.info("[PRODUCER] Iniciando claimer atómico de PROCESS_SERVICE...")
    last_health_refresh = 0.0
    while True:
        try:
            background_tasks_status["lead_processing"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["lead_processing"]["status"] = "running"
            loop = asyncio.get_running_loop()
            owner = f"render-{os.getenv('RENDER_INSTANCE_ID', 'local')}"
            claimed_count = 0
            while not lead_processing_queue.full():
                claimed = await loop.run_in_executor(
                    _PROCESS_THREAD_POOL,
                    lambda: LeadProcessingService.claim_next(owner),
                )
                if not claimed:
                    break
                await lead_processing_queue.put(claimed)
                claimed_count += 1
            background_tasks_status["lead_processing"].update({
                "max_concurrency": 2,
                "queued": lead_processing_queue.qsize(),
                "claims_last_cycle": claimed_count,
            })
            if time.monotonic() - last_health_refresh >= 10.0:
                from chatbot.processing_service import get_process_service_health
                from chatbot.storage import get_db
                background_tasks_status["lead_processing"]["health_snapshot"] = await run_in_threadpool(
                    get_process_service_health, get_db(),
                )
                background_tasks_status["lead_processing"]["health_snapshot_at"] = datetime.now(timezone.utc).isoformat()
                last_health_refresh = time.monotonic()
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            background_tasks_status["lead_processing"]["status"] = f"error: {str(e)}"
            logger.error(f"[BACKGROUND] Error en loop de procesamiento de leads: {e}")
            await asyncio.sleep(60)

async def cache_prewarmer_loop():
    """
    PRE-WARMING DE CACHE: precalienta el overview real del Leads Dashboard.
    El reporte ejecutivo antiguo no alimenta /api/leads-dashboard/overview,
    por lo que no evitaba el primer request lento de week/month/today.
    """
    logger.info("[CACHE_WARMER] Iniciando pre-warming de cache leads-intelligence (smart mode)...")
    # Espera inicial para no competir con el startup
    await asyncio.sleep(30)
    local_warm_in_progress = False
    cache_key = "leads_dashboard_overview_v1"
    lock_key = "lock_cache_prewarm_leads_intel_v1"
    while True:
        try:
            # Evitar solapes locales de warm si el ciclo previo no cerró aún.
            if local_warm_in_progress:
                await asyncio.sleep(30)
                continue

            from chatbot.storage import get_db
            from datetime import timezone
            db = get_db()
            now_utc = datetime.now(timezone.utc)

            # 1) Skip inteligente: si el lock/ciclo aún está vigente, no
            # recalcular todos los rangos en cada vuelta.
            loop_ref = asyncio.get_running_loop()
            cache_doc = await loop_ref.run_in_executor(
                _WORKER_THREAD_POOL,
                lambda: db["cache_store"].find_one({"_id": cache_key}, {"expires_at": 1})
            )
            expires_at = cache_doc.get("expires_at") if cache_doc else None
            if expires_at:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                ttl_left = (expires_at - now_utc).total_seconds()
                if ttl_left > 120:
                    logger.debug(f"[CACHE_WARMER] Skip: cache vigente ({ttl_left:.0f}s restantes)")
                    await asyncio.sleep(60)
                    continue

            # 2) Lock distribuido: evita prewarm simultáneo entre instancias/restarts.
            lock_until = now_utc + timedelta(seconds=90)
            def _acquire_lock():
                try:
                    return db["cache_locks"].find_one_and_update(
                        {"_id": lock_key, "$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$lte": now_utc}}]},
                        {"$set": {"expires_at": lock_until, "updated_at": now_utc}},
                        upsert=True, return_document=ReturnDocument.AFTER
                    )
                except DuplicateKeyError:
                    # Otra instancia ganó el upsert simultáneamente.
                    return None
            lock_doc = await loop_ref.run_in_executor(_WORKER_THREAD_POOL, _acquire_lock)
            if not lock_doc:
                logger.debug("[CACHE_WARMER] Skip: lock activo en otra instancia")
                await asyncio.sleep(60)
                continue

            local_warm_in_progress = True
            loop = asyncio.get_running_loop()
            t0 = time.time()
            # Warmer pool dedicado: evita competir con workers de procesamiento.
            # Los nombres de preset generan la misma clave canónica que las
            # peticiones del frontend, incluso cuando este envía fechas explícitas.
            warm_specs = (("today", "Hoy"), ("week", "Semana"), ("month", "Mes"), ("30d", "30 días"))
            for preset, label in warm_specs:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        _WARMER_THREAD_POOL,
                        lambda p=preset: get_leads_dashboard_overview(
                            compare="auto", period_preset=p,
                        ),
                    ),
                    timeout=25.0,
                )
                logger.info("[CACHE_WARMER] overview %s precalentado", label)
            elapsed_ms = (time.time() - t0) * 1000
            logger.debug(f"[CACHE_WARMER] LEADS_INTELLIGENCE: cache pre-warmed en {elapsed_ms:.0f}ms")
            # Liberar lock explícitamente tras warm exitoso.
            await loop_ref.run_in_executor(_WORKER_THREAD_POOL, lambda: db["cache_locks"].update_one({"_id": lock_key}, {"$set": {"expires_at": now_utc}}))
        except asyncio.TimeoutError:
            logger.warning("[CACHE_WARMER] Timeout >8s; se omite este ciclo para evitar jitter")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[CACHE_WARMER] Error al pre-warm cache: {e}")
        finally:
            local_warm_in_progress = False
        await asyncio.sleep(60)

async def event_loop_monitor_loop():
    """
    Monitor global del event loop. Mide el lag real para detectar operaciones bloqueantes.
    """
    logger.info("[EVENT_LOOP_MONITOR] Iniciando monitor de lag...")
    while True:
        try:
            start = time.time()
            await asyncio.sleep(1.0)
            duration = time.time() - start
            lag_ms = (duration - 1.0) * 1000
            
            if lag_ms > 1000:
                observability_mark("event_loop_blocked")
                logger.error(f"[EVENT_LOOP_BLOCKED] lag={lag_ms:.0f}ms possible_blocking_operation=true")
                logger.critical(
                    f"[CRITICAL] [ASYNC_VIOLATION] type=event_loop_blocked lag_ms={lag_ms:.0f} "
                    f"op=none collection=none caller=event_loop_monitor_loop trace=none "
                    f"impact=HIGH action_required=true async_context=true thread_type=main safe=false"
                )
                recent = observability_event_loop_blocked_recent(10)
                if recent > 3:
                    logger.critical(
                        f"[CRITICAL] [EVENT_LOOP_SATURATED] count={recent} window=10s action=INVESTIGATE_BLOCKING_CALLS"
                    )
                # Dump completo solo en bloqueos severos para evitar ruido excesivo.
                if lag_ms > 5000:
                    now = time.time()
                    for task in asyncio.all_tasks():
                        if task.done():
                            continue
                        coro = task.get_coro()
                        task_name = task.get_name()
                        state = "cancelled" if task.cancelled() else "pending"
                        stack_frames = task.get_stack(limit=8)
                        stack_text = ""
                        if stack_frames:
                            stack_text = "".join(traceback.format_list(traceback.extract_stack(stack_frames[-1])))
                        logger.error(
                            f"[EVENT_LOOP_TASK_DUMP] name={task_name} coro={getattr(coro, '__qualname__', str(coro))} "
                            f"state={state} ts={now:.3f} stack={stack_text[:1200]}"
                        )
            elif lag_ms > 250:
                logger.warning(f"[EVENT_LOOP_BLOCKED] lag={lag_ms:.0f}ms possible_blocking_operation=true")
                observability_mark("event_loop_blocked")
                logger.critical(
                    f"[CRITICAL] [ASYNC_VIOLATION] type=event_loop_blocked lag_ms={lag_ms:.0f} "
                    f"op=none collection=none caller=event_loop_monitor_loop trace=none "
                    f"impact=HIGH action_required=true async_context=true thread_type=main safe=false"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[EVENT_LOOP_MONITOR] Error: {e}")
            await asyncio.sleep(5)
        if int(time.time()) % 60 == 0:
            try:
                snap = observability_snapshot_and_reset()
                status_level = "OK" if snap["mongo_sync_on_loop"] == 0 and snap["event_loop_blocked"] == 0 else "DEGRADED"
                if snap["mongo_sync_on_loop"] > 5 or snap["event_loop_blocked"] > 5:
                    status_level = "CRITICAL"
                # Solo loguear en INFO/WARNING si hay degradación; OK va a DEBUG para no contaminar
                summary_msg = (
                    f"[HEALTH_SUMMARY]\nmongo_sync_on_loop={snap['mongo_sync_on_loop']}\n"
                    f"event_loop_blocked={snap['event_loop_blocked']}\nstatus={status_level}"
                )
                if status_level == "CRITICAL":
                    logger.error(summary_msg)
                elif status_level == "DEGRADED":
                    logger.warning(summary_msg)
                else:
                    logger.debug(summary_msg)
            except Exception:
                logger.exception("[HEALTH_SUMMARY] error=failed_to_emit")

async def threadpool_forensics_loop():
    """Forensics de threadpools: tamaño, ocupación aproximada y cola."""
    logger.info("[THREADPOOL_MONITOR] Iniciando monitor de threadpools...")
    last_snapshot = {}
    last_heartbeat_log = 0.0
    saturation_streak = {"WEB": 0, "WORKER": 0, "WARMER": 0}
    while True:
        try:
            pools = [
                ("WEB", _WEB_THREAD_POOL),
                ("WORKER", _WORKER_THREAD_POOL),
                ("WARMER", _WARMER_THREAD_POOL),
            ]
            for name, pool in pools:
                max_workers = getattr(pool, "_max_workers", -1)
                threads = getattr(pool, "_threads", set())
                active_threads = len([t for t in threads if t.is_alive()])
                q = getattr(pool, "_work_queue", None)
                queued = q.qsize() if q is not None and hasattr(q, "qsize") else -1
                snapshot = (active_threads, max_workers, queued)
                prev = last_snapshot.get(name)
                now = time.time()
                # Log solo cuando cambia estado (INFO); heartbeat periódico va a DEBUG para no contaminar.
                if prev != snapshot:
                    logger.info(
                        f"[THREADPOOL_FORENSICS] pool={name} active={active_threads} max={max_workers} queued={queued}"
                    )
                elif (now - last_heartbeat_log) >= 60:
                    logger.debug(
                        f"[THREADPOOL_FORENSICS] pool={name} active={active_threads} max={max_workers} queued={queued}"
                    )
                last_snapshot[name] = snapshot
                # Saturación real sostenida: evitar alertas por picos breves de cola=1.
                is_saturated_now = (
                    queued >= 3 and
                    max_workers > 0 and
                    active_threads >= max_workers
                )
                if is_saturated_now:
                    saturation_streak[name] = saturation_streak.get(name, 0) + 1
                else:
                    saturation_streak[name] = 0

                # Alertar solo si se mantiene en al menos 2 ciclos consecutivos (~10s).
                if saturation_streak[name] >= 2:
                    logger.warning(
                        f"[THREADPOOL_SATURATED] pool={name} queued={queued} active={active_threads} max={max_workers}"
                    )
                if (now - last_heartbeat_log) >= 60:
                    last_heartbeat_log = now
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[THREADPOOL_MONITOR] Error: {e}")
        await asyncio.sleep(5)

async def daily_report_loop():
    """Loop de fondo para enviar el reporte de SLA y Captaciones una vez al día."""
    logger.info("[DAILY_REPORT] Iniciando monitor de reporte diario (SLA + Captaciones)...")
    from chatbot.daily_report import check_and_run_daily_report, check_and_run_personalized_summary
    from chatbot.captacion_report import check_and_run_meta_diaria_report
    while True:
        try:
            background_tasks_status["daily_report"] = {
                "status": "running", 
                "last_heartbeat": datetime.now(CHILE_TZ).isoformat()
            }
            # Reporte 1: Leads críticos SLA (09:30 AM)
            await check_and_run_daily_report()
            
            # Reporte 2: Resumen Matutino Personalizado (09:00 AM)
            await check_and_run_personalized_summary()
            
            # Reporte 3: calcula y envía el semanal de Captaciones (lunes 08:30).
            await check_and_run_meta_diaria_report()
            # Monitoreo de anomalias (cada lunes a las 08:00)
            now = datetime.now(CHILE_TZ)
            if now.weekday() == 0 and now.hour == 8 and now.minute < 5:
                try:
                    from scripts.monitor_anomalies import run_anomaly_check
                    run_anomaly_check()
                    logger.info("[DAILY_REPORT] Monitor de anomalias ejecutado (lunes 08:00).")
                except Exception as em:
                    logger.warning(f"[DAILY_REPORT] Error en monitor_anomalias: {em}")
        except Exception as e:
            logger.error(f"[DAILY_REPORT] Error en loop: {e}")
            if "daily_report" in background_tasks_status:
                background_tasks_status["daily_report"]["status"] = f"error: {str(e)}"
        
        # Revisar cada 5 minutos
        await asyncio.sleep(300)


async def captacion_daily_production_scheduler_loop():
    """Production Captación daily sender: Tue-Fri at 08:30 Chile time."""
    from chatbot.captacion_daily_report import run_scheduled_production_daily_report
    from chatbot.storage import get_db
    logger.info(
        "[CAPTACION_DAILY_PRODUCTION] scheduler active=%s timezone=America/Santiago window=Tue-Fri 08:30-12:00",
        Config.CAPTACION_DAILY_PRODUCTION_ENABLED and not Config.CAPTACION_TEST_MODE,
    )
    while True:
        try:
            now = datetime.now(CHILE_TZ)
            background_tasks_status["captacion_daily_production"] = {
                "status": "running" if Config.CAPTACION_DAILY_PRODUCTION_ENABLED and not Config.CAPTACION_TEST_MODE else "disabled",
                "timezone": "America/Santiago",
                "schedule": "Tuesday-Friday 08:30-12:00 catch-up",
                "last_heartbeat": now.isoformat(),
            }
            if Config.CAPTACION_DAILY_PRODUCTION_ENABLED and not Config.CAPTACION_TEST_MODE:
                result = await run_scheduled_production_daily_report(get_db(), run_at=now)
                background_tasks_status["captacion_daily_production"]["last_result"] = result.get("status")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[CAPTACION_DAILY_PRODUCTION] scheduler error: %s", exc)
            background_tasks_status.setdefault("captacion_daily_production", {})["status"] = "error"
            background_tasks_status["captacion_daily_production"]["error"] = str(exc)
        await asyncio.sleep(30)

if __name__ == "__main__":
    import pathlib
    module_name = pathlib.Path(__file__).stem
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Bot PRO iniciado → http://localhost:{port}")
    uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=port, reload=True, log_level="info")




