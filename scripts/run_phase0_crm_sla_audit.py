"""Phase 0 quantitative CRM SLA audit.

This script is deliberately read-only against MongoDB. It creates local Markdown
and CSV artifacts only; it does not import the application startup or any
assignment worker. No MongoDB write method is called here.

Usage:
    python scripts/run_phase0_crm_sla_audit.py
"""
from __future__ import annotations

import csv
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from pymongo import MongoClient, ReadPreference

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config
from chatbot.constants import BUSINESS_DAYS, BUSINESS_END_HOUR, BUSINESS_START_HOUR, CHILE_TZ
from chatbot.crm_metrics import (
    calculate_sla,
    coerce_utc_datetime,
    commercial_sla_start_at,
    event_evidence,
    normalize_result,
)
from chatbot.crm_sla_alert_evaluator import (
    CLOSED_STAGES,
    EXCLUDED_ORIGINS,
    SLA_STOP_RESULTS,
    SYNTHETIC_PHONES,
    _is_test_lead,
)
from chatbot.crm_sla_alert_settings import CUTOVER_AT
from chatbot.crm_sla_alert_evaluator import add_business_minutes
from chatbot.utils import calculate_business_minutes


OUTPUT_REPORT = ROOT / "docs" / "AUDITORIA_CUANTITATIVA_SLA_CRM_20260909.md"
OUTPUT_DIR = ROOT / "docs" / "auditoria_sla_data"
WINDOWS = (30, 60, 90)
UNASSIGNED_NAMES = {"", "no asignado", "sin asignar", "no_assigned", "none", "null"}
OUR_TEAM = (
    "Erika Garrido",
    "Mariela Arriagada",
    "María Paz Galleguillos",
    "Hernán Castro",
    "Susana Ensignia",
    "Raquel Cheneaux",
    "Paula Morales",
    "Rocío Aliaga",
)
TEMPORARILY_INACTIVE = {"raquel cheneaux"}
ROUND_ROBIN_RM = ("Mariela Arriagada", "Hernán Castro", "María Paz Galleguillos")
MARIELA_PRIORITY_COMUNAS = {
    "macul", "nunoa", "providencia", "las condes", "santiago",
}


def clean(value: Any) -> str:
    return str(value or "").strip()


def norm(value: Any) -> str:
    text = clean(value).lower()
    text = "".join(
        char for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )
    return re.sub(r"\s+", " ", text).strip()


def path_get(document: dict, path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def first_value(document: dict, paths: Iterable[str]) -> Any:
    for path in paths:
        value = path_get(document, path)
        if value not in (None, "", [], {}):
            return value
    return None


def first_text(document: dict, paths: Iterable[str]) -> str:
    return clean(first_value(document, paths))


def parse_dt(value: Any) -> datetime | None:
    return coerce_utc_datetime(value)


def local_dt(value: datetime | None) -> str:
    if not value:
        return ""
    return value.astimezone(CHILE_TZ).isoformat()


def iso(value: Any) -> str:
    parsed = parse_dt(value)
    return parsed.isoformat() if parsed else clean(value)


def pct(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) * 100.0 / float(denominator), 1)


def fmt_pct(value: Any) -> str:
    return "N/D" if value is None else f"{value:.1f}%"


def fmt_num(value: Any, decimals: int = 1) -> str:
    if value is None:
        return "N/D"
    if isinstance(value, float) and math.isfinite(value):
        return f"{value:.{decimals}f}"
    return str(value)


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        result = ordered[lower]
    else:
        fraction = position - lower
        result = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(result, 1)


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def sample_note(n: int) -> str:
    if n == 0:
        return "sin observaciones"
    if n == 1:
        return "1 observación: no permite evaluar una distribución"
    if n < 5:
        return f"{n} observaciones: P90/P95 tienen estabilidad limitada"
    return f"{n} observaciones: revisar junto con el volumen total"


def is_nonempty(value: Any) -> bool:
    return value not in (None, "", [], {}, "N/A", "N/D")


def is_assigned_name(value: Any) -> bool:
    return norm(value) not in UNASSIGNED_NAMES


def stage_of(lead: dict) -> str:
    return clean(
        first_value(lead, ("pipeline_stage", "stage", "crm_estado"))
        or ""
    ).upper()


def lead_owner(lead: dict) -> str:
    return first_text(lead, ("ejecutivo_asignado", "prospecto.ejecutivo"))


def property_code_of(lead: dict) -> str:
    return first_text(
        lead,
        (
            "prospecto.codigo",
            "datos_propiedad.codigo",
            "property_code",
            "prospecto.codigo_propiedad",
            "prospecto.codigo_referencia",
            "prospecto.codigo_yapo",
            "prospecto.codigo_mercadolibre",
        ),
    )


def operation_of(lead: dict, prop: dict | None = None) -> str:
    value = first_text(
        lead,
        (
            "operacion",
            "prospecto.operacion",
            "prospecto.tipo_operacion",
            "datos_propiedad.operacion",
        ),
    )
    if not value and prop:
        value = first_text(prop, ("operacion", "tipo_operacion.tipo"))
        operation = prop.get("tipo_operacion") or {}
        if isinstance(operation, dict):
            venta = operation.get("venta") is True
            arriendo = operation.get("arriendo") is True
            if venta and arriendo:
                value = "Venta/Arriendo"
            elif venta:
                value = "Venta"
            elif arriendo:
                value = "Arriendo"
    return value or "No informado"


def origin_of(lead: dict) -> str:
    return first_text(
        lead,
        (
            "origen",
            "lead_origin",
            "origin",
            "source_type",
            "prospecto.canal_origen",
            "prospecto.origen",
            "prospecto.metodo_ingreso",
        ),
    ) or "No informado"


def comuna_of(lead: dict, prop: dict | None = None) -> str:
    value = first_text(
        lead,
        (
            "comuna",
            "prospecto.comuna",
            "prospecto.ubicacion.comuna",
            "datos_propiedad.comuna",
        ),
    )
    if not value and prop:
        value = first_text(prop, ("ubicacion.comuna", "comuna"))
    return value or "No informado"


def region_of(lead: dict, prop: dict | None = None) -> str:
    value = first_text(
        lead,
        (
            "region",
            "prospecto.region",
            "prospecto.ubicacion.region",
            "datos_propiedad.region",
        ),
    )
    if not value and prop:
        value = first_text(prop, ("ubicacion.region", "region"))
    return value or "No informado"


def phone_present(lead: dict) -> bool:
    value = first_value(lead, ("phone", "prospecto.phone", "whatsapp_phone"))
    return is_nonempty(value) and clean(value) not in SYNTHETIC_PHONES


def property_summary(lead: dict, properties: dict[str, dict]) -> dict:
    code = property_code_of(lead)
    prop = properties.get(clean(code)) if code else None
    return {
        "code": code,
        "doc": prop,
        "comuna": comuna_of(lead, prop),
        "region": region_of(lead, prop),
        "operation": operation_of(lead, prop),
    }


