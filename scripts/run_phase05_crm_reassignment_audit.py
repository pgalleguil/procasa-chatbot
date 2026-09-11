"""Fase 0.5: certificación analítica de leads CRM reasignables por SLA.

El script es estrictamente de lectura sobre MongoDB. Solo escribe los tres
artefactos locales de esta fase y un informe Markdown. No importa workers de
asignación ni ejecuta el router productivo, porque ese router mantiene estado
de round-robin.
"""
from __future__ import annotations

import csv
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient, ReadPreference

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config
from chatbot.constants import CHILE_TZ, BUSINESS_DAYS, BUSINESS_START_HOUR, BUSINESS_END_HOUR
from chatbot.crm_metrics import (
    coerce_utc_datetime,
    commercial_sla_start_at,
    event_evidence,
    normalize_result,
    calculate_sla,
)
from chatbot.crm_sla_alert_evaluator import (
    CLOSED_STAGES,
    EXCLUDED_ORIGINS,
    INSTRUMENTATION_CUTOVER,
    SLA_STOP_RESULTS,
    SYNTHETIC_PHONES,
    _is_test_lead,
    add_business_minutes,
)
from chatbot.crm_sla_alert_settings import CUTOVER_AT
from chatbot.utils import calculate_business_minutes

# Read-side reconstruction of the existing router. This function does not call
# get_next_round_robin_executive(), which writes routing state in MongoDB.
from scripts.run_phase0_crm_sla_audit import router_candidates, user_name_match


REPORT = ROOT / "docs" / "AUDITORIA_ELEGIBILIDAD_REASIGNACION_SLA_20260909.md"
DATA_DIR = ROOT / "docs" / "auditoria_sla_data"


def text(value: Any) -> str:
    return str(value or "").strip()


def norm(value: Any) -> str:
    value = text(value).lower()
    value = "".join(
        char for char in unicodedata.normalize("NFD", value)
        if unicodedata.category(char) != "Mn"
    )
    return re.sub(r"\s+", " ", value).strip()


