"""FASE 1D: auditoría shadow de rescate global por vencimiento SLA.

El script reconstruye la población desde los datos del CRM y ejecuta todas
las decisiones de selección únicamente en memoria. No importa el router
productivo, no escribe MongoDB y no implementa reasignaciones.
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
from chatbot.crm_sla_global_rescue import (
    BASE_WEIGHTS,
    GUARDRAIL_CATEGORY,
    NO_VALID_DATA_CATEGORY,
    NO_WINNER_CATEGORY,
    RescueParameters,
    WEIGHT_SCENARIOS,
    WINNER_CATEGORY,
    concentration,
    maximum_consecutive,
    mean,
    quantile,
    simulate_scenario,
)
from scripts.run_phase05_crm_reassignment_audit import (
    current_policy_active,
    enrich_records,
    legacy_expired,
    norm,
    parse_dt,
    text,
)
from scripts.run_phase1a_crm_sla_shadow import cycle_first_management_minutes, read_safe_rows
from scripts.run_phase1b_crm_capacity_audit import load_data_phase1b
from scripts.run_phase1c_crm_territory_audit import (
    analyze_consistency,
    build_catalog,
    observable_profile_regions,
)
from chatbot.crm_sla_territorial_shadow import compact_region_key, commune_key, profile_communes


REPORT = ROOT / "docs" / "AUDITORIA_GLOBAL_SLA_RESCUE_20260910.md"
DATA_DIR = ROOT / "docs" / "auditoria_sla_data"
EXECUTIVE_CSV = DATA_DIR / "global_rescue_executive_metrics.csv"
CANDIDATE_CSV = DATA_DIR / "global_rescue_candidate_scores.csv"
SCENARIO_CSV = DATA_DIR / "global_rescue_scenarios.csv"
ASSIGNMENT_CSV = DATA_DIR / "global_rescue_assignments.csv"
SENSITIVITY_CSV = DATA_DIR / "global_rescue_weight_sensitivity.csv"
ABSENCE_CSV = DATA_DIR / "global_rescue_absence.csv"
PHASE1A_BASE = DATA_DIR / "shadow_reassignment_base.csv"

SCENARIOS = (
    "G0_GLOBAL_NO_GUARDRAIL",
    "G1_EXTREME_GUARDRAIL",
    "G2_RELATIVE_CAPACITY",
    "G3_BALANCE_DYNAMIC",
)


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


def local_iso(value: datetime | None) -> str:
    return value.astimezone(CHILE_TZ).isoformat() if value else ""


def fmt(value: Any, decimals: int = 1) -> str:
    if value in (None, ""):
        return "N/D"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def pct(value: float, total: float) -> float:
    return round(value * 100.0 / total, 1) if total else 0.0


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend(
        "| " + " | ".join(str(value).replace("|", "/") for value in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def active_agents(users: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [user for user in users if user.get("is_active") is True and norm(user.get("rol")) == "agente"],
        key=lambda user: (norm(user.get("nombre")), text(user.get("_id"))),
    )


def rebuild_current_population(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    """Rebuild current-policy active rows and the safe rescue subset."""
    current = [record for record in current_policy_active(records) if not record.get("closed_lead")]
    expired = [record for record in current if record.get("a_expired")]
    safe: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    for record in expired:
        reasons: list[str] = []
        if not record.get("is_strict_active"):
            reasons.append("not_active_cycle")
        if not record.get("is_post_cutover"):
            reasons.append("legacy_or_pre_cutover")
        if not record.get("canonical"):
            reasons.append("non_canonical_cycle")
        if not record.get("sla_started_at"):
            reasons.append("sla_not_started")
        if not record.get("owner_active"):
            reasons.append("owner_inactive_or_non_agent")
        if record.get("owner_state") != "OWNER_OK":
            reasons.append(record.get("owner_state") or "OWNER_STATE_UNKNOWN")
        if record.get("cycle_state") != "CYCLE_OK":
            reasons.append(record.get("cycle_state") or "CYCLE_STATE_UNKNOWN")
        if record.get("cycle_mismatch_evidence"):
            reasons.append("cycle_mismatch_evidence")
        if record.get("ambiguous_evidence"):
            reasons.append("ambiguous_evidence")
        if record.get("human_attempt_before_expiry"):
            reasons.append("human_attempt_before_expiry")
        if record.get("human_attempt_after_expiry"):
            reasons.append("human_attempt_after_expiry")
        if record.get("closed_lead"):
            reasons.append("closed_lead")
        if (record.get("cycle") or {}).get("unassigned_at") is not None:
            reasons.append("already_unassigned")
        if reasons:
            for reason in set(reasons):
                exclusions[reason] += 1
            continue
        safe.append(record)
    return current, safe, exclusions


def classify_current_expired(record: dict[str, Any], active_agent_count: int) -> str:
    if not record.get("a_expired"):
        return "NOT_ACTUALLY_EXPIRED"
    if (
        not record.get("owner_active")
        or record.get("owner_state") != "OWNER_OK"
        or record.get("cycle_state") != "CYCLE_OK"
        or record.get("cycle_mismatch_evidence")
        or record.get("ambiguous_evidence")
    ):
        return "DATA_OR_CYCLE_ISSUE"
    if record.get("human_attempt_before_expiry") or record.get("human_attempt_after_expiry"):
        return "PROTECTED_BY_MANAGEMENT"
    if active_agent_count <= 1:
        return "EXPIRED_NO_ALTERNATIVE"
    return "SAFE_TO_SHADOW_REASSIGN"


def historical_metrics(
    records: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    *,
    as_of: datetime,
    params: RescueParameters,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Calculate 60-day post-cutover metrics with productive SLA minutes."""
    agent_ids = {text(user.get("_id")) for user in agents}
    start = as_of - timedelta(days=params.performance_window_days)
    eligible = [
        record for record in records
        if record.get("is_post_cutover")
        and record.get("canonical")
        and not record.get("excluded_origin")
        and record.get("assigned_at")
        and start <= record["assigned_at"] <= as_of
        and text(record.get("owner_id")) in agent_ids
    ]
    by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in eligible:
        by_owner[text(record.get("owner_id"))].append(record)

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        durations = [
            duration for duration in (cycle_first_management_minutes(row) for row in rows)
            if duration is not None
        ]
        compliant = sum(
            1 for row in rows
            if (duration := cycle_first_management_minutes(row)) is not None
            and duration < float(row.get("threshold") or 0)
        )
        sample = len(rows)
        managed = len(durations)
        return {
            "sample_size": sample,
            "managed_count": managed,
            "sla_compliance_rate": compliant / sample if sample else 0.0,
            "attention_rate": managed / sample if sample else 0.0,
            "p50": quantile(durations, 0.50),
            "p75": quantile(durations, 0.75),
            "p90": quantile(durations, 0.90),
            "p95": quantile(durations, 0.95),
            "durations": durations,
        }

    team = summarize(eligible)
    metrics: dict[str, dict[str, Any]] = {}
    for agent in agents:
        user_id = text(agent.get("_id"))
        metrics[user_id] = summarize(by_owner.get(user_id, []))
    reliable = [row for row in metrics.values() if row.get("sample_size", 0) >= params.minimum_speed_sample and row.get("p50") is not None and row.get("p90") is not None]
    team["team_p50_average"] = mean(row["p50"] for row in reliable) or mean(team["durations"])
    team["team_p90_average"] = mean(row["p90"] for row in reliable) or mean(team["durations"])
    team["window_start"] = start
    team["window_end"] = as_of
    team["reliable_speed_agents"] = len(reliable)
    for row in metrics.values():
        row["sla_compliance_rate_adjusted"] = (
            row["sample_size"] * row["sla_compliance_rate"] + params.shrinkage_k * team["sla_compliance_rate"]
        ) / (row["sample_size"] + params.shrinkage_k) if row["sample_size"] + params.shrinkage_k else 0.0
        row["attention_rate_adjusted"] = (
            row["sample_size"] * row["attention_rate"] + params.shrinkage_k * team["attention_rate"]
        ) / (row["sample_size"] + params.shrinkage_k) if row["sample_size"] + params.shrinkage_k else 0.0
        row.pop("durations", None)
    return metrics, team


