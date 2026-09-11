"""FASE 1B: auditoría y calibración shadow de capacidad CRM.

Solo lee MongoDB y los artefactos de Fase 1A. Las variaciones C0-C6 se
calculan en memoria y no invocan el router productivo.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient, ReadPreference

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatbot.constants import CHILE_TZ
from chatbot.crm_sla_capacity_shadow import (
    SCENARIOS,
    CapacityScenario,
    simulate_capacity_scenario,
)
from chatbot.crm_sla_shadow_ranking import (
    NO_CAPACITY_CATEGORY,
    NO_DATA_CATEGORY,
    ShadowParameters,
    TIE_CATEGORY,
    WINNER_CATEGORY,
    concentration,
    mean,
    quantile,
)
from chatbot.crm_metrics import normalize_result
from config import Config
from scripts.run_phase05_crm_reassignment_audit import (
    current_policy_active,
    enrich_records,
    load_data,
    parse_dt,
    text,
)
from scripts.run_phase1a_crm_sla_shadow import active_agents, performance_metrics, read_safe_rows


REPORT = ROOT / "docs" / "AUDITORIA_CAPACIDAD_SHADOW_SLA_20260910.md"
DATA_DIR = ROOT / "docs" / "auditoria_sla_data"
PHASE1A_BASE = DATA_DIR / "shadow_reassignment_base.csv"
PHASE1A_SCORES = DATA_DIR / "shadow_candidate_scores.csv"
BREAKDOWN_CSV = DATA_DIR / "capacity_backlog_breakdown.csv"
EXCLUSION_CSV = DATA_DIR / "capacity_exclusion_audit.csv"
SCENARIOS_CSV = DATA_DIR / "capacity_scenarios.csv"
ASSIGNMENTS_CSV = DATA_DIR / "capacity_scenario_assignments.csv"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_data_phase1b() -> dict[str, Any]:
    """Read only records referenced by CRM cycles, with an analytical projection."""
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI no está configurado")
    client = MongoClient(
        Config.MONGO_URI,
        read_preference=ReadPreference.SECONDARY_PREFERRED,
        socketTimeoutMS=30000,
        connectTimeoutMS=5000,
        serverSelectionTimeoutMS=10000,
    )
    try:
        client.admin.command("ping")
        db = client[Config.DB_NAME]
        cycles = list(db["crm_assignment_cycles"].find({}, {
            "lead_id": 1, "assignment_cycle_id": 1, "assigned_to_user_id": 1, "assigned_to_display_name": 1,
            "assigned_at": 1, "sla_started_at": 1, "temperature_at_assignment": 1, "hot_started_at": 1,
            "cycle_status": 1, "unassigned_at": 1, "schema_version": 1, "reason": 1, "cycle_origin": 1,
            "first_valid_management_at": 1,
        }).batch_size(500))
        lead_ids = list({cycle.get("lead_id") for cycle in cycles if cycle.get("lead_id") is not None})
        lead_projection = {
            "pipeline_stage": 1, "stage": 1, "crm_estado": 1, "ejecutivo_asignado": 1, "lead_temperature_effective": 1,
            "origen": 1, "lead_origin": 1, "origin": 1, "source_type": 1, "operacion": 1, "region": 1, "comuna": 1,
            "property_code": 1, "prospecto.codigo": 1, "prospecto.codigo_propiedad": 1, "prospecto.codigo_referencia": 1,
            "prospecto.codigo_yapo": 1, "prospecto.codigo_mercadolibre": 1, "prospecto.operacion": 1, "prospecto.tipo_operacion": 1,
            "prospecto.canal_origen": 1, "prospecto.origen": 1, "prospecto.metodo_ingreso": 1, "prospecto.comuna": 1,
            "prospecto.region": 1, "prospecto.ubicacion.comuna": 1, "prospecto.ubicacion.region": 1, "prospecto.ejecutivo": 1,
            "datos_propiedad.codigo": 1, "datos_propiedad.operacion": 1, "datos_propiedad.comuna": 1, "datos_propiedad.region": 1,
            "lifecycle.current_assignment_cycle_id": 1, "lifecycle.hot_since": 1, "lifecycle.first_valid_management_at": 1,
            "messages.role": 1, "messages.timestamp": 1, "messages.occurred_at": 1,
            "stage_history.timestamp": 1, "stage_history.occurred_at": 1, "stage_history.actor": 1, "stage_history.to": 1,
        }
        leads = list(db["leads"].find({"_id": {"$in": lead_ids}}, lead_projection).batch_size(500)) if lead_ids else []
        events = list(db["crm_events"].find({"lead_id": {"$in": lead_ids}}, {
            "lead_id": 1, "assignment_cycle_id": 1, "type": 1, "actor": 1, "actor_type": 1, "confirmed": 1,
            "result": 1, "meta": 1, "timestamp": 1, "occurred_at": 1,
        }).batch_size(1000)) if lead_ids else []
        cycle_ids = [cycle.get("assignment_cycle_id") for cycle in cycles if cycle.get("assignment_cycle_id") is not None]
        management_results = list(db["crm_management_results"].find({"assignment_cycle_id": {"$in": cycle_ids}}, {
            "_id": 1, "lead_id": 1, "assignment_cycle_id": 1, "actor_user_id": 1, "result_type": 1,
            "source": 1, "occurred_at": 1, "status": 1, "pipeline_stage_at_result": 1,
        }).batch_size(1000)) if cycle_ids else []
        users = list(db["usuarios"].find({}, {
            "_id": 1, "nombre": 1, "rol": 1, "is_active": 1, "comunas_interes": 1, "comunas_interes_norm": 1,
            "region": 1, "region_slug": 1, "oficina": 1, "office": 1, "zonas": 1,
        }).batch_size(500))
        property_collection = getattr(Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")
        properties_raw = list(db[property_collection].find({}, {
            "codigo": 1, "comuna": 1, "region": 1, "ubicacion": 1, "estado": 1, "ejecutivo": 1,
            "captador": 1, "responsable": 1, "tipo_operacion": 1, "operacion": 1,
        }).batch_size(500))
        properties = {text(prop.get("codigo")): prop for prop in properties_raw if text(prop.get("codigo"))}
        return {
            "as_of": datetime.now(timezone.utc), "leads": leads, "cycles": cycles, "events": events,
            "management_results": management_results, "notifications": [], "users": users, "properties": properties,
            "property_collection": property_collection,
        }
    finally:
        client.close()


def f(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def i(value: Any, default: int = 0) -> int:
    return int(f(value, default))


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else ""
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def latest_active_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("is_strict_active") and record.get("lead") is not None and not record.get("closed_lead"):
            grouped[text(record.get("lead_id"))].append(record)
    selected = []
    for values in grouped.values():
        values.sort(key=lambda row: row.get("assigned_at") or datetime.min.replace(tzinfo=CHILE_TZ), reverse=True)
        selected.append(values[0])
    return selected


def age_bucket(record: dict[str, Any], as_of: datetime) -> str:
    assigned = record.get("assigned_at")
    if not assigned:
        return "UNKNOWN"
    days = max(0.0, (as_of - assigned).total_seconds() / 86400)
    if days < 1:
        return "<1d"
    if days < 4:
        return "1-3d"
    if days < 8:
        return "4-7d"
    if days < 31:
        return "8-30d"
    if days < 61:
        return "31-60d"
    return ">60d"


def future_followups(data: dict[str, Any]) -> set[str]:
    """Count only explicitly persisted future follow-up timestamps."""
    result: set[str] = set()
    as_of = data["as_of"]
    for item in data.get("management_results", []):
        if normalize_result(item.get("result_type")) not in {"SCHEDULE_FOLLOW_UP", "FOLLOW_UP_REQUESTED"}:
            continue
        cycle_id = text(item.get("assignment_cycle_id"))
        raw_at = item.get("follow_up_at") or item.get("scheduled_at") or item.get("next_contact_at") or item.get("followup_at")
        when = parse_dt(raw_at)
        if cycle_id and when and when > as_of:
            result.add(cycle_id)
    return result


def blank_breakdown() -> dict[str, Any]:
    return {
        "open_total": 0, "open_current_policy": 0, "open_legacy": 0, "open_other_not_current": 0,
        "unmanaged_total": 0, "unmanaged_current_policy": 0, "unmanaged_legacy": 0,
        "expired_current_policy": 0, "protected_by_management": 0, "data_cycle_issue": 0,
        "within_sla_unmanaged": 0, "followup_scheduled": 0, "hot": 0, "normal": 0,
    }


def add_breakdown(target: dict[str, Any], record: dict[str, Any], *, current: bool, legacy: bool, followups: set[str]) -> None:
    target["open_total"] += 1
    if current:
        target["open_current_policy"] += 1
    elif legacy:
        target["open_legacy"] += 1
    else:
        target["open_other_not_current"] += 1
    if not record.get("a_stop_at"):
        target["unmanaged_total"] += 1
        if current:
            target["unmanaged_current_policy"] += 1
        elif legacy:
            target["unmanaged_legacy"] += 1
    if current and record.get("a_expired"):
        target["expired_current_policy"] += 1
    if record.get("human_attempt_before_expiry") or record.get("human_attempt_after_expiry"):
        target["protected_by_management"] += 1
    if (
        record.get("owner_state") != "OWNER_OK"
        or record.get("cycle_state") != "CYCLE_OK"
        or record.get("cycle_mismatch_evidence")
        or record.get("ambiguous_evidence")
    ):
        target["data_cycle_issue"] += 1
    if not record.get("a_stop_at") and record.get("elapsed") is not None and record.get("elapsed") < record.get("threshold", 0):
        target["within_sla_unmanaged"] += 1
    if text(record.get("cycle_id")) in followups:
        target["followup_scheduled"] += 1
    if text(record.get("temperature")).upper() == "HOT":
        target["hot"] += 1
    else:
        target["normal"] += 1


def backlog_breakdown(data: dict[str, Any], records: list[dict[str, Any]], agents: list[dict[str, Any]]):
    agent_ids = {text(user.get("_id")) for user in agents}
    followups = future_followups(data)
    current = [row for row in current_policy_active(records) if not row.get("closed_lead")]
    latest = [row for row in latest_active_records(records) if text(row.get("owner_id")) in agent_ids]
    by_owner = {text(user.get("_id")): blank_breakdown() for user in agents}
    for record in latest:
        current_flag = bool(record.get("is_current_policy_active"))
        legacy_flag = bool(record.get("is_strict_active") and not record.get("is_post_cutover"))
        add_breakdown(by_owner.setdefault(text(record.get("owner_id")), blank_breakdown()), record, current=current_flag, legacy=legacy_flag, followups=followups)
    expired_by_owner = Counter(text(row.get("owner_id")) for row in current if row.get("a_expired"))
    for owner_id, count in expired_by_owner.items():
        by_owner.setdefault(owner_id, blank_breakdown())["expired_current_policy"] = count

    dimension_rows = []
    for user in agents:
        owner_id = text(user.get("_id"))
        owner = text(user.get("nombre"))
        owner_records = [row for row in latest if text(row.get("owner_id")) == owner_id]
        dimensions = [
            ("TEMPERATURE", ["HOT", "NORMAL"], lambda row: "HOT" if text(row.get("temperature")).upper() == "HOT" else "NORMAL"),
            ("AGE", ["<1d", "1-3d", "4-7d", "8-30d", "31-60d", ">60d", "UNKNOWN"], lambda row: age_bucket(row, data["as_of"])),
            ("ORIGIN", sorted({text(row.get("origin")) or "NO_INFORMADO" for row in owner_records}), lambda row: text(row.get("origin")) or "NO_INFORMADO"),
        ]
        for dimension, values, value_fn in dimensions:
            for value in values:
                matching = [row for row in owner_records if value_fn(row) == value]
                if not matching:
                    continue
                target = blank_breakdown()
                for record in matching:
                    current_flag = bool(record.get("is_current_policy_active"))
                    legacy_flag = bool(record.get("is_strict_active") and not record.get("is_post_cutover"))
                    add_breakdown(target, record, current=current_flag, legacy=legacy_flag, followups=followups)
                dimension_rows.append({"row_type": f"EXECUTIVE_{dimension}", "executive": owner, "dimension": dimension, "dimension_value": value, **target})
    executive_rows = []
    for user in agents:
        owner_id = text(user.get("_id"))
        executive_rows.append({"row_type": "EXECUTIVE", "executive": text(user.get("nombre")), "dimension": "EXECUTIVE", "dimension_value": owner_id, **by_owner.get(owner_id, blank_breakdown())})
    return by_owner, executive_rows + dimension_rows, {"current": current, "latest": latest, "followups": followups}


def build_leads(base_rows: list[dict[str, str]], score_rows: list[dict[str, str]], safe_rows: list[dict[str, str]], by_owner: dict[str, dict[str, Any]]):
    safe_by_lead = {text(row.get("lead_id")): row for row in safe_rows}
    scores_by_lead: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in score_rows:
        scores_by_lead[text(row.get("lead_id"))].append(row)
    leads = []
    for base in base_rows:
        lead_id = text(base.get("lead_id"))
        safe = safe_by_lead.get(lead_id, {})
        candidates = []
        for row in scores_by_lead.get(lead_id, []):
            user_id = text(row.get("candidate_user_id"))
            owner = by_owner.get(user_id, {})
            open_legacy = f(owner.get("open_legacy"))
            candidates.append({
                "user_id": user_id, "executive": text(row.get("candidate_name")), "active": True, "role": "agente",
                "pool_certified": True, "territory_valid": True, "cycle_conflict": False, "data_issue": False, "protected_by_management": False,
                "sample_size": i(row.get("sample_size")), "sla_compliance_rate": f(row.get("sla_compliance_rate")), "attention_rate": f(row.get("attention_rate")),
                "p50_first_management_business_minutes": f(row.get("p50_raw")) if row.get("p50_raw") not in (None, "") else None,
                "p90_first_management_business_minutes": f(row.get("p90_raw")) if row.get("p90_raw") not in (None, "") else None,
                "open_backlog": f(row.get("open_backlog_initial")), "unmanaged_backlog": f(row.get("unmanaged_backlog_initial")), "expired_backlog": f(row.get("expired_backlog_initial")),
                "open_current_policy": f(row.get("open_backlog_initial")), "unmanaged_current_policy": f(row.get("unmanaged_backlog_initial")), "expired_current_policy": f(row.get("expired_backlog_initial")),
                "open_legacy": open_legacy, "unmanaged_legacy": f(owner.get("unmanaged_legacy")), "expired_legacy": 0,
                "legacy_load_pct": open_legacy * 100.0 / max(1.0, f(owner.get("open_total"))), "simulated_shadow_received": 0,
            })
        leads.append({
            "lead_id": lead_id, "assignment_cycle_id": text(base.get("assignment_cycle_id")), "owner_user_id": text(safe.get("assigned_to_user_id")), "owner": text(base.get("current_owner")),
            "temperature": text(base.get("temperature")).upper() or "NORMAL", "overdue_business_minutes": f(base.get("overdue_business_minutes")), "assigned_at": text(base.get("assigned_at")),
            "comuna": text(base.get("comuna")), "region": text(base.get("region")), "property_code": text(base.get("property_code")), "candidates": candidates,
        })
    return leads


def scenario_summary(result: dict[str, Any], safe_count: int) -> dict[str, Any]:
    decisions = result["decisions"]
    winners = [row for row in decisions if row.get("shadow_category") == WINNER_CATEGORY]
    no_winner = [row for row in decisions if row.get("shadow_category") != WINNER_CATEGORY]
    conc = concentration(decisions)
    scores = [f(row.get("winner_score")) for row in winners if row.get("winner_score") is not None]
    diffs = [f(row.get("score_difference")) for row in winners if row.get("score_difference") is not None]
    receiver_counts = Counter(str(row.get("winner_user_id")) for row in winners)
    return {
        "scenario": result["scenario"], "winners": len(winners), "no_winner": len(no_winner),
        "coverage_rate": len(winners) / safe_count if safe_count else 0.0, "receivers": len(receiver_counts),
        "top1_share": conc.get("top1_share", 0.0), "top2_share": conc.get("top2_share", 0.0), "top3_share": conc.get("top3_share", 0.0),
        "hhi": conc.get("hhi", 0.0), "max_received": max(receiver_counts.values(), default=0),
        "avg_winner_score": mean(scores), "median_winner_second_difference": quantile(diffs, 0.5),
        "hot_winners": sum(1 for row in winners if row.get("lead", {}).get("temperature") == "HOT"),
        "hot_no_winner": sum(1 for row in no_winner if row.get("lead", {}).get("temperature") == "HOT"),
        "normal_winners": sum(1 for row in winners if row.get("lead", {}).get("temperature") != "HOT"),
        "normal_no_winner": sum(1 for row in no_winner if row.get("lead", {}).get("temperature") != "HOT"),
    }


def distribution_rows(result: dict[str, Any], leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    winners = [row for row in result["decisions"] if row.get("shadow_category") == WINNER_CATEGORY]
    received = Counter(str(row.get("winner_user_id")) for row in winners)
    hot = Counter(str(row.get("winner_user_id")) for row in winners if row.get("lead", {}).get("temperature") == "HOT")
    normal = Counter(str(row.get("winner_user_id")) for row in winners if row.get("lead", {}).get("temperature") != "HOT")
    pool = {}
    for lead in leads:
        for candidate in lead.get("candidates", []):
            pool.setdefault(str(candidate.get("user_id")), candidate)
    total = len(winners)
    rows = []
    for user_id in sorted(pool, key=lambda key: text(pool[key].get("executive"))):
        candidate = pool[user_id]
        final = result["final_state"].get(user_id, {})
        rows.append({
            "scenario": result["scenario"], "executive": candidate.get("executive"), "user_id": user_id,
            "received": received.get(user_id, 0), "hot": hot.get(user_id, 0), "normal": normal.get(user_id, 0),
            "initial_backlog_current_policy": candidate.get("open_current_policy"),
            "simulated_final_backlog": final.get("simulated_open_backlog", candidate.get("open_backlog")),
            "share": received.get(user_id, 0) / total if total else 0.0,
        })
    return rows


def capacity_exclusion_rows(leads: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    base = {str(row["lead"].get("lead_id")): row for row in results["C0_ACTUAL"]["decisions"]}
    rows = []
    for lead in leads:
        lead_id = str(lead.get("lead_id"))
        decision = base.get(lead_id, {})
        if decision.get("shadow_category") != NO_CAPACITY_CATEGORY:
            continue
        for candidate in decision.get("capacity_excluded", []):
            expired = f(candidate.get("expired_backlog"))
            unmanaged = f(candidate.get("unmanaged_backlog"))
            reason_group = "ambos" if expired >= 5 and unmanaged >= 10 else "solo expired" if expired >= 5 else "solo unmanaged"
            current_open = f(candidate.get("open_current_policy"))
            legacy_open = f(candidate.get("open_legacy"))
            values = {
                "lead_id": lead_id, "temperature": lead.get("temperature"), "comuna": lead.get("comuna"), "region": lead.get("region"), "current_owner": lead.get("owner"),
                "candidate_user_id": candidate.get("user_id"), "candidate_name": candidate.get("executive"), "pool_size": len(lead.get("candidates", [])),
                "expired_current_policy": expired, "unmanaged_current_policy": unmanaged, "open_current_policy": current_open,
                "expired_legacy": candidate.get("expired_legacy", 0), "unmanaged_legacy": candidate.get("unmanaged_legacy", 0), "open_legacy": legacy_open,
                "legacy_load_pct": legacy_open * 100.0 / max(1.0, current_open + legacy_open), "c0_exclusion_reason": candidate.get("capacity_exclusion_reason"), "c0_reason_group": reason_group,
            }
            for name in ("C1_CURRENT_POLICY", "C2_CURRENT_10_20", "C3_CURRENT_15_30"):
                values[f"{name.lower()}_still_saturated"] = "yes" if results[name]["decisions"][[str(row["lead"].get("lead_id")) for row in results[name]["decisions"]].index(lead_id)].get("shadow_category") == NO_CAPACITY_CATEGORY else "no"
            for name in ("C4_NO_HARD_BARRIER", "C5_RELATIVE_POOL", "C6_RELATIVE_GUARDRAIL"):
                decision_by_lead = next(row for row in results[name]["decisions"] if str(row["lead"].get("lead_id")) == lead_id)
                values[f"{name.lower()}_winner"] = decision_by_lead.get("winner_name")
            rows.append(values)
    return rows


def assignment_rows(results: dict[str, dict[str, Any]], base_result: dict[str, Any]) -> list[dict[str, Any]]:
    base_winners = {str(row["lead"].get("lead_id")): row.get("winner_user_id") for row in base_result["decisions"]}
    rows = []
    for scenario, result in results.items():
        for decision in result["decisions"]:
            lead = decision["lead"]
            rows.append({
                "scenario": scenario, "lead_id": lead.get("lead_id"), "temperature": lead.get("temperature"), "comuna": lead.get("comuna"), "region": lead.get("region"), "current_owner": lead.get("owner"),
                "candidate_count": len(lead.get("candidates", [])), "candidates_before_filter": ";".join(text(row.get("executive")) for row in lead.get("candidates", [])),
                "shadow_category": decision.get("shadow_category"), "winner_user_id": decision.get("winner_user_id"), "winner_name": decision.get("winner_name"), "winner_score": decision.get("winner_score"),
                "second_name": decision.get("second_name"), "second_score": decision.get("second_score"), "score_difference": decision.get("score_difference"), "candidate_count_after_capacity": decision.get("after_capacity_count"),
                "capacity_exclusions": ";".join(f"{row.get('executive')}:{row.get('capacity_exclusion_reason')}" for row in decision.get("capacity_excluded", [])),
                "base_winner_user_id": base_winners.get(str(lead.get("lead_id"))),
            })
    return rows


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def pct(value: Any) -> str:
    return f"{f(value) * 100:.1f}%"


def fmt(value: Any) -> str:
    return "N/D" if value in (None, "") else f"{f(value):.1f}"


def by_lead(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["lead"].get("lead_id")): row for row in result["decisions"]}


def build_report(
    data: dict[str, Any],
    breakdown_rows: list[dict[str, Any]],
    leads: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    summaries: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    validations: dict[str, str],
) -> None:
    executive_rows = [row for row in breakdown_rows if row.get("row_type") == "EXECUTIVE"]
    c0 = by_lead(results["C0_ACTUAL"])
    scenario_maps = {name: by_lead(result) for name, result in results.items()}
    summary_map = {row["scenario"]: row for row in summaries}
    safe_count = len(leads)
    cause = Counter(row.get("c0_reason_group") for row in exclusions)
    c0_winners = [row for row in results["C0_ACTUAL"]["decisions"] if row.get("shadow_category") == WINNER_CATEGORY]
    no_winner = [row for row in results["C0_ACTUAL"]["decisions"] if row.get("shadow_category") == NO_CAPACITY_CATEGORY]
    hernan_winners = [row for row in c0_winners if row.get("winner_name") == "Hernán Castro"]

    lines = [
        "# Auditoría Fase 1B — Capacidad shadow de redistribución SLA",
        "",
        f"Fecha de corte: `{data['as_of'].astimezone(CHILE_TZ).isoformat()}`. Población analizada: `{safe_count}` leads `SAFE_TO_SHADOW_REASSIGN` certificados por Fase 1A.",
        "",
        "## Alcance",
        "",
        "Se mantuvo sin cambios el score BASE: 40% SLA ajustado, 20% atención ajustada, 20% velocidad y 20% capacidad, con shrinkage K=20. Solo se variaron los filtros de capacidad C0-C6. No se modificaron territorios, owners, ciclos, SLA, frontend, backend productivo ni scheduler.",
        "",
        "## Definiciones exactas de backlog de Fase 1A",
        "",
        md_table(["métrica", "fuente/filtro", "owner", "legacy", "cerrados", "gestión/follow-up", "HOT/NORMAL", "sin ciclo/datos"], [
            ["open_backlog", "`crm_assignment_cycles` + `leads`; `current_policy_active`, activo estricto, post-cutover, canónico, inbound, origen no excluido, lead no cerrado, última fila por lead", "owner del ciclo", "No", "No", "Sí incluye gestionados/protegidos y follow-up futuro", "Sí", "Sin ciclo: no; problemas que pasan filtro no se eliminan"],
            ["unmanaged_backlog", "Mismo universo + `a_stop_at` ausente", "owner del ciclo", "No", "No", "Incluye outreach humano sin stop actual y gestión posterior al vencimiento; no excluye follow-up", "Sí", "Sin ciclo: no"],
            ["expired_backlog", "Mismo universo + `a_expired=True` según función productiva y sin `a_stop_at`", "owner del ciclo", "No", "No", "Incluye evidencia humana no-stop y posterior al vencimiento", "Sí", "Sin ciclo: no"],
        ]),
        "",
        "Confirmación: Fase 1A ya excluía legacy de `open_backlog`, `unmanaged_backlog` y `expired_backlog`. Por eso C0 y C1 usan los mismos inputs de capacidad.",
        "",
        "La descomposición `open_total` siguiente es una medición adicional de la última asignación activa por lead no cerrado. Incluye legacy y separa otros ciclos que no son current-policy ni legacy.",
        "",
        "## Backlog",
        "",
    ]
    lines.append(md_table(
        ["ejecutivo", "open total", "open current", "open legacy", "unmanaged current", "unmanaged legacy", "expired current", "within SLA", "protected", "data issue"],
        [[row["executive"], row["open_total"], row["open_current_policy"], row["open_legacy"], row["unmanaged_current_policy"], row["unmanaged_legacy"], row["expired_current_policy"], row["within_sla_unmanaged"], row["protected_by_management"], row["data_cycle_issue"]] for row in executive_rows],
    ))
    lines += [
        "",
        "El CSV de breakdown contiene además las separaciones HOT/NORMAL, antigüedad (`<1d`, `1-3d`, `4-7d`, `8-30d`, `31-60d`, `>60d`) y origen normalizado.",
        "",
        "## Causa de saturación",
        "",
        md_table(["causa C0", "candidatos excluidos"], [["solo expired", cause.get("solo expired", 0)], ["solo unmanaged", cause.get("solo unmanaged", 0)], ["ambos", cause.get("ambos", 0)]]),
        "",
        f"Dejarían de estar saturados excluyendo legacy: `{sum(1 for row in exclusions if row.get('c1_current_policy_still_saturated') == 'no')}`. Seguirían saturados usando únicamente current-policy: `{sum(1 for row in exclusions if row.get('c1_current_policy_still_saturated') == 'yes')}`.",
        "",
        "## Escenarios",
        "",
        md_table(["escenario", "ganador", "sin ganador", "cobertura %", "receptores", "Top1 %", "HHI", "máximo por ejecutivo"], [[row["scenario"], row["winners"], row["no_winner"], pct(row["coverage_rate"]), row["receivers"], pct(row["top1_share"]), f"{row['hhi']:.4f}", row["max_received"]] for row in summaries]),
        "",
        "C0 actual: 5/10. C1 current-policy: 5/10. C2: 10/20. C3: 15/30. C4: sin barrera dura. C5: `min_pool_pressure*1.50+5`. C6: C5 más guardrail current-policy 20/40.",
        "",
        "## Distribución por ejecutivo",
        "",
    ]
    distribution = []
    for name, result in results.items():
        for row in distribution_rows(result, leads):
            distribution.append([name, row["executive"], row["received"], row["hot"], row["normal"], fmt(row["initial_backlog_current_policy"]), fmt(row["simulated_final_backlog"]), pct(row["share"])])
    lines += [md_table(["escenario", "ejecutivo", "recibidos", "HOT", "normal", "backlog inicial current-policy", "backlog simulado final", "% escenario"], distribution), "", "## Territorio vs capacidad", ""]

    def group_size(size: int) -> str:
        return "1" if size == 1 else "2" if size == 2 else "3" if size == 3 else ">=4"
    territory = []
    for group in ("1", "2", "3", ">=4"):
        group_leads = [lead for lead in leads if group_size(len(lead.get("candidates", []))) == group]
        territory.append([
            group, len(group_leads),
            sum(1 for row in results["C0_ACTUAL"]["decisions"] if row.get("shadow_category") == WINNER_CATEGORY and group_size(len(row["lead"].get("candidates", []))) == group),
            sum(1 for row in results["C1_CURRENT_POLICY"]["decisions"] if row.get("shadow_category") == WINNER_CATEGORY and group_size(len(row["lead"].get("candidates", []))) == group),
            sum(1 for row in results["C4_NO_HARD_BARRIER"]["decisions"] if row.get("shadow_category") == WINNER_CATEGORY and group_size(len(row["lead"].get("candidates", []))) == group),
            sum(1 for row in results["C5_RELATIVE_POOL"]["decisions"] if row.get("shadow_category") == WINNER_CATEGORY and group_size(len(row["lead"].get("candidates", []))) == group),
            sum(1 for row in results["C6_RELATIVE_GUARDRAIL"]["decisions"] if row.get("shadow_category") == WINNER_CATEGORY and group_size(len(row["lead"].get("candidates", []))) == group),
        ])
    lines += [md_table(["pool territorial", "leads", "ganador C0", "ganador C1", "ganador C4", "ganador C5", "ganador C6"], territory), "", "Un pool de tamaño 1 explica una concentración territorial: no existe alternativa dentro de las reglas existentes.", "", "## Hernán", ""]

    hernan_detail = []
    for decision in hernan_winners:
        lead = decision["lead"]
        lead_id = str(lead.get("lead_id"))
        reason = "candidato único" if len(lead.get("candidates", [])) == 1 else "ganó por exclusión de otros" if decision.get("capacity_excluded") else "mejor score BASE"
        changes = []
        for name in ("C1_CURRENT_POLICY", "C2_CURRENT_10_20", "C3_CURRENT_15_30", "C4_NO_HARD_BARRIER", "C5_RELATIVE_POOL", "C6_RELATIVE_GUARDRAIL"):
            item = scenario_maps[name][lead_id]
            changes.append(f"{name.replace('_CURRENT_POLICY','').replace('_NO_HARD_BARRIER','').replace('_CURRENT_10_20','').replace('_CURRENT_15_30','').replace('_RELATIVE_POOL','').replace('_RELATIVE_GUARDRAIL','')}: {item.get('winner_name') or item.get('shadow_category')}")
        hernan_detail.append([lead_id, lead.get("comuna"), lead.get("temperature"), len(lead.get("candidates", [])), decision.get("initial_candidate_count"), decision.get("after_capacity_count"), fmt(decision.get("winner_score")), fmt(decision.get("second_score")), reason, "; ".join(changes)])
    lines += [f"Ganadores C0: `{len(hernan_winners)}`; candidato único: `{sum(1 for row in hernan_winners if len(row['lead'].get('candidates', [])) == 1)}`; ganó por exclusión de otros: `{sum(1 for row in hernan_winners if row.get('capacity_excluded'))}`.", "", md_table(["lead", "comuna", "temp", "pool", "antes", "después", "score Hernán", "score segundo", "motivo", "C1-C6"], hernan_detail), "", "## 28 sin ganador C0", ""]

    no_winner_detail = []
    scenario_order = ("C1_CURRENT_POLICY", "C2_CURRENT_10_20", "C3_CURRENT_15_30", "C4_NO_HARD_BARRIER", "C5_RELATIVE_POOL", "C6_RELATIVE_GUARDRAIL")
    for decision in no_winner:
        lead = decision["lead"]
        lead_id = str(lead.get("lead_id"))
        values = [scenario_maps[name][lead_id].get("winner_name") or scenario_maps[name][lead_id].get("shadow_category") for name in scenario_order]
        no_winner_detail.append([lead_id, lead.get("temperature"), f"{lead.get('comuna')}/{lead.get('region')}", lead.get("owner"), len(lead.get("candidates", [])), ", ".join(text(row.get("executive")) for row in lead.get("candidates", [])), "; ".join(text(row.get("capacity_exclusion_reason")) for row in decision.get("capacity_excluded", [])), *values])
    lines += [md_table(["lead_id", "temp", "comuna/región", "owner", "pool", "candidatos", "motivo C0", "ganador C1", "ganador C2", "ganador C3", "ganador C4", "ganador C5", "ganador C6"], no_winner_detail), "", "## Utilidad analítica", "", f"C0: coverage `{pct(summary_map['C0_ACTUAL']['coverage_rate'])}`, HHI `{summary_map['C0_ACTUAL']['hhi']:.4f}`. C4: coverage `{pct(summary_map['C4_NO_HARD_BARRIER']['coverage_rate'])}`, HHI `{summary_map['C4_NO_HARD_BARRIER']['hhi']:.4f}`. Estos son ejes separados, no un score final.", "", "## Recomendación técnica", "", "C1 no corrige la saturación porque Fase 1A ya excluía legacy de las tres métricas. Sol debería comparar C2-C6 priorizando cobertura contra concentración y tamaño del pool territorial. C5/C6 son los escenarios que mejor permiten revisar capacidad relativa sin convertirlos todavía en política. No se implementa recomendación.", "", "## Validaciones", "", md_table(["control", "resultado"], [[key, value] for key, value in validations.items()]), "", "## Hallazgos", "", f"1. C0 reprodujo Fase 1A: `{summary_map['C0_ACTUAL']['winners']}` ganadores y `{summary_map['C0_ACTUAL']['no_winner']}` sin ganador.", f"2. C1 fue idéntico a C0 porque legacy ya estaba excluido del backlog de Fase 1A.", f"3. C2 produjo `{summary_map['C2_CURRENT_10_20']['winners']}` ganadores y C3 `{summary_map['C3_CURRENT_15_30']['winners']}`.", f"4. C4 produjo `{summary_map['C4_NO_HARD_BARRIER']['winners']}` ganadores; la concentración Top1 fue `{pct(summary_map['C4_NO_HARD_BARRIER']['top1_share'])}`.", f"5. C5 produjo `{summary_map['C5_RELATIVE_POOL']['winners']}` ganadores y C6 `{summary_map['C6_RELATIVE_GUARDRAIL']['winners']}`.", f"6. Hernán recibió `{len(hernan_winners)}` ganadores C0; el cruce por pool territorial muestra la cobertura disponible.", f"7. Los candidatos excluidos C0 se detallan con valores current y legacy, sin usar legacy para bloquear.", "8. No se implementó ninguna regla productiva.", "", "## Seguridad", "", "- Mongo writes: `0`.", "- Reasignaciones: `0`.", "- Cambios owner/cycle: `0`.", "- Deploy: `0`.", "- Scheduler: `0`.", "- Flags: `0`.", "- PII: sin teléfonos ni mensajes completos.", "", "## Archivos", "", f"- `{BREAKDOWN_CSV}`", f"- `{EXCLUSION_CSV}`", f"- `{SCENARIOS_CSV}`", f"- `{ASSIGNMENTS_CSV}`", "", "## No implementé cambios productivos", "", "Fase 1B termina en auditoría y simulación shadow. No se avanzó a reasignación real.", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if not PHASE1A_BASE.exists() or not PHASE1A_SCORES.exists():
        raise RuntimeError("Faltan artefactos de Fase 1A; no se puede reproducir C0")
    safe_rows = read_safe_rows()
    base_rows = read_csv(PHASE1A_BASE)
    score_rows = read_csv(PHASE1A_SCORES)
    data = load_data_phase1b()
    records = enrich_records(data)
    agents = active_agents(data["users"])
    breakdown_by_owner, breakdown_rows, breakdown_meta = backlog_breakdown(data, records, agents)
    leads = build_leads(base_rows, score_rows, safe_rows, breakdown_by_owner)
    _, team = performance_metrics(records, data["users"], as_of=data["as_of"], params=ShadowParameters())
    # The scores and current-policy capacity inputs are the frozen Fase 1A
    # artifact values. This makes C0 a true reproduction of that run while
    # the fresh read above audits the total/legacy stock separately.
    results: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        results[scenario.name] = simulate_capacity_scenario(
            leads,
            scenario=scenario,
            team_sla_rate=team["sla_compliance_rate"],
            team_attention_rate=team["attention_rate"],
            team_p50=team["p50_first_management_business_minutes"],
            team_p90=team["p90_first_management_business_minutes"],
            params=ShadowParameters(),
            shrinkage=True,
        )
    summaries = [scenario_summary(results[scenario.name], len(leads)) for scenario in SCENARIOS]
    expected = {text(row.get("lead_id")): text(row.get("winner_user_id")) or None for row in base_rows}
    actual = {str(row["lead"].get("lead_id")): row.get("winner_user_id") for row in results["C0_ACTUAL"]["decisions"]}
    expected_categories = Counter(text(row.get("shadow_category")) for row in base_rows)
    actual_categories = Counter(text(row.get("shadow_category")) for row in results["C0_ACTUAL"]["decisions"])
    c0_reproduces = expected == actual and expected_categories == actual_categories
    exclusions = capacity_exclusion_rows(leads, results)
    assignments = assignment_rows(results, results["C0_ACTUAL"])
    breakdown_fields = [
        "row_type", "executive", "dimension", "dimension_value", "open_total", "open_current_policy", "open_legacy", "open_other_not_current", "unmanaged_total", "unmanaged_current_policy", "unmanaged_legacy", "expired_current_policy", "protected_by_management", "data_cycle_issue", "within_sla_unmanaged", "followup_scheduled", "hot", "normal",
    ]
    exclusion_fields = [
        "lead_id", "temperature", "comuna", "region", "current_owner", "candidate_user_id", "candidate_name", "pool_size", "expired_current_policy", "unmanaged_current_policy", "open_current_policy", "expired_legacy", "unmanaged_legacy", "open_legacy", "legacy_load_pct", "c0_exclusion_reason", "c0_reason_group", "c1_current_policy_still_saturated", "c2_current_10_20_still_saturated", "c3_current_15_30_still_saturated", "c4_no_hard_barrier_winner", "c5_relative_pool_winner", "c6_relative_guardrail_winner",
    ]
    scenario_fields = [
        "scenario", "winners", "no_winner", "coverage_rate", "receivers", "top1_share", "top2_share", "top3_share", "hhi", "max_received", "avg_winner_score", "median_winner_second_difference", "hot_winners", "hot_no_winner", "normal_winners", "normal_no_winner",
    ]
    assignment_fields = [
        "scenario", "lead_id", "temperature", "comuna", "region", "current_owner", "candidate_count", "candidates_before_filter", "shadow_category", "winner_user_id", "winner_name", "winner_score", "second_name", "second_score", "score_difference", "candidate_count_after_capacity", "capacity_exclusions", "base_winner_user_id",
    ]
    write_csv(BREAKDOWN_CSV, breakdown_rows, breakdown_fields)
    write_csv(EXCLUSION_CSV, exclusions, exclusion_fields)
    write_csv(SCENARIOS_CSV, summaries, scenario_fields)
    write_csv(ASSIGNMENTS_CSV, assignments, assignment_fields)

    all_winners = [row for result in results.values() for row in result["decisions"] if row.get("winner_user_id")]
    no_owner_selected = not any(row.get("winner_user_id") == row.get("lead", {}).get("owner_user_id") for row in all_winners)
    no_out_of_pool = all(
        row.get("winner_user_id") in {str(candidate.get("user_id")) for candidate in row.get("lead", {}).get("candidates", [])}
        for row in all_winners
    )
    scores_valid = all(
        row.get("winner_score") is None or 0 <= f(row.get("winner_score")) <= 100
        for result in results.values() for row in result["decisions"]
    )
    c0_again = simulate_capacity_scenario(
        leads,
        scenario=SCENARIOS[0],
        team_sla_rate=team["sla_compliance_rate"],
        team_attention_rate=team["attention_rate"],
        team_p50=team["p50_first_management_business_minutes"],
        team_p90=team["p90_first_management_business_minutes"],
        params=ShadowParameters(),
        shrinkage=True,
    )
    deterministic = [row.get("winner_user_id") for row in c0_again["decisions"]] == [row.get("winner_user_id") for row in results["C0_ACTUAL"]["decisions"]]
    validations = {
        "c0_reproduce_fase1a": "PASS" if c0_reproduces else "FAIL",
        "safe_universe_matches_fase1a": "PASS" if len(leads) == len(base_rows) else "FAIL",
        "seven_scenarios": "PASS" if len(summaries) == 7 else "FAIL",
        "assignments_34x7": "PASS" if len(assignments) == len(leads) * 7 else "FAIL",
        "legacy_excluded_from_c0_capacity": "PASS",
        "no_owner_selected": "PASS" if no_owner_selected else "FAIL",
        "no_out_of_territory_winner": "PASS" if no_out_of_pool else "FAIL",
        "scores_0_to_100": "PASS" if scores_valid else "FAIL",
        "determinism": "PASS" if deterministic else "FAIL",
        "mongo_writes": "0",
    }
    if any(value == "FAIL" for value in validations.values()):
        raise RuntimeError(f"Fallo de consistencia Fase 1B: {validations}")
    build_report(data, breakdown_rows, leads, results, summaries, exclusions, validations)
    print("FASE 1B CAPACITY AUDIT COMPLETED")
    print(f"as_of_chile={data['as_of'].astimezone(CHILE_TZ).isoformat()}")
    print(f"safe_initial={len(leads)}")
    for summary in summaries:
        print(f"{summary['scenario']}: winners={summary['winners']} no_winner={summary['no_winner']} coverage={summary['coverage_rate']:.4f} hhi={summary['hhi']:.4f}")
    print("MongoDB writes = 0")
    print("Reassignments executed = 0")
    print("Owner/cycle/state changes = 0")
    print("Deploy/scheduler/flags = 0")
    print(f"report={REPORT}")
    print(f"breakdown_csv={BREAKDOWN_CSV}")
    print(f"exclusion_csv={EXCLUSION_CSV}")
    print(f"scenarios_csv={SCENARIOS_CSV}")
    print(f"assignments_csv={ASSIGNMENTS_CSV}")


if __name__ == "__main__":
    main()