def path_get(document: dict[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def first_value(document: dict[str, Any], paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = path_get(document, path)
        if value not in (None, "", [], {}):
            return value
    return None


def first_text(document: dict[str, Any], paths: tuple[str, ...]) -> str:
    return text(first_value(document, paths))


def parse_dt(value: Any) -> datetime | None:
    return coerce_utc_datetime(value)


def local_iso(value: datetime | None) -> str:
    return value.astimezone(CHILE_TZ).isoformat() if value else ""


def fmt(value: Any) -> str:
    if value is None:
        return "N/D"
    if isinstance(value, float) and math.isfinite(value):
        return f"{value:.1f}"
    return str(value)


def pct(numerator: int, denominator: int) -> float:
    return round((numerator * 100.0 / denominator), 1) if denominator else 0.0


def stage_of(lead: dict[str, Any]) -> str:
    return text(first_value(lead, ("pipeline_stage", "stage", "crm_estado"))).upper()


def lead_owner(lead: dict[str, Any]) -> str:
    return first_text(lead, ("ejecutivo_asignado", "prospecto.ejecutivo"))


def property_code_of(lead: dict[str, Any]) -> str:
    return first_text(lead, (
        "prospecto.codigo", "datos_propiedad.codigo", "property_code",
        "prospecto.codigo_propiedad", "prospecto.codigo_referencia",
        "prospecto.codigo_yapo", "prospecto.codigo_mercadolibre",
    ))


def operation_of(lead: dict[str, Any], prop: dict[str, Any] | None = None) -> str:
    raw = first_text(lead, (
        "operacion", "prospecto.operacion", "prospecto.tipo_operacion",
        "datos_propiedad.operacion",
    ))
    if not raw and prop:
        raw = first_text(prop, ("operacion", "tipo_operacion.tipo"))
        op = prop.get("tipo_operacion") or {}
        if isinstance(op, dict):
            if op.get("venta") is True and op.get("arriendo") is True:
                raw = "Venta/Arriendo"
            elif op.get("venta") is True:
                raw = "Venta"
            elif op.get("arriendo") is True:
                raw = "Arriendo"
    value = norm(raw)
    if value in {"venta", "v"}:
        return "VENTA"
    if value in {"arriendo", "arr", "a"}:
        return "ARRIENDO"
    if value in {"venta/arriendo", "venta y arriendo"}:
        return "VENTA_ARRIENDO"
    return re.sub(r"\s+", " ", text(raw)).upper() or "NO_INFORMADO"


def origin_of(lead: dict[str, Any]) -> str:
    raw = first_text(lead, (
        "origen", "lead_origin", "origin", "source_type",
        "prospecto.canal_origen", "prospecto.origen", "prospecto.metodo_ingreso",
    ))
    value = norm(raw)
    if value == "portal inmobiliario":
        return "PORTAL_INMOBILIARIO"
    if value in {"toctoc", "toc toc"}:
        return "TOCTOC"
    return re.sub(r"\s+", " ", text(raw)).upper() or "NO_INFORMADO"


def location_of(lead: dict[str, Any], prop: dict[str, Any] | None, field: str) -> str:
    if field == "comuna":
        paths = ("comuna", "prospecto.comuna", "prospecto.ubicacion.comuna", "datos_propiedad.comuna")
        prop_paths = ("ubicacion.comuna", "comuna")
    else:
        paths = ("region", "prospecto.region", "prospecto.ubicacion.region", "datos_propiedad.region")
        prop_paths = ("ubicacion.region", "region")
    raw = first_text(lead, paths)
    if not raw and prop:
        raw = first_text(prop, prop_paths)
    return re.sub(r"\s+", " ", text(raw)).upper() or "NO_INFORMADO"


def property_code_and_doc(lead: dict[str, Any], properties: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    code = property_code_of(lead)
    return code, properties.get(code) if code else None


def active_agent_by_id(users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        text(user.get("_id")): user for user in users
        if user.get("is_active") is True and text(user.get("rol")) == "agente"
    }


def resolve_owner(cycle: dict[str, Any], users: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any] | None]:
    raw_id = text(cycle.get("assigned_to_user_id"))
    by_id = {text(user.get("_id")): user for user in users if user.get("_id") is not None}
    user = by_id.get(raw_id)
    if user:
        return raw_id, text(user.get("nombre")), user
    display = text(cycle.get("assigned_to_display_name"))
    for candidate in users:
        if user_name_match(display, candidate.get("nombre")):
            return raw_id or text(candidate.get("_id")), text(candidate.get("nombre")), candidate
    return raw_id or display, display or raw_id or "SIN_RECEPTOR", None


def human_actor(value: Any, actor_type: Any = None) -> bool:
    actor = norm(value)
    kind = norm(actor_type)
    if not actor or actor in {"system", "sistema", "bot", "none", "null"}:
        return False
    return not kind or kind in {"human", "agent", "administrator", "supervisor"}


def event_time(item: dict[str, Any]) -> datetime | None:
    return parse_dt(item.get("timestamp") or item.get("occurred_at"))


def event_cycle_matches(item: dict[str, Any], cycle_id: str) -> bool:
    event_cycle = text(item.get("assignment_cycle_id"))
    return not event_cycle or event_cycle == cycle_id


def event_is_confirmed_outreach(item: dict[str, Any]) -> bool:
    meta = item.get("meta") or {}
    return bool(
        item.get("confirmed")
        or meta.get("provider_message_id")
        or meta.get("sent") is True
    )


def result_is_current_stop(result: dict[str, Any]) -> bool:
    return normalize_result(result.get("result_type")) in SLA_STOP_RESULTS


def load_data() -> dict[str, Any]:
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI no está configurado")
    client = MongoClient(
        Config.MONGO_URI,
        read_preference=ReadPreference.PRIMARY_PREFERRED,
        socketTimeoutMS=20000,
        connectTimeoutMS=5000,
        serverSelectionTimeoutMS=10000,
    )
    client.admin.command("ping")
    db = client[Config.DB_NAME]
    # Se incluyen messages únicamente en memoria para identificar actividad
    # automática/entrante; nunca se exporta su contenido.
    leads = list(db["leads"].find({}))
    cycles = list(db["crm_assignment_cycles"].find({}))
    events = list(db["crm_events"].find({}, {
        "lead_id": 1, "assignment_cycle_id": 1, "type": 1, "actor": 1,
        "actor_type": 1, "confirmed": 1, "result": 1, "meta": 1,
        "timestamp": 1, "occurred_at": 1,
    }))
    management_results = list(db["crm_management_results"].find({}, {
        "_id": 1, "lead_id": 1, "assignment_cycle_id": 1, "actor_user_id": 1,
        "result_type": 1, "source": 1, "occurred_at": 1, "status": 1,
        "pipeline_stage_at_result": 1,
    }))
    notifications = list(db["crm_notifications_v1"].find({}, {
        "assignment_cycle_id": 1, "metadata": 1, "state": 1,
        "provider_message_id": 1, "notification_type": 1,
    }))
    users = list(db["usuarios"].find({}, {
        "_id": 1, "nombre": 1, "rol": 1, "is_active": 1,
        "comunas_interes": 1, "comunas_interes_norm": 1, "region": 1,
        "region_slug": 1, "oficina": 1, "office": 1, "zonas": 1,
    }))
    property_collection = getattr(Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")
    properties_raw = list(db[property_collection].find({}, {
        "codigo": 1, "comuna": 1, "region": 1, "ubicacion": 1,
        "estado": 1, "ejecutivo": 1, "captador": 1, "responsable": 1,
        "tipo_operacion": 1, "operacion": 1,
    }))
    properties = {
        text(prop.get("codigo")): prop for prop in properties_raw
        if text(prop.get("codigo"))
    }
    client.close()
    return {
        "as_of": datetime.now(timezone.utc),
        "leads": leads,
        "cycles": cycles,
        "events": events,
        "management_results": management_results,
        "notifications": notifications,
        "users": users,
        "properties": properties,
        "property_collection": property_collection,
    }


def index_data(data: dict[str, Any]) -> dict[str, Any]:
    leads_by_id = {text(lead.get("_id")): lead for lead in data["leads"] if lead.get("_id") is not None}
    events_by_lead: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in data["events"]:
        if event.get("lead_id") is not None:
            events_by_lead[text(event.get("lead_id"))].append(event)
    results_by_cycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in data["management_results"]:
        if result.get("assignment_cycle_id") is not None:
            results_by_cycle[text(result.get("assignment_cycle_id"))].append(result)
    notifications_by_cycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for notification in data["notifications"]:
        metadata = notification.get("metadata") or {}
        cycle_id = text(notification.get("assignment_cycle_id") or metadata.get("assignment_cycle_id"))
        if cycle_id:
            notifications_by_cycle[cycle_id].append(notification)
    active_by_lead: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cycle in data["cycles"]:
        if cycle.get("cycle_status") == "active" and cycle.get("unassigned_at") is None:
            active_by_lead[text(cycle.get("lead_id"))].append(cycle)
    return {
        "leads_by_id": leads_by_id,
        "events_by_lead": events_by_lead,
        "results_by_cycle": results_by_cycle,
        "notifications_by_cycle": notifications_by_cycle,
        "active_by_lead": active_by_lead,
    }


def record_base(cycle: dict[str, Any], data: dict[str, Any], idx: dict[str, Any]) -> dict[str, Any]:
    lead_id = text(cycle.get("lead_id"))
    lead = idx["leads_by_id"].get(lead_id)
    assigned_at = parse_dt(cycle.get("assigned_at"))
    persisted_start = parse_dt(cycle.get("sla_started_at"))
    # El evaluador productivo usa assigned_at cuando sla_started_at no existe.
    effective_start = persisted_start or assigned_at
    start_source = "persisted" if persisted_start else ("fallback" if assigned_at else "missing")
    owner_id, owner_name, owner_user = resolve_owner(cycle, data["users"])
    temperature = text(cycle.get("temperature_at_assignment") or "").upper()
    if temperature not in {"HOT", "NORMAL"}:
        temperature = "HOT" if text((lead or {}).get("lead_temperature_effective")).upper() == "HOT" else "NORMAL"
    threshold = 60 if temperature == "HOT" else 180
    lifecycle = (lead or {}).get("lifecycle") or {}
    hot_start = parse_dt(cycle.get("hot_started_at")) or parse_dt(lifecycle.get("hot_since"))
    if hot_start and effective_start and hot_start < effective_start:
        hot_start = effective_start
    sla = calculate_sla(
        assigned_at=effective_start,
        first_valid_management_at=None,
        now=data["as_of"],
        temperature=temperature,
        hot_started_at=hot_start,
    ) if effective_start else None
    measured = None
    if sla:
        measured = sla.get("hot_minutes") if temperature == "HOT" else sla.get("minutes")
    deadline_start = hot_start if temperature == "HOT" and hot_start else effective_start
    deadline = add_business_minutes(deadline_start, threshold) if deadline_start else None
    code, prop = property_code_and_doc(lead or {}, data["properties"])
    lead_owner_name = lead_owner(lead or {})
    owner_active = bool(
        owner_user and owner_user.get("is_active") is True and text(owner_user.get("rol")) == "agente"
    )
    owner_state = "OWNER_OK"
    if not owner_user or not owner_active:
        owner_state = "DATA_INSUFFICIENT"
    elif not lead_owner_name:
        owner_state = "DATA_INSUFFICIENT"
    elif not user_name_match(lead_owner_name, owner_name):
        owner_state = "OWNER_MISMATCH"
    active_cycles = idx["active_by_lead"].get(lead_id, [])
    cycle_ids = {text(item.get("assignment_cycle_id")) for item in active_cycles}
    current_cycle_id = text((lifecycle or {}).get("current_assignment_cycle_id"))
    cycle_state = "CYCLE_OK"
    if len(active_cycles) > 1:
        cycle_state = "CONCURRENT_ACTIVITY_RISK"
    elif current_cycle_id and current_cycle_id != text(cycle.get("assignment_cycle_id")):
        cycle_state = "CYCLE_MISMATCH"
    prior_cycles = [
        item for item in data["cycles"]
        if text(item.get("lead_id")) == lead_id
        and text(item.get("assignment_cycle_id")) != text(cycle.get("assignment_cycle_id"))
    ]
    return {
        "cycle": cycle,
        "lead": lead,
        "lead_id": lead_id,
        "cycle_id": text(cycle.get("assignment_cycle_id")),
        "assigned_at": assigned_at,
        "sla_started_at": effective_start,
        "sla_start_source": start_source,
        "hot_started_at": hot_start,
        "deadline": deadline,
        "temperature": temperature,
        "threshold": threshold,
        "elapsed": float(measured) if measured is not None else None,
        "stage": stage_of(lead or {}),
        "closed_lead": stage_of(lead or {}) in CLOSED_STAGES,
        "owner_id": owner_id,
        "owner_name": owner_name or "SIN_RECEPTOR",
        "lead_owner_name": lead_owner_name,
        "owner_state": owner_state,
        "owner_active": owner_active,
        "cycle_state": cycle_state,
        "active_cycle_count": len(active_cycles),
        "active_cycle_ids": ";".join(sorted(cycle_ids)),
        "prior_cycle_count": len(prior_cycles),
        "cycle_reopened": any(
            item.get("cycle_status") in {"closed", "reassigned"} or item.get("unassigned_at") is not None
            for item in prior_cycles
        ),
        "property_code": code,
        "property_found": bool(prop),
        "property": prop,
        "comuna": location_of(lead or {}, prop, "comuna"),
        "region": location_of(lead or {}, prop, "region"),
        "origin": origin_of(lead or {}),
        "operation": operation_of(lead or {}, prop),
        "excluded_origin": (
            norm(cycle.get("reason")) in EXCLUDED_ORIGINS
            or norm(cycle.get("cycle_origin")) in EXCLUDED_ORIGINS
            or _is_test_lead(lead or {})
        ),
        "canonical": (
            text(cycle.get("schema_version")) == "crm_assignment_cycle_v1"
            and bool(text(cycle.get("assignment_cycle_id")))
            and bool(text(cycle.get("lead_id")))
            and bool(text(cycle.get("assigned_to_user_id")))
        ),
        "inbound_crm": True,
        "evidence": [],
    }


def add_evidence(record: dict[str, Any], *, kind: str, source: str,
                 occurred: datetime | None, human_attempt: bool = False,
                 current_stop: bool = False, automatic: bool = False,
                 open_click: bool = False, status_only: bool = False,
                 ambiguous: bool = False, legacy: bool = False,
                 detail: str = "", cycle_match: bool = True) -> None:
    record["evidence"].append({
        "kind": kind,
        "source": source,
        "occurred": occurred,
        "human_attempt": human_attempt,
        "current_stop": current_stop,
        "automatic": automatic,
        "open_click": open_click,
        "status_only": status_only,
        "ambiguous": ambiguous,
        "legacy": legacy,
        "detail": detail,
        "cycle_match": cycle_match,
    })


def attach_evidence(record: dict[str, Any], data: dict[str, Any], idx: dict[str, Any]) -> None:
    assigned = record["assigned_at"]
    if not assigned or not record["lead_id"]:
        return
    cycle_id = record["cycle_id"]
    # The evaluator productivo reads events by lead_id and timestamp. We keep
    # that behavior and separately flag events explicitly tied to another cycle.
    for event in sorted(idx["events_by_lead"].get(record["lead_id"], []), key=lambda x: event_time(x) or datetime.min.replace(tzinfo=timezone.utc)):
        occurred = event_time(event)
        if not occurred or occurred < assigned or occurred > data["as_of"]:
            continue
        raw_type = text(event.get("type")).upper()
        match = event_cycle_matches(event, cycle_id)
        ev = event_evidence(event)
        detail_result = normalize_result(event.get("result") or (event.get("meta") or {}).get("result") or (event.get("meta") or {}).get("contact_result"))
        if ev["management"]:
            add_evidence(
                record, kind="VALID_MANAGEMENT_CANONICAL", source=f"crm_events:{raw_type}",
                occurred=occurred, human_attempt=True, current_stop=True,
                detail=detail_result or "valid_management", cycle_match=match,
            )
            continue
        if raw_type in {"SEND_WA_LEAD", "SEND_EMAIL_LEAD"}:
            if human_actor(event.get("actor"), event.get("actor_type")):
                # An operator-created SEND event is an auditable human action;
                # provider confirmation is retained as detail, not invented.
                add_evidence(
                    record, kind="HUMAN_OUTREACH_NOT_SLA_STOP", source=f"crm_events:{raw_type}",
                    occurred=occurred, human_attempt=True,
                    detail="confirmed" if event_is_confirmed_outreach(event) else "human_action_unconfirmed_delivery",
                    legacy=not match, cycle_match=match,
                )
            else:
                add_evidence(
                    record, kind="SYSTEM_GENERATED", source=f"crm_events:{raw_type}",
                    occurred=occurred, automatic=True, legacy=not match, cycle_match=match,
                )
            continue
        if raw_type == "CALL_COMPLETED_LEAD":
            if human_actor(event.get("actor"), event.get("actor_type")):
                add_evidence(
                    record, kind="HUMAN_OUTREACH_NOT_SLA_STOP", source="crm_events:CALL_COMPLETED_LEAD",
                    occurred=occurred, human_attempt=True,
                    detail="call_action", legacy=not match, cycle_match=match,
                )
            else:
                add_evidence(record, kind="SYSTEM_GENERATED", source="crm_events:CALL_COMPLETED_LEAD", occurred=occurred, automatic=True, legacy=not match, cycle_match=match)
            continue
        if raw_type in {
            "CLICK_WHATSAPP_LEAD", "CLICK_PHONE_LEAD", "CLICK_EMAIL_LEAD",
            "CLICK_WHATSAPP_OWNER", "CLICK_PHONE_OWNER", "CLICK_EMAIL_OWNER",
            "OPEN_DETAIL", "PAGE_VIEW", "PAGE_EXIT", "TOGGLE_TIMELINE",
            "TOGGLE_LEAD_DETAILS", "TOGGLE_PROP_DETAILS", "NAVIGATION", "FILTER",
        }:
            add_evidence(record, kind="OPEN_CLICK_NOT_ATTEMPT", source=f"crm_events:{raw_type}", occurred=occurred, open_click=True, legacy=not match, cycle_match=match)
            continue
        if raw_type in {"BOT_MSG", "ALERT", "ALERT_SENT", "alert_sent", "ASSIGNMENT", "assignment"}:
            add_evidence(record, kind="SYSTEM_GENERATED", source=f"crm_events:{raw_type}", occurred=occurred, automatic=True, legacy=not match, cycle_match=match)
            continue
        if raw_type == "msg_in":
            add_evidence(record, kind="INBOUND_CLIENT_MESSAGE", source="crm_events:msg_in", occurred=occurred, detail="client_message", legacy=not match, cycle_match=match)
            continue
        if raw_type in {"STATUS_CHANGE", "stage_change"}:
            add_evidence(record, kind="STATUS_ONLY_NO_EVIDENCE", source=f"crm_events:{raw_type}", occurred=occurred, status_only=True, legacy=not match, cycle_match=match)
            continue
        if raw_type in {"HUMAN_NOTE", "GESTION_LOG", "MANUAL_ENTRY", "CONTACT_RESULT", "CRM_NOTE_ADDED"}:
            if human_actor(event.get("actor"), event.get("actor_type")) and (detail_result or (event.get("meta") or {}).get("meaningful_change")):
                add_evidence(record, kind="AMBIGUOUS_HUMAN_EVIDENCE", source=f"crm_events:{raw_type}", occurred=occurred, ambiguous=True, detail=detail_result or "human_event_without_current_stop", legacy=not match, cycle_match=match)
            else:
                add_evidence(record, kind="STATUS_ONLY_NO_EVIDENCE", source=f"crm_events:{raw_type}", occurred=occurred, status_only=True, legacy=not match, cycle_match=match)
            continue
        if human_actor(event.get("actor"), event.get("actor_type")):
            add_evidence(record, kind="UNKNOWN", source=f"crm_events:{raw_type or 'NO_TYPE'}", occurred=occurred, ambiguous=True, legacy=not match, cycle_match=match)
        else:
            add_evidence(record, kind="SYSTEM_GENERATED", source=f"crm_events:{raw_type or 'NO_TYPE'}", occurred=occurred, automatic=True, legacy=not match, cycle_match=match)

    for result in sorted(idx["results_by_cycle"].get(cycle_id, []), key=lambda x: parse_dt(x.get("occurred_at")) or datetime.min.replace(tzinfo=timezone.utc)):
        occurred = parse_dt(result.get("occurred_at"))
        if not occurred or occurred < assigned or occurred > data["as_of"]:
            continue
        normalized = normalize_result(result.get("result_type"))
        is_human = human_actor(result.get("actor_user_id"))
        if result_is_current_stop(result):
            add_evidence(record, kind="VALID_MANAGEMENT_CANONICAL", source=f"crm_management_results:{normalized or 'UNKNOWN'}", occurred=occurred, human_attempt=is_human, current_stop=True, detail=normalized or "unknown_result")
        elif is_human:
            add_evidence(record, kind="HUMAN_OUTREACH_NOT_SLA_STOP", source=f"crm_management_results:{normalized or 'UNKNOWN'}", occurred=occurred, human_attempt=True, detail=normalized or "human_result_without_current_stop")
        else:
            add_evidence(record, kind="SYSTEM_GENERATED", source=f"crm_management_results:{normalized or 'UNKNOWN'}", occurred=occurred, automatic=True, detail=normalized or "system_result")

    lead = record["lead"] or {}
    for change in lead.get("stage_history") or []:
        if not isinstance(change, dict):
            continue
        occurred = parse_dt(change.get("timestamp") or change.get("occurred_at"))
        if not occurred or occurred < assigned or occurred > data["as_of"]:
            continue
        actor = change.get("actor")
        if human_actor(actor):
            add_evidence(
                record, kind="STATUS_ONLY_NO_EVIDENCE", source="lead.stage_history",
                occurred=occurred, status_only=True,
                detail=text(change.get("to") or "stage_change"),
            )
        else:
            add_evidence(
                record, kind="SYSTEM_GENERATED", source="lead.stage_history",
                occurred=occurred, automatic=True,
                detail=text(change.get("to") or "stage_change"),
            )

    for message in lead.get("messages") or []:
        if not isinstance(message, dict):
            continue
        occurred = parse_dt(message.get("timestamp") or message.get("occurred_at"))
        if not occurred or occurred < assigned or occurred > data["as_of"]:
            continue
        role = norm(message.get("role"))
        if role in {"assistant", "system"}:
            add_evidence(record, kind="SYSTEM_GENERATED", source=f"messages:{role}", occurred=occurred, automatic=True)
        elif role == "user":
            add_evidence(record, kind="INBOUND_CLIENT_MESSAGE", source="messages:user", occurred=occurred, detail="client_message")
        else:
            add_evidence(record, kind="UNKNOWN", source=f"messages:{role or 'NO_ROLE'}", occurred=occurred, ambiguous=True)

    # These persisted fields are useful for reconciliation but do not become a
    # current SLA stop by themselves: the production evaluator checks results
    # and event_evidence().
    field_candidates = [
        ("cycle.first_valid_management_at", parse_dt(record["cycle"].get("first_valid_management_at"))),
        ("lead.lifecycle.first_valid_management_at", parse_dt((lead.get("lifecycle") or {}).get("first_valid_management_at"))),
    ]
    canonical_times = [e["occurred"] for e in record["evidence"] if e["current_stop"] and e["occurred"]]
    for source, occurred in field_candidates:
        if occurred and occurred >= assigned and not any(abs((occurred - candidate).total_seconds()) <= 2 for candidate in canonical_times):
            add_evidence(record, kind="LEGACY_EVIDENCE", source=source, occurred=occurred, legacy=True, ambiguous=True, detail="persisted_field_without_matching_canonical_evidence")


def finalize_evidence(record: dict[str, Any]) -> None:
    evidence = record["evidence"]
    a_times = [e["occurred"] for e in evidence if e["current_stop"] and e["occurred"]]
    b_times = [e["occurred"] for e in evidence if e["human_attempt"] and e["occurred"]]
    record["a_stop_at"] = min(a_times) if a_times else None
    record["b_stop_at"] = min(b_times) if b_times else None
    all_stop_times = [value for value in (record["a_stop_at"], record["b_stop_at"]) if value]
    record["b_effective_stop_at"] = min(all_stop_times) if all_stop_times else None
    record["human_attempt_before_expiry"] = bool(record["deadline"] and any(e["human_attempt"] and e["occurred"] <= record["deadline"] for e in evidence if e["occurred"]))
    record["human_attempt_after_expiry"] = bool(record["deadline"] and any(e["occurred"] > record["deadline"] for e in evidence if e["human_attempt"]))
    record["valid_stop_before_expiry"] = bool(record["deadline"] and record["a_stop_at"] and record["a_stop_at"] <= record["deadline"])
    record["valid_stop_after_expiry"] = bool(record["deadline"] and record["a_stop_at"] and record["a_stop_at"] > record["deadline"])
    record["human_activity_after_expiry"] = record["human_attempt_after_expiry"]
    record["only_status_contacted"] = record["stage"] == "CONTACTED" and not any(e["human_attempt"] or e["current_stop"] or e["automatic"] for e in evidence)
    record["only_automatic_activity"] = bool(evidence) and not any(e["human_attempt"] or e["current_stop"] for e in evidence) and all(e["automatic"] for e in evidence)
    record["ambiguous_evidence"] = any(e["ambiguous"] for e in evidence)
    record["legacy_evidence"] = any(e["legacy"] for e in evidence)
    record["cycle_mismatch_evidence"] = any(not e["cycle_match"] for e in evidence)
    threshold_reached = record["elapsed"] is not None and record["elapsed"] >= record["threshold"]
    # Exact current evaluator behavior: any canonical SLA-stop result/event
    # removes the cycle from the active expired set. A late human action is
    # still retained as an SLA-compliance risk, but it is not a current alert
    # candidate under the productive evaluator.
    record["a_expired"] = bool(threshold_reached and not record["a_stop_at"])
    record["b_expired"] = bool(threshold_reached and not (
        record["deadline"] and record["b_effective_stop_at"] and record["b_effective_stop_at"] <= record["deadline"]
    ))
    record["current_origin"] = contacted_origin(record)


def contacted_origin(record: dict[str, Any]) -> str:
    evidence = record["evidence"]
    if any(e["current_stop"] for e in evidence):
        return "VALID_MANAGEMENT_CANONICAL"
    if any(e["human_attempt"] for e in evidence):
        return "HUMAN_OUTREACH_NOT_SLA_STOP"
    if any(e["automatic"] for e in evidence):
        return "SYSTEM_GENERATED"
    if any(e["legacy"] for e in evidence):
        return "LEGACY_EVIDENCE"
    if any(e["ambiguous"] for e in evidence):
        return "UNKNOWN"
    return "STATUS_ONLY_NO_EVIDENCE"


def enrich_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    idx = index_data(data)
    records = []
    for cycle in data["cycles"]:
        record = record_base(cycle, data, idx)
        attach_evidence(record, data, idx)
        finalize_evidence(record)
        record["is_strict_active"] = cycle.get("cycle_status") == "active" and cycle.get("unassigned_at") is None
        record["is_post_cutover"] = bool(
            record["assigned_at"] and record["sla_started_at"]
            and record["assigned_at"] >= coerce_utc_datetime(INSTRUMENTATION_CUTOVER)
            and record["sla_started_at"] >= CUTOVER_AT
        )
        record["is_current_policy_active"] = bool(
            record["is_strict_active"] and record["is_post_cutover"]
            and record["lead"] is not None and record["canonical"]
            and not record["excluded_origin"]
        )
        records.append(record)
    return records


def current_policy_active(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # One current active cycle per lead is expected. If data exposes more than
    # one, retain the latest factual assignment for analysis and flag risk.
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["is_current_policy_active"] and record["lead_id"]:
            grouped[record["lead_id"]].append(record)
    selected = []
    for lead_id, values in grouped.items():
        values.sort(key=lambda r: r["assigned_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        selected.append(values[0])
    return selected


def legacy_expired(records: list[dict[str, Any]], excluded_lead_ids: set[str]) -> list[dict[str, Any]]:
    candidates = [
        r for r in records
        if r["is_strict_active"] and r["lead"] is not None and not r["closed_lead"]
        and r["sla_started_at"] and r["sla_started_at"] < CUTOVER_AT
        and r["elapsed"] is not None and r["elapsed"] >= r["threshold"]
        and not r["a_stop_at"]
        and not r["excluded_origin"]
        and r["lead_id"] not in excluded_lead_ids
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in candidates:
        grouped[record["lead_id"]].append(record)
    selected = []
    for values in grouped.values():
        values.sort(key=lambda r: r["assigned_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        selected.append(values[0])
    return selected


def territory_for(record: dict[str, Any], data: dict[str, Any], backlog_by_owner: Counter[str]) -> dict[str, Any]:
    active_users = [
        user for user in data["users"]
        if user.get("is_active") is True and text(user.get("rol")) == "agente"
    ]
    if not record["property_code"]:
        return {"classification": "TERRITORY_UNKNOWN", "reason": "property_code_missing", "candidates": []}
    if not record["property_found"]:
        return {"classification": "PROPERTY_DATA_MISSING", "reason": "property_not_found", "candidates": []}
    classification, candidates, reason = router_candidates(record, active_users, data["properties"])
    if classification == "DATA_INSUFFICIENT":
        classification = "PROPERTY_DATA_MISSING"
    details = []
    for candidate in candidates:
        name = text(candidate.get("nombre"))
        details.append({
            "id": text(candidate.get("_id")),
            "name": name,
            "backlog": backlog_by_owner.get(norm(name), 0),
        })
    return {"classification": classification, "reason": reason, "candidates": details}


def classify_final(record: dict[str, Any], territory: dict[str, Any] | None) -> str:
    if record.get("legacy_not_eligible"):
        return "LEGACY_NOT_ELIGIBLE"
    if not record.get("a_expired"):
        return "NOT_ACTUALLY_EXPIRED"
    if (
        record["owner_state"] != "OWNER_OK"
        or record["cycle_state"] != "CYCLE_OK"
        or record["cycle_mismatch_evidence"]
        or record["ambiguous_evidence"]
        or not record["owner_active"]
    ):
        return "DATA_OR_CYCLE_ISSUE"
    if record["human_attempt_before_expiry"] or record["human_attempt_after_expiry"]:
        return "PROTECTED_BY_MANAGEMENT"
    if territory and territory["classification"] == "HAS_TERRITORIAL_CANDIDATES":
        return "SAFE_TO_SHADOW_REASSIGN"
    if territory and territory["classification"] == "NO_TERRITORIAL_CANDIDATE":
        return "EXPIRED_NO_ALTERNATIVE"
    return "DATA_OR_CYCLE_ISSUE"


def prepare_analysis(data: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    active = [record for record in current_policy_active(records) if not record["closed_lead"]]
    policy_expired = [r for r in active if r["a_expired"] and not r["closed_lead"]]
    policy_not_expired = [r for r in active if not r["a_expired"] and not r["closed_lead"]]
    legacy = legacy_expired(records, {record["lead_id"] for record in active})
    backlog_by_owner = Counter(
        norm(r["owner_name"]) for r in active
        if not r["closed_lead"] and not r["a_stop_at"]
    )
    # Territory is evaluated only after the evidence/owner/cycle filters. A
    # protected lead is not a future reassignment candidate, so its territory
    # is deliberately left unevaluated.
    for record in policy_expired:
        record["territory"] = None
        if (
            record["owner_state"] == "OWNER_OK"
            and record["cycle_state"] == "CYCLE_OK"
            and record["owner_active"]
            and not record["cycle_mismatch_evidence"]
            and not record["ambiguous_evidence"]
            and not record["human_attempt_before_expiry"]
            and not record["human_attempt_after_expiry"]
        ):
            record["territory"] = territory_for(record, data, backlog_by_owner)
        record["final_category"] = classify_final(record, record["territory"])
    for record in policy_not_expired:
        record["final_category"] = "NOT_ACTUALLY_EXPIRED"
        record["territory"] = None
    for record in legacy:
        record["legacy_not_eligible"] = True
        record["final_category"] = "LEGACY_NOT_ELIGIBLE"
        record["territory"] = None
    return {
        "active": active,
        "policy_expired": policy_expired,
        "policy_not_expired": policy_not_expired,
        "legacy": legacy,
        "backlog_by_owner": backlog_by_owner,
    }


def evidence_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    current = records
    return {
        "none_human": sum(not any(e["human_attempt"] for e in r["evidence"]) for r in current),
        "attempt_before": sum(r["human_attempt_before_expiry"] for r in current),
        "attempt_after": sum(r["human_attempt_after_expiry"] for r in current),
        "valid_before": sum(r["valid_stop_before_expiry"] for r in current),
        "valid_after": sum(r["valid_stop_after_expiry"] for r in current),
        "only_status": sum(r["only_status_contacted"] for r in current),
        "only_auto": sum(r["only_automatic_activity"] for r in current),
        "ambiguous": sum(r["ambiguous_evidence"] for r in current),
        "contacted_origin": Counter(r["current_origin"] for r in current if r["stage"] == "CONTACTED"),
    }


def evidence_table(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, set[tuple[str, str]]] = defaultdict(set)
    before_by_kind: dict[str, set[tuple[str, str]]] = defaultdict(set)
    after_by_kind: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for record in records:
        key = (record["lead_id"], record["cycle_id"])
        for evidence in record["evidence"]:
            if evidence["occurred"] is None:
                continue
            kind = evidence["kind"]
            if evidence["current_stop"]:
                kind = "VALID_MANAGEMENT_CANONICAL"
            buckets[kind].add(key)
            if record["deadline"] and evidence["occurred"] <= record["deadline"]:
                before_by_kind[kind].add(key)
            elif record["deadline"]:
                after_by_kind[kind].add(key)
    rows = []
    for kind in sorted(set(buckets) | {"VALID_MANAGEMENT_CANONICAL"}):
        rows.append({
            "tipo_evidencia": kind,
            "casos": len(buckets[kind]),
            "antes_sla": len(before_by_kind[kind]),
            "despues_sla": len(after_by_kind[kind]),
        })
    return rows


def counterfactual_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    a_expired = sum(r["a_expired"] for r in records)
    b_expired = sum(r["b_expired"] for r in records)
    a_protected = sum(r["valid_stop_before_expiry"] for r in records)
    b_protected = sum(bool(r["b_effective_stop_at"] and r["deadline"] and r["b_effective_stop_at"] <= r["deadline"]) for r in records)
    return [
        {"escenario": "A_REGLA_ACTUAL", "vencidos": a_expired, "protegidos": a_protected, "diferencia": 0},
        {"escenario": "B_INTENTO_HUMANO_AUDITABLE", "vencidos": b_expired, "protegidos": b_protected, "diferencia": b_expired - a_expired},
    ]


def csv_row(record: dict[str, Any]) -> dict[str, Any]:
    territory = record.get("territory") or {}
    candidates = territory.get("candidates") or []
    return {
        "lead_id": record["lead_id"],
        "assignment_cycle_id": record["cycle_id"],
        "assigned_to_user_id": record["owner_id"],
        "assigned_to_display_name": record["owner_name"],
        "assigned_at": local_iso(record["assigned_at"]),
        "sla_started_at_efectivo": local_iso(record["sla_started_at"]),
        "sla_started_at_source": record["sla_start_source"],
        "temperature": record["temperature"],
        "sla_threshold_minutes": record["threshold"],
        "current_business_minutes_elapsed": fmt(record["elapsed"]),
        "sla_expired_at_calculado": local_iso(record["deadline"]),
        "pipeline_stage_actual": record["stage"],
        "origen_normalizado": record["origin"],
        "operacion_normalizada": record["operation"],
        "comuna_normalizada": norm(record["comuna"]),
        "region_normalizada": norm(record["region"]),
        "propiedad_asociada": record["property_code"],
        "owner_actual_lead": record["lead_owner_name"],
        "owner_cycle_state": record["owner_state"],
        "cycle_state": record["cycle_state"],
        "active_cycle_count": record["active_cycle_count"],
        "active_cycle_ids": record["active_cycle_ids"],
        "cycle_reopened": "yes" if record["cycle_reopened"] else "no",
        "human_evidence_before_expiry": "yes" if record["human_attempt_before_expiry"] else "no",
        "human_evidence_after_expiry": "yes" if record["human_attempt_after_expiry"] else "no",
        "valid_management_before_expiry": "yes" if record["valid_stop_before_expiry"] else "no",
        "valid_management_after_expiry": "yes" if record["valid_stop_after_expiry"] else "no",
        "scenario_a_expired": "yes" if record["a_expired"] else "no",
        "scenario_b_expired": "yes" if record["b_expired"] else "no",
        "scenario_b_switch_event": (record["b_stop_at"] or "").isoformat() if record["b_stop_at"] and record["b_stop_at"] != record["a_stop_at"] else "",
        "contacted_origin": record["current_origin"],
        "ambiguous_evidence": "yes" if record["ambiguous_evidence"] else "no",
        "candidate_classification": territory.get("classification", "NOT_EVALUATED"),
        "candidate_count": len(candidates),
        "candidate_names": "; ".join(item["name"] for item in candidates),
        "candidate_backlogs": "; ".join(f"{item['name']}={item['backlog']}" for item in candidates),
        "final_category": record.get("final_category", ""),
    }


def contacted_csv_row(record: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(e["kind"] for e in record["evidence"])
    return {
        "lead_id": record["lead_id"],
        "assignment_cycle_id": record["cycle_id"],
        "assigned_to_display_name": record["owner_name"],
        "stage_actual": record["stage"],
        "contacted_origin": record["current_origin"],
        "evidence_count": len(record["evidence"]),
        "valid_management_canonical": counts["VALID_MANAGEMENT_CANONICAL"],
        "human_outreach_not_sla_stop": counts["HUMAN_OUTREACH_NOT_SLA_STOP"],
        "status_only_no_evidence": counts["STATUS_ONLY_NO_EVIDENCE"],
        "system_generated": counts["SYSTEM_GENERATED"],
        "legacy_evidence": counts["LEGACY_EVIDENCE"],
        "open_click_not_attempt": counts["OPEN_CLICK_NOT_ATTEMPT"],
        "client_inbound_event_count": counts["INBOUND_CLIENT_MESSAGE"],
        "ambiguous_human_evidence": counts["AMBIGUOUS_HUMAN_EVIDENCE"],
        "human_attempt_before_expiry": "yes" if record["human_attempt_before_expiry"] else "no",
        "human_attempt_after_expiry": "yes" if record["human_attempt_after_expiry"] else "no",
        "only_status_contacted": "yes" if record["only_status_contacted"] else "no",
        "only_automatic_activity": "yes" if record["only_automatic_activity"] else "no",
        "cycle_mismatch_evidence": "yes" if record["cycle_mismatch_evidence"] else "no",
    }


def counterfactual_csv_row(record: dict[str, Any]) -> dict[str, Any]:
    a_event = next((e for e in record["evidence"] if e["current_stop"] and e["occurred"] == record["a_stop_at"]), None)
    b_event = next((e for e in record["evidence"] if e["human_attempt"] and e["occurred"] == record["b_stop_at"]), None)
    return {
        "lead_id": record["lead_id"],
        "assignment_cycle_id": record["cycle_id"],
        "scenario_a_status": "EXPIRED" if record["a_expired"] else ("PROTECTED" if record["valid_stop_before_expiry"] else "NOT_EXPIRED"),
        "scenario_a_stop_at": local_iso(record["a_stop_at"]),
        "scenario_a_stop_source": a_event["source"] if a_event else "",
        "scenario_b_status": "EXPIRED" if record["b_expired"] else ("PROTECTED" if record["b_effective_stop_at"] and record["deadline"] and record["b_effective_stop_at"] <= record["deadline"] else "NOT_EXPIRED"),
        "scenario_b_human_attempt_at": local_iso(record["b_stop_at"]),
        "scenario_b_event": b_event["source"] if b_event else "",
        "changed_between_scenarios": "yes" if record["a_expired"] != record["b_expired"] else "no",
        "change_reason": b_event["source"] if record["a_expired"] != record["b_expired"] and b_event else "",
        "human_attempt_before_expiry": "yes" if record["human_attempt_before_expiry"] else "no",
        "human_attempt_after_expiry": "yes" if record["human_attempt_after_expiry"] else "no",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value).replace("|", "/") for value in row) + " |" for row in rows)
    return "\n".join(lines)


def build_report(data: dict[str, Any], records: list[dict[str, Any]], analysis: dict[str, Any]) -> str:
    target = analysis["policy_expired"]
    current_contacted = [r for r in analysis["active"] if r["stage"] == "CONTACTED"]
    es = evidence_summary(target)
    evidence_rows = evidence_table(target)
    # The requested counterfactual is explicitly over the reconstructed
    # CURRENT_POLICY_ACTIVE_EXPIRED population, not over non-expired active
    # leads or over legacy cycles.
    cf_rows = counterfactual_rows(target)
    final_order = [
        "SAFE_TO_SHADOW_REASSIGN", "EXPIRED_NO_ALTERNATIVE", "PROTECTED_BY_MANAGEMENT",
        "DATA_OR_CYCLE_ISSUE", "NOT_ACTUALLY_EXPIRED", "LEGACY_NOT_ELIGIBLE",
    ]
    final_counts = Counter()
    for record in target:
        final_counts[record["final_category"]] += 1
    for record in analysis["legacy"]:
        final_counts["LEGACY_NOT_ELIGIBLE"] += 1
    final_total = sum(final_counts.values())

    def territory_label(record: dict[str, Any]) -> str:
        territory = record.get("territory") or {}
        return territory.get("classification", "NO_EVALUATED")

    execs = defaultdict(lambda: {"expired": 0, "none": 0, "protected": 0, "safe": 0, "no_alt": 0, "issue": 0})
    for record in target:
        bucket = execs[record["owner_name"]]
        bucket["expired"] += 1
        if not any(e["human_attempt"] for e in record["evidence"]):
            bucket["none"] += 1
        if record["final_category"] == "PROTECTED_BY_MANAGEMENT":
            bucket["protected"] += 1
        if record["final_category"] == "SAFE_TO_SHADOW_REASSIGN":
            bucket["safe"] += 1
        if record["final_category"] == "EXPIRED_NO_ALTERNATIVE":
            bucket["no_alt"] += 1
        if record["final_category"] == "DATA_OR_CYCLE_ISSUE":
            bucket["issue"] += 1

    territorial_groups = defaultdict(lambda: {"safe": 0, "candidate": 0, "no_candidate": 0})
    for record in target:
        if record["final_category"] not in {"SAFE_TO_SHADOW_REASSIGN", "EXPIRED_NO_ALTERNATIVE"}:
            continue
        key = f"{record['comuna']} / {record['region']}"
        territorial_groups[key]["candidate"] += 1
        if record["final_category"] == "SAFE_TO_SHADOW_REASSIGN":
            territorial_groups[key]["safe"] += 1
        else:
            territorial_groups[key]["no_candidate"] += 1

    lines = [
        "# AUDITORÍA DE ELEGIBILIDAD DE REASIGNACIÓN SLA CRM — 2026-09-09",
        "",
        f"Fecha de corte: **{local_iso(data['as_of'])}**. Ejecución exclusivamente analítica y de lectura.",
        "",
        "## 1. Alcance y definiciones productivas",
        "",
        "Se reconstruyó el universo desde `leads`, `crm_assignment_cycles`, `crm_events`, `crm_management_results`, `usuarios`, mensajes del lead y la colección de propiedades asociada. Se aplicaron las condiciones del evaluador productivo: ciclo activo (`cycle_status=active`, `unassigned_at=None`), cutover vigente, lead abierto, origen/test excluido y cálculo con `calculate_sla()` y minutos hábiles Chile.",
        "",
        f"La población objetivo reconstruida bajo regla actual es **{len(target):,} leads/ciclos**. El dato anterior de 202 no se hardcodeó.",
        "",
        "Para el contrafactual B, un envío de WhatsApp/email con actor humano y ciclo trazable se considera un intento humano iniciado, aunque el evento no pruebe entrega del proveedor. Aperturas, clicks, chatbot, mensajes `assistant/system` y cambios de estado no se consideran intento humano.",
        "",
        "## 2. A. Universo",
        "",
        md_table(["Clasificación", "Cantidad"], [
            ["CURRENT_POLICY_ACTIVE", len(analysis["active"])],
            ["CURRENT_POLICY_ACTIVE_EXPIRED", len(target)],
            ["CURRENT_POLICY_NOT_EXPIRED", len(analysis["policy_not_expired"])],
            ["LEGACY_NOT_ELIGIBLE", len(analysis["legacy"])],
        ]),
        "",
        "Los ciclos legacy se muestran solo para trazabilidad histórica y no entran en la población candidata.",
        "",
        "## 3. B. Evidencia de `CONTACTED` y de los vencidos",
        "",
        f"Se auditaron **{len(current_contacted):,} ciclos actuales en `CONTACTED`**. Su origen de estado fue: "
        + "; ".join(f"{key}: {value}" for key, value in sorted(Counter(r["current_origin"] for r in current_contacted).items())) + ".",
        "",
        md_table(["Tipo evidencia", "Casos", "Antes SLA", "Después SLA"], [
            [row["tipo_evidencia"], row["casos"], row["antes_sla"], row["despues_sla"]]
            for row in evidence_rows
        ] or [["Sin evidencia temporal", 0, 0, 0]]),
        "",
        "Interpretación de eventos: `VALID_MANAGEMENT_CANONICAL` detiene el SLA actual; `HUMAN_OUTREACH_NOT_SLA_STOP` es actividad humana auditable que hoy no lo detiene; `OPEN_CLICK_NOT_ATTEMPT` es apertura/click; `SYSTEM_GENERATED` incluye chatbot y mensajes automáticos; `STATUS_ONLY_NO_EVIDENCE` es cambio de estado sin gestión demostrable; `LEGACY_EVIDENCE` es un campo persistido sin evidencia canónica coincidente.",
        "",
        "## 4. Diferenciación operacional",
        "",
        md_table(["Tipo", "Cuenta como apertura/visualización", "Intento humano", "Detiene SLA actual", "Evidencia"], [
            ["CLICK / OPEN_DETAIL / PAGE_VIEW", "Sí", "No", "No", "Evento de UI"],
            ["SEND_WA_LEAD / SEND_EMAIL_LEAD con actor humano", "No", "Sí, iniciado y trazable", "No", "Evento directo con actor y ciclo"],
            ["CALL_COMPLETED_LEAD con actor humano", "No", "Sí", "No por sí solo", "Acción de llamada"],
            ["MESSAGE_SENT_WAITING_RESPONSE", "No", "Sí si actor humano", "No", "Resultado CRM actual OUTREACH"],
            ["EMAIL_SENT", "No", "Sí si actor humano", "No", "Resultado CRM actual OUTREACH"],
            ["CALL_NO_ANSWER / EFFECTIVE_CONTACT / FOLLOW_UP_REQUESTED / INVALID_NUMBER / NOT_INTERESTED", "No", "Sí", "Sí", "`SLA_STOP_RESULTS`"],
            ["HUMAN_NOTE / GESTION_LOG / MANUAL_ENTRY con resultado válido", "No", "Sí", "Sí", "`event_evidence()` productivo"],
            ["STATUS_CHANGE a CONTACTED", "No", "No", "No", "Solo cambio de estado"],
            ["BOT_MSG / messages assistant/system", "No", "No", "No", "Actividad automática"],
            ["messages user / msg_in", "No", "No del ejecutivo", "No", "Mensaje entrante del cliente"],
        ]),
        "",
        "## 5. 4. Análisis de los vencidos actuales",
        "",
        md_table(["Medición", "Casos"], [
            ["Ninguna evidencia humana", es["none_human"]],
            ["Intento humano antes de vencer", es["attempt_before"]],
            ["Intento humano después de vencer", es["attempt_after"]],
            ["Gestión válida antes de vencer", es["valid_before"]],
            ["Gestión válida después de vencer", es["valid_after"]],
            ["Solo estado CONTACTED", es["only_status"]],
            ["Solo actividad automática", es["only_auto"]],
            ["Evidencia ambigua", es["ambiguous"]],
            ["Vencidos solo por no contar outreach humano en SLA_STOP_RESULTS", sum(bool(r["a_expired"] and r["b_stop_at"] and r["deadline"] and r["b_stop_at"] <= r["deadline"] and not r["a_stop_at"]) for r in target)],
        ]),
        "",
        "Pregunta A: los casos sin evidencia humana antes del vencimiento son la base de seguridad, pero solo llegan a `SAFE_TO_SHADOW_REASSIGN` si owner/ciclo y territorio también son consistentes. Pregunta B: cualquier intento humano auditable, incluso posterior al vencimiento, se marca como protegido para no retirarlo automáticamente. Pregunta C: se informa sin modificar `SLA_STOP_RESULTS`.",
        "",
        "## 6. C. Contrafactual",
        "",
        md_table(["Escenario", "Vencidos", "Protegidos", "Diferencia vs A"], [
            [row["escenario"], row["vencidos"], row["protegidos"], row["diferencia"]]
            for row in cf_rows
        ]),
        "",
        f"La diferencia exacta entre A y B es **{cf_rows[1]['vencidos'] - cf_rows[0]['vencidos']:+,} vencidos**. Los cambios individuales y el evento que los provoca están en `sla_stop_counterfactual.csv`.",
        "",
        "## 7. 6. Temporalidad: cumplimiento vs protección",
        "",
        "El KPI SLA se mantiene basado en el primer stop canónico y conserva incumplimiento cuando la gestión ocurrió después de la fecha calculada de vencimiento. La protección de reasignación es una dimensión separada: un intento humano posterior al vencimiento no corrige el KPI, pero impide certificar el lead como seguro para retirar.",
        "",
        "## 8. 7. Owner y ciclo",
        "",
        md_table(["Estado", "Casos en vencidos actuales"], [
            ["OWNER_OK", sum(r["owner_state"] == "OWNER_OK" for r in target)],
            ["OWNER_MISMATCH", sum(r["owner_state"] == "OWNER_MISMATCH" for r in target)],
            ["CYCLE_OK", sum(r["cycle_state"] == "CYCLE_OK" for r in target)],
            ["CYCLE_MISMATCH", sum(r["cycle_state"] == "CYCLE_MISMATCH" for r in target)],
            ["CONCURRENT_ACTIVITY_RISK", sum(r["cycle_state"] == "CONCURRENT_ACTIVITY_RISK" for r in target)],
            ["Cycle reabierto con historial previo", sum(r["cycle_reopened"] for r in target)],
            ["Evidencia ligada a otro ciclo", sum(r["cycle_mismatch_evidence"] for r in target)],
        ]),
        "",
        "No se reparó ningún owner, ciclo o estado.",
        "",
        "## 9. 8. Territorio de la población que sobrevivió filtros",
        "",
        md_table(["Clasificación territorial", "Cantidad"], [
            ["HAS_ALTERNATIVE_CANDIDATE", sum(r["final_category"] == "SAFE_TO_SHADOW_REASSIGN" for r in target)],
            ["NO_ALTERNATIVE_CANDIDATE", sum(r["final_category"] == "EXPIRED_NO_ALTERNATIVE" for r in target)],
            ["TERRITORY_UNKNOWN", sum((r.get("territory") or {}).get("classification") == "TERRITORY_UNKNOWN" for r in target)],
            ["PROPERTY_DATA_MISSING", sum((r.get("territory") or {}).get("classification") == "PROPERTY_DATA_MISSING" for r in target)],
            ["NO EVALUADO: protegido o issue", sum(r["final_category"] in {"PROTECTED_BY_MANAGEMENT", "DATA_OR_CYCLE_ISSUE"} for r in target)],
        ]),
        "",
        "Solo se reconstruyeron candidatos para leads sin gestión protegida y sin issue de owner/ciclo/evidencia. No se eligió ganador. Se usaron únicamente las reglas de lectura existentes del router; no se llamó al round-robin porque muta estado.",
        "",
        "### Cobertura por comuna/región",
        "",
        md_table(["Comuna/región", "Safe shadow", "Con candidato", "Sin candidato"], [
            [key, value["safe"], value["candidate"], value["no_candidate"]]
            for key, value in sorted(territorial_groups.items(), key=lambda item: (-item[1]["candidate"], norm(item[0])))
        ] or [["Sin casos territoriales evaluables", 0, 0, 0]]),
        "",
        "## 10. D. Elegibilidad final",
        "",
        md_table(["Categoría", "Cantidad", "%"], [
            [category, final_counts[category], f"{pct(final_counts[category], final_total):.1f}%"]
            for category in final_order
        ] + [["TOTAL", final_total, "100.0%"]]),
        "",
        "Regla de exclusión: ningún legacy puede ser `SAFE_TO_SHADOW_REASSIGN`; ningún caso con gestión humana, owner/ciclo inconsistente o evidencia ambigua fue certificado como safe.",
        "",
        "## 11. E. Resumen por ejecutivo",
        "",
        md_table(["Ejecutivo", "Vencidos actuales", "Sin evidencia humana", "Protegidos", "Safe shadow", "Sin candidato", "Data/cycle issue"], [
            [name, value["expired"], value["none"], value["protected"], value["safe"], value["no_alt"], value["issue"]]
            for name, value in sorted(execs.items(), key=lambda item: norm(item[0]))
        ]),
        "",
        "## 12. Consistencia final",
        "",
        f"Suma de categorías finales: **{final_total}**, igual a {len(target)} vencidos bajo política vigente + {len(analysis['legacy'])} legacy. Leads/ciclos duplicados en categorías finales: 0, por selección de un solo ciclo actual por lead. Legacy certificados como safe: 0. Gestión protegida certificada como safe: 0. Safe sin candidato territorial: 0.",
        "",
        "Ninguna evidencia automática fue considerada humana. Los tiempos usan `calculate_sla()` y minutos hábiles productivos de Chile.",
        "",
        "## 13. Limitaciones y bloqueos para Fase 1",
        "",
        "- El envío humano de WhatsApp/email está registrado como acción iniciada, pero muchos eventos no tienen confirmación de entrega del proveedor; por eso se conserva esa distinción en los CSV.",
        "- El sistema no contiene distancia, ETA, oficina ni coordenadas para agregar una regla territorial nueva.",
        "- La elegibilidad safe es un pool sombra; no es ranking ni selección de ganador.",
        "- No se corrigieron los casos de owner/ciclo/evidencia inconsistente.",
        "",
        "## 14. Seguridad de la ejecución",
        "",
        "- MongoDB writes: **0**",
        "- Reasignaciones: **0**",
        "- Cambios de owner, ciclos, estados, SLA, prompts, flags o variables de entorno: **0**",
        "- Deploy: **NO**",
        "- Llamadas a proveedores externos: **0**",
        "",
        "## 15. Archivos generados",
        "",
        "- `docs/auditoria_sla_data/reassignment_eligibility_current_policy.csv`",
        "- `docs/auditoria_sla_data/contacted_evidence_audit.csv`",
        "- `docs/auditoria_sla_data/sla_stop_counterfactual.csv`",
        "- `scripts/run_phase05_crm_reassignment_audit.py`",
        "",
        "Los artefactos no contienen teléfonos completos, mensajes completos ni PII innecesaria.",
        "",
    ]
    return "\n".join(lines)


def consistency_checks(analysis: dict[str, Any]) -> None:
    target = analysis["policy_expired"]
    target_lead_ids = {record["lead_id"] for record in target}
    legacy_lead_ids = {record["lead_id"] for record in analysis["legacy"]}
    if len(target_lead_ids) != len(target) or len(legacy_lead_ids) != len(analysis["legacy"]):
        raise RuntimeError("Lead duplicado en el universo final")
    if target_lead_ids & legacy_lead_ids:
        raise RuntimeError("Lead presente en política vigente y legacy")
    final_categories = [record["final_category"] for record in target] + ["LEGACY_NOT_ELIGIBLE"] * len(analysis["legacy"])
    allowed = {
        "SAFE_TO_SHADOW_REASSIGN", "EXPIRED_NO_ALTERNATIVE", "PROTECTED_BY_MANAGEMENT",
        "DATA_OR_CYCLE_ISSUE", "NOT_ACTUALLY_EXPIRED", "LEGACY_NOT_ELIGIBLE",
    }
    if any(category not in allowed for category in final_categories):
        raise RuntimeError("Categoría final fuera del contrato")
    if len(final_categories) != len(set((r["lead_id"], r["cycle_id"]) for r in target)) + len(analysis["legacy"]):
        raise RuntimeError("Duplicación inesperada en categorías finales")
    if any(r["final_category"] == "SAFE_TO_SHADOW_REASSIGN" and r.get("legacy_not_eligible") for r in target):
        raise RuntimeError("Legacy certificado como safe")
    if any(r["final_category"] == "SAFE_TO_SHADOW_REASSIGN" and (r["human_attempt_before_expiry"] or r["human_attempt_after_expiry"]) for r in target):
        raise RuntimeError("Gestión protegida certificada como safe")
    if any(r["final_category"] == "SAFE_TO_SHADOW_REASSIGN" and (not r.get("territory") or r["territory"].get("classification") != "HAS_TERRITORIAL_CANDIDATES") for r in target):
        raise RuntimeError("Safe sin candidato territorial")
    if any(r["final_category"] == "SAFE_TO_SHADOW_REASSIGN" and (r["owner_state"] != "OWNER_OK" or r["cycle_state"] != "CYCLE_OK" or r["cycle_mismatch_evidence"] or r["ambiguous_evidence"]) for r in target):
        raise RuntimeError("Safe con issue de owner/ciclo/evidencia")


def main() -> None:
    # The report is the output of this phase-specific script. Re-running the
    # read-only audit refreshes only these local analytical artifacts.
    data = load_data()
    records = enrich_records(data)
    analysis = prepare_analysis(data, records)
    consistency_checks(analysis)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(DATA_DIR / "reassignment_eligibility_current_policy.csv", [csv_row(record) for record in analysis["policy_expired"]])
    contacted = [record for record in analysis["active"] if record["stage"] == "CONTACTED"]
    write_csv(DATA_DIR / "contacted_evidence_audit.csv", [contacted_csv_row(record) for record in contacted])
    write_csv(DATA_DIR / "sla_stop_counterfactual.csv", [counterfactual_csv_row(record) for record in analysis["policy_expired"]])
    REPORT.write_text(build_report(data, records, analysis), encoding="utf-8")
    final_counts = Counter(record["final_category"] for record in analysis["policy_expired"])
    print("FASE 0.5 AUDIT COMPLETED")
    print(f"as_of_chile={local_iso(data['as_of'])}")
    print(f"current_policy_active={len(analysis['active'])}")
    print(f"current_policy_active_expired={len(analysis['policy_expired'])}")
    print(f"legacy_not_eligible={len(analysis['legacy'])}")
    print(f"contacted_audited={len(contacted)}")
    print(f"final_categories={dict(final_counts)}")
    print("MongoDB writes = 0")
    print("Reassignments executed = 0")
    print("Deploy = NO")
    print("Flags modified = 0")
    print(f"report={REPORT}")
    print(f"data_dir={DATA_DIR}")


if __name__ == "__main__":
    main()