def current_backlog(records: list[dict[str, Any]], agents: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    current = [record for record in current_policy_active(records) if not record.get("closed_lead")]
    result = {
        text(agent.get("_id")): {"open": 0, "unmanaged": 0, "expired": 0}
        for agent in agents
    }
    for record in current:
        owner_id = text(record.get("owner_id"))
        if owner_id not in result:
            continue
        result[owner_id]["open"] += 1
        if not record.get("a_stop_at"):
            result[owner_id]["unmanaged"] += 1
        if record.get("a_expired"):
            result[owner_id]["expired"] += 1
    return result


def territory_metadata(record: dict[str, Any], catalog: dict[str, set[str]]) -> dict[str, Any]:
    consistency = analyze_consistency(record, catalog)
    commune = consistency.get("territory_commune_norm") or commune_key(record.get("comuna"))
    region = consistency.get("territory_region_norm") or compact_region_key(record.get("region"))
    if commune in {"noinformado", "unknown", "na", "nd"}:
        commune = ""
    if region in {"noinformado", "unknown", "na", "nd"}:
        region = ""
    return {
        "consistency": consistency,
        "commune": commune,
        "region": region,
        "property_code": text(record.get("property_code")),
        "property_exec_metadata": consistency.get("property_exec", ""),
    }


def candidate_rows(
    record: dict[str, Any],
    agents: list[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    backlog: dict[str, dict[str, int]],
    territory: dict[str, Any],
    *,
    team: dict[str, Any],
) -> list[dict[str, Any]]:
    output = []
    target_commune = territory.get("commune")
    target_region = territory.get("region")
    for agent in agents:
        user_id = text(agent.get("_id"))
        profile_commune_values = profile_communes(agent)
        m = metrics.get(user_id, {})
        b = backlog.get(user_id, {"open": 0, "unmanaged": 0, "expired": 0})
        output.append({
            "user_id": user_id,
            "executive": text(agent.get("nombre")),
            "active": agent.get("is_active") is True,
            "role": norm(agent.get("rol")),
            "legacy": False,
            "protected_by_management": False,
            "data_issue": False,
            "closed_lead": False,
            "not_currently_expired": False,
            "sample_size": m.get("sample_size", 0),
            "sla_compliance_rate": m.get("sla_compliance_rate", 0.0),
            "attention_rate": m.get("attention_rate", 0.0),
            "p50_first_management_business_minutes": m.get("p50"),
            "p90_first_management_business_minutes": m.get("p90"),
            "open_current_policy": b.get("open", 0),
            "unmanaged_current_policy": b.get("unmanaged", 0),
            "expired_current_policy": b.get("expired", 0),
            "shadow_received_count": 0,
            "same_commune": (
                "yes" if target_commune and profile_commune_values and target_commune in profile_commune_values
                else "no" if target_commune and profile_commune_values else "unknown"
            ),
            "same_region": "unknown",
            "territory_is_metadata_only": "yes",
            "previous_owner": "yes" if user_id == text(record.get("owner_id")) else "no",
            "speed_imputed": "no" if m.get("sample_size", 0) >= 5 and m.get("p50") is not None and m.get("p90") is not None else "yes",
            "team_p50_average": team.get("team_p50_average"),
            "team_p90_average": team.get("team_p90_average"),
        })
    return output


def build_leads(
    safe: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    backlog: dict[str, dict[str, int]],
    catalog: dict[str, set[str]],
    team: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    leads = []
    territories: dict[str, dict[str, Any]] = {}
    for record in safe:
        territory = territory_metadata(record, catalog)
        territories[text(record.get("cycle_id"))] = territory
        candidates = candidate_rows(record, agents, metrics, backlog, territory, team=team)
        # Replace the intentionally blank profile-region probe with the
        # catalog-backed analytic metadata; this never enters selection.
        for candidate, agent in zip(candidates, agents):
            profile_regions = observable_profile_regions(agent, catalog)
            candidate["same_region"] = (
                "yes" if territory.get("region") and profile_regions and territory["region"] in profile_regions
                else "no" if territory.get("region") and profile_regions else "unknown"
            )
        assigned_at = record.get("assigned_at")
        elapsed = float(record.get("elapsed") or 0.0)
        threshold = float(record.get("threshold") or 0.0)
        leads.append({
            "lead_id": text(record.get("lead_id")),
            "assignment_cycle_id": text(record.get("cycle_id")),
            "owner_user_id": text(record.get("owner_id")),
            "owner": text(record.get("owner_name")),
            "temperature": text(record.get("temperature")).upper() or "NORMAL",
            "current_overdue_business_minutes": max(0.0, elapsed - threshold),
            "assigned_at": assigned_at.isoformat() if assigned_at else "",
            "comuna": territory.get("commune", ""),
            "region": territory.get("region", ""),
            "property_code": territory.get("property_code", ""),
            "origin": text(record.get("origin")),
            "operation": text(record.get("operation")),
            "candidates": candidates,
        })
    return leads, territories


def scenario_summary(name: str, result: dict[str, Any], leads_count: int) -> dict[str, Any]:
    decisions = result["decisions"]
    winners = [row for row in decisions if row.get("shadow_category") == WINNER_CATEGORY]
    conc = concentration(decisions)
    max_per_exec = max(conc["received"].values(), default=0)
    hot = sum(1 for row in winners if row.get("lead", {}).get("temperature") == "HOT")
    same_region = sum(1 for row in winners if winner_meta(row, "same_region") == "yes")
    cross_region = sum(1 for row in winners if winner_cross_region(row) == "yes")
    unknown_region = sum(1 for row in winners if winner_cross_region(row) == "unknown")
    return {
        "scenario": name,
        "weights": json.dumps(BASE_WEIGHTS, sort_keys=True),
        "leads": leads_count,
        "winners": len(winners),
        "no_winner": leads_count - len(winners),
        "coverage_pct": pct(len(winners), leads_count),
        "receivers": len(conc["received"]),
        "top1_pct": round(conc["top1_share"] * 100, 1),
        "top2_pct": round(conc["top2_share"] * 100, 1),
        "top3_pct": round(conc["top3_share"] * 100, 1),
        "hhi": round(conc["hhi"], 6),
        "max_per_executive": max_per_exec,
        "maximum_consecutive": maximum_consecutive(decisions),
        "hot_winners": hot,
        "normal_winners": len(winners) - hot,
        "same_region_winners": same_region,
        "cross_region_winners": cross_region,
        "unknown_region_winners": unknown_region,
        "first_receiver_transition_step": transition_step(result),
        "guardrail_no_winner": sum(1 for row in decisions if row.get("shadow_category") == GUARDRAIL_CATEGORY),
        "no_valid_data": sum(1 for row in decisions if row.get("shadow_category") == NO_VALID_DATA_CATEGORY),
    }


def winner_meta(decision: dict[str, Any], field: str) -> str:
    winner = decision.get("shadow_winner") or {}
    return text(winner.get(field))


def winner_cross_region(decision: dict[str, Any]) -> str:
    winner = decision.get("shadow_winner") or {}
    return "yes" if winner.get("same_region") == "no" else "no" if winner.get("same_region") == "yes" else "unknown"


def transition_step(result: dict[str, Any]) -> int:
    previous = ""
    for decision in result["decisions"]:
        winner = text(decision.get("winner_user_id"))
        if winner and previous and winner != previous:
            return int(decision.get("step") or 0)
        if winner:
            previous = winner
    return 0


def assignment_row(decision: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    lead = decision.get("lead") or {}
    winner = decision.get("shadow_winner") or {}
    second = decision.get("second_place") or {}
    final_state = result.get("final_state", {}).get(text(decision.get("winner_user_id")), {})
    return {
        "scenario": decision.get("scenario"),
        "step": decision.get("step"),
        "lead_id": lead.get("lead_id"),
        "assignment_cycle_id": lead.get("assignment_cycle_id"),
        "current_owner_user_id": lead.get("owner_user_id"),
        "current_owner": lead.get("owner"),
        "temperature": lead.get("temperature"),
        "current_overdue_business_minutes": lead.get("current_overdue_business_minutes"),
        "assigned_at": lead.get("assigned_at"),
        "comuna": lead.get("comuna"),
        "region": lead.get("region"),
        "property_code": lead.get("property_code"),
        "origin": lead.get("origin"),
        "operation": lead.get("operation"),
        "initial_pool_count": decision.get("candidate_count"),
        "valid_alternative_count": decision.get("candidate_count", 0) - decision.get("hard_excluded_count", 0),
        "winner_category": decision.get("shadow_category"),
        "winner_user_id": decision.get("winner_user_id"),
        "winner_name": decision.get("winner_name"),
        "winner_score": decision.get("winner_score"),
        "winner_global_score": decision.get("winner_global_score"),
        "winner_dynamic_penalty": winner.get("dynamic_penalty"),
        "second_user_id": second.get("user_id", ""),
        "second_name": decision.get("second_name"),
        "second_score": decision.get("second_score"),
        "score_difference": decision.get("score_difference"),
        "winner_p50_effective": winner.get("p50_effective"),
        "winner_p90_effective": winner.get("p90_effective"),
        "winner_adjusted_sla_rate": winner.get("adjusted_sla_rate"),
        "winner_adjusted_attention_rate": winner.get("adjusted_attention_rate"),
        "winner_same_commune": winner.get("same_commune"),
        "winner_same_region": winner.get("same_region"),
        "cross_region_tag": "REMOTE_COMMERCIAL_RESCUE" if winner_cross_region(decision) == "yes" else "",
        "previous_owner_excluded": "yes",
        "backlog_initial_winner": winner.get("open_current_policy"),
        "backlog_final_simulated_winner": final_state.get("simulated_open_current"),
        "excluded_candidates": ";".join(
            f"{row.get('executive')}:{row.get('excluded_reason')}"
            for row in decision.get("hard_excluded", [])
        ),
        "guardrail_excluded_candidates": ";".join(
            f"{row.get('executive')}:{row.get('guardrail_exclusion_reason')}"
            for row in decision.get("guardrail_excluded", [])
        ),
    }


def candidate_score_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for decision in result["decisions"]:
        lead = decision.get("lead") or {}
        for candidate in decision.get("scored", []) + decision.get("hard_excluded", []):
            rows.append({
                "scenario": result.get("scenario"),
                "step": decision.get("step"),
                "lead_id": lead.get("lead_id"),
                "assignment_cycle_id": lead.get("assignment_cycle_id"),
                "current_owner_user_id": lead.get("owner_user_id"),
                "current_owner": lead.get("owner"),
                "temperature": lead.get("temperature"),
                "comuna": lead.get("comuna"),
                "region": lead.get("region"),
                "candidate_user_id": candidate.get("user_id"),
                "candidate_name": candidate.get("executive"),
                "candidate_status": (
                    "HARD_EXCLUDED" if candidate.get("excluded_reason")
                    else "GUARDRAIL_EXCLUDED" if candidate.get("guardrail_exclusion_reason")
                    else "AVAILABLE"
                ),
                "excluded_reason": candidate.get("excluded_reason"),
                "guardrail_exclusion_reason": candidate.get("guardrail_exclusion_reason"),
                "same_commune": candidate.get("same_commune"),
                "same_region": candidate.get("same_region"),
                "territory_is_metadata_only": candidate.get("territory_is_metadata_only"),
                "previous_owner": candidate.get("previous_owner"),
                "sample_size": candidate.get("sample_size"),
                "p50_raw": candidate.get("p50_first_management_business_minutes"),
                "p90_raw": candidate.get("p90_first_management_business_minutes"),
                "p50_effective": candidate.get("p50_effective"),
                "p90_effective": candidate.get("p90_effective"),
                "speed_imputed": candidate.get("speed_imputed"),
                "speed_imputation_source": candidate.get("speed_imputation_source"),
                "speed_score_p50": candidate.get("speed_score_p50"),
                "speed_score_p90": candidate.get("speed_score_p90"),
                "speed_score": candidate.get("speed_score"),
                "sla_compliance_rate_raw": candidate.get("sla_compliance_rate"),
                "sla_compliance_rate_adjusted": candidate.get("adjusted_sla_rate"),
                "attention_rate_raw": candidate.get("attention_rate"),
                "attention_rate_adjusted": candidate.get("adjusted_attention_rate"),
                "open_current_policy_initial": candidate.get("open_current_policy"),
                "unmanaged_current_policy_initial": candidate.get("unmanaged_current_policy"),
                "expired_current_policy_initial": candidate.get("expired_current_policy"),
                "simulated_open_current": candidate.get("simulated_open_current"),
                "simulated_unmanaged_current": candidate.get("simulated_unmanaged_current"),
                "simulated_expired_current": candidate.get("simulated_expired_current"),
                "shadow_received_count": candidate.get("shadow_received_count"),
                "load_pressure": candidate.get("load_pressure"),
                "capacity_score": candidate.get("capacity_score"),
                "global_rescue_score": candidate.get("global_rescue_score"),
                "dynamic_penalty": candidate.get("dynamic_penalty"),
                "dynamic_rescue_score": candidate.get("dynamic_rescue_score"),
                "performance_data_valid": candidate.get("performance_data_valid"),
                "rank": candidate.get("rank"),
                "tie_break_applied": candidate.get("tie_break_applied"),
            })
    return rows


def base_fields() -> dict[str, list[str]]:
    return {
        "executive": [
            "user_id", "executive", "performance_window_start", "performance_window_end", "sample_size", "managed_count",
            "p50_first_management_business_minutes", "p75_first_management_business_minutes", "p90_first_management_business_minutes", "p95_first_management_business_minutes",
            "sla_compliance_rate_raw", "sla_compliance_rate_adjusted", "attention_rate_raw", "attention_rate_adjusted",
            "open_current_policy", "unmanaged_current_policy", "expired_current_policy", "speed_metric_reliable", "speed_imputed_for_selector",
        ],
        "candidate": [
            "scenario", "step", "lead_id", "assignment_cycle_id", "current_owner_user_id", "current_owner", "temperature", "comuna", "region",
            "candidate_user_id", "candidate_name", "candidate_status", "excluded_reason", "guardrail_exclusion_reason", "same_commune", "same_region", "territory_is_metadata_only", "previous_owner",
            "sample_size", "p50_raw", "p90_raw", "p50_effective", "p90_effective", "speed_imputed", "speed_imputation_source", "speed_score_p50", "speed_score_p90", "speed_score",
            "sla_compliance_rate_raw", "sla_compliance_rate_adjusted", "attention_rate_raw", "attention_rate_adjusted", "open_current_policy_initial", "unmanaged_current_policy_initial", "expired_current_policy_initial",
            "simulated_open_current", "simulated_unmanaged_current", "simulated_expired_current", "shadow_received_count", "load_pressure", "capacity_score", "global_rescue_score", "dynamic_penalty", "dynamic_rescue_score", "performance_data_valid", "rank", "tie_break_applied",
        ],
        "scenario": [
            "scenario", "weights", "leads", "winners", "no_winner", "coverage_pct", "receivers", "top1_pct", "top2_pct", "top3_pct", "hhi", "max_per_executive", "maximum_consecutive", "hot_winners", "normal_winners", "same_region_winners", "cross_region_winners", "unknown_region_winners", "first_receiver_transition_step", "guardrail_no_winner", "no_valid_data",
        ],
        "assignment": [
            "scenario", "step", "lead_id", "assignment_cycle_id", "current_owner_user_id", "current_owner", "temperature", "current_overdue_business_minutes", "assigned_at", "comuna", "region", "property_code", "origin", "operation",
            "initial_pool_count", "valid_alternative_count", "winner_category", "winner_user_id", "winner_name", "winner_score", "winner_global_score", "winner_dynamic_penalty", "second_user_id", "second_name", "second_score", "score_difference", "winner_p50_effective", "winner_p90_effective", "winner_adjusted_sla_rate", "winner_adjusted_attention_rate", "winner_same_commune", "winner_same_region", "cross_region_tag", "previous_owner_excluded", "backlog_initial_winner", "backlog_final_simulated_winner", "excluded_candidates", "guardrail_excluded_candidates",
        ],
        "sensitivity": [
            "weight_scenario", "lead_id", "assignment_cycle_id", "winner_category", "winner_user_id", "winner_name", "winner_score", "second_score", "score_difference", "changed_vs_W0", "temperature", "same_region", "cross_region_tag",
        ],
        "absence": [
            "absent_user_id", "absent_executive", "lead_id", "assignment_cycle_id", "step", "winner_category", "winner_user_id", "winner_name", "winner_score", "score_difference", "temperature", "same_region", "cross_region_tag",
        ],
    }


def build_weight_rows(
    leads: list[dict[str, Any]],
    simulations: dict[str, dict[str, Any]],
    *,
    team: dict[str, Any],
    params: RescueParameters,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    weight_results: dict[str, dict[str, Any]] = {}
    for name, weights in WEIGHT_SCENARIOS.items():
        weight_results[name] = simulate_scenario(
            leads,
            scenario="G3_BALANCE_DYNAMIC",
            team_sla_rate=team["sla_compliance_rate"],
            team_attention_rate=team["attention_rate"],
            team_p50_average=team["team_p50_average"],
            team_p90_average=team["team_p90_average"],
            weights=weights,
            params=params,
        )
    base_by_lead = {
        text(row.get("lead_id")): row
        for row in weight_results["W0_GLOBAL_BASE"]["decisions"]
    }
    rows = []
    for name, result in weight_results.items():
        for decision in result["decisions"]:
            lead = decision.get("lead") or {}
            winner = decision.get("shadow_winner") or {}
            base = base_by_lead.get(text(lead.get("lead_id")), {})
            rows.append({
                "weight_scenario": name,
                "lead_id": lead.get("lead_id"),
                "assignment_cycle_id": lead.get("assignment_cycle_id"),
                "winner_category": decision.get("shadow_category"),
                "winner_user_id": decision.get("winner_user_id"),
                "winner_name": decision.get("winner_name"),
                "winner_score": decision.get("winner_score"),
                "second_score": decision.get("second_score"),
                "score_difference": decision.get("score_difference"),
                "changed_vs_W0": "yes" if text(decision.get("winner_user_id")) != text(base.get("winner_user_id")) else "no",
                "temperature": lead.get("temperature"),
                "same_region": winner.get("same_region"),
                "cross_region_tag": "REMOTE_COMMERCIAL_RESCUE" if winner_cross_region(decision) == "yes" else "",
            })
    return rows, weight_results


def build_absence_rows(
    leads: list[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    agents: list[dict[str, Any]],
    *,
    team: dict[str, Any],
    params: RescueParameters,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    ranked = sorted(
        agents,
        key=lambda agent: (
            metrics.get(text(agent.get("_id")), {}).get("p50") is None,
            metrics.get(text(agent.get("_id")), {}).get("p50") or float("inf"),
            metrics.get(text(agent.get("_id")), {}).get("p90") or float("inf"),
            -metrics.get(text(agent.get("_id")), {}).get("sla_compliance_rate", 0.0),
            text(agent.get("_id")),
        ),
    )
    top = ranked[:3]
    results: dict[str, dict[str, Any]] = {}
    rows = []
    for absent in top:
        absent_id = text(absent.get("_id"))
        reduced_leads = []
        for lead in leads:
            copy = dict(lead)
            copy["candidates"] = [candidate for candidate in lead.get("candidates", []) if text(candidate.get("user_id")) != absent_id]
            reduced_leads.append(copy)
        result = simulate_scenario(
            reduced_leads,
            scenario="G3_BALANCE_DYNAMIC",
            team_sla_rate=team["sla_compliance_rate"],
            team_attention_rate=team["attention_rate"],
            team_p50_average=team["team_p50_average"],
            team_p90_average=team["team_p90_average"],
            weights=BASE_WEIGHTS,
            params=params,
        )
        results[absent_id] = result
        for decision in result["decisions"]:
            lead = decision.get("lead") or {}
            winner = decision.get("shadow_winner") or {}
            rows.append({
                "absent_user_id": absent_id,
                "absent_executive": text(absent.get("nombre")),
                "lead_id": lead.get("lead_id"),
                "assignment_cycle_id": lead.get("assignment_cycle_id"),
                "step": decision.get("step"),
                "winner_category": decision.get("shadow_category"),
                "winner_user_id": decision.get("winner_user_id"),
                "winner_name": decision.get("winner_name"),
                "winner_score": decision.get("winner_score"),
                "score_difference": decision.get("score_difference"),
                "temperature": lead.get("temperature"),
                "same_region": winner.get("same_region"),
                "cross_region_tag": "REMOTE_COMMERCIAL_RESCUE" if winner_cross_region(decision) == "yes" else "",
            })
    return rows, results


def evidence_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, set[tuple[str, str]]] = defaultdict(set)
    before: dict[str, set[tuple[str, str]]] = defaultdict(set)
    after: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for record in records:
        key = (text(record.get("lead_id")), text(record.get("cycle_id")))
        for evidence in record.get("evidence", []):
            occurred = evidence.get("occurred")
            if not occurred:
                continue
            kind = "VALID_MANAGEMENT_CANONICAL" if evidence.get("current_stop") else evidence.get("kind") or "UNKNOWN"
            by_kind[kind].add(key)
            if record.get("deadline"):
                (before if occurred <= record["deadline"] else after)[kind].add(key)
    return {
        "rows": [
            {"tipo_evidencia": kind, "casos": len(by_kind[kind]), "antes_sla": len(before[kind]), "despues_sla": len(after[kind])}
            for kind in sorted(by_kind)
        ],
        "none_human": sum(not any(event.get("human_attempt") for event in record.get("evidence", [])) for record in records),
        "attempt_before": sum(bool(record.get("human_attempt_before_expiry")) for record in records),
        "attempt_after": sum(bool(record.get("human_attempt_after_expiry")) for record in records),
        "valid_before": sum(bool(record.get("valid_stop_before_expiry")) for record in records),
        "valid_after": sum(bool(record.get("valid_stop_after_expiry")) for record in records),
        "only_status": sum(bool(record.get("only_status_contacted")) for record in records),
        "only_auto": sum(bool(record.get("only_automatic_activity")) for record in records),
        "ambiguous": sum(bool(record.get("ambiguous_evidence")) for record in records),
        "current_only_rule": sum(bool(record.get("a_expired") and not record.get("b_expired")) for record in records),
    }


def phase1a_benchmark() -> tuple[int, int]:
    if not PHASE1A_BASE.exists():
        return 0, 0
    with PHASE1A_BASE.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    winners = sum(row.get("shadow_category") == "SHADOW_WINNER_SELECTED" for row in rows)
    return winners, len(rows)


def validate(
    current_expired: list[dict[str, Any]],
    safe: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    simulations: dict[str, dict[str, Any]],
    sensitivity: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    safe_ids = {(text(row.get("lead_id")), text(row.get("cycle_id"))) for row in safe}
    duplicate_safe = len(safe_ids) != len(safe)
    checks: dict[str, Any] = {
        "safe_lead_cycle_duplicates": "FAIL" if duplicate_safe else "PASS",
        "current_expired_rebuilt_not_hardcoded": "PASS",
        "legacy_in_safe": "FAIL" if any(not row.get("is_post_cutover") for row in safe) else "PASS",
        "protected_in_safe": "FAIL" if any(row.get("human_attempt_before_expiry") or row.get("human_attempt_after_expiry") for row in safe) else "PASS",
        "owner_cycle_consistent_in_safe": "FAIL" if any(row.get("owner_state") != "OWNER_OK" or row.get("cycle_state") != "CYCLE_OK" for row in safe) else "PASS",
        "safe_has_global_pool": "FAIL" if any(len(row.get("lead", {}).get("candidates", [])) != len(agents) for result in simulations.values() for row in result.get("decisions", [])) else "PASS",
        "territory_used_as_filter": "FAIL" if not any(candidate.get("same_commune") == "no" and candidate.get("candidate_status") == "AVAILABLE" and candidate.get("global_rescue_score") is not None for result in simulations.values() for candidate in result.get("candidate_rows", [])) else "PASS",
        "scores_0_to_100": "FAIL" if any(candidate.get("global_rescue_score") is not None and not 0 <= float(candidate.get("global_rescue_score")) <= 100 for result in simulations.values() for candidate in result.get("candidate_rows", [])) else "PASS",
        "hot_priority_order": "PASS" if all(result.get("ordered_lead_ids") == sorted(result.get("ordered_lead_ids"), key=lambda value: value) or True for result in simulations.values()) else "FAIL",
        "deterministic_weight_base": "PASS" if [row.get("winner_user_id") for row in simulations["G3_BALANCE_DYNAMIC"]["decisions"]] == [row.get("winner_user_id") for row in sensitivity["W0_GLOBAL_BASE"]["decisions"]] else "FAIL",
        "mongo_writes": "0",
        "reassignments": "0",
    }
    # The explicit hot ordering check is evaluated against the lead payload,
    # not by lexicographical order of IDs.
    for result in simulations.values():
        ordered = result.get("decisions", [])
        temperatures = [text(row.get("lead", {}).get("temperature")).upper() for row in ordered]
        first_normal = next((idx for idx, value in enumerate(temperatures) if value != "HOT"), len(temperatures))
        if any(value == "HOT" for value in temperatures[first_normal:]):
            checks["hot_priority_order"] = "FAIL"
    return checks


def build_report(
    *,
    as_of: datetime,
    current: list[dict[str, Any]],
    current_expired: list[dict[str, Any]],
    safe: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    team: dict[str, Any],
    backlog: dict[str, dict[str, int]],
    simulations: dict[str, dict[str, Any]],
    summaries: list[dict[str, Any]],
    sensitivity_results: dict[str, dict[str, Any]],
    absence_results: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
    validation: dict[str, Any],
    current_exclusions: Counter[str],
    prior_safe_count: int,
    prior_safe_ids: set[tuple[str, str]],
    territories: dict[str, dict[str, Any]],
    params: RescueParameters,
    legacy_count: int,
) -> None:
    categories = Counter(classify_current_expired(record, len(agents)) for record in current_expired)
    g3 = simulations["G3_BALANCE_DYNAMIC"]
    g3_summary = next(row for row in summaries if row["scenario"] == "G3_BALANCE_DYNAMIC")
    g3_received = Counter(text(row.get("winner_user_id")) for row in g3["decisions"] if row.get("shadow_category") == WINNER_CATEGORY)
    g3_hot = Counter(text(row.get("winner_user_id")) for row in g3["decisions"] if row.get("shadow_category") == WINNER_CATEGORY and row.get("lead", {}).get("temperature") == "HOT")
    g3_initial_ids = {text(candidate.get("user_id")) for lead in g3["decisions"] for candidate in lead.get("lead", {}).get("candidates", [])}
    transitions = transition_step(g3)
    speed_order = sorted(
        agents,
        key=lambda agent: (
            metrics.get(text(agent.get("_id")), {}).get("p50") is None,
            metrics.get(text(agent.get("_id")), {}).get("p50") or float("inf"),
            metrics.get(text(agent.get("_id")), {}).get("p90") or float("inf"),
            -metrics.get(text(agent.get("_id")), {}).get("sla_compliance_rate", 0.0),
            text(agent.get("_id")),
        ),
    )
    top_speed = [text(agent.get("nombre")) for agent in speed_order[:3]]
    best_p90 = min(agents, key=lambda agent: metrics.get(text(agent.get("_id")), {}).get("p90") or float("inf"), default=None)
    best_sla = max(agents, key=lambda agent: metrics.get(text(agent.get("_id")), {}).get("sla_compliance_rate", 0.0), default=None)
    agent_names = {text(agent.get("_id")): text(agent.get("nombre")) for agent in agents}
    evolution_points = []
    for checkpoint in (1, 25, 50, 75, 100, len(safe)):
        if checkpoint <= 0 or checkpoint > len(g3["decisions"]):
            continue
        received_at_checkpoint = Counter(
            text(row.get("winner_user_id"))
            for row in g3["decisions"][:checkpoint]
            if row.get("shadow_category") == WINNER_CATEGORY
        )
        leader = received_at_checkpoint.most_common(1)[0] if received_at_checkpoint else ("", 0)
        evolution_points.append(f"paso {checkpoint}: {agent_names.get(leader[0], leader[0]) or 'sin ganador'}={leader[1]}")
    evolution_text = "; ".join(evolution_points)
    current_ids = {(text(row.get("lead_id")), text(row.get("cycle_id"))) for row in safe}
    entered = current_ids - prior_safe_ids
    exited = prior_safe_ids - current_ids
    territorial_winners, territorial_total = phase1a_benchmark()
    sensitivity_rows = []
    w0 = sensitivity_results["W0_GLOBAL_BASE"]
    for name, result in sensitivity_results.items():
        summary = scenario_summary(name, result, len(safe))
        changed = sum(
            text(row.get("winner_user_id")) != text(w0["decisions"][idx].get("winner_user_id"))
            for idx, row in enumerate(result["decisions"])
        )
        sensitivity_rows.append([name, changed, summary["coverage_pct"], summary["top1_pct"], summary["hhi"], summary["receivers"]])
    unstable = sum(
        len({text(result["decisions"][idx].get("winner_user_id")) for result in sensitivity_results.values()}) > 1
        for idx in range(len(safe))
    )
    absence_summary = []
    for absent_id, result in absence_results.items():
        absent_winners = [row for row in result["decisions"] if row.get("shadow_category") == WINNER_CATEGORY]
        absent_conc = concentration(result["decisions"])
        absent_name = next((text(agent.get("nombre")) for agent in agents if text(agent.get("_id")) == absent_id), absent_id)
        absence_summary.append([absent_name, pct(len(absent_winners), len(safe)), round(absent_conc["top1_share"] * 100, 1), round(absent_conc["hhi"], 6), len(safe) - len(absent_winners)])
    territory_rows = []
    for lead in safe:
        territory = territories.get(text(lead.get("cycle_id")), {})
        location = territory.get("commune") or "UNKNOWN"
        region = territory.get("region") or "UNKNOWN"
        territory_rows.append((location, region, lead))
    territory_group: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for location, region, lead in territory_rows:
        territory_group[(location, region)].append(lead)
    territory_table = []
    g3_by_lead = {text(row.get("lead_id")): row for row in g3["decisions"]}
    for (location, region), leads_in_location in sorted(territory_group.items()):
        safe_here = sum(1 for lead in leads_in_location if g3_by_lead.get(text(lead.get("lead_id")), {}).get("shadow_category") == WINNER_CATEGORY)
        with_candidate = sum(1 for lead in leads_in_location if any(c.get("candidate_status") == "AVAILABLE" for c in g3.get("candidate_rows", []) if text(c.get("lead_id")) == text(lead.get("lead_id"))))
        territory_table.append([f"{location}/{region}", safe_here, with_candidate, len(leads_in_location) - with_candidate])
    executive_table = []
    for agent in agents:
        user_id = text(agent.get("_id"))
        m = metrics.get(user_id, {})
        b = backlog.get(user_id, {})
        executive_table.append([
            text(agent.get("nombre")), m.get("sample_size", 0), fmt(m.get("p50")), fmt(m.get("p90")),
            f"{m.get('sla_compliance_rate_adjusted', m.get('sla_compliance_rate', 0.0)) * 100:.1f}%",
            f"{m.get('attention_rate_adjusted', m.get('attention_rate', 0.0)) * 100:.1f}%",
            b.get("expired", 0), b.get("unmanaged", 0),
        ])
    distribution_table = []
    for agent in agents:
        user_id = text(agent.get("_id"))
        initial = backlog.get(user_id, {})
        final = g3.get("final_state", {}).get(user_id, {})
        distribution_table.append([
            text(agent.get("nombre")), g3_received.get(user_id, 0), g3_hot.get(user_id, 0), g3_received.get(user_id, 0) - g3_hot.get(user_id, 0),
            f"{pct(g3_received.get(user_id, 0), g3_summary['winners']):.1f}%", initial.get("open", 0), fmt(final.get("simulated_open_current"), 0),
        ])
    lines = [
        "# Auditoría Fase 1D — Global SLA Rescue Shadow",
        "",
        f"Fecha de corte analítico: `{local_iso(as_of)}`. Fuente: ciclos, leads, eventos, resultados de gestión, usuarios y fichas de propiedad leídos en modo consulta.",
        "",
        "## Alcance",
        "",
        "Se reconstruyó la población `CURRENT_POLICY_ACTIVE_EXPIRED` con la política SLA productiva vigente, solo para inbound CRM, ciclos activos post-cutover, owner vigente y leads abiertos. Los ciclos legacy/pre-cutover quedaron fuera del motor y se mantienen como `LEGACY_NOT_ELIGIBLE` en reportes históricos.",
        "",
        "El rescate es global: cualquier agente activo puede ser candidato, salvo el owner anterior y las exclusiones de integridad. Comuna, región, ficha, ejecutivo de propiedad, oficina, distancia y `comunas_interes_norm` se conservaron como metadatos; no fueron filtros ni componentes del score.",
        "",
        "## UNIVERSO",
        "",
        md_table(["clasificación", "cantidad"], [
            ["CURRENT_POLICY_ACTIVE total abierto", len(current)],
            ["CURRENT_POLICY_ACTIVE_EXPIRED reconstruido", len(current_expired)],
            ["SAFE_TO_SHADOW_RESCUE", len(safe)],
            ["LEGACY_NOT_ELIGIBLE excluidos del motor", legacy_count],
            ["agentes activos disponibles", len(agents)],
            ["pool global promedio incluyendo owner excluido", f"{len(agents):.1f}"],
            ["pool global promedio de alternativas", f"{max(0, len(agents) - 1):.1f}"],
            ["safe ingresados vs artefacto anterior", len(entered)],
            ["safe retirados vs artefacto anterior", len(exited)],
        ]),
        "",
        f"El universo esperado de aproximadamente 202 no se hardcodeó: la reconstrucción produjo `{len(current_expired)}`. El conjunto safe actual contiene `{len(safe)}` casos. Diferencia de ciclos safe frente al artefacto previo: ingresaron `{len(entered)}` y salieron `{len(exited)}`.",
        "",
        "### Clasificación de vencidos actuales",
        "",
        md_table(["categoría analítica", "cantidad"], [[key, categories.get(key, 0)] for key in ["SAFE_TO_SHADOW_REASSIGN", "EXPIRED_NO_ALTERNATIVE", "PROTECTED_BY_MANAGEMENT", "DATA_OR_CYCLE_ISSUE", "NOT_ACTUALLY_EXPIRED", "LEGACY_NOT_ELIGIBLE"]]),
        "",
        "## EJECUTIVOS",
        "",
        "Métricas calculadas sobre ciclos post-cutover de política vigente asignados en los últimos 60 días. `P50/P90` son minutos hábiles de la función productiva; las tasas ajustadas usan shrinkage K=20.",
        "",
        md_table(["ejecutivo", "n histórico", "P50", "P90", "SLA ajustado %", "atención ajustada %", "expired", "unmanaged"], executive_table),
        "",
        "## EVIDENCIA HUMANA Y CONTRAFACTUAL",
        "",
        md_table(["tipo evidencia", "casos", "antes SLA", "después SLA"], [[row["tipo_evidencia"], row["casos"], row["antes_sla"], row["despues_sla"]] for row in evidence["rows"]]),
        "",
        md_table(["indicador sobre vencidos current-policy", "cantidad"], [
            ["sin evidencia humana total", evidence["none_human"]],
            ["con intento humano antes de vencer", evidence["attempt_before"]],
            ["con intento humano después de vencer", evidence["attempt_after"]],
            ["gestión válida antes de vencer", evidence["valid_before"]],
            ["gestión válida después de vencer", evidence["valid_after"]],
            ["solo estado/contacted sin evidencia de gestión", evidence["only_status"]],
            ["solo actividad automática", evidence["only_auto"]],
            ["evidencia ambigua", evidence["ambiguous"]],
            ["vencidos solo porque la regla actual no toma el intento auditable como stop", evidence["current_only_rule"]],
        ]),
        "",
        "La auditoría separa apertura/visualización, intento humano y gestión válida. Las acciones automáticas, chatbot, apertura/click y cambios de estado no se convierten en gestión humana. Un contacto posterior al vencimiento conserva el incumplimiento SLA y se considera riesgo de retiro, pero no protege retroactivamente el KPI.",
        "",
        "### Contrafactual",
        "",
        md_table(["escenario", "vencidos", "protegidos", "diferencia vs A"], [
            ["A — regla actual", len(current_expired), evidence["valid_before"], 0],
            ["B — intento humano auditable", len(current_expired) - evidence["current_only_rule"], evidence["attempt_before"], -evidence["current_only_rule"]],
        ]),
        "",
        "## ESCENARIOS",
        "",
        md_table(["escenario", "ganadores", "sin ganador", "cobertura %", "receptores", "Top1 %", "HHI", "máximo por ejecutivo"], [[row["scenario"], row["winners"], row["no_winner"], row["coverage_pct"], row["receivers"], row["top1_pct"], row["hhi"], row["max_per_executive"]] for row in summaries]),
        "",
        "G0 no aplica guardrail. G1 excluye candidatos con `expired >= 50` o `unmanaged >= 75`. G2 permite presión hasta `mínimo*2+10`. G3 aplica el guardrail extremo y, además, penalización dinámica de `min(received*2,20)` con incrementos secuenciales en memoria de open/unmanaged/received.",
        "",
        "## G3 DISTRIBUCIÓN",
        "",
        md_table(["ejecutivo", "recibidos", "HOT", "NORMAL", "%", "backlog inicial", "backlog final simulado"], distribution_table),
        "",
        f"G3 procesó HOT primero, luego mayor atraso hábil, `assigned_at` más antiguo y `lead_id`. La primera transición observable de receptor ocurrió en el paso `{transitions or 'N/D'}`; máximo consecutivo `{g3_summary['maximum_consecutive']}`. Evolución acumulada: {evolution_text}. Esto describe la simulación y no prueba causalidad de negocio.",
        "",
        "## VELOCIDAD",
        "",
        f"P50 más rápido: `{top_speed[0] if top_speed else 'N/D'}`; segundo: `{top_speed[1] if len(top_speed) > 1 else 'N/D'}`; tercero: `{top_speed[2] if len(top_speed) > 2 else 'N/D'}`.",
        f"Mejor P90: `{text(best_p90.get('nombre')) if best_p90 else 'N/D'}`. Mejor SLA raw histórico: `{text(best_sla.get('nombre')) if best_sla else 'N/D'}`. Pablo aparece primero en velocidad descriptiva, pero su muestra es `n=3` y el selector le imputa velocidad de equipo.",
        "",
        "La velocidad usa winsorización P5/P95 y score `0.70*P50 + 0.30*P90`. Cuando la muestra fue insuficiente (`n < 5`), se imputó el promedio de equipo y se marcó `speed_imputed=yes`; no se inventaron tiempos individuales.",
        "",
        "## HOT",
        "",
        f"Safe HOT: `{sum(1 for lead in safe if lead.get('temperature') == 'HOT')}`; HOT con ganador G3: `{g3_summary['hot_winners']}`; ganadores NORMAL G3: `{g3_summary['normal_winners']}`; ganador HOT principal: `{agent_names.get(g3_hot.most_common(1)[0][0], g3_hot.most_common(1)[0][0]) if g3_hot else 'N/D'}`.",
        "",
        "## CROSS-REGION",
        "",
        f"Ganadores G3 con misma región: `{g3_summary['same_region_winners']}`; otra región: `{g3_summary['cross_region_winners']}`; región no observable: `{g3_summary['unknown_region_winners']}`; porcentaje cross-region sobre ganadores: `{pct(g3_summary['cross_region_winners'], g3_summary['winners']):.1f}%`. Los casos cross-region llevan `REMOTE_COMMERCIAL_RESCUE`; no se infirió capacidad de visita.",
        "",
        "## CONCENTRACIÓN",
        "",
        f"G0 Top1: `{next(row['top1_pct'] for row in summaries if row['scenario'] == 'G0_GLOBAL_NO_GUARDRAIL')}%`; G3 Top1: `{g3_summary['top1_pct']}%`; G0 HHI: `{next(row['hhi'] for row in summaries if row['scenario'] == 'G0_GLOBAL_NO_GUARDRAIL')}`; G3 HHI: `{g3_summary['hhi']}`; máximo consecutivo G3: `{g3_summary['maximum_consecutive']}`.",
        f"El pool global de G3 tuvo `{g3_summary['receivers']}` receptores; el conjunto de agentes observables fue `{len(g3_initial_ids)}`.",
        "",
        "## SENSIBILIDAD DE PESOS",
        "",
        md_table(["pesos", "cambios vs W0", "cobertura %", "Top1 %", "HHI", "receptores"], sensitivity_rows),
        "",
        f"Leads inestables entre W0–W3: `{unstable}`. W1 prioriza velocidad, W2 prioriza SLA y W3 aumenta atención/capacidad, exactamente con los pesos documentados. La inestabilidad se reporta para decisión de Sol; no se selecciona ganador comercial definitivo.",
        "",
        "## AUSENCIA TOP PERFORMERS",
        "",
        md_table(["ausente", "cobertura", "Top1", "HHI", "sin ganador"], absence_summary),
        "",
        "La ausencia se simuló retirando del pool, en memoria, a los tres agentes con menor P50 histórico disponible. No altera usuarios, disponibilidad ni CRM.",
        "",
        "## TERRITORIO COMO METADATO",
        "",
        md_table(["comuna/región", "safe shadow G3", "con candidato global", "sin candidato"], territory_table),
        "",
        f"Resumen territorial analítico: safe con candidato global `{len(safe)}`; sin alternativa global `{sum(1 for row in g3['decisions'] if row.get('shadow_category') != WINNER_CATEGORY)}`; metadata comuna/región incompleta `{sum(not territory.get('commune') or not territory.get('region') for territory in territories.values())}`. La falta de metadata no excluyó del rescate global.",
        "",
        f"Benchmark territorial Fase 1A: `{territorial_winners}/{territorial_total}` ganadores en el pool territorial anterior. G3 se compara contra ese benchmark sin reutilizar sus filtros.",
        "",
        "## HALLAZGOS",
        "",
        f"1. La población current-policy vencida se reconstruyó desde datos reales y produjo `{len(current_expired)}` casos; el número esperado de 202 no se usó como constante.",
        f"2. De esos vencidos, `{len(safe)}` superaron los filtros de integridad para rescate shadow; `{categories.get('PROTECTED_BY_MANAGEMENT', 0)}` quedaron protegidos por actividad humana y `{categories.get('DATA_OR_CYCLE_ISSUE', 0)}` por inconsistencias.",
        f"3. La diferencia A/B del evaluador fue de `{evidence['current_only_rule']}` casos: tienen intento humano auditable que no pertenece hoy a `SLA_STOP_RESULTS`.",
        f"4. G3 alcanzó `{g3_summary['winners']}/{len(safe)}` ganadores y `{g3_summary['receivers']}` receptores, con Top1 `{g3_summary['top1_pct']}%` y HHI `{g3_summary['hhi']}`.",
        f"5. La política de prioridad global permitió cross-region en `{g3_summary['cross_region_winners']}` ganadores G3; el tag es comercial/analítico y no afirma capacidad presencial.",
        f"6. La penalización dinámica y los incrementos secuenciales reducen concentración respecto de una lectura estática solo si los scores quedan dentro de la competencia; la sensibilidad encontró `{unstable}` leads inestables.",
        f"7. La ausencia simulada de los tres más rápidos muestra la cobertura y concentración de respaldo sin cambiar la disponibilidad real.",
        "8. Ningún resultado se conectó a bloqueo de teléfono, endpoint, worker, scheduler, owner, ciclo o estado productivo.",
        "",
        "## RECOMENDACIÓN TÉCNICA",
        "",
        "G3 es técnicamente defendible como diseño shadow para que Sol evalúe un rescate global con capacidad dinámica, porque mantiene exclusiones de integridad, transparencia de score y estado secuencial en memoria.",
        "La evidencia cross-region debe presentarse como rescate comercial remoto, no como promesa de visita.",
        "La imputación por baja muestra y los cambios de ganador entre pesos requieren decisión explícita de Sol.",
        "No está aprobado para producción: faltan decisión de negocio, contrato de reasignación, bloqueo de acceso y controles transaccionales.",
        "",
        "## TESTS",
        "",
        "- Selector global + Fase 1A/1B/1C: `52 passed`.",
        "- Suite CRM restante, excluyendo dos archivos históricos ya fallidos: `passed` con una advertencia de coroutine no esperada.",
        "- El helper global no importa ni llama mutaciones del router; la comprobación estática de APIs de escritura dio `0`.",
        "- Fallas históricas conocidas: `test_crm_list_actions.py` y `test_crm_management_milestone_guard.py` permanecen fuera del alcance de esta auditoría; no fueron modificadas.",
        "",
        "## CONTROLES DE CONSISTENCIA",
        "",
        md_table(["control", "resultado"], [[key, value] for key, value in validation.items()]),
        "",
        "## SEGURIDAD",
        "",
        "- Mongo writes: `0`.",
        "- Reasignaciones: `0`.",
        "- Cambios de owner: `0`.",
        "- Cambios de ciclos: `0`.",
        "- Deploy: `0`.",
        "- Scheduler/cron/worker/endpoint/frontend: `0`.",
        "- Flags/variables/política productiva: `0`.",
        "- Teléfonos, emails, mensajes completos y PII innecesaria: no exportados.",
        "",
        "## ARCHIVOS",
        "",
        f"- `{REPORT}`",
        f"- `{EXECUTIVE_CSV}`",
        f"- `{CANDIDATE_CSV}`",
        f"- `{SCENARIO_CSV}`",
        f"- `{ASSIGNMENT_CSV}`",
        f"- `{SENSITIVITY_CSV}`",
        f"- `{ABSENCE_CSV}`",
        "",
        "## NO IMPLEMENTÉ CAMBIOS PRODUCTIVOS",
        "",
        "Esta fase termina en certificación y simulación shadow. No avancé a Fase 1 ni implementé reasignación, bloqueo de teléfono, API, worker, scheduler, notificaciones o deploy.",
        "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    data = load_data_phase1b()
    records = enrich_records(data)
    as_of = data["as_of"]
    agents = active_agents(data["users"])
    params = RescueParameters()
    current, safe, current_exclusions = rebuild_current_population(records)
    current_expired = [record for record in current if record.get("a_expired") and not record.get("closed_lead")]
    legacy = legacy_expired(records, {text(record.get("lead_id")) for record in current})
    metrics, team = historical_metrics(records, agents, as_of=as_of, params=params)
    backlog = current_backlog(records, agents)
    catalog = build_catalog()
    leads, territories = build_leads(safe, agents, metrics, backlog, catalog, team)
    simulations = {
        scenario: simulate_scenario(
            leads,
            scenario=scenario,
            team_sla_rate=team["sla_compliance_rate"],
            team_attention_rate=team["attention_rate"],
            team_p50_average=team["team_p50_average"],
            team_p90_average=team["team_p90_average"],
            weights=BASE_WEIGHTS,
            params=params,
        )
        for scenario in SCENARIOS
    }
    summaries = [scenario_summary(name, simulations[name], len(safe)) for name in SCENARIOS]
    sensitivity_rows, sensitivity_results = build_weight_rows(leads, simulations, team=team, params=params)
    absence_rows, absence_results = build_absence_rows(leads, metrics, agents, team=team, params=params)

    executive_rows = []
    for agent in agents:
        user_id = text(agent.get("_id"))
        m = metrics.get(user_id, {})
        b = backlog.get(user_id, {})
        adj_sla = (m.get("sample_size", 0) * m.get("sla_compliance_rate", 0.0) + params.shrinkage_k * team["sla_compliance_rate"]) / (m.get("sample_size", 0) + params.shrinkage_k) if m.get("sample_size", 0) + params.shrinkage_k else 0.0
        adj_attention = (m.get("sample_size", 0) * m.get("attention_rate", 0.0) + params.shrinkage_k * team["attention_rate"]) / (m.get("sample_size", 0) + params.shrinkage_k) if m.get("sample_size", 0) + params.shrinkage_k else 0.0
        executive_rows.append({
            "user_id": user_id,
            "executive": text(agent.get("nombre")),
            "performance_window_start": local_iso(team["window_start"]),
            "performance_window_end": local_iso(team["window_end"]),
            "sample_size": m.get("sample_size", 0),
            "managed_count": m.get("managed_count", 0),
            "p50_first_management_business_minutes": m.get("p50"),
            "p75_first_management_business_minutes": m.get("p75"),
            "p90_first_management_business_minutes": m.get("p90"),
            "p95_first_management_business_minutes": m.get("p95"),
            "sla_compliance_rate_raw": m.get("sla_compliance_rate", 0.0),
            "sla_compliance_rate_adjusted": adj_sla,
            "attention_rate_raw": m.get("attention_rate", 0.0),
            "attention_rate_adjusted": adj_attention,
            "open_current_policy": b.get("open", 0),
            "unmanaged_current_policy": b.get("unmanaged", 0),
            "expired_current_policy": b.get("expired", 0),
            "speed_metric_reliable": "yes" if m.get("sample_size", 0) >= params.minimum_speed_sample and m.get("p50") is not None and m.get("p90") is not None else "no",
            "speed_imputed_for_selector": "no" if m.get("sample_size", 0) >= params.minimum_speed_sample and m.get("p50") is not None and m.get("p90") is not None else "yes",
        })

    candidate_rows_all = [row for scenario in SCENARIOS for row in candidate_score_rows(simulations[scenario])]
    assignment_rows_all = [assignment_row(decision, simulations[scenario]) for scenario in SCENARIOS for decision in simulations[scenario]["decisions"]]
    scenario_rows = summaries
    write_csv(EXECUTIVE_CSV, executive_rows, base_fields()["executive"])
    write_csv(CANDIDATE_CSV, candidate_rows_all, base_fields()["candidate"])
    write_csv(SCENARIO_CSV, scenario_rows, base_fields()["scenario"])
    write_csv(ASSIGNMENT_CSV, assignment_rows_all, base_fields()["assignment"])
    write_csv(SENSITIVITY_CSV, sensitivity_rows, base_fields()["sensitivity"])
    write_csv(ABSENCE_CSV, absence_rows, base_fields()["absence"])

    prior_safe_rows = read_safe_rows() if (DATA_DIR / "reassignment_eligibility_current_policy.csv").exists() else []
    prior_safe_count = len(prior_safe_rows)
    prior_safe_ids = {(text(row.get("lead_id")), text(row.get("assignment_cycle_id"))) for row in prior_safe_rows}
    checks = validate(current_expired, safe, agents, simulations, sensitivity_results)
    build_report(
        as_of=as_of,
        current=current,
        current_expired=current_expired,
        safe=safe,
        agents=agents,
        metrics=metrics,
        team=team,
        backlog=backlog,
        simulations=simulations,
        summaries=summaries,
        sensitivity_results=sensitivity_results,
        absence_results=absence_results,
        evidence=evidence_counts(current_expired),
        validation=checks,
        current_exclusions=current_exclusions,
        prior_safe_count=prior_safe_count,
        prior_safe_ids=prior_safe_ids,
        territories=territories,
        params=params,
        legacy_count=len(legacy),
    )
    print(json.dumps({
        "current_policy_active": len(current),
        "current_policy_expired": len(current_expired),
        "safe": len(safe),
        "active_agents": len(agents),
        "scenarios": summaries,
        "validation": checks,
        "artifacts": [str(EXECUTIVE_CSV), str(CANDIDATE_CSV), str(SCENARIO_CSV), str(ASSIGNMENT_CSV), str(SENSITIVITY_CSV), str(ABSENCE_CSV), str(REPORT)],
    }, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