def user_name_match(left: Any, right: Any) -> bool:
    a = norm(left)
    b = norm(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return " ".join(a.split()[:2]) == " ".join(b.split()[:2])


def load_data() -> dict:
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI no está configurado")

    client = MongoClient(
        Config.MONGO_URI,
        read_preference=ReadPreference.SECONDARY_PREFERRED,
        socketTimeoutMS=15000,
        connectTimeoutMS=5000,
        serverSelectionTimeoutMS=10000,
    )
    client.admin.command("ping")
    db = client[Config.DB_NAME]

    lead_projection = {"messages": 0, "stage_history": 0}
    leads = list(db["leads"].find({}, lead_projection))
    cycles = list(db["crm_assignment_cycles"].find({}))
    users = list(db["usuarios"].find({}, {
        "_id": 1, "nombre": 1, "username": 1, "rol": 1, "is_active": 1,
        "telefono": 1, "tel": 1, "movil": 1, "comunas_interes": 1,
        "comunas_interes_norm": 1, "region": 1, "region_slug": 1,
        "oficina": 1, "office": 1, "zonas": 1,
    }))

    as_of = datetime.now(timezone.utc)
    start_90 = as_of - timedelta(days=90)
    event_query = {
        "$or": [
            {"timestamp": {"$gte": start_90}},
            {"occurred_at": {"$gte": start_90}},
            {"timestamp": {"$exists": False}, "occurred_at": {"$exists": False}},
        ]
    }
    events = list(db["crm_events"].find(event_query, {
        "lead_id": 1, "type": 1, "actor": 1, "actor_type": 1,
        "confirmed": 1, "result": 1, "meta": 1, "timestamp": 1,
        "occurred_at": 1,
    }))
    management_results = list(db["crm_management_results"].find({}, {
        "lead_id": 1, "assignment_cycle_id": 1, "result_type": 1,
        "occurred_at": 1, "status": 1, "actor_user_id": 1,
    }))

    property_collection = getattr(Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")
    property_docs = list(db[property_collection].find({}, {
        "codigo": 1, "comuna": 1, "region": 1, "ubicacion": 1,
        "estado": 1, "ejecutivo": 1, "captador": 1, "responsable": 1,
        "tipo_operacion": 1, "operacion": 1,
    }))
    properties = {}
    for prop in property_docs:
        if is_nonempty(prop.get("codigo")):
            properties[clean(prop.get("codigo"))] = prop

    client.close()
    return {
        "as_of": as_of,
        "leads": leads,
        "cycles": cycles,
        "users": users,
        "events": events,
        "management_results": management_results,
        "properties": properties,
        "property_collection": property_collection,
        "collection_counts": {
            "leads": len(leads),
            "crm_assignment_cycles": len(cycles),
            "crm_events_window90": len(events),
            "crm_management_results": len(management_results),
            "usuarios": len(users),
            property_collection: len(property_docs),
        },
    }


def field_presence(documents: list[dict], paths: list[str]) -> dict[str, int]:
    result = {}
    for path in paths:
        result[path] = sum(1 for document in documents if is_nonempty(path_get(document, path)))
    return result


def management_maps(data: dict) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    events_by_lead: dict[str, list[dict]] = defaultdict(list)
    for event in data["events"]:
        if event.get("lead_id") is not None:
            events_by_lead[str(event["lead_id"])].append(event)
    results_by_cycle: dict[str, list[dict]] = defaultdict(list)
    for result in data["management_results"]:
        if result.get("assignment_cycle_id") is not None:
            results_by_cycle[str(result["assignment_cycle_id"])].append(result)
    return events_by_lead, results_by_cycle


def first_valid_management(
    cycle: dict,
    lead: dict,
    events_by_lead: dict[str, list[dict]],
    results_by_cycle: dict[str, list[dict]],
) -> tuple[datetime | None, str | None]:
    assigned_at = parse_dt(cycle.get("assigned_at"))
    if not assigned_at:
        return None, None
    candidates: list[tuple[datetime, str]] = []

    cycle_first = parse_dt(cycle.get("first_valid_management_at"))
    if cycle_first and cycle_first >= assigned_at:
        candidates.append((cycle_first, "cycle.first_valid_management_at"))

    cycle_id = str(cycle.get("assignment_cycle_id") or "")
    for result in results_by_cycle.get(cycle_id, []):
        occurred = parse_dt(result.get("occurred_at"))
        normalized = normalize_result(result.get("result_type"))
        if occurred and occurred >= assigned_at and normalized in SLA_STOP_RESULTS:
            candidates.append((occurred, "crm_management_results"))

    lead_id = str(cycle.get("lead_id") or lead.get("_id") or "")
    for event in events_by_lead.get(lead_id, []):
        occurred = parse_dt(event.get("timestamp") or event.get("occurred_at"))
        if occurred and occurred >= assigned_at and event_evidence(event)["management"]:
            candidates.append((occurred, "crm_events"))

    if not candidates:
        return None, None
    return min(candidates, key=lambda item: item[0])


def has_management_evidence(
    cycle: dict,
    lead: dict,
    events_by_lead: dict[str, list[dict]],
    results_by_cycle: dict[str, list[dict]],
) -> bool:
    first, _ = first_valid_management(cycle, lead, events_by_lead, results_by_cycle)
    if first:
        return True
    assigned_at = parse_dt(cycle.get("assigned_at"))
    if not assigned_at:
        return False
    cycle_id = str(cycle.get("assignment_cycle_id") or "")
    for result in results_by_cycle.get(cycle_id, []):
        occurred = parse_dt(result.get("occurred_at"))
        normalized = normalize_result(result.get("result_type"))
        if occurred and occurred >= assigned_at and normalized:
            return True
    lead_id = str(cycle.get("lead_id") or lead.get("_id") or "")
    for event in events_by_lead.get(lead_id, []):
        occurred = parse_dt(event.get("timestamp") or event.get("occurred_at"))
        evidence = event_evidence(event)
        if occurred and occurred >= assigned_at and (evidence["management"] or evidence["contact_attempt"]):
            return True
    return False


def resolve_cycle_owner(cycle: dict, users_by_id: dict[str, dict]) -> tuple[str, str, dict | None]:
    raw_id = clean(cycle.get("assigned_to_user_id"))
    user = users_by_id.get(raw_id)
    if user:
        return raw_id, clean(user.get("nombre") or raw_id), user
    display = clean(cycle.get("assigned_to_display_name"))
    for candidate in users_by_id.values():
        if user_name_match(display, candidate.get("nombre")):
            return raw_id or str(candidate.get("_id")), clean(candidate.get("nombre")), candidate
    return raw_id or display or "Sin receptor", display or raw_id or "Sin receptor", None


def analyze_cycles(data: dict) -> list[dict]:
    leads_by_id = {str(lead.get("_id")): lead for lead in data["leads"] if lead.get("_id") is not None}
    users_by_id = {str(user.get("_id")): user for user in data["users"] if user.get("_id") is not None}
    events_by_lead, results_by_cycle = management_maps(data)
    analyzed = []
    for cycle in data["cycles"]:
        lead = leads_by_id.get(str(cycle.get("lead_id")))
        assigned_at = parse_dt(cycle.get("assigned_at"))
        sla_started_at = parse_dt(cycle.get("sla_started_at")) or commercial_sla_start_at(assigned_at)
        owner_id, owner_name, owner_user = resolve_cycle_owner(cycle, users_by_id)
        first_at, first_source = first_valid_management(cycle, lead or {}, events_by_lead, results_by_cycle) if lead else (None, None)
        temperature = clean(cycle.get("temperature_at_assignment") or "").upper()
        if not temperature:
            temperature = "HOT" if clean((lead or {}).get("lead_temperature_effective")).upper() == "HOT" else "NORMAL"
        if temperature != "HOT":
            temperature = "NORMAL"
        lifecycle = (lead or {}).get("lifecycle") or {}
        hot_started = parse_dt(cycle.get("hot_started_at")) or parse_dt(lifecycle.get("hot_since"))
        if hot_started and sla_started_at and hot_started < sla_started_at:
            hot_started = sla_started_at
        sla = None
        if sla_started_at:
            sla = calculate_sla(
                assigned_at=sla_started_at,
                first_valid_management_at=first_at,
                now=data["as_of"],
                temperature=temperature,
                hot_started_at=hot_started,
            )
        measured = None
        threshold = 60 if temperature == "HOT" else 180
        if sla:
            measured = sla.get("hot_minutes") if temperature == "HOT" else sla.get("minutes")
        stage = stage_of(lead or {})
        breached = bool(sla and ((not first_at and sla.get("status") == "critical") or (first_at and measured is not None and measured >= threshold)))
        within = bool(first_at and measured is not None and measured < threshold)
        outside = bool(first_at and measured is not None and measured >= threshold)
        strict_active = cycle.get("cycle_status") == "active" and cycle.get("unassigned_at") is None
        closed_cycle = cycle.get("unassigned_at") is not None or cycle.get("cycle_status") == "closed"
        reason = clean(cycle.get("reason")).lower()
        cycle_origin = clean(cycle.get("cycle_origin")).lower()
        excluded_origin = reason in EXCLUDED_ORIGINS or cycle_origin in EXCLUDED_ORIGINS or _is_test_lead(lead or {})
        canonical = (
            clean(cycle.get("schema_version")) == "crm_assignment_cycle_v1"
            and is_nonempty(cycle.get("assignment_cycle_id"))
            and is_nonempty(cycle.get("lead_id"))
            and is_nonempty(cycle.get("assigned_to_user_id"))
        )
        policy_eligible = bool(canonical and lead and assigned_at and sla_started_at and not excluded_origin)
        if policy_eligible and CUTOVER_AT and sla_started_at < CUTOVER_AT:
            policy_eligible = False
        summary = property_summary(lead or {}, data["properties"])
        analyzed.append({
            "cycle": cycle,
            "lead": lead,
            "lead_id": str(cycle.get("lead_id") or ""),
            "cycle_id": clean(cycle.get("assignment_cycle_id")),
            "assigned_at": assigned_at,
            "sla_started_at": sla_started_at,
            "unassigned_at": parse_dt(cycle.get("unassigned_at")),
            "owner_id": owner_id,
            "owner_name": owner_name or owner_id or "Sin receptor",
            "owner_user": owner_user,
            "first_at": first_at,
            "first_source": first_source,
            "temperature": temperature,
            "sla": sla,
            "measured_minutes": float(measured) if measured is not None else None,
            "threshold": threshold,
            "within": within,
            "outside": outside,
            "breached": breached,
            "stage": stage,
            "closed_lead": stage in CLOSED_STAGES,
            "strict_active": strict_active,
            "closed_cycle": closed_cycle,
            "canonical": canonical,
            "policy_eligible": policy_eligible,
            "excluded_origin": excluded_origin,
            "has_management_evidence": has_management_evidence(cycle, lead or {}, events_by_lead, results_by_cycle) if lead else False,
            "property_code": summary["code"],
            "comuna": summary["comuna"],
            "region": summary["region"],
            "operation": summary["operation"],
            "origin": origin_of(lead or {}),
            "phone_present": phone_present(lead or {}),
            "created_at": parse_dt((lead or {}).get("created_at")),
        })
    return analyzed


def cycle_quality(data: dict, records: list[dict]) -> dict:
    cycles = data["cycles"]
    leads = data["leads"]
    lead_ids = {str(lead.get("_id")) for lead in leads if lead.get("_id") is not None}
    active = [record for record in records if record["strict_active"]]
    active_by_lead: dict[str, list[dict]] = defaultdict(list)
    for record in active:
        if record["lead_id"]:
            active_by_lead[record["lead_id"]].append(record)

    duplicate_cycle_ids = Counter(clean(cycle.get("assignment_cycle_id")) for cycle in cycles if is_nonempty(cycle.get("assignment_cycle_id")))
    duplicate_identity = Counter(
        (
            str(cycle.get("lead_id")),
            clean(cycle.get("assigned_to_user_id")),
            iso(cycle.get("assigned_at")),
        )
        for cycle in cycles
        if is_nonempty(cycle.get("lead_id")) and is_nonempty(cycle.get("assigned_to_user_id")) and parse_dt(cycle.get("assigned_at"))
    )

    missing_assigned_at = sum(1 for cycle in cycles if not parse_dt(cycle.get("assigned_at")))
    missing_recipient = sum(1 for cycle in cycles if not is_nonempty(cycle.get("assigned_to_user_id")))
    missing_lead_id = sum(1 for cycle in cycles if not is_nonempty(cycle.get("lead_id")))
    orphan_cycles = sum(1 for cycle in cycles if is_nonempty(cycle.get("lead_id")) and str(cycle.get("lead_id")) not in lead_ids)
    missing_reason_or_origin = sum(1 for cycle in cycles if not clean(cycle.get("reason")) and not clean(cycle.get("cycle_origin")))

    chronology = []
    owner_mismatch = 0
    owner_unavailable = 0
    leads_by_id = {str(lead.get("_id")): lead for lead in leads if lead.get("_id") is not None}
    for record in records:
        cycle = record["cycle"]
        assigned = record["assigned_at"]
        sla_started = record["sla_started_at"]
        unassigned = record["unassigned_at"]
        first_at = record["first_at"]
        created = record["created_at"]
        impossible = []
        if assigned and sla_started and sla_started < assigned:
            impossible.append("sla_started_at_before_assigned_at")
        if assigned and unassigned and unassigned < assigned:
            impossible.append("unassigned_at_before_assigned_at")
        if assigned and first_at and first_at < assigned:
            impossible.append("first_management_before_assigned_at")
        if created and assigned and created > assigned:
            impossible.append("lead_created_after_assigned_at")
        if first_at and unassigned and first_at > unassigned:
            impossible.append("first_management_after_unassigned_at")
        if impossible:
            chronology.append({"cycle_id": record["cycle_id"], "lead_id": record["lead_id"], "reasons": impossible})

        lead = leads_by_id.get(record["lead_id"])
        current_owner = lead_owner(lead or {})
        if record["strict_active"] and current_owner and record["owner_name"] and not user_name_match(current_owner, record["owner_name"]):
            owner_mismatch += 1
        if record["strict_active"] and (not current_owner or not record["owner_name"]):
            owner_unavailable += 1

    assigned_leads_without_cycle = 0
    active_lead_ids = {record["lead_id"] for record in active if record["lead_id"]}
    for lead in leads:
        if is_assigned_name(lead_owner(lead)) and str(lead.get("_id")) not in active_lead_ids:
            assigned_leads_without_cycle += 1

    duplicate_cycle_id_excess = sum(count - 1 for count in duplicate_cycle_ids.values() if count > 1)
    duplicate_identity_excess = sum(count - 1 for count in duplicate_identity.values() if count > 1)
    active_duplicate_leads = sum(1 for values in active_by_lead.values() if len(values) > 1)
    active_duplicate_excess = sum(len(values) - 1 for values in active_by_lead.values() if len(values) > 1)
    status_active_without_unassigned = sum(1 for cycle in cycles if cycle.get("cycle_status") == "active" and cycle.get("unassigned_at") is not None)
    unassigned_without_closed_status = sum(1 for cycle in cycles if cycle.get("unassigned_at") is None and cycle.get("cycle_status") == "closed")
    return {
        "total_cycles": len(cycles),
        "strict_active": len(active),
        "closed": sum(1 for record in records if record["closed_cycle"]),
        "missing_assigned_at": missing_assigned_at,
        "missing_recipient": missing_recipient,
        "missing_lead_id": missing_lead_id,
        "orphan_cycles": orphan_cycles,
        "missing_reason_or_origin": missing_reason_or_origin,
        "duplicate_cycle_id_excess": duplicate_cycle_id_excess,
        "duplicate_identity_excess": duplicate_identity_excess,
        "active_duplicate_leads": active_duplicate_leads,
        "active_duplicate_excess": active_duplicate_excess,
        "owner_mismatch_active": owner_mismatch,
        "owner_unknown_active": owner_unavailable,
        "assigned_leads_without_active_cycle": assigned_leads_without_cycle,
        "chronology_issues": len(chronology),
        "chronology_examples": chronology[:20],
        "active_status_with_unassigned_at": status_active_without_unassigned,
        "closed_status_without_unassigned_at": unassigned_without_closed_status,
        "canonical_cycles": sum(1 for record in records if record["canonical"]),
        "policy_eligible_cycles": sum(1 for record in records if record["policy_eligible"]),
    }


def metrics_for_records(records: list[dict], *, current_records: list[dict] | None = None) -> dict:
    current_records = current_records or []
    managed = [record for record in records if record["first_at"]]
    durations = [record["measured_minutes"] for record in managed if record["measured_minutes"] is not None]
    current_open = [record for record in current_records if not record["closed_lead"]]
    current_pending = [record for record in current_open if not record["first_at"]]
    current_expired = [record for record in current_pending if record["sla"] and record["sla"].get("status") == "critical"]
    leads_unique = {record["lead_id"] for record in records if record["lead_id"]}
    closed_leads = {record["lead_id"] for record in records if record["closed_lead"] and record["lead_id"]}
    return {
        "cycles": len(records),
        "leads_unique": len(leads_unique),
        "hot": sum(1 for record in records if record["temperature"] == "HOT"),
        "normal": sum(1 for record in records if record["temperature"] != "HOT"),
        "closed_leads": len(closed_leads),
        "open_leads": len(leads_unique - closed_leads),
        "managed_cycles": len(managed),
        "unmanaged_cycles": len(records) - len(managed),
        "attention_pct": pct(len(managed), len(records)),
        "unmanaged_pct": pct(len(records) - len(managed), len(records)),
        "within": sum(1 for record in records if record["within"]),
        "outside": sum(1 for record in records if record["outside"]),
        "breached": sum(1 for record in records if record["breached"]),
        "sla_ok_pct": pct(sum(1 for record in records if record["within"]), len(records)),
        "sla_breach_pct": pct(sum(1 for record in records if record["breached"]), len(records)),
        "breached_never_managed": sum(1 for record in records if record["breached"] and not record["first_at"]),
        "breached_later_managed": sum(1 for record in records if record["outside"]),
        "p50": quantile(durations, 0.50),
        "p75": quantile(durations, 0.75),
        "p90": quantile(durations, 0.90),
        "p95": quantile(durations, 0.95),
        "mean": mean(durations),
        "duration_sample": len(durations),
        "current_open": len(current_open),
        "current_pending_no_first": len(current_pending),
        "current_expired": len(current_expired),
    }


def build_window_metrics(records: list[dict], as_of: datetime) -> dict[int, dict]:
    output = {}
    for days in WINDOWS:
        start = as_of - timedelta(days=days)
        all_window = [record for record in records if record["assigned_at"] and record["assigned_at"] >= start]
        eligible = [record for record in all_window if record["policy_eligible"]]
        metrics = metrics_for_records(eligible)
        metrics.update({
            "days": days,
            "start": start,
            "end": as_of,
            "cycles_all": len(all_window),
            "cycles_policy_excluded": len(all_window) - len(eligible),
            "pre_policy_cutover": sum(1 for record in all_window if record["assigned_at"] and record["sla_started_at"] and CUTOVER_AT and record["sla_started_at"] < CUTOVER_AT),
        })
        output[days] = metrics
    return output


def current_snapshot(records: list[dict]) -> tuple[list[dict], list[dict]]:
    active = [record for record in records if record["strict_active"] and record["lead"] and record["owner_id"]]
    by_lead: dict[str, list[dict]] = defaultdict(list)
    for record in active:
        by_lead[record["lead_id"]].append(record)
    latest = []
    for values in by_lead.values():
        latest.append(max(values, key=lambda record: record["assigned_at"] or datetime.min.replace(tzinfo=timezone.utc)))
    pending = [record for record in latest if not record["closed_lead"] and not record["first_at"]]
    expired = [record for record in pending if record["sla"] and record["sla"].get("status") == "critical"]
    return latest, expired


def executive_key(record: dict) -> str:
    return record["owner_id"] or norm(record["owner_name"])


def build_executive_metrics(records: list[dict], current: list[dict]) -> dict[int, list[dict]]:
    output: dict[int, list[dict]] = {}
    observed_names = {}
    for record in records:
        observed_names[executive_key(record)] = (record["owner_id"], record["owner_name"])
    current_map: dict[str, list[dict]] = defaultdict(list)
    for record in current:
        current_map[executive_key(record)].append(record)

    for days in WINDOWS:
        start = records[0]["assigned_at"] if not records else None
        # The actual window is reconstructed from the global as_of in the caller.
        # This placeholder is overwritten by the caller's filtered list.
        output[days] = []
    return output


def executive_rows_for_window(records: list[dict], current: list[dict]) -> list[dict]:
    keys = {executive_key(record) for record in records}
    keys.update(executive_key(record) for record in current)
    rows = []
    for key in sorted(keys, key=lambda item: norm(next((r["owner_name"] for r in records + current if executive_key(r) == item), item))):
        own = [record for record in records if executive_key(record) == key]
        current_own = [record for record in current if executive_key(record) == key and not record["closed_lead"]]
        metric = metrics_for_records(own, current_records=current_own)
        owner_id = own[0]["owner_id"] if own else current_own[0]["owner_id"]
        owner_name = own[0]["owner_name"] if own else current_own[0]["owner_name"]
        rows.append({
            "executive_id": owner_id,
            "executive": owner_name,
            "cycles_received": metric["cycles"],
            "leads_unique_received": metric["leads_unique"],
            "hot": metric["hot"],
            "normal": metric["normal"],
            "cycles_attended": metric["managed_cycles"],
            "cycles_without_management": metric["unmanaged_cycles"],
            "sla_ok_pct": metric["sla_ok_pct"],
            "sla_breach_pct": metric["sla_breach_pct"],
            "p50_business_minutes": metric["p50"],
            "p90_business_minutes": metric["p90"],
            "p95_business_minutes": metric["p95"],
            "time_observations": metric["duration_sample"],
            "sample_note": sample_note(metric["duration_sample"]),
            "backlog_current": metric["current_open"],
            "pending_without_first_management": metric["current_pending_no_first"],
            "currently_expired": metric["current_expired"],
            "attention_rate_pct": metric["attention_pct"],
            "closed_leads": metric["closed_leads"],
            "sample_size_cycles": metric["cycles"],
        })
    return rows


def current_age_bucket(overdue_minutes: float | None) -> str:
    if overdue_minutes is None:
        return "dato insuficiente"
    if overdue_minutes < 60:
        return "vencido <1 hora"
    if overdue_minutes < 180:
        return "1–3 horas"
    if overdue_minutes < 480:
        return "3–8 horas"
    if overdue_minutes < 1440:
        return "8–24 horas"
    if overdue_minutes < 2880:
        return "1–2 días"
    if overdue_minutes < 7200:
        return "2–5 días"
    return ">5 días"


def build_current_expired_rows(expired: list[dict]) -> list[dict]:
    rows = []
    for record in expired:
        assigned = record["assigned_at"]
        sla_started = record["sla_started_at"]
        elapsed = record["measured_minutes"]
        overdue = max(0.0, elapsed - record["threshold"]) if elapsed is not None else None
        rows.append({
            "lead_id": record["lead_id"],
            "assignment_cycle_id": record["cycle_id"],
            "executive_id": record["owner_id"],
            "executive": record["owner_name"],
            "temperature": record["temperature"],
            "origin": record["origin"],
            "operation": record["operation"],
            "comuna": record["comuna"],
            "region": record["region"],
            "property_code": record["property_code"],
            "phone_present": "yes" if record["phone_present"] else "no",
            "assigned_at_local": local_dt(assigned),
            "sla_started_at_local": local_dt(sla_started),
            "age_since_assignment_hours": round(max(0.0, (record["as_of"] - assigned).total_seconds() / 3600), 1) if record.get("as_of") and assigned else "",
            "age_since_sla_business_minutes": round(elapsed, 1) if elapsed is not None else "",
            "threshold_business_minutes": record["threshold"],
            "overdue_business_minutes": round(overdue, 1) if overdue is not None else "",
            "overdue_bucket": current_age_bucket(overdue),
            "assigned_hour_local": assigned.astimezone(CHILE_TZ).hour if assigned else "",
            "assigned_weekday_local": assigned.astimezone(CHILE_TZ).strftime("%A") if assigned else "",
            "stage": record["stage"],
            "management_evidence": "yes" if record["has_management_evidence"] else "no",
        })
    return rows


def router_candidates(record: dict, active_users: list[dict], properties: dict[str, dict]) -> tuple[str, list[dict], str]:
    """Reconstruct only the existing CRM router's candidate pool.

    This does not call the router because that function mutates round-robin
    state. It mirrors its current read-side rules without selecting a winner.
    """
    lead = record["lead"] or {}
    code = record["property_code"]
    prop = properties.get(clean(code)) if code else None
    if not code:
        return "TERRITORY_UNKNOWN", [], "current_router_no_property"
    if not prop:
        return "DATA_INSUFFICIENT", [], "property_not_found_for_existing_router"

    location_region = norm(first_text(prop, ("ubicacion.region", "region")))
    location_comuna = norm(first_text(prop, ("ubicacion.comuna", "comuna")))
    state = prop.get("estado") or {}
    original = first_text(prop, ("estado.ejecutivo", "estado.captador", "estado.responsable", "ejecutivo", "captador", "responsable"))
    original_norm = norm(original)
    active_by_name = [
        user for user in active_users
        if clean(user.get("nombre")) and clean(user.get("rol")) == "agente"
    ]

    def lookup(name: str) -> dict | None:
        for user in active_by_name:
            if user_name_match(name, user.get("nombre")):
                return user
        return None

    target_names: list[str]
    if any(user_name_match(original, team_name) for team_name in OUR_TEAM) and original:
        matched = next(team_name for team_name in OUR_TEAM if user_name_match(original, team_name))
        if norm(matched) in TEMPORARILY_INACTIVE:
            target_names = ["Mariela Arriagada"] if norm(matched) == "raquel cheneaux" else ["Erika Garrido"]
        else:
            target_names = [matched]
    else:
        if "jorge pablo caro" in original_norm or not lookup(original):
            if "metropolitana" in location_region or "xiii" in location_region:
                target_names = [
                    name for name in ROUND_ROBIN_RM
                    if name != "Mariela Arriagada" or location_comuna in MARIELA_PRIORITY_COMUNAS
                ]
            elif "maule" in location_region or "vii" in location_region:
                target_names = ["Paula Morales"]
            elif any(token in location_region for token in ("nuble", "bio", "xvi", "viii", "valparaiso", "quinta")):
                target_names = ["Rocío Aliaga"]
            else:
                target_names = ["Erika Garrido"]
        else:
            target_names = [original]

    candidates = []
    for target in target_names:
        user = lookup(target)
        if user and not user_name_match(user.get("nombre"), record["owner_name"]):
            candidates.append(user)
    if not candidates:
        return "NO_TERRITORIAL_CANDIDATE", [], "existing_router_pool_empty_after_current_owner"
    return "HAS_TERRITORIAL_CANDIDATES", candidates, "existing_router_rules"


def build_territory_analysis(data: dict, records: list[dict], current_expired: list[dict], executive_rows_90: list[dict]) -> tuple[list[dict], dict]:
    active_users = [
        user for user in data["users"]
        if user.get("is_active") is True and clean(user.get("rol")) == "agente"
    ]
    metric_by_name = {norm(row["executive"]): row for row in executive_rows_90}
    current_all, _ = current_snapshot(records)
    backlog_by_name = Counter(norm(record["owner_name"]) for record in current_all if not record["closed_lead"] and not record["first_at"])
    rows = []
    classifications = Counter()
    for record in current_expired:
        if record["has_management_evidence"]:
            classification, candidates, reason = "MANAGEMENT_EVIDENCE_PROTECTED", [], "management_evidence"
        else:
            classification, candidates, reason = router_candidates(record, active_users, data["properties"])
        classifications[classification] += 1
        candidate_details = []
        for candidate in candidates:
            candidate_name = clean(candidate.get("nombre"))
            candidate_metric = metric_by_name.get(norm(candidate_name), {})
            candidate_details.append({
                "id": str(candidate.get("_id") or ""),
                "name": candidate_name,
                "backlog_current": backlog_by_name.get(norm(candidate_name), 0),
                "sla_ok_pct_90d": candidate_metric.get("sla_ok_pct"),
                "sla_breach_pct_90d": candidate_metric.get("sla_breach_pct"),
                "p50_90d": candidate_metric.get("p50_business_minutes"),
                "p90_90d": candidate_metric.get("p90_business_minutes"),
                "sample_90d": candidate_metric.get("time_observations", 0),
            })
        rows.append({
            "lead_id": record["lead_id"],
            "assignment_cycle_id": record["cycle_id"],
            "current_executive": record["owner_name"],
            "temperature": record["temperature"],
            "comuna": record["comuna"],
            "region": record["region"],
            "origin": record["origin"],
            "operation": record["operation"],
            "classification": classification,
            "reason": reason,
            "candidate_count": len(candidate_details),
            "candidates": "; ".join(
                f"{item['name']} [backlog={item['backlog_current']}, SLA={fmt_pct(item['sla_ok_pct_90d'])}, P50={fmt_num(item['p50_90d'])}, P90={fmt_num(item['p90_90d'])}, n={item['sample_90d']} ]"
                for item in candidate_details
            ),
        })

    territory_by_commune = defaultdict(lambda: {"expired": 0, "classifications": Counter(), "candidate_names": set()})
    for row in rows:
        item = territory_by_commune[row["comuna"]]
        item["expired"] += 1
        item["classifications"][row["classification"]] += 1
        if row["candidates"]:
            for candidate in row["candidates"].split("; "):
                item["candidate_names"].add(candidate.split(" [", 1)[0])
    coverage_rows = []
    for comuna, item in sorted(territory_by_commune.items(), key=lambda pair: (-pair[1]["expired"], norm(pair[0]))):
        coverage_rows.append({
            "comuna": comuna,
            "vencidos_actuales": item["expired"],
            "has_candidates": item["classifications"].get("HAS_TERRITORIAL_CANDIDATES", 0),
            "no_candidate": item["classifications"].get("NO_TERRITORIAL_CANDIDATE", 0),
            "territory_unknown": item["classifications"].get("TERRITORY_UNKNOWN", 0),
            "data_insufficient": item["classifications"].get("DATA_INSUFFICIENT", 0),
            "management_protected": item["classifications"].get("MANAGEMENT_EVIDENCE_PROTECTED", 0),
            "existing_candidates": ", ".join(sorted(item["candidate_names"], key=norm)),
            "candidate_count_distinct": len(item["candidate_names"]),
        })
    summary = {
        "classifications": dict(classifications),
        "active_agents": len(active_users),
        "coverage_rows": coverage_rows,
        "communes_single_candidate": sum(1 for row in coverage_rows if row["candidate_count_distinct"] == 1),
        "communes_multiple_candidates": sum(1 for row in coverage_rows if row["candidate_count_distinct"] > 1),
        "communes_without_clear_candidate": sum(1 for row in coverage_rows if row["candidate_count_distinct"] == 0),
    }
    return rows, summary


def segment_metrics(records: list[dict], key: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[clean(record.get(key)) or "No informado"].append(record)
    output = []
    for segment, values in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), norm(pair[0]))):
        metric = metrics_for_records(values)
        output.append({
            "segment": segment,
            "volume": len(values),
            "sla_ok_pct": metric["sla_ok_pct"],
            "sla_breach_pct": metric["sla_breach_pct"],
            "p50_business_minutes": metric["p50"],
            "p90_business_minutes": metric["p90"],
            "time_observations": metric["duration_sample"],
        })
    return output


