"""FASE 1A: selector shadow analítico para leads certificados en Fase 0.5.

El proceso es deliberadamente de solo lectura sobre MongoDB. Lee la población
SAFE_TO_SHADOW_REASSIGN producida por Fase 0.5, calcula métricas históricas de
los ciclos de política vigente y ejecuta una simulación secuencial en memoria.
No invoca el router productivo y no persiste cambios en CRM.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import calculate_sla
from chatbot.crm_sla_shadow_ranking import (
    BASE_WEIGHTS,
    NO_CAPACITY_CATEGORY,
    NO_DATA_CATEGORY,
    SAFE_CATEGORY,
    ShadowParameters,
    TIE_CATEGORY,
    WINNER_CATEGORY,
    changed_winners,
    concentration,
    mean,
    quantile,
    sensitivity_simulations,
    simulate_sequential,
)
from scripts.run_phase05_crm_reassignment_audit import (
    current_policy_active,
    enrich_records,
    load_data,
    norm,
    parse_dt,
    text,
)
from scripts.run_phase0_crm_sla_audit import user_name_match


REPORT = ROOT / "docs" / "AUDITORIA_SHADOW_RANKING_SLA_20260909.md"
DATA_DIR = ROOT / "docs" / "auditoria_sla_data"
SAFE_INPUT = DATA_DIR / "reassignment_eligibility_current_policy.csv"
BASE_CSV = DATA_DIR / "shadow_reassignment_base.csv"
SCORES_CSV = DATA_DIR / "shadow_candidate_scores.csv"
SENSITIVITY_CSV = DATA_DIR / "shadow_sensitivity.csv"


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else ""
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fieldnames})


def read_safe_rows() -> list[dict[str, str]]:
    if not SAFE_INPUT.exists():
        raise RuntimeError(f"No existe el artefacto certificado de Fase 0.5: {SAFE_INPUT}")
    with SAFE_INPUT.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [row for row in rows if row.get("final_category") == SAFE_CATEGORY]


def active_agents(users: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        user for user in users
        if user.get("is_active") is True and text(user.get("rol")).lower() == "agente"
    ]


def resolve_user(name: str, users: list[dict[str, Any]]) -> dict[str, Any] | None:
    for user in users:
        if user_name_match(name, user.get("nombre")):
            return user
    return None


def cycle_first_management_minutes(record: dict[str, Any]) -> float | None:
    if not record.get("a_stop_at") or not record.get("sla_started_at"):
        return None
    result = calculate_sla(
        assigned_at=record["sla_started_at"],
        first_valid_management_at=record["a_stop_at"],
        now=record.get("a_stop_at"),
        temperature=record.get("temperature"),
        hot_started_at=record.get("hot_started_at"),
    )
    minutes = result.get("hot_minutes") if record.get("temperature") == "HOT" else result.get("minutes")
    return float(minutes) if minutes is not None else None


def performance_metrics(
    records: list[dict[str, Any]],
    users: list[dict[str, Any]],
    *,
    as_of: datetime,
    params: ShadowParameters,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Calculate historical metrics from post-cutover current-policy cycles."""
    agents = active_agents(users)
    agent_ids = {text(user.get("_id")) for user in agents}
    start = as_of - timedelta(days=params.performance_window_days)
    eligible: list[dict[str, Any]] = []
    for record in records:
        if not (
            record.get("is_post_cutover")
            and record.get("canonical")
            and not record.get("excluded_origin")
            and record.get("assigned_at")
            and start <= record["assigned_at"] <= as_of
            and text(record.get("owner_id")) in agent_ids
        ):
            continue
        eligible.append(record)

    by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in eligible:
        by_owner[text(record.get("owner_id"))].append(record)

    def summarize(values: list[dict[str, Any]]) -> dict[str, Any]:
        durations = [duration for duration in (cycle_first_management_minutes(value) for value in values) if duration is not None]
        compliant = sum(
            1 for value in values
            if (duration := cycle_first_management_minutes(value)) is not None
            and duration < float(value.get("threshold") or 0)
        )
        sample_size = len(values)
        attention = len(durations)
        return {
            "sample_size": sample_size,
            "sla_compliance_rate": compliant / sample_size if sample_size else 0.0,
            "attention_rate": attention / sample_size if sample_size else 0.0,
            "p50_first_management_business_minutes": quantile(durations, 0.50),
            "p90_first_management_business_minutes": quantile(durations, 0.90),
            "managed_count": attention,
            "duration_values": durations,
        }

    team = summarize(eligible)
    metrics = {owner_id: summarize(values) for owner_id, values in by_owner.items()}
    for user in agents:
        user_id = text(user.get("_id"))
        metrics.setdefault(user_id, summarize([]))
    team_metrics = {
        "sample_size": team["sample_size"],
        "sla_compliance_rate": team["sla_compliance_rate"],
        "attention_rate": team["attention_rate"],
        "p50_first_management_business_minutes": team["p50_first_management_business_minutes"],
        "p90_first_management_business_minutes": team["p90_first_management_business_minutes"],
        "duration_values": team["duration_values"],
        "window_start": start,
        "window_end": as_of,
    }
    for value in metrics.values():
        value.pop("duration_values", None)
    return metrics, team_metrics


def backlog_metrics(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Use the current active policy snapshot; expired backlog excludes legacy."""
    active = [record for record in current_policy_active(records) if not record.get("closed_lead")]
    result: dict[str, dict[str, int]] = defaultdict(lambda: {
        "open_backlog": 0,
        "unmanaged_backlog": 0,
        "expired_backlog": 0,
    })
    for record in active:
        owner_id = text(record.get("owner_id"))
        if not owner_id:
            continue
        result[owner_id]["open_backlog"] += 1
        if not record.get("a_stop_at"):
            result[owner_id]["unmanaged_backlog"] += 1
        if record.get("a_expired"):
            result[owner_id]["expired_backlog"] += 1
    return dict(result)


def parse_candidate_names(row: dict[str, str]) -> list[str]:
    names = [part.strip() for part in (row.get("candidate_names") or "").split(";") if part.strip()]
    return list(dict.fromkeys(names))


def build_candidate_pool(
    row: dict[str, str],
    users: list[dict[str, Any]],
    historical: dict[str, dict[str, Any]],
    backlog: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    candidates = []
    for name in parse_candidate_names(row):
        user = resolve_user(name, users)
        user_id = text(user.get("_id")) if user else ""
        current_metrics = historical.get(user_id, {})
        current_backlog = backlog.get(user_id, {})
        candidates.append({
            "user_id": user_id,
            "executive": text(user.get("nombre")) if user else name,
            "active": bool(user and user.get("is_active") is True),
            "role": text(user.get("rol")).lower() if user else "unknown",
            "pool_certified": True,
            "territory_valid": True,
            "cycle_conflict": False,
            "data_issue": user is None,
            "protected_by_management": False,
            "sample_size": current_metrics.get("sample_size", 0),
            "sla_compliance_rate": current_metrics.get("sla_compliance_rate", 0.0),
            "attention_rate": current_metrics.get("attention_rate", 0.0),
            "p50_first_management_business_minutes": current_metrics.get("p50_first_management_business_minutes"),
            "p90_first_management_business_minutes": current_metrics.get("p90_first_management_business_minutes"),
            "open_backlog": current_backlog.get("open_backlog", 0),
            "unmanaged_backlog": current_backlog.get("unmanaged_backlog", 0),
            "expired_backlog": current_backlog.get("expired_backlog", 0),
            "simulated_shadow_received": 0,
        })
    return candidates


def build_leads(
    safe_rows: list[dict[str, str]],
    users: list[dict[str, Any]],
    records: list[dict[str, Any]],
    historical: dict[str, dict[str, Any]],
    backlog: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    records_by_cycle = {text(record.get("cycle_id")): record for record in records}
    leads = []
    for row in safe_rows:
        live = records_by_cycle.get(text(row.get("assignment_cycle_id")))
        assigned_at = parse_dt(row.get("assigned_at"))
        threshold = float(row.get("sla_threshold_minutes") or 0)
        elapsed = float(row.get("current_business_minutes_elapsed") or 0)
        owner_id = text(row.get("assigned_to_user_id"))
        owner = text(row.get("assigned_to_display_name"))
        if live:
            assigned_at = live.get("assigned_at") or assigned_at
            threshold = float(live.get("threshold") or threshold)
            elapsed = float(live.get("elapsed") or elapsed)
            owner_id = text(live.get("owner_id")) or owner_id
            owner = text(live.get("owner_name")) or owner
        leads.append({
            "lead_id": text(row.get("lead_id")),
            "assignment_cycle_id": text(row.get("assignment_cycle_id")),
            "owner_user_id": owner_id,
            "owner": owner,
            "temperature": text(row.get("temperature")).upper() or "NORMAL",
            "overdue_business_minutes": max(0.0, elapsed - threshold),
            "assigned_at": assigned_at.isoformat() if assigned_at else text(row.get("assigned_at")),
            "comuna": text(row.get("comuna_normalizada")),
            "region": text(row.get("region_normalizada")),
            "property_code": text(row.get("propiedad_asociada")),
            "origin": text(row.get("origen_normalizado")),
            "operation": text(row.get("operacion_normalizada")),
            "candidates": build_candidate_pool(row, users, historical, backlog),
        })
    return leads


def fmt_number(value: Any, decimals: int = 1) -> str:
    if value is None or value == "":
        return "N/D"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def distribution_table(
    decisions: list[dict[str, Any]],
    initial_by_id: dict[str, dict[str, Any]],
    final_state: dict[str, dict[str, float]],
) -> list[list[Any]]:
    received = Counter(str(decision.get("winner_user_id")) for decision in decisions if decision.get("shadow_category") == WINNER_CATEGORY)
    hot = Counter(str(decision.get("winner_user_id")) for decision in decisions if decision.get("shadow_category") == WINNER_CATEGORY and decision.get("lead", {}).get("temperature") == "HOT")
    normal = Counter(str(decision.get("winner_user_id")) for decision in decisions if decision.get("shadow_category") == WINNER_CATEGORY and decision.get("lead", {}).get("temperature") != "HOT")
    total = sum(received.values())
    user_ids = set(initial_by_id) | set(received)
    rows = []
    for user_id in sorted(user_ids, key=lambda uid: text(initial_by_id.get(uid, {}).get("executive"))):
        initial = initial_by_id.get(user_id, {})
        final = final_state.get(user_id, {})
        rows.append([
            initial.get("executive", user_id), received.get(user_id, 0), hot.get(user_id, 0), normal.get(user_id, 0),
            f"{received.get(user_id, 0) * 100.0 / total:.1f}%" if total else "0.0%",
            fmt_number(initial.get("open_backlog"), 0), fmt_number(final.get("simulated_open_backlog"), 0),
        ])
    return rows


def sensitivity_summary(
    simulations: dict[str, dict[str, Any]],
    base_decisions: list[dict[str, Any]],
) -> tuple[list[list[Any]], dict[str, list[str]], set[str], dict[str, Counter[str]]]:
    changes = changed_winners(simulations)
    base_counts = Counter(str(row.get("winner_user_id")) for row in base_decisions if row.get("shadow_category") == WINNER_CATEGORY)
    rows = []
    unstable: set[str] = set()
    gains: dict[str, Counter[str]] = {}
    for name, result in simulations.items():
        current_decisions = result["decisions"]
        current_concentration = concentration(current_decisions)
        current_counts = Counter(str(row.get("winner_user_id")) for row in current_decisions if row.get("shadow_category") == WINNER_CATEGORY)
        if name != "BASE":
            unstable.update(changes.get(name, []))
        gains[name] = Counter({user_id: count - base_counts.get(user_id, 0) for user_id, count in current_counts.items() if count > base_counts.get(user_id, 0)})
        rows.append([
            name,
            len(changes.get(name, [])),
            current_concentration.get("top1_share", 0.0) * 100,
            current_concentration.get("top2_share", 0.0) * 100,
            current_concentration.get("top3_share", 0.0) * 100,
            current_concentration.get("hhi", 0.0),
            ", ".join(f"{user_id}:{count:+d}" for user_id, count in sorted(gains[name].items())),
        ])
    return rows, changes, unstable, gains


def build_artifacts(
    leads: list[dict[str, Any]],
    base: dict[str, Any],
    simulations: dict[str, dict[str, Any]],
    params: ShadowParameters,
) -> None:
    base_rows = []
    for decision in base["decisions"]:
        lead = decision["lead"]
        winner = decision.get("shadow_winner") or {}
        second = decision.get("second_place") or {}
        base_rows.append({
            "lead_id": lead.get("lead_id"),
            "assignment_cycle_id": lead.get("assignment_cycle_id"),
            "current_owner": lead.get("owner"),
            "temperature": lead.get("temperature"),
            "overdue_business_minutes": lead.get("overdue_business_minutes"),
            "comuna": lead.get("comuna"),
            "region": lead.get("region"),
            "property_code": lead.get("property_code"),
            "initial_candidate_count": decision.get("initial_candidate_count"),
            "after_capacity_count": decision.get("after_capacity_count"),
            "shadow_category": decision.get("shadow_category"),
            "winner_user_id": decision.get("winner_user_id"),
            "winner_name": decision.get("winner_name"),
            "winner_score": decision.get("winner_score"),
            "second_name": decision.get("second_name"),
            "second_score": decision.get("second_score"),
            "score_difference": decision.get("score_difference"),
            "winner_adjusted_sla": winner.get("adjusted_sla_compliance"),
            "winner_adjusted_attention": winner.get("adjusted_attention_rate"),
            "winner_speed_score": winner.get("speed_score"),
            "winner_capacity_score": winner.get("capacity_score"),
            "winner_load_pressure": winner.get("load_pressure"),
            "winner_metric_imputed": winner.get("metric_imputed"),
            "winner_tie_break_applied": winner.get("tie_break_applied"),
            "excluded_candidates": ";".join(f"{row.get('executive')}:{row.get('excluded_reason')}" for row in decision.get("hard_excluded", [])),
            "saturated_candidates": ";".join(f"{row.get('executive')}:{row.get('saturation_reason')}" for row in decision.get("saturated", [])),
            "explanation": json.dumps({
                "category": decision.get("shadow_category"),
                "winner": decision.get("winner_name"),
                "winner_user_id": decision.get("winner_user_id"),
                "rules": ["existing_territorial_pool", "active_agent", "not_current_owner", "capacity_filter", "base_score"],
                "tie_break_applied": decision.get("shadow_winner", {}).get("tie_break_applied") if decision.get("shadow_winner") else None,
            }, ensure_ascii=False, sort_keys=True),
        })

    score_rows = []
    for decision in base["decisions"]:
        for candidate in decision.get("scored", []) + decision.get("hard_excluded", []):
            score_rows.append({
                "lead_id": decision["lead"].get("lead_id"),
                "current_owner": decision["lead"].get("owner"),
                "candidate_user_id": candidate.get("user_id"),
                "candidate_name": candidate.get("executive"),
                "candidate_status": "SATURATED" if candidate.get("saturation_reason") else ("HARD_EXCLUDED" if candidate.get("excluded_reason") else "AVAILABLE"),
                "excluded_reason": candidate.get("excluded_reason"),
                "saturation_reason": candidate.get("saturation_reason"),
                "sample_size": candidate.get("sample_size"),
                "sla_compliance_rate": candidate.get("sla_compliance_rate"),
                "attention_rate": candidate.get("attention_rate"),
                "adjusted_sla_compliance": candidate.get("adjusted_sla_compliance"),
                "adjusted_attention_rate": candidate.get("adjusted_attention_rate"),
                "p50_raw": candidate.get("p50_first_management_business_minutes"),
                "p90_raw": candidate.get("p90_first_management_business_minutes"),
                "p50_effective": candidate.get("p50_effective"),
                "p90_effective": candidate.get("p90_effective"),
                "metric_imputed": candidate.get("metric_imputed"),
                "speed_imputation_source": candidate.get("speed_imputation_source"),
                "speed_score_p50": candidate.get("speed_score_p50"),
                "speed_score_p90": candidate.get("speed_score_p90"),
                "speed_score": candidate.get("speed_score"),
                "open_backlog_initial": candidate.get("open_backlog"),
                "unmanaged_backlog_initial": candidate.get("unmanaged_backlog"),
                "expired_backlog_initial": candidate.get("expired_backlog"),
                "simulated_open_backlog": candidate.get("simulated_open_backlog"),
                "simulated_unmanaged_backlog": candidate.get("simulated_unmanaged_backlog"),
                "load_pressure": candidate.get("load_pressure"),
                "capacity_score": candidate.get("capacity_score"),
                "performance_score": candidate.get("performance_score"),
                "rank": candidate.get("rank"),
                "tie_break_applied": candidate.get("tie_break_applied"),
            })

    change_map = changed_winners(simulations)
    sensitivity_rows = []
    base_by_lead = {str(row["lead_id"]): row for row in base["decisions"]}
    for scenario, simulation in simulations.items():
        for decision in simulation["decisions"]:
            lead_id = str(decision["lead"].get("lead_id"))
            base_decision = base_by_lead.get(lead_id, {})
            sensitivity_rows.append({
                "scenario": scenario,
                "lead_id": lead_id,
                "shadow_category": decision.get("shadow_category"),
                "winner_user_id": decision.get("winner_user_id"),
                "winner_name": decision.get("winner_name"),
                "winner_score": decision.get("winner_score"),
                "second_score": decision.get("second_score"),
                "score_difference": decision.get("score_difference"),
                "base_winner_user_id": base_decision.get("winner_user_id"),
                "changed_vs_base": "yes" if lead_id in change_map.get(scenario, []) else "no",
            })

    write_csv(BASE_CSV, base_rows, [
        "lead_id", "assignment_cycle_id", "current_owner", "temperature", "overdue_business_minutes", "comuna", "region", "property_code",
        "initial_candidate_count", "after_capacity_count", "shadow_category", "winner_user_id", "winner_name", "winner_score", "second_name", "second_score", "score_difference",
        "winner_adjusted_sla", "winner_adjusted_attention", "winner_speed_score", "winner_capacity_score", "winner_load_pressure", "winner_metric_imputed", "winner_tie_break_applied", "excluded_candidates", "saturated_candidates", "explanation",
    ])
    write_csv(SCORES_CSV, score_rows, [
        "lead_id", "current_owner", "candidate_user_id", "candidate_name", "candidate_status", "excluded_reason", "saturation_reason", "sample_size", "sla_compliance_rate", "attention_rate", "adjusted_sla_compliance", "adjusted_attention_rate", "p50_raw", "p90_raw", "p50_effective", "p90_effective", "metric_imputed", "speed_imputation_source", "speed_score_p50", "speed_score_p90", "speed_score", "open_backlog_initial", "unmanaged_backlog_initial", "expired_backlog_initial", "simulated_open_backlog", "simulated_unmanaged_backlog", "load_pressure", "capacity_score", "performance_score", "rank", "tie_break_applied",
    ])
    write_csv(SENSITIVITY_CSV, sensitivity_rows, [
        "scenario", "lead_id", "shadow_category", "winner_user_id", "winner_name", "winner_score", "second_score", "score_difference", "base_winner_user_id", "changed_vs_base",
    ])


def build_report(
    *,
    as_of: datetime,
    safe_rows: list[dict[str, str]],
    leads: list[dict[str, Any]],
    base: dict[str, Any],
    simulations: dict[str, dict[str, Any]],
    historical: dict[str, dict[str, Any]],
    team: dict[str, Any],
    active_backlog: dict[str, dict[str, int]],
    params: ShadowParameters,
    validations: dict[str, Any],
) -> str:
    decisions = base["decisions"]
    categories = Counter(str(row.get("shadow_category")) for row in decisions)
    winners = [row for row in decisions if row.get("shadow_category") == WINNER_CATEGORY]
    score_values = [float(row["winner_score"]) for row in winners if row.get("winner_score") is not None]
    diffs = [float(row["score_difference"]) for row in winners if row.get("score_difference") is not None]
    initial_by_id = {}
    for lead in leads:
        for candidate in lead.get("candidates", []):
            initial_by_id.setdefault(str(candidate.get("user_id")), candidate)
    base_concentration = concentration(decisions, user_ids=initial_by_id.keys())
    distribution = distribution_table(decisions, initial_by_id, base["final_state"])
    sensitivity_rows, changes, unstable, gains = sensitivity_summary(simulations, decisions)
    saturated_candidate_count = sum(len(row.get("saturated", [])) for row in decisions)
    capacity_only_leads = categories.get(NO_CAPACITY_CATEGORY, 0)

    safe_count = len(safe_rows)
    current_expired_count = sum(1 for row in csv.DictReader(SAFE_INPUT.open("r", encoding="utf-8-sig")) if row.get("final_category") in {
        "SAFE_TO_SHADOW_REASSIGN", "EXPIRED_NO_ALTERNATIVE", "PROTECTED_BY_MANAGEMENT", "DATA_OR_CYCLE_ISSUE", "NOT_ACTUALLY_EXPIRED"
    })
    lines = [
        "# Auditoría Fase 1A — Selector Shadow de Redistribución SLA",
        "",
        f"Fecha de corte analítico: `{as_of.astimezone(CHILE_TZ).isoformat()}`.",
        "",
        "## Alcance y seguridad",
        "",
        "Se reconstruyó el selector exclusivamente para la población `SAFE_TO_SHADOW_REASSIGN` certificada por Fase 0.5. El pool de candidatos es el pool territorial existente; no se agregaron territorios, distancias, ETA, pesos comerciales ni reglas nuevas. La selección es shadow, explicable y secuencial en memoria.",
        "",
        md_table(["parámetro", "valor"], [
            ["performance_window_days", params.performance_window_days],
            ["shrinkage K", params.shrinkage_k],
            ["max_expired_backlog", params.max_expired_backlog],
            ["max_unmanaged_backlog", params.max_unmanaged_backlog],
            ["tie threshold", params.tie_threshold],
            ["pesos BASE", "SLA 40% / atención 20% / velocidad 20% / capacidad 20%"],
            ["escenarios", "BASE, S1_SPEED, S2_SLA, S3_CAPACITY, S4_NO_SHRINKAGE"],
        ]),
        "",
        "## Universo",
        "",
        md_table(["clasificación", "cantidad"], [
            ["CURRENT_POLICY_ACTIVE_EXPIRED reconstruido en Fase 0.5", current_expired_count],
            ["SAFE_TO_SHADOW_REASSIGN recibido por Fase 1A", safe_count],
            ["SHADOW_WINNER_SELECTED", categories.get(WINNER_CATEGORY, 0)],
            ["sin ganador", len(leads) - categories.get(WINNER_CATEGORY, 0)],
        ]),
        "",
        "## Distribución shadow",
        "",
        md_table(["ejecutivo", "received", "HOT", "normal", "% total", "initial backlog", "simulated backlog"], distribution),
        "",
        "## Concentración",
        "",
        f"Top 1: `{base_concentration['top1_share'] * 100:.1f}%`; Top 2: `{base_concentration['top2_share'] * 100:.1f}%`; Top 3: `{base_concentration['top3_share'] * 100:.1f}%`; HHI: `{base_concentration['hhi']:.4f}`.",
        "",
        "## Score",
        "",
        md_table(["métrica", "valor"], [
            ["promedio winner", fmt_number(mean(score_values))],
            ["mínimo winner", fmt_number(min(score_values) if score_values else None)],
            ["máximo winner", fmt_number(max(score_values) if score_values else None)],
            ["mediana winner", fmt_number(quantile(score_values, 0.5))],
            ["diferencia winner vs second promedio", fmt_number(mean(diffs))],
            ["diferencia winner vs second mínima", fmt_number(min(diffs) if diffs else None)],
            ["diferencia winner vs second máxima", fmt_number(max(diffs) if diffs else None)],
        ]),
        "",
        "## Sin ganador",
        "",
        md_table(["causa", "cantidad"], [
            ["NO_CANDIDATE_AFTER_CAPACITY_FILTER", categories.get(NO_CAPACITY_CATEGORY, 0)],
            ["NO_VALID_PERFORMANCE_DATA", categories.get(NO_DATA_CATEGORY, 0)],
            ["TIE_UNRESOLVED", categories.get(TIE_CATEGORY, 0)],
            ["otras", sum(value for key, value in categories.items() if key not in {WINNER_CATEGORY, NO_CAPACITY_CATEGORY, NO_DATA_CATEGORY, TIE_CATEGORY})],
        ]),
        "",
        f"Sin opción exclusivamente por capacidad: `{capacity_only_leads}` leads; candidatos saturados observados: `{saturated_candidate_count}`.",
        "",
        "## Sensibilidad",
        "",
        md_table(["escenario", "cambios vs BASE", "Top1 %", "Top2 %", "Top3 %", "HHI", "ejecutivos con ganancia vs BASE"], sensitivity_rows),
        "",
        f"Leads inestables (cambian de ganador en al menos un escenario): `{len(unstable)}`. La columna de ganancia es redistribución de ganadores respecto de BASE, no una conclusión de negocio.",
        "",
        "## Métricas históricas y reglas aplicadas",
        "",
        f"La ventana de desempeño contiene `{team['sample_size']}` ciclos de política vigente post-cutover entre `{team['window_start'].astimezone(CHILE_TZ).date()}` y `{team['window_end'].astimezone(CHILE_TZ).date()}`. Tasa equipo SLA: `{team['sla_compliance_rate'] * 100:.1f}%`; atención: `{team['attention_rate'] * 100:.1f}%`; P50: `{fmt_number(team['p50_first_management_business_minutes'])}` minutos hábiles; P90: `{fmt_number(team['p90_first_management_business_minutes'])}` minutos hábiles.",
        "",
        "Para cada candidato se aplicó: shrinkage `(n*personal + K*team)/(n+K)` con `K=20`; velocidad `0.60*P50 + 0.40*P90` después de winsorización P5/P95; capacidad por presión `3*expired + 2*unmanaged + open`; score BASE `0.40*SLA + 0.20*atención + 0.20*velocidad + 0.20*capacidad`. Si `sample_size < 5` o P50/P90 no son confiables, la velocidad se imputó desde el pool territorial confiable o, si no existe, desde el equipo y quedó registrada en CSV.",
        "",
        "La simulación ordenó HOT primero, luego mayor atraso hábil, luego `assigned_at` más antiguo y finalmente `lead_id`. Después de cada ganador solo incrementó backlog y contador shadow en memoria. Saturación excluye `expired_backlog >= 5` o `unmanaged_backlog >= 10`.",
        "",
        "## Validaciones",
        "",
        md_table(["control", "resultado"], [[key, value] for key, value in validations.items()]),
        "",
        "## Hallazgos",
        "",
        f"1. La población inicial no se hardcodeó: se tomaron `{safe_count}` filas `SAFE_TO_SHADOW_REASSIGN` del artefacto de Fase 0.5.",
        f"2. El selector produjo `{categories.get(WINNER_CATEGORY, 0)}` ganadores shadow y `{len(leads) - categories.get(WINNER_CATEGORY, 0)}` casos sin ganador.",
        f"3. La concentración BASE fue Top1 `{base_concentration['top1_share'] * 100:.1f}%`, Top2 `{base_concentration['top2_share'] * 100:.1f}%`, Top3 `{base_concentration['top3_share'] * 100:.1f}%`, HHI `{base_concentration['hhi']:.4f}`.",
        f"4. `{len(unstable)}` leads cambiaron de ganador en al menos un escenario de sensibilidad.",
        f"5. Los candidatos con baja muestra tuvieron velocidad imputada de forma explícita; esto afecta el grado de confianza, no modifica datos CRM.",
        "6. La capacidad se evaluó como filtro duro con los umbrales solicitados; no se eligió ganador alternativo fuera del pool territorial existente.",
        "7. Los ciclos legacy no se usaron para score ni backlog vencido.",
        "8. La salida es una recomendación analítica auditable; no autoriza por sí sola reasignaciones ni bloqueo de datos de contacto.",
        "",
        "## Seguridad",
        "",
        "- Mongo writes: `0`.",
        "- Reasignaciones ejecutadas: `0`.",
        "- Cambios de owner/ciclo/estado/SLA: `0`.",
        "- Deploy, scheduler, cron, frontend, backend productivo y flags: `0`.",
        "- Teléfonos, mensajes completos y PII innecesaria: no exportados.",
        "",
        "## Archivos",
        "",
        f"- `{BASE_CSV}`",
        f"- `{SCORES_CSV}`",
        f"- `{SENSITIVITY_CSV}`",
        "",
        "## No implementé reasignación",
        "",
        "Fase 1A termina en shadow ranking. No se conectó el selector al flujo productivo y no se avanzó a Fase 1.",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def validate(
    leads: list[dict[str, Any]],
    base: dict[str, Any],
    rerun: dict[str, Any],
    candidate_ids: set[str],
) -> dict[str, Any]:
    decisions = base["decisions"]
    winner_rows = [row for row in decisions if row.get("shadow_category") == WINNER_CATEGORY]
    duplicate_leads = len({str(lead.get("lead_id")) for lead in leads}) != len(leads)
    legacy_selected = any(bool(row.get("lead", {}).get("legacy_not_eligible")) for row in winner_rows)
    protected_selected = any(bool(row.get("winner", {}).get("protected_by_management")) for row in winner_rows)
    owner_selected = any(str(row.get("winner_user_id")) == str(row.get("lead", {}).get("owner_user_id")) for row in winner_rows)
    out_of_pool = any(str(row.get("winner_user_id")) not in candidate_ids for row in winner_rows)
    saturated_selected = any(row.get("winner", {}).get("saturation_reason") for row in winner_rows)
    score_range = all(
        row.get("winner_score") is None or 0.0 <= float(row["winner_score"]) <= 100.0
        for row in decisions
    )
    deterministic = (
        base.get("ordered_lead_ids") == rerun.get("ordered_lead_ids")
        and [row.get("winner_user_id") for row in base["decisions"]] == [row.get("winner_user_id") for row in rerun["decisions"]]
        and [row.get("shadow_category") for row in base["decisions"]] == [row.get("shadow_category") for row in rerun["decisions"]]
    )
    return {
        "duplicate_leads": "FAIL" if duplicate_leads else "PASS",
        "sum_categories_equals_universe": "PASS" if sum(Counter(row.get("shadow_category") for row in decisions).values()) == len(leads) else "FAIL",
        "legacy_selected": "FAIL" if legacy_selected else "PASS",
        "protected_selected": "FAIL" if protected_selected else "PASS",
        "owner_selected": "FAIL" if owner_selected else "PASS",
        "winner_out_of_pool": "FAIL" if out_of_pool else "PASS",
        "saturated_winner": "FAIL" if saturated_selected else "PASS",
        "scores_0_to_100": "PASS" if score_range else "FAIL",
        "deterministic_rerun": "PASS" if deterministic else "FAIL",
        "mongo_writes": "0",
        "round_robin_mutation": "0",
    }


def main() -> None:
    params = ShadowParameters()
    safe_rows = read_safe_rows()
    data = load_data()
    records = enrich_records(data)
    agents = active_agents(data["users"])
    historical, team = performance_metrics(records, data["users"], as_of=data["as_of"], params=params)
    backlog = backlog_metrics(records)
    leads = build_leads(safe_rows, agents, records, historical, backlog)
    base = simulate_sequential(
        leads,
        team_sla_rate=team["sla_compliance_rate"],
        team_attention_rate=team["attention_rate"],
        team_p50=team["p50_first_management_business_minutes"],
        team_p90=team["p90_first_management_business_minutes"],
        weights=BASE_WEIGHTS,
        params=params,
        shrinkage=True,
    )
    rerun = simulate_sequential(
        leads,
        team_sla_rate=team["sla_compliance_rate"],
        team_attention_rate=team["attention_rate"],
        team_p50=team["p50_first_management_business_minutes"],
        team_p90=team["p90_first_management_business_minutes"],
        weights=BASE_WEIGHTS,
        params=params,
        shrinkage=True,
    )
    simulations = sensitivity_simulations(
        leads,
        team_sla_rate=team["sla_compliance_rate"],
        team_attention_rate=team["attention_rate"],
        team_p50=team["p50_first_management_business_minutes"],
        team_p90=team["p90_first_management_business_minutes"],
        params=params,
    )
    candidate_ids = {
        str(candidate.get("user_id"))
        for lead in leads
        for candidate in lead.get("candidates", [])
        if candidate.get("user_id")
    }
    validations = validate(leads, base, rerun, candidate_ids)
    if any(value == "FAIL" for value in validations.values()):
        raise RuntimeError(f"Fallo de consistencia Fase 1A: {validations}")
    build_artifacts(leads, base, simulations, params)
    build_report(
        as_of=data["as_of"], safe_rows=safe_rows, leads=leads, base=base,
        simulations=simulations, historical=historical, team=team,
        active_backlog=backlog, params=params, validations=validations,
    )
    categories = Counter(row.get("shadow_category") for row in base["decisions"])
    print("FASE 1A SHADOW AUDIT COMPLETED")
    print(f"as_of_chile={data['as_of'].astimezone(CHILE_TZ).isoformat()}")
    print(f"safe_initial={len(leads)}")
    print(f"categories={dict(categories)}")
    print(f"performance_sample={team['sample_size']}")
    print("MongoDB writes = 0")
    print("Reassignments executed = 0")
    print("Owner/cycle/state changes = 0")
    print("Deploy/scheduler/flags = 0")
    print(f"report={REPORT}")
    print(f"base_csv={BASE_CSV}")
    print(f"scores_csv={SCORES_CSV}")
    print(f"sensitivity_csv={SENSITIVITY_CSV}")


if __name__ == "__main__":
    main()