def temporal_metrics(records: list[dict]) -> tuple[list[dict], list[dict]]:
    by_hour: dict[int, list[dict]] = defaultdict(list)
    by_weekday: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if not record["assigned_at"]:
            continue
        local = record["assigned_at"].astimezone(CHILE_TZ)
        by_hour[local.hour].append(record)
        by_weekday[local.strftime("%A")].append(record)

    def rows(grouped: dict[Any, list[dict]]) -> list[dict]:
        result = []
        for label, values in sorted(grouped.items(), key=lambda pair: (pair[0] if isinstance(pair[0], int) else norm(pair[0]))):
            result.append({
                "bucket": label,
                "volume": len(values),
                "sla_ok_pct": metrics_for_records(values)["sla_ok_pct"],
                "sla_breach_pct": metrics_for_records(values)["sla_breach_pct"],
                "unmanaged": sum(1 for value in values if not value["first_at"]),
            })
        return result
    return rows(by_hour), rows(by_weekday)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def presence_table(presence: dict[str, int], total: int) -> str:
    return markdown_table(
        ["Campo", "Presentes", "%"],
        [[path, count, fmt_pct(pct(count, total))] for path, count in presence.items()],
    )


def build_report(data: dict, records: list[dict], quality: dict, window_metrics: dict, executive_by_window: dict[int, list[dict]], current: list[dict], expired: list[dict], expired_rows: list[dict], territory_rows: list[dict], territory_summary: dict, origin_tables: dict[int, list[dict]], operation_tables: dict[int, list[dict]], hour_table: list[dict], weekday_table: list[dict], field_profiles: dict) -> str:
    as_of = data["as_of"]
    current_metric = metrics_for_records([record for record in current if not record["closed_lead"]], current_records=current)
    current_origin = Counter(row["origin"] for row in expired_rows)
    current_exec = Counter(row["executive"] for row in expired_rows)
    current_bucket = Counter(row["overdue_bucket"] for row in expired_rows)
    current_comuna = Counter(row["comuna"] for row in expired_rows)
    current_region = Counter(row["region"] for row in expired_rows)
    current_temp = Counter(row["temperature"] for row in expired_rows)
    current_hour = Counter(str(row["assigned_hour_local"]) for row in expired_rows)
    current_day = Counter(row["assigned_weekday_local"] for row in expired_rows)
    classifications = territory_summary["classifications"]
    active_agents = [user for user in data["users"] if user.get("is_active") is True and clean(user.get("rol")) == "agente"]

    lines = [
        "# Auditoría cuantitativa SLA CRM — Fase 0",
        "",
        f"**Fecha de ejecución:** {as_of.astimezone(CHILE_TZ).isoformat()}  ",
        f"**Ventana de datos:** {((as_of - timedelta(days=90)).astimezone(CHILE_TZ)).date().isoformat()} a {as_of.astimezone(CHILE_TZ).date().isoformat()}  ",
        "**Alcance:** exclusivamente leads inbound del CRM. No incluye captación de propiedades.",
        "",
        "## 1. Resumen ejecutivo",
        "",
        f"La auditoría leyó {data['collection_counts']['leads']:,} leads, {data['collection_counts']['crm_assignment_cycles']:,} ciclos de asignación, {data['collection_counts']['crm_events_window90']:,} eventos CRM dentro de la ventana de 90 días y {data['collection_counts']['crm_management_results']:,} resultados de gestión. La reasignación no fue ejecutada y no se modificó ningún documento de MongoDB.",
        "",
        f"La fotografía actual muestra **{len(expired_rows):,} leads vencidos** entre {len(current):,} leads con ciclo activo seleccionado por lead. El análisis territorial no elige ganador: solo reconstruye si el pool del router CRM existente deja candidatos alternativos.",
        "",
        "Los percentiles de velocidad están expresados en minutos hábiles según la función productiva del CRM: lunes a viernes, 09:00–19:00, timezone Chile. La política actual usa 60 minutos para HOT y 180 para normal; los ciclos anteriores al cutover se contabilizan aparte y no se mezclan con la población evaluable de la política vigente.",
        "",
        "## 2. Fuentes de datos",
        "",
        markdown_table(
            ["Fuente", "Colección", "Documentos leídos", "Uso"],
            [
                ["Leads", "leads", data["collection_counts"]["leads"], "identidad, estado, prioridad, origen, operación, territorio y teléfono presente/ausente"],
                ["Ciclos", "crm_assignment_cycles", data["collection_counts"]["crm_assignment_cycles"], "asignación histórica, receptor, inicio SLA, cierre y primera gestión"],
                ["Eventos", "crm_events", data["collection_counts"]["crm_events_window90"], "evidencia de gestión y primera acción"],
                ["Resultados", "crm_management_results", data["collection_counts"]["crm_management_results"], "resultado válido por ciclo"],
                ["Usuarios", "usuarios", data["collection_counts"]["usuarios"], "estado activo, rol y datos territoriales existentes"],
                ["Propiedades asociadas", data["property_collection"], data["collection_counts"][data["property_collection"]], "comuna, región, operación y ejecutivo de ficha para reconstruir el router CRM"],
            ],
        ),
        "",
        "### Definiciones confirmadas desde el código",
        "",
        f"- Colección de leads: `leads`; ciclos: `crm_assignment_cycles`; resultados: `crm_management_results`; eventos: `crm_events`.",
        f"- Ciclo activo estricto: `cycle_status=active` y `unassigned_at=None`, igual que el evaluador de SLA.",
        f"- Gestión válida: `first_valid_management_at` del ciclo, o evidencia reconstruible desde resultados cuyo resultado normalizado pertenece a `SLA_STOP_RESULTS`, o eventos humanos de gestión según `event_evidence()`.",
        f"- Resultados de outreach como `MESSAGE_SENT_WAITING_RESPONSE` y `EMAIL_SENT` no detienen el SLA en el evaluador vigente.",
        f"- Horario hábil: {BUSINESS_START_HOUR:02d}:00–{BUSINESS_END_HOUR:02d}:00, lunes a viernes; timezone `Chile/Continental`.",
        f"- Ejecutivo activo para el pool de análisis: `usuarios.is_active=True` y `usuarios.rol=agente`.",
        "- No se exportan teléfonos completos; los CSV solo contienen `phone_present=yes/no`.",
        "",
        "### Cobertura de campos observada",
        "",
        "#### Leads",
        "",
        presence_table(field_profiles["leads"], data["collection_counts"]["leads"]),
        "",
        "#### Ciclos",
        "",
        presence_table(field_profiles["cycles"], data["collection_counts"]["crm_assignment_cycles"]),
        "",
        "## 3. Calidad de ciclos",
        "",
        markdown_table(
            ["Control", "Resultado"],
            [
                ["Ciclos totales", quality["total_cycles"]],
                ["Ciclos activos estrictos", quality["strict_active"]],
                ["Ciclos cerrados", quality["closed"]],
                ["Sin `assigned_at`", quality["missing_assigned_at"]],
                ["Sin receptor", quality["missing_recipient"]],
                ["Sin `lead_id`", quality["missing_lead_id"]],
                ["Ciclos huérfanos, lead inexistente", quality["orphan_cycles"]],
                ["Sin motivo ni origen", quality["missing_reason_or_origin"]],
                ["Exceso de IDs de ciclo duplicados", quality["duplicate_cycle_id_excess"]],
                ["Exceso de identidades duplicadas (lead/receptor/fecha)", quality["duplicate_identity_excess"]],
                ["Leads con más de un ciclo activo", quality["active_duplicate_leads"]],
                ["Ciclos activos excedentes por lead", quality["active_duplicate_excess"]],
                ["Owner activo no coincide con owner del lead", quality["owner_mismatch_active"]],
                ["Owner activo no comparable por dato faltante", quality["owner_unknown_active"]],
                ["Leads asignados sin ciclo activo", quality["assigned_leads_without_active_cycle"]],
                ["Inconsistencias cronológicas", quality["chronology_issues"]],
                ["Ciclos canónicos", quality["canonical_cycles"]],
                ["Ciclos elegibles para política SLA vigente", quality["policy_eligible_cycles"]],
            ],
        ),
        "",
        "La calidad no se corrigió durante esta fase. Las inconsistencias solo se informan.",
        "",
        "## 4. Métricas 30/60/90",
        "",
        markdown_table(
            ["Ventana", "Ciclos total", "Ciclos SLA vigente", "Leads únicos", "HOT", "Normales", "Cerrados", "Abiertos", "Atendidos", "Sin gestión", "SLA OK", "SLA vencido", "Vencidos sin gestión", "Vencidos gestionados después", "P50", "P90", "P95", "Muestra"],
            [
                [f"{days} días", m["cycles_all"], m["cycles"], m["leads_unique"], m["hot"], m["normal"], m["closed_leads"], m["open_leads"], m["managed_cycles"], m["unmanaged_cycles"], fmt_pct(m["sla_ok_pct"]), fmt_pct(m["sla_breach_pct"]), m["breached_never_managed"], m["breached_later_managed"], fmt_num(m["p50"]), fmt_num(m["p90"]), fmt_num(m["p95"]), m["duration_sample"]]
                for days, m in ((days, window_metrics[days]) for days in WINDOWS)
            ],
        ),
        "",
        "La columna `SLA OK` usa como denominador los ciclos elegibles para la política vigente. Los ciclos sin primera gestión y ya vencidos cuentan como incumplimiento. `P50/P90/P95` usan únicamente ciclos con primera gestión válida.",
        "",
        "## 5. Ranking descriptivo por ejecutivo",
        "",
        "No se calcula un score compuesto ni se declara un ganador. La tabla está ordenada por nombre para mostrar componentes separados.",
        "",
    ]
    for days in WINDOWS:
        lines.extend([
            f"### Ventana {days} días",
            "",
            markdown_table(
                ["Ejecutivo", "Asignados", "Leads únicos", "HOT", "Normales", "Atendidos", "Sin gestión", "SLA OK %", "Vencidos %", "P50", "P90", "P95", "n tiempo", "Backlog", "Pendientes sin 1ª gestión", "Vencidos actuales", "Cerrados", "Nota muestra"],
                [
                    [row["executive"], row["cycles_received"], row["leads_unique_received"], row["hot"], row["normal"], row["cycles_attended"], row["cycles_without_management"], fmt_pct(row["sla_ok_pct"]), fmt_pct(row["sla_breach_pct"]), fmt_num(row["p50_business_minutes"]), fmt_num(row["p90_business_minutes"]), fmt_num(row["p95_business_minutes"]), row["time_observations"], row["backlog_current"], row["pending_without_first_management"], row["currently_expired"], row["closed_leads"], row["sample_note"]]
                    for row in executive_by_window[days]
                ],
            ),
            "",
        ])
    lines.extend([
        "La nota de muestra es analítica, no una regla comercial: una muestra de 1 no permite una distribución; con 2–4 observaciones los percentiles altos tienen estabilidad limitada. No se fija un umbral definitivo de elegibilidad.",
        "",
        "## 6. Estado actual de leads desatendidos",
        "",
        f"Leads con ciclo activo seleccionado por lead: **{len(current):,}**. Leads abiertos sin primera gestión: **{current_metric['current_pending_no_first']:,}**. Leads actualmente vencidos: **{len(expired_rows):,}**.",
        "",
        "### Distribución de vencidos actuales",
        "",
        markdown_table(["Dimensión", "Distribución"], [
            ["Ejecutivo", "; ".join(f"{key}: {value}" for key, value in current_exec.most_common()) or "Ninguno"],
            ["Edad del vencimiento", "; ".join(f"{key}: {value}" for key, value in current_bucket.most_common()) or "Ninguno"],
            ["Comuna", "; ".join(f"{key}: {value}" for key, value in current_comuna.most_common(20)) or "Ninguna"],
            ["Región", "; ".join(f"{key}: {value}" for key, value in current_region.most_common(20)) or "Ninguna"],
            ["Origen", "; ".join(f"{key}: {value}" for key, value in current_origin.most_common(20)) or "Ninguno"],
            ["HOT/normal", "; ".join(f"{key}: {value}" for key, value in current_temp.most_common()) or "Ninguno"],
            ["Hora local de asignación", "; ".join(f"{key}: {value}" for key, value in sorted(current_hour.items())) or "Ninguna"],
            ["Día local de asignación", "; ".join(f"{key}: {value}" for key, value in current_day.most_common()) or "Ninguno"],
        ]),
        "",
        "Los buckets de edad usan minutos hábiles vencidos sobre el umbral del perfil: <1h, 1–3h, 3–8h, 8–24h, 1–2d, 2–5d y >5d. No se convierten silenciosamente en horas calendario.",
        "",
        "El detalle sin datos personales innecesarios está en `docs/auditoria_sla_data/current_expired_leads.csv`.",
        "",
        "## 7. Análisis territorial",
        "",
        f"Ejecutivos activos en el pool analítico: **{len(active_agents)}** (`is_active=True`, `rol=agente`). El router CRM actual usa principalmente la propiedad asociada, el ejecutivo de la ficha y reglas regionales existentes; no se usaron kilómetros, coordenadas ni tiempos de viaje.",
        "",
        markdown_table(["Clasificación", "Total"], [[key, classifications.get(key, 0)] for key in ("HAS_TERRITORIAL_CANDIDATES", "NO_TERRITORIAL_CANDIDATE", "TERRITORY_UNKNOWN", "MANAGEMENT_EVIDENCE_PROTECTED", "DATA_INSUFFICIENT")]),
        "",
        f"Comunas con un único candidato distinto: {territory_summary['communes_single_candidate']}; con múltiples candidatos distintos: {territory_summary['communes_multiple_candidates']}; sin candidato claro: {territory_summary['communes_without_clear_candidate']}.",
        "",
        "No se eligió ganador. Para cada caso con candidatos, el CSV incluye nombres/IDs, backlog y métricas históricas descriptivas de 90 días. Los nombres no constituyen un score.",
        "",
        "## 8. Análisis por origen",
        "",
    ])
    for days in WINDOWS:
        lines.extend([
            f"### Origen — {days} días",
            "",
            markdown_table(["Origen real", "Volumen", "SLA OK %", "Vencido %", "P50", "P90", "n"], [[row["segment"], row["volume"], fmt_pct(row["sla_ok_pct"]), fmt_pct(row["sla_breach_pct"]), fmt_num(row["p50_business_minutes"]), fmt_num(row["p90_business_minutes"]), row["time_observations"]] for row in origin_tables[days]]),
            "",
        ])
    lines.extend(["## 9. Análisis por operación", ""])
    for days in WINDOWS:
        lines.extend([
            f"### Operación — {days} días",
            "",
            markdown_table(["Operación real", "Volumen", "SLA OK %", "Vencido %", "P50", "P90", "n"], [[row["segment"], row["volume"], fmt_pct(row["sla_ok_pct"]), fmt_pct(row["sla_breach_pct"]), fmt_num(row["p50_business_minutes"]), fmt_num(row["p90_business_minutes"]), row["time_observations"]] for row in operation_tables[days]]),
            "",
        ])
    lines.extend([
        "## 10. Análisis temporal",
        "",
        "La siguiente tabla cuantifica asignaciones por hora local y día de semana. No propone cambiar el SLA.",
        "",
        "### Hora local de asignación",
        "",
        markdown_table(["Hora", "Volumen", "SLA OK %", "Vencido %", "Sin gestión"], [[f"{row['bucket']:02d}:00", row["volume"], fmt_pct(row["sla_ok_pct"]), fmt_pct(row["sla_breach_pct"]), row["unmanaged"]] for row in hour_table]),
        "",
        "### Día de semana de asignación",
        "",
        markdown_table(["Día", "Volumen", "SLA OK %", "Vencido %", "Sin gestión"], [[row["bucket"], row["volume"], fmt_pct(row["sla_ok_pct"]), fmt_pct(row["sla_breach_pct"]), row["unmanaged"]] for row in weekday_table]),
        "",
        "Nota: el CRM normaliza asignaciones fuera de horario hacia el siguiente slot hábil para el reloj SLA. Por eso cualquier asignación fuera de lunes–viernes 09:00–19:00 debe interpretarse como una señal de calidad de datos o de una ruta legacy, no como una nueva regla.",
        "",
        "## 11. Pools territoriales potenciales",
        "",
        "La simulación es analítica y no modifica owner, ciclo, lead ni estado. Solo se consideran candidatos alternativos que pueden reconstruirse desde las reglas actuales del router CRM y usuarios activos. El router productivo no fue llamado porque mantiene estado de round-robin.",
        "",
        markdown_table(["Clasificación", "Significado"], [
            ["HAS_TERRITORIAL_CANDIDATES", "Existe al menos un candidato alternativo del pool actual."],
            ["NO_TERRITORIAL_CANDIDATE", "Las reglas actuales no dejan un candidato alternativo activo después de excluir al owner actual."],
            ["TERRITORY_UNKNOWN", "No existe propiedad asociada suficiente para reconstruir el territorio del router."],
            ["MANAGEMENT_EVIDENCE_PROTECTED", "Existe evidencia de gestión/intentado y se protege analíticamente."],
            ["DATA_INSUFFICIENT", "Existe código de propiedad, pero no se pudo resolver la ficha necesaria para las reglas actuales."],
        ]),
        "",
        "## 12. Casos anómalos y de riesgo",
        "",
        markdown_table(["Caso", "Cantidad / evidencia"], [
            ["Leads vencidos HOT", current_temp.get("HOT", 0)],
            ["Ejecutivos con vencidos actuales", len(current_exec)],
            ["Vencidos sin candidato territorial", classifications.get("NO_TERRITORIAL_CANDIDATE", 0)],
            ["Vencidos con territorio desconocido", classifications.get("TERRITORY_UNKNOWN", 0)],
            ["Vencidos con datos insuficientes", classifications.get("DATA_INSUFFICIENT", 0)],
            ["Vencidos protegidos por gestión", classifications.get("MANAGEMENT_EVIDENCE_PROTECTED", 0)],
            ["Leads con más de un ciclo activo", quality["active_duplicate_leads"]],
            ["Owner de ciclo activo distinto del owner del lead", quality["owner_mismatch_active"]],
            ["Ciclos con fechas imposibles", quality["chronology_issues"]],
        ]),
        "",
        "## 13. Limitaciones de datos",
        "",
        "- No se repararon ciclos históricos ni se crearon índices.",
        "- La relación histórica lead↔ejecutivo se basa en el receptor del ciclo y, para comparar owner actual, en los campos legacy de nombre del lead; el esquema actual no expone un owner ID canónico directamente en todos los leads.",
        "- La definición de gestión válida depende de los campos canónicos y eventos compatibles; los eventos de simple apertura/click no se consideran gestión.",
        "- El sistema tiene datos de comuna/región en leads y/o propiedades asociadas, pero no se encontró una implementación CRM de kilómetros, coordenadas, radio o tiempo de traslado para usarla en esta fase.",
        "- El pool territorial es una reconstrucción de reglas existentes y no una recomendación de negocio.",
        "- `crm_sla_alert_evaluator` está diseñado principalmente para ciclos activos posteriores al cutover; para la auditoría histórica se mantuvieron ciclos cerrados para medir resultados, separando los ciclos fuera de la política vigente.",
        "- Los CSV no contienen teléfonos completos ni mensajes del cliente.",
        "",
        "## 14. Hallazgos que afectan el diseño posterior",
        "",
        "1. La decisión debe basarse en ciclos históricos, no en el owner actual del lead.",
        "2. La población de SLA vigente debe mantenerse separada de ciclos legacy/pre-cutover.",
        "3. La mediana/P90/P95 son componentes descriptivos; no se construyó score.",
        "4. Antes de una reasignación será necesario resolver la identidad canónica del owner y la carrera entre evaluación y actualización.",
        "5. El pool territorial actual puede no dejar alternativa cuando el router conserva la propiedad con el ejecutivo de la ficha.",
        "6. Los casos HOT deben observarse separadamente por su umbral de 60 minutos.",
        "7. La evidencia de gestión debe continuar protegiendo el lead del retiro automático.",
        "8. La ausencia de kilómetros/ETA impide justificar decisiones geográficas nuevas en esta fase.",
        "",
        "## 15. Recomendaciones para la siguiente fase — sin implementar cambios",
        "",
        "- Revisar con Sol las cifras de volumen, vencimiento y cobertura territorial antes de definir una política.",
        "- Ejecutar una segunda revisión del modelo de owner/ciclo sobre los casos de mismatch y múltiples ciclos activos.",
        "- Diseñar un shadow output que conserve la explicación de elegibilidad y no seleccione ganador todavía.",
        "- Definir posteriormente, fuera de esta Fase 0, qué métricas y restricciones serán decisiones de negocio.",
        "- Mantener desactivada la reasignación hasta cerrar estas decisiones y probar el bloqueo de acceso/teléfono.",
        "",
        "## Validación de seguridad de esta ejecución",
        "",
        "- Mongo writes = 0",
        "- Reasignaciones ejecutadas = 0",
        "- Leads modificados = 0",
        "- Variables de entorno modificadas = 0",
        "- Deploy = NO",
        "- Flags modificados = 0",
        "- Llamadas a proveedores externos = 0",
        "",
        "La escritura realizada por esta auditoría se limita a los artefactos locales indicados en la sección siguiente.",
        "",
        "## Archivos de apoyo",
        "",
        "- `docs/auditoria_sla_data/executive_metrics_30d.csv`",
        "- `docs/auditoria_sla_data/executive_metrics_60d.csv`",
        "- `docs/auditoria_sla_data/executive_metrics_90d.csv`",
        "- `docs/auditoria_sla_data/current_expired_leads.csv`",
        "- `docs/auditoria_sla_data/territorial_coverage.csv`",
        "- `docs/auditoria_sla_data/territorial_pool_candidates.csv`",
        "- `docs/auditoria_sla_data/origin_metrics_30d.csv`, `origin_metrics_60d.csv`, `origin_metrics_90d.csv`",
        "- `docs/auditoria_sla_data/operation_metrics_30d.csv`, `operation_metrics_60d.csv`, `operation_metrics_90d.csv`",
        "- `docs/auditoria_sla_data/temporal_hour_90d.csv`, `temporal_weekday_90d.csv`",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    if OUTPUT_REPORT.exists():
        raise RuntimeError(f"El informe ya existe; no se sobrescribe: {OUTPUT_REPORT}")

    data = load_data()
    records = analyze_cycles(data)
    quality = cycle_quality(data, records)
    window_metrics = build_window_metrics(records, data["as_of"])
    current, expired = current_snapshot(records)
    for record in expired:
        record["as_of"] = data["as_of"]
    expired_rows = build_current_expired_rows(expired)

    executive_by_window = {}
    executive_csv_rows = {}
    for days in WINDOWS:
        start = data["as_of"] - timedelta(days=days)
        window_records = [record for record in records if record["policy_eligible"] and record["assigned_at"] and record["assigned_at"] >= start]
        rows = executive_rows_for_window(window_records, current)
        executive_by_window[days] = rows
        executive_csv_rows[days] = rows

    territory_rows, territory_summary = build_territory_analysis(
        data, records, expired, executive_by_window[90]
    )
    origin_tables = {}
    operation_tables = {}
    for days in WINDOWS:
        start = data["as_of"] - timedelta(days=days)
        window_records = [record for record in records if record["policy_eligible"] and record["assigned_at"] and record["assigned_at"] >= start]
        origin_tables[days] = segment_metrics(window_records, "origin")
        operation_tables[days] = segment_metrics(window_records, "operation")
    hour_table, weekday_table = temporal_metrics([
        record for record in records
        if record["policy_eligible"] and record["assigned_at"] and record["assigned_at"] >= data["as_of"] - timedelta(days=90)
    ])

    field_profiles = {
        "leads": field_presence(data["leads"], [
            "_id", "phone", "created_at", "pipeline_stage", "stage", "crm_estado",
            "ejecutivo_asignado", "prospecto.ejecutivo", "lead_temperature_effective",
            "lifecycle.assigned_at", "lifecycle.first_valid_management_at",
            "lifecycle.current_assignment_cycle_id", "origen", "lead_origin", "source_type",
            "prospecto.origen", "prospecto.canal_origen", "operacion", "prospecto.operacion",
            "comuna", "region", "zone", "prospecto.comuna", "prospecto.region",
            "prospecto.ubicacion", "prospecto.codigo", "datos_propiedad.codigo",
            "address", "direccion", "latitud", "longitud", "coordinates",
        ]),
        "cycles": field_presence(data["cycles"], [
            "_id", "assignment_cycle_id", "lead_id", "assigned_to_user_id",
            "assigned_to_display_name", "assigned_at", "sla_started_at",
            "temperature_at_assignment", "hot_started_at", "first_valid_management_at",
            "unassigned_at", "cycle_status", "reason", "cycle_origin",
            "schema_version", "assigned_by", "closed_at", "closed_reason",
        ]),
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=False)
    write_csv(OUTPUT_DIR / "current_expired_leads.csv", expired_rows)
    write_csv(OUTPUT_DIR / "territorial_pool_candidates.csv", territory_rows)
    write_csv(OUTPUT_DIR / "territorial_coverage.csv", territory_summary["coverage_rows"])
    for days in WINDOWS:
        write_csv(OUTPUT_DIR / f"executive_metrics_{days}d.csv", executive_csv_rows[days])
        write_csv(OUTPUT_DIR / f"origin_metrics_{days}d.csv", origin_tables[days])
        write_csv(OUTPUT_DIR / f"operation_metrics_{days}d.csv", operation_tables[days])
    write_csv(OUTPUT_DIR / "temporal_hour_90d.csv", hour_table)
    write_csv(OUTPUT_DIR / "temporal_weekday_90d.csv", weekday_table)

    report = build_report(
        data,
        records,
        quality,
        window_metrics,
        executive_by_window,
        current,
        expired,
        expired_rows,
        territory_rows,
        territory_summary,
        origin_tables,
        operation_tables,
        hour_table,
        weekday_table,
        field_profiles,
    )
    OUTPUT_REPORT.write_text(report, encoding="utf-8")

    print("FASE 0 AUDIT COMPLETED")
    print(f"as_of_chile={data['as_of'].astimezone(CHILE_TZ).isoformat()}")
    print(f"leads={len(data['leads'])} cycles={len(data['cycles'])} active_cycles={quality['strict_active']}")
    print(f"current_active_leads={len(current)} current_expired={len(expired_rows)}")
    print(f"territory={dict(territory_summary['classifications'])}")
    print(f"report={OUTPUT_REPORT}")
    print(f"data_dir={OUTPUT_DIR}")
    print("MongoDB writes = 0")
    print("Reassignments executed = 0")
    print("Leads modified = 0")
    print("Environment variables modified = 0")
    print("Deploy = NO")
    print("Flags modified = 0")


if __name__ == "__main__":
    main()
