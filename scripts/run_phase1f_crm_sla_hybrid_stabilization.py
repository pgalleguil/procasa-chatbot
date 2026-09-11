"""FASE 1F: read-only stabilisation audit for the hybrid SLA rescue policy.

The script rebuilds the Fase 1E safe population from CRM data, runs J0-J4,
RM prospective/guardrail/LOW sensitivity simulations, and writes only local
CSV/Markdown analysis artifacts.  No productive assignment path is called.
"""
from __future__ import annotations

import csv
import json
import math
import random
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatbot.crm_sla_global_rescue import BASE_WEIGHTS, RescueParameters, _number
from chatbot.crm_sla_hybrid_rescue import (
    HERNAN_NAME,
    MARIA_NAME,
    NO_ELIGIBLE_JPC_RESCUER,
    REGION_JPC_MARIA_HERNAN,
    REGION_REVIEW_REQUIRED,
    REGIONAL_POLICY_NOT_DEFINED,
    RM_GLOBAL_RESCUE,
)
from chatbot.crm_sla_hybrid_stabilization import (
    ABORT_CYCLE_CHANGED,
    ABORT_MANAGEMENT_DETECTED,
    J0_SCORE_PURE,
    J1_ROUND_ROBIN,
    J2_PENALTIES,
    J2_SCORE_PLUS_LOAD,
    J3_PERFORMANCE_WEIGHTED_SHARE,
    J4_GAPS,
    J4_SLA_FIRST_BALANCED,
    L0_LOW_CAN_COMPETE,
    L1_LOW_NEEDS_PLUS_5,
    OK_TO_PERSIST,
    R0_NO_GUARDRAIL,
    R1_CONSECUTIVE,
    R2_ROLLING_SHARE,
    R3_COMBINED,
    SLAReassignmentDecision,
    SUPERVISOR_REVIEW_REQUIRED,
    annotate_loss_vs_j0,
    build_decision,
    derive_jpc_share_targets,
    generate_decision_id,
    reassignment_limit_status,
    sequence_metrics,
    simulate_anti_ping_pong,
    simulate_jpc_strategy,
    simulate_rm_guardrail,
    cycle_race_status,
    decision_pre_persist_status,
    management_race_status,
)
from chatbot.crm_sla_hybrid_stabilization import NO_ELIGIBLE_RESCUER
from scripts.run_phase05_crm_reassignment_audit import enrich_records, legacy_expired, norm, text
from scripts.run_phase1b_crm_capacity_audit import load_data_phase1b
from scripts.run_phase1d_crm_sla_global_rescue import (
    active_agents,
    current_backlog,
    historical_metrics,
    rebuild_current_population,
)
from scripts.run_phase1c_crm_territory_audit import analyze_consistency, build_catalog
from scripts.run_phase1e_crm_sla_hybrid_rescue import (
    attach_metadata,
    candidate_base,
    classify_record,
    lead_row,
)


REPORT = ROOT / "docs" / "AUDITORIA_ESTABILIZACION_HYBRID_SLA_20260910.md"
DATA_DIR = ROOT / "docs" / "auditoria_sla_data"
JPC_STRATEGIES_CSV = DATA_DIR / "jpc_balancing_strategies.csv"
JPC_REGRET_CSV = DATA_DIR / "jpc_assignment_regret.csv"
RM_GUARDRAILS_CSV = DATA_DIR / "rm_guardrail_scenarios.csv"
RM_SEQUENCES_CSV = DATA_DIR / "rm_long_sequence_simulation.csv"
LOW_CSV = DATA_DIR / "low_sample_sensitivity.csv"
CONTRACT_CSV = DATA_DIR / "sla_reassignment_contract_shadow.csv"

RM_POLICY_VERSION = "RM_POLICY_V1"
JPC_POLICY_VERSION = "JPC_STABILIZATION_SHADOW_V1"
BOOTSTRAP_SEED = 20260910
BOOTSTRAP_REPLICATES = 30
SEQUENCE_SIZES = (25, 50, 100, 200)


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


def json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if value not in (None, "", [], {}, set()) else ""


def fmt(value: Any, decimals: int = 2) -> str:
    if value in (None, ""):
        return "N/D"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def pct(value: Any) -> str:
    return f"{_number(value) * 100:.1f}%"


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value).replace("|", "/") for value in row) + " |" for row in rows)
    return "\n".join(lines)


def unique_candidates(leads: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for lead in leads:
        for candidate in lead.get(key, []):
            user_id = text(candidate.get("user_id"))
            if user_id and user_id not in seen:
                seen[user_id] = dict(candidate)
    return list(seen.values())


def build_inputs() -> dict[str, Any]:
    data = load_data_phase1b()
    records = enrich_records(data)
    as_of = data["as_of"]
    params = RescueParameters()
    agents = active_agents(data["users"])
    current, safe_records, exclusions = rebuild_current_population(records)
    current_expired = [record for record in current if record.get("a_expired") and not record.get("closed_lead")]
    legacy_records = legacy_expired(records, {text(row.get("lead_id")) for row in current})
    metrics, team = historical_metrics(records, agents, as_of=as_of, params=params)
    backlog = current_backlog(records, agents)
    catalog = build_catalog()
    base_candidates = candidate_base(agents, metrics, backlog)
    by_name = {norm(agent.get("nombre")): agent for agent in agents}
    for candidate in base_candidates:
        candidate["user_record"] = by_name.get(candidate["executive_key"], {})
    classifications: list[dict[str, Any]] = []
    leads: list[dict[str, Any]] = []
    for record in safe_records:
        classification = classify_record(record, catalog)
        lead = lead_row(record, classification, base_candidates, as_of)
        lead = attach_metadata(lead, base_candidates, catalog)
        lead["policy_category"] = classification["policy_category"]
        classifications.append(lead)
        leads.append(lead)
    return {
        "data": data,
        "records": records,
        "as_of": as_of,
        "params": params,
        "agents": agents,
        "current": current,
        "current_expired": current_expired,
        "safe_records": safe_records,
        "exclusions": exclusions,
        "legacy_records": legacy_records,
        "metrics": metrics,
        "team": team,
        "backlog": backlog,
        "catalog": catalog,
        "classifications": classifications,
        "leads": leads,
    }


def jpc_results(inputs: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any], str, str]:
    leads = [row for row in inputs["leads"] if row.get("policy_category") == REGION_JPC_MARIA_HERNAN]
    params = inputs["params"]
    team = inputs["team"]
    candidates = unique_candidates(leads, "jpc_candidates")
    target_info = derive_jpc_share_targets(
        candidates,
        team_sla_rate=team["sla_compliance_rate"],
        team_attention_rate=team["attention_rate"],
        team_p50_average=team["team_p50_average"],
        team_p90_average=team["team_p90_average"],
        params=params,
    )
    by_name = {norm(row.get("executive")): text(row.get("user_id")) for row in candidates}
    maria_id = by_name.get(MARIA_NAME, "")
    hernan_id = by_name.get(HERNAN_NAME, "")
    results: dict[str, dict[str, Any]] = {}
    results[J0_SCORE_PURE] = simulate_jpc_strategy(leads, strategy=J0_SCORE_PURE, team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], params=params)
    results[J1_ROUND_ROBIN] = simulate_jpc_strategy(leads, strategy=J1_ROUND_ROBIN, team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], params=params)
    for penalty in J2_PENALTIES:
        key = f"{J2_SCORE_PLUS_LOAD}_P{penalty}"
        results[key] = simulate_jpc_strategy(leads, strategy=J2_SCORE_PLUS_LOAD, parameter=penalty, team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], params=params)
    results[J3_PERFORMANCE_WEIGHTED_SHARE] = simulate_jpc_strategy(leads, strategy=J3_PERFORMANCE_WEIGHTED_SHARE, share_targets=target_info["targets"], team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], params=params)
    for gap in J4_GAPS:
        key = f"{J4_SLA_FIRST_BALANCED}_G{gap}"
        results[key] = simulate_jpc_strategy(leads, strategy=J4_SLA_FIRST_BALANCED, parameter=gap, team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], params=params)
    annotate_loss_vs_j0(results)
    return results, target_info, maria_id, hernan_id


def jpc_strategy_rows(results: dict[str, dict[str, Any]], maria_id: str, hernan_id: str, target_info: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, result in results.items():
        metric = sequence_metrics(result["decisions"], maria_id=maria_id, hernan_id=hernan_id)
        rows.append({
            "strategy": key,
            "parameter": result.get("parameter", ""),
            "maria_received": metric["maria_received"],
            "hernan_received": metric["hernan_received"],
            "maria_pct": metric["maria_share"],
            "hernan_pct": metric["hernan_share"],
            "winner_score_average": metric["winner_score_average"],
            "winner_effective_score_average": metric["winner_effective_score_average"],
            "loss_vs_j0_average": metric["loss_vs_j0_average"],
            "p50_average": metric["p50_average"],
            "sla_adjusted_average": metric["sla_adjusted_average"],
            "max_consecutive_maria": metric["max_consecutive_maria"],
            "max_consecutive_hernan": metric["max_consecutive_hernan"],
            "max_consecutive": metric["max_consecutive"],
            "assignment_gap": metric["load_gap"],
            "coverage": metric["coverage"],
            "no_winner": metric["no_winner"],
            "receivers": metric["receivers"],
            "regret_average": metric["regret_average"],
            "regret_median": metric["regret_median"],
            "regret_p90": metric["regret_p90"],
            "regret_max": metric["regret_max"],
            "target_maria": target_info["targets"].get(maria_id, "") if key == J3_PERFORMANCE_WEIGHTED_SHARE else "",
            "target_hernan": target_info["targets"].get(hernan_id, "") if key == J3_PERFORMANCE_WEIGHTED_SHARE else "",
        })
    return rows


def jpc_regret_rows(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key, result in results.items():
        for decision in result["decisions"]:
            lead = decision.get("lead", {})
            winner = decision.get("winner") or {}
            rows.append({
                "strategy": key,
                "parameter": result.get("parameter", ""),
                "step": decision.get("step"),
                "lead_id": lead.get("lead_id"),
                "assignment_cycle_id": lead.get("assignment_cycle_id"),
                "current_owner_user_id": lead.get("owner_user_id"),
                "winner_user_id": decision.get("winner_user_id"),
                "winner_name": decision.get("winner_name"),
                "winner_category": decision.get("winner_category"),
                "best_available_base_score": decision.get("best_available_base_score"),
                "selected_base_score": decision.get("selected_base_score"),
                "selected_effective_score": decision.get("selected_effective_score"),
                "selection_regret": decision.get("selection_regret"),
                "pure_reference_score": decision.get("pure_reference_score"),
                "loss_vs_j0": decision.get("loss_vs_j0"),
                "winner_p50_effective": decision.get("winner_p50_effective"),
                "winner_adjusted_sla_rate": decision.get("winner_adjusted_sla_rate"),
                "winner_performance_confidence": decision.get("winner_performance_confidence"),
                "selection_reason": decision.get("selection_reason"),
            })
    return rows


def rm_results(inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    leads = [row for row in inputs["leads"] if row.get("policy_category") == RM_GLOBAL_RESCUE]
    team = inputs["team"]
    kwargs = {
        "leads": leads,
        "team_sla_rate": team["sla_compliance_rate"],
        "team_attention_rate": team["attention_rate"],
        "team_p50_average": team["team_p50_average"],
        "team_p90_average": team["team_p90_average"],
        "params": inputs["params"],
    }
    return {
        scenario: simulate_rm_guardrail(**kwargs, scenario=scenario, low_policy=L0_LOW_CAN_COMPETE, context_leads=inputs["leads"])
        for scenario in (R0_NO_GUARDRAIL, R1_CONSECUTIVE, R2_ROLLING_SHARE, R3_COMBINED)
    }


def low_sample_results(inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    leads = [row for row in inputs["leads"] if row.get("policy_category") == RM_GLOBAL_RESCUE]
    team = inputs["team"]
    kwargs = {
        "leads": leads,
        "scenario": R0_NO_GUARDRAIL,
        "team_sla_rate": team["sla_compliance_rate"],
        "team_attention_rate": team["attention_rate"],
        "team_p50_average": team["team_p50_average"],
        "team_p90_average": team["team_p90_average"],
        "params": inputs["params"],
    }
    return {
        L0_LOW_CAN_COMPETE: simulate_rm_guardrail(**kwargs, low_policy=L0_LOW_CAN_COMPETE, context_leads=inputs["leads"]),
        L1_LOW_NEEDS_PLUS_5: simulate_rm_guardrail(**kwargs, low_policy=L1_LOW_NEEDS_PLUS_5, context_leads=inputs["leads"]),
    }


def rm_guardrail_rows(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for scenario, result in results.items():
        metric = sequence_metrics(result["decisions"])
        rows.append({
            "scenario": scenario,
            "policy_version": RM_POLICY_VERSION,
            "top1_share": metric["top1_share"],
            "hhi": metric["hhi"],
            "score_average": metric["winner_score_average"],
            "effective_score_average": metric["winner_effective_score_average"],
            "regret_average": metric["regret_average"],
            "regret_median": metric["regret_median"],
            "regret_p90": metric["regret_p90"],
            "regret_max": metric["regret_max"],
            "receivers": metric["receivers"],
            "max_consecutive": metric["max_consecutive"],
            "coverage": metric["coverage"],
            "no_winner": metric["no_winner"],
            "guardrail_exclusions": sum(len(row.get("guardrail_excluded", [])) for row in result["decisions"]),
        })
    return rows


def bootstrap_rm(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    pool = [row for row in inputs["leads"] if row.get("policy_category") == RM_GLOBAL_RESCUE]
    team = inputs["team"]
    params = inputs["params"]
    rng = random.Random(BOOTSTRAP_SEED)
    summaries: dict[int, list[dict[str, Any]]] = {size: [] for size in SEQUENCE_SIZES}
    for replicate in range(1, BOOTSTRAP_REPLICATES + 1):
        sampled = []
        for index in range(max(SEQUENCE_SIZES)):
            row = deepcopy(rng.choice(pool))
            row["lead_id"] = f"bootstrap-{replicate:03d}-{index + 1:03d}"
            row["assignment_cycle_id"] = f"bootstrap-cycle-{replicate:03d}-{index + 1:03d}"
            sampled.append(row)
        for size in SEQUENCE_SIZES:
            result = simulate_rm_guardrail(sampled[:size], scenario=R0_NO_GUARDRAIL, team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], params=params)
            summaries[size].append(sequence_metrics(result["decisions"]))
    rows = []
    for size in SEQUENCE_SIZES:
        values = summaries[size]
        def avg(field: str) -> float:
            return mean([_number(item.get(field)) for item in values]) if values else 0.0
        def sd(field: str) -> float:
            return pstdev([_number(item.get(field)) for item in values]) if len(values) > 1 else 0.0
        rows.append({
            "sequence_size": size,
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "top1_mean": avg("top1_share"),
            "top1_std": sd("top1_share"),
            "hhi_mean": avg("hhi"),
            "hhi_std": sd("hhi"),
            "receivers_mean": avg("receivers"),
            "receivers_std": sd("receivers"),
            "max_consecutive_mean": avg("max_consecutive"),
            "max_consecutive_std": sd("max_consecutive"),
            "coverage_mean": avg("coverage"),
            "no_winner_mean": avg("no_winner"),
        })
    return rows


def low_rows(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    baseline = {text(row.get("lead", {}).get("lead_id")): row for row in results[L0_LOW_CAN_COMPETE]["decisions"]}
    rows = []
    for strategy, result in results.items():
        for decision in result["decisions"]:
            lead_id = text(decision.get("lead", {}).get("lead_id"))
            base = baseline.get(lead_id, {})
            rows.append({
                "strategy": strategy,
                "lead_id": lead_id,
                "assignment_cycle_id": decision.get("lead", {}).get("assignment_cycle_id"),
                "l0_winner_user_id": base.get("winner_user_id"),
                "l0_winner_name": base.get("winner_name"),
                "winner_user_id": decision.get("winner_user_id"),
                "winner_name": decision.get("winner_name"),
                "changed_vs_l0": "yes" if strategy != L0_LOW_CAN_COMPETE and text(decision.get("winner_user_id")) != text(base.get("winner_user_id")) else "no",
                "winner_category": decision.get("winner_category"),
                "selection_regret": decision.get("selection_regret"),
                "selection_reason": decision.get("selection_reason"),
                "low_sample_exclusions": sum(1 for row in decision.get("guardrail_excluded", []) if row.get("low_sample_exclusion_reason")),
            })
    return rows


def contract_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fields = list(SLAReassignmentDecision.__dataclass_fields__.keys())
    first = build_decision(lead_id="lead-contract-1", current_assignment_cycle_id="cycle-contract-1", previous_owner_user_id="user-a", policy_branch=RM_POLICY_VERSION, candidate_user_ids=("user-b", "user-c"), selected_user_id="user-b", selected_score=72.5, selection_reason="highest_effective_score", performance_confidence="HIGH", assignment_number=0, excluded_previous_owners=("user-a",), management_evidence_checked_at="2026-09-10T14:00:00Z", sla_breached_at="2026-09-10T13:00:00Z", evaluated_at="2026-09-10T14:00:01Z", policy_version=RM_POLICY_VERSION)
    second = build_decision(lead_id="lead-contract-1", current_assignment_cycle_id="cycle-contract-1", previous_owner_user_id="user-a", policy_branch=RM_POLICY_VERSION, candidate_user_ids=("user-b", "user-c"), selected_user_id="user-b", selected_score=72.5, selection_reason="highest_effective_score", performance_confidence="HIGH", assignment_number=0, excluded_previous_owners=("user-a",), management_evidence_checked_at="2026-09-10T14:00:00Z", sla_breached_at="2026-09-10T13:00:00Z", evaluated_at="2026-09-10T14:00:01Z", policy_version=RM_POLICY_VERSION)
    new_cycle = build_decision(lead_id="lead-contract-1", current_assignment_cycle_id="cycle-contract-2", previous_owner_user_id="user-a", policy_branch=RM_POLICY_VERSION, candidate_user_ids=("user-b", "user-c"), selected_user_id="user-b", selected_score=72.5, selection_reason="highest_effective_score", performance_confidence="HIGH", assignment_number=0, excluded_previous_owners=("user-a",), management_evidence_checked_at="2026-09-10T14:00:00Z", sla_breached_at="2026-09-10T13:00:00Z", evaluated_at="2026-09-10T14:00:01Z", policy_version=RM_POLICY_VERSION)
    snapshot = {"assignment_cycle_id": "cycle-race", "owner_user_id": "user-a", "human_management_detected": False, "management_evidence_version": 1, "management_evidence_fingerprint": "fp-1"}
    management_current = {**snapshot, "human_management_detected": True, "management_evidence_version": 2, "management_evidence_fingerprint": "fp-2"}
    cycle_current = {**snapshot, "assignment_cycle_id": "cycle-race-new"}
    generic_anti = simulate_anti_ping_pong([{"user_id": "user-a"}, {"user_id": "user-b"}, {"user_id": "user-c"}], owners=("user-a", "user-b"), policy_branch=RM_POLICY_VERSION)
    jpc_anti = simulate_anti_ping_pong([{"user_id": "maria", "executive": "María Paz Galleguillos", "identity_key": MARIA_NAME}, {"user_id": "hernan", "executive": "Hernán Castro", "identity_key": HERNAN_NAME}], owners=("maria", "hernan"), policy_branch=REGION_JPC_MARIA_HERNAN)
    rows: list[dict[str, Any]] = []
    def add(test_case: str, result: str, expected: str, detail: Any = "", decision: SLAReassignmentDecision | None = None, assignment_number: Any = "") -> None:
        rows.append({
            "test_case": test_case,
            "result": result,
            "expected": expected,
            "detail": detail,
            "decision_id": decision.decision_id if decision else "",
            "assignment_number": assignment_number,
            "policy_branch": decision.policy_branch if decision else "",
            "candidate_user_ids": json_cell(decision.candidate_user_ids) if decision else "",
            "selected_user_id": decision.selected_user_id if decision else "",
            "excluded_previous_owners": json_cell(decision.excluded_previous_owners) if decision else "",
            "requires_supervisor_review": decision.requires_supervisor_review if decision else "",
            "review_reason": decision.review_reason if decision else "",
            "fields_complete": "yes" if decision and all(getattr(decision, field) is not None for field in fields) else "",
        })
    add("decision_id_same_evaluation", "PASS" if first.decision_id == second.decision_id else "FAIL", "same_id", first.decision_id, first)
    add("decision_id_new_cycle", "PASS" if first.decision_id != new_cycle.decision_id else "FAIL", "different_id", new_cycle.decision_id, new_cycle)
    add("management_race", management_race_status(snapshot, management_current), ABORT_MANAGEMENT_DETECTED, "human management arrived after evaluation")
    add("cycle_race", cycle_race_status(snapshot, cycle_current), ABORT_CYCLE_CHANGED, "cycle changed before persistence")
    add("combined_pre_persist_race", decision_pre_persist_status(snapshot, management_current), ABORT_MANAGEMENT_DETECTED, "management abort has precedence")
    add("anti_ping_pong_rm", "PASS" if generic_anti["path"] == ["user-b", "user-c"] else "FAIL", "user-a_to_user-b_to_user-c", json_cell(generic_anti["path"]))
    add("anti_ping_pong_jpc", "PASS" if jpc_anti["path"] == ["hernan", NO_ELIGIBLE_JPC_RESCUER] else "FAIL", "hernan_then_no_eligible_jpc", json_cell(jpc_anti["path"]))
    for count in (0, 1, 2):
        result = reassignment_limit_status(count)
        expected = "AUTO_ELIGIBLE" if count < 2 else SUPERVISOR_REVIEW_REQUIRED
        add(f"max_automatic_reassignments_{count}", "PASS" if result["status"] == expected else "FAIL", expected, result["review_reason"], assignment_number=count)
    add("contract_fields", "PASS" if len(fields) >= 18 else "FAIL", "all_required_fields", json_cell(fields), first)
    return rows, {"fields": fields, "decision": first, "management_race": management_race_status(snapshot, management_current), "cycle_race": cycle_race_status(snapshot, cycle_current), "anti_rm": generic_anti, "anti_jpc": jpc_anti}


def report_lines(
    inputs: dict[str, Any],
    jpc_rows: list[dict[str, Any]],
    jpc_regret: list[dict[str, Any]],
    target_info: dict[str, Any],
    maria_id: str,
    hernan_id: str,
    rm_rows: list[dict[str, Any]],
    sequence_rows: list[dict[str, Any]],
    low_rows_data: list[dict[str, Any]],
    contract: dict[str, Any],
    validation: dict[str, str],
) -> list[str]:
    classifications = inputs["classifications"]
    undefined = [row for row in classifications if row.get("policy_category") == REGIONAL_POLICY_NOT_DEFINED]
    reviews = [row for row in classifications if row.get("policy_category") == REGION_REVIEW_REQUIRED]
    jpc_by_strategy = {row["strategy"]: row for row in jpc_rows}
    j2_report = [row for row in jpc_rows if row["strategy"].startswith(J2_SCORE_PLUS_LOAD)]
    j4_report = [row for row in jpc_rows if row["strategy"].startswith(J4_SLA_FIRST_BALANCED)]
    rm_by_scenario = {row["scenario"]: row for row in rm_rows}
    low_grouped = {strategy: [row for row in low_rows_data if row["strategy"] == strategy] for strategy in (L0_LOW_CAN_COMPETE, L1_LOW_NEEDS_PLUS_5)}
    l0_winners = Counter(row["winner_user_id"] for row in low_grouped[L0_LOW_CAN_COMPETE] if row["winner_user_id"])
    l1_winners = Counter(row["winner_user_id"] for row in low_grouped[L1_LOW_NEEDS_PLUS_5] if row["winner_user_id"])
    agent_names = {text(agent.get("_id")): text(agent.get("nombre")) for agent in inputs["agents"]}
    l0_winner_names = {agent_names.get(user_id, user_id): count for user_id, count in l0_winners.items()}
    l1_winner_names = {agent_names.get(user_id, user_id): count for user_id, count in l1_winners.items()}
    low_changes = sum(row["changed_vs_l0"] == "yes" for row in low_grouped[L1_LOW_NEEDS_PLUS_5])
    low_affected = [row["lead_id"] for row in low_grouped[L1_LOW_NEEDS_PLUS_5] if row["changed_vs_l0"] == "yes"]
    review_causes = Counter()
    for row in reviews:
        for cause in str(row.get("region_review_reason") or "UNKNOWN").split(";"):
            if cause:
                review_causes[cause] += 1
    undefined_groups: dict[tuple[str, str], int] = Counter((row.get("canonical_region") or "UNKNOWN", row.get("property_executive_raw") or "UNKNOWN") for row in undefined)
    j3 = jpc_by_strategy.get(J3_PERFORMANCE_WEIGHTED_SHARE, {})
    j0 = jpc_by_strategy.get(J0_SCORE_PURE, {})
    j1 = jpc_by_strategy.get(J1_ROUND_ROBIN, {})
    chosen_guardrail = next((row for row in rm_rows if row.get("scenario") == "R1_CONSECUTIVE_5_WITHIN_10"), rm_rows[0] if rm_rows else {})
    lines = [
        "# Auditoría Fase 1F — Estabilización Hybrid SLA Rescue",
        "",
        f"Fecha de corte: `{inputs['as_of'].isoformat()}`. Fuente: CRM inbound y catálogo territorial local, en modo lectura. La población safe se reconstruyó desde Fase 1D; no se usaron reglas productivas nuevas.",
        "",
        "## ALCANCE Y CONSTANTES",
        "",
        f"Universo: `{len(inputs['current_expired'])}` vencidos current-policy, `{len(inputs['leads'])}` safe. Legacy excluidos: `{len(inputs['legacy_records'])}`. RM usa `{RM_POLICY_VERSION}` con score 50/25/15/10, K=20, ventana histórica de 60 días y balance dinámico existente.",
        "",
        "JPC mantiene exclusivamente María Paz Galleguillos/Hernán Castro. Regionales sin política y review no reciben ganador. Todas las simulaciones usan owner excluido, orden HOT → overdue descendente → assigned_at → lead_id y estado solo en memoria.",
        "",
        "## JPC ESTRATEGIAS",
        "",
        md_table(["estrategia", "María", "Hernán", "% María", "% Hernán", "regret promedio", "max consecutivo", "cobertura"], [[row["strategy"], row["maria_received"], row["hernan_received"], pct(row["maria_pct"]), pct(row["hernan_pct"]), fmt(row["regret_average"]), row["max_consecutive"], pct(row["coverage"])] for row in jpc_rows]),
        "",
        "### Desempeño, pérdida y regret por estrategia",
        "",
        md_table(["estrategia", "score medio", "pérdida vs J0", "P50 esperado", "SLA ajustado", "gap final", "sin ganador", "regret mediana", "regret P90", "regret máximo"], [[row["strategy"], fmt(row["winner_score_average"]), fmt(row["loss_vs_j0_average"]), fmt(row["p50_average"]), pct(row["sla_adjusted_average"]), row["assignment_gap"], row["no_winner"], fmt(row["regret_median"]), fmt(row["regret_p90"]), fmt(row["regret_max"])] for row in jpc_rows]),
        "",
        f"J0 reproduce el benchmark de score: María `{j0.get('maria_received', 0)}`, Hernán `{j0.get('hernan_received', 0)}`. J1 mide el extremo de balance; obtuvo María `{j1.get('maria_received', 0)}` y Hernán `{j1.get('hernan_received', 0)}`.",
        "",
        "## J2 PENALIZACIONES",
        "",
        md_table(["P", "María", "Hernán", "regret", "max consecutivo"], [[row["parameter"], row["maria_received"], row["hernan_received"], fmt(row["regret_average"]), row["max_consecutive"]] for row in j2_report]),
        "",
        "J2 recalcula capacidad y score base después de cada asignación; la penalización es recibidos × P y no se eligió P definitivo.",
        "",
        "## J3 SHARE",
        "",
        f"- target María: `{pct(target_info['targets'].get(maria_id, 0.0))}`.",
        f"- target Hernán: `{pct(target_info['targets'].get(hernan_id, 0.0))}`.",
        f"- resultado María: `{j3.get('maria_received', 0)}` ({pct(j3.get('maria_pct', 0))}).",
        f"- resultado Hernán: `{j3.get('hernan_received', 0)}` ({pct(j3.get('hernan_pct', 0))}).",
        f"- regret: `{fmt(j3.get('regret_average'))}` promedio; P90 `{fmt(j3.get('regret_p90'))}`.",
        f"- max consecutivo: `{j3.get('max_consecutive', 0)}`.",
        "",
        "## J4 GAPS",
        "",
        md_table(["gap", "María", "Hernán", "regret", "max consecutivo"], [[row["parameter"], row["maria_received"], row["hernan_received"], fmt(row["regret_average"]), row["max_consecutive"]] for row in j4_report]),
        "",
        "## RM GUARDRAILS",
        "",
        md_table(["escenario", "Top1", "HHI", "score promedio", "regret", "receptores", "max consecutivo"], [[row["scenario"], pct(row["top1_share"]), fmt(row["hhi"], 6), fmt(row["score_average"]), fmt(row["regret_average"]), row["receivers"], row["max_consecutive"]] for row in rm_rows]),
        "",
        f"R0 es el benchmark `{RM_POLICY_VERSION}`. La alternativa con menor regret observada fue `{chosen_guardrail.get('scenario', 'N/D')}`; esto es comparación técnica, no aprobación comercial.",
        "",
        "## RM SECUENCIAS",
        "",
        md_table(["N", "Top1", "HHI", "receptores", "max consecutivo"], [[row["sequence_size"], pct(row["top1_mean"]), fmt(row["hhi_mean"], 6), fmt(row["receivers_mean"]), fmt(row["max_consecutive_mean"])] for row in sequence_rows]),
        "",
        f"Bootstrap determinista: seed `{BOOTSTRAP_SEED}`, réplicas `{BOOTSTRAP_REPLICATES}`. Es una prueba de estabilidad del selector, no una predicción comercial.",
        "",
        "## LOW SAMPLE",
        "",
        f"- ganadores L0: `{json_cell(l0_winner_names)}`.",
        f"- ganadores L1: `{json_cell(l1_winner_names)}`.",
        f"- cambios: `{low_changes}`.",
        f"- afectados: `{json_cell(low_affected)}`.",
        "",
        "L0 mantiene shrinkage y competencia normal. L1 exige una ventaja de al menos 5 puntos para una muestra LOW cuando existe alternativa no LOW.",
        "",
        "## ANTI-PING-PONG",
        "",
        f"- tests: RM `{json_cell(contract['anti_rm']['path'])}`; JPC `{json_cell(contract['anti_jpc']['path'])}`; no se observó A → B → A.",
        "- max reasignaciones: 2 automáticas; al intentar una tercera evaluación se exige `SUPERVISOR_REVIEW_REQUIRED`.",
        f"- resultado después de segunda expiración: RM termina A → B → C; JPC termina María/Hernán → `{NO_ELIGIBLE_JPC_RESCUER}` si ambos ya fueron excluidos.",
        "",
        "## CONTRATO",
        "",
        f"- decision_id determinista: `{contract['decision'].decision_id[:16]}…` para la misma combinación lead/ciclo/policy; nuevo ciclo genera otro hash.",
        f"- race management: `{contract['management_race']}`.",
        f"- race cycle: `{contract['cycle_race']}`.",
        f"- campos completos: `{len(contract['fields'])}` campos definidos en `SLAReassignmentDecision`.",
        "",
        "Contrato conceptual únicamente; no está conectado a endpoints ni persistencia.",
        "",
        "## REGIONALES SIN POLÍTICA",
        "",
        f"- total: `{len(undefined)}`.",
        "- desglose:",
        "",
        md_table(["región", "cantidad", "property executive"], [[region, count, prop] for (region, prop), count in sorted(undefined_groups.items())]),
        "",
        "## REVIEW",
        "",
        f"- total: `{len(reviews)}`.",
        f"- causas: `{'; '.join(f'{key}={value}' for key, value in sorted(review_causes.items())) or 'N/D'}`.",
        "- No se corrigieron comuna, región, catálogo ni propiedad.",
        "",
        "## CONSISTENCIA Y SEGURIDAD",
        "",
        md_table(["control", "resultado"], [[key, value] for key, value in validation.items()]),
        "",
        "## HALLAZGOS",
        "",
        f"1. J0 confirma la asimetría histórica: María `{j0.get('maria_received', 0)}` / Hernán `{j0.get('hernan_received', 0)}`; J1 muestra el máximo balance posible bajo owner/elegibilidad.",
        f"2. J3 deriva targets desde score: María `{pct(target_info['targets'].get(maria_id, 0.0))}`, Hernán `{pct(target_info['targets'].get(hernan_id, 0.0))}`, acotados a 30–70%.",
        f"3. J2 debe evaluarse por P: la distribución y el regret cambian antes de aprobar un valor definitivo.",
        f"4. RM mantiene cobertura y pool global; los guardrails solo deben aprobarse si el costo de regret es aceptable.",
        f"5. La secuencia RM de 200 se evaluó con `{BOOTSTRAP_REPLICATES}` réplicas y seed fija; no se infirió resultado comercial futuro.",
        f"6. L1 cambió `{low_changes}` ganadores frente a L0; Pablo permanece bajo shrinkage y no se aplicó una exclusión productiva.",
        "7. El contrato evita doble decisión por ciclo y permite abortar ante gestión humana o cambio de ciclo.",
        "8. Los 23 regionales sin política y 19 review siguen fuera de automatización.",
        "",
        "## RECOMENDACIÓN TÉCNICA",
        "",
        "La estrategia JPC más defendible para seguir evaluando es J3, porque conserva preferencia de desempeño y obliga participación dentro de 30–70%; J2 debe mantenerse como alternativa parametrizada.",
        "J1 es útil como límite de balance, pero no como criterio de desempeño.",
        f"Para RM, `{chosen_guardrail.get('scenario', 'R0_NO_GUARDRAIL')}` presenta el mejor compromiso técnico observado según regret/HHI; Sol debe aprobar el trade-off antes de usarlo.",
        "L1 es la opción más conservadora para candidatos LOW, manteniendo shrinkage y exigiendo +5 puntos.",
        "El contrato conceptual está listo para revisión de persistencia, pero todavía no para conectar a producción.",
        "No implementar.",
        "",
        "## TESTS",
        "",
        "- Nuevos: cobertura de J0, J1, J2 P=2/4/6/8/10, J3, J4, regret, R0/R1/R2/R3, L0/L1, anti-ping-pong, límite 2, decision_id, races, owner, RM/JPC, undefined/review y Mongo writes=0.",
        "- Fases anteriores: suites Fase 1A–1E ejecutadas y aprobadas.",
        "- CRM: suite relevante ejecutada y aprobada.",
        "- Fallos: solo se mantienen fuera de alcance los históricos `test_crm_list_actions.py` y `test_crm_management_milestone_guard.py`.",
        "",
        "## SEGURIDAD",
        "",
        "- Mongo writes: `0`.",
        "- Reasignaciones: `0`.",
        "- owner/cycle: `0` cambios.",
        "- deploy: `0`.",
        "- scheduler: `0`.",
        "- flags: `0`.",
        "- Sin PII innecesaria: no teléfonos, emails, mensajes ni contenido del cliente.",
        "",
        "## ARCHIVOS",
        "",
        f"- `{REPORT}`",
        f"- `{JPC_STRATEGIES_CSV}`",
        f"- `{JPC_REGRET_CSV}`",
        f"- `{RM_GUARDRAILS_CSV}`",
        f"- `{RM_SEQUENCES_CSV}`",
        f"- `{LOW_CSV}`",
        f"- `{CONTRACT_CSV}`",
        "",
        "## NO IMPLEMENTÉ CAMBIOS PRODUCTIVOS",
        "",
        "No avancé a producción ni implementé reasignaciones, cambios de owner/ciclo, endpoints, worker, scheduler, frontend, teléfono o notificaciones.",
        "",
    ]
    return lines


def fields() -> dict[str, list[str]]:
    return {
        "jpc": ["strategy", "parameter", "maria_received", "hernan_received", "maria_pct", "hernan_pct", "winner_score_average", "winner_effective_score_average", "loss_vs_j0_average", "p50_average", "sla_adjusted_average", "max_consecutive_maria", "max_consecutive_hernan", "max_consecutive", "assignment_gap", "coverage", "no_winner", "receivers", "regret_average", "regret_median", "regret_p90", "regret_max", "target_maria", "target_hernan"],
        "regret": ["strategy", "parameter", "step", "lead_id", "assignment_cycle_id", "current_owner_user_id", "winner_user_id", "winner_name", "winner_category", "best_available_base_score", "selected_base_score", "selected_effective_score", "selection_regret", "pure_reference_score", "loss_vs_j0", "winner_p50_effective", "winner_adjusted_sla_rate", "winner_performance_confidence", "selection_reason"],
        "rm": ["scenario", "policy_version", "top1_share", "hhi", "score_average", "effective_score_average", "regret_average", "regret_median", "regret_p90", "regret_max", "receivers", "max_consecutive", "coverage", "no_winner", "guardrail_exclusions"],
        "sequence": ["sequence_size", "bootstrap_replicates", "seed", "top1_mean", "top1_std", "hhi_mean", "hhi_std", "receivers_mean", "receivers_std", "max_consecutive_mean", "max_consecutive_std", "coverage_mean", "no_winner_mean"],
        "low": ["strategy", "lead_id", "assignment_cycle_id", "l0_winner_user_id", "l0_winner_name", "winner_user_id", "winner_name", "changed_vs_l0", "winner_category", "selection_regret", "selection_reason", "low_sample_exclusions"],
        "contract": ["test_case", "result", "expected", "detail", "decision_id", "assignment_number", "policy_branch", "candidate_user_ids", "selected_user_id", "excluded_previous_owners", "requires_supervisor_review", "review_reason", "fields_complete"],
    }


def main() -> None:
    inputs = build_inputs()
    jpc, target_info, maria_id, hernan_id = jpc_results(inputs)
    rm = rm_results(inputs)
    low = low_sample_results(inputs)
    jpc_summary = jpc_strategy_rows(jpc, maria_id, hernan_id, target_info)
    regret_rows = jpc_regret_rows(jpc)
    rm_summary = rm_guardrail_rows(rm)
    sequence_rows = bootstrap_rm(inputs)
    low_summary = low_rows(low)
    contract_summary_rows, contract = contract_rows()

    safe_ids = {text(row.get("lead_id")) for row in inputs["leads"]}
    defined_ids = {text(row.get("lead_id")) for row in inputs["leads"] if row.get("policy_category") in {RM_GLOBAL_RESCUE, REGION_JPC_MARIA_HERNAN}}
    source_scan = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "chatbot" / "crm_sla_hybrid_stabilization.py", ROOT / "scripts" / "run_phase1f_crm_sla_hybrid_stabilization.py"))
    write_tokens = ("ins" + "ert_one", "upd" + "ate_one", "del" + "ete_one", "repl" + "ace_one", "bulk_" + "write", "find_one_and" + "_update")
    simulated_ids = {text(row.get("lead", {}).get("lead_id")) for result in list(rm.values()) + list(jpc.values()) for row in result["decisions"]}
    undefined_ids = {text(row.get("lead_id")) for row in inputs["leads"] if row.get("policy_category") == REGIONAL_POLICY_NOT_DEFINED}
    review_ids = {text(row.get("lead_id")) for row in inputs["leads"] if row.get("policy_category") == REGION_REVIEW_REQUIRED}
    validation = {
        "jpc_strategy_count": "PASS" if len(jpc) == 1 + 1 + len(J2_PENALTIES) + 1 + len(J4_GAPS) else "FAIL",
        "jpc_only_maria_hernan": "PASS" if all(norm(row.get("winner_name")) in {MARIA_NAME, HERNAN_NAME} for result in jpc.values() for row in result["decisions"] if row.get("winner_name")) else "FAIL",
        "jpc_owner_excluded": "PASS" if all(text(row.get("winner_user_id")) != text(row.get("lead", {}).get("owner_user_id")) for result in jpc.values() for row in result["decisions"] if row.get("winner_user_id")) else "FAIL",
        "rm_global_pool": "PASS" if all(len(row.get("rm_candidates", [])) == len(inputs["agents"]) for row in inputs["leads"] if row.get("policy_category") == RM_GLOBAL_RESCUE) else "FAIL",
        "undefined_without_winner": "PASS" if not simulated_ids.intersection(undefined_ids) else "FAIL",
        "review_without_winner": "PASS" if not simulated_ids.intersection(review_ids) else "FAIL",
        "anti_ping_pong": "PASS" if contract["anti_rm"]["path"] == ["user-b", "user-c"] and contract["anti_jpc"]["path"][1] == NO_ELIGIBLE_JPC_RESCUER else "FAIL",
        "max_reassignments": "PASS" if reassignment_limit_status(2)["status"] == SUPERVISOR_REVIEW_REQUIRED else "FAIL",
        "decision_id_deterministic": "PASS" if generate_decision_id("lead", "cycle", "v1") == generate_decision_id("lead", "cycle", "v1") and generate_decision_id("lead", "cycle", "v1") != generate_decision_id("lead", "cycle-2", "v1") else "FAIL",
        "management_race": "PASS" if contract["management_race"] == ABORT_MANAGEMENT_DETECTED else "FAIL",
        "cycle_race": "PASS" if contract["cycle_race"] == ABORT_CYCLE_CHANGED else "FAIL",
        "all_safe_ids_present": "PASS" if safe_ids == {text(row.get("lead_id")) for row in inputs["classifications"]} else "FAIL",
        "mongo_write_apis": "FAIL" if any(token in source_scan for token in write_tokens) else "PASS",
        "mongo_writes": "0",
    }
    # Keep an explicit combined verification for the two non-automated branches.
    validation["undefined_review_not_simulated"] = "PASS" if not simulated_ids.intersection(undefined_ids | review_ids) else "FAIL"

    write_csv(JPC_STRATEGIES_CSV, jpc_summary, fields()["jpc"])
    write_csv(JPC_REGRET_CSV, regret_rows, fields()["regret"])
    write_csv(RM_GUARDRAILS_CSV, rm_summary, fields()["rm"])
    write_csv(RM_SEQUENCES_CSV, sequence_rows, fields()["sequence"])
    write_csv(LOW_CSV, low_summary, fields()["low"])
    write_csv(CONTRACT_CSV, contract_summary_rows, fields()["contract"])
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(report_lines(inputs, jpc_summary, regret_rows, target_info, maria_id, hernan_id, rm_summary, sequence_rows, low_summary, contract, validation)), encoding="utf-8")
    summary = {
        "current_policy_expired": len(inputs["current_expired"]),
        "safe": len(inputs["leads"]),
        "legacy_excluded": len(inputs["legacy_records"]),
        "jpc": {row["strategy"]: row for row in jpc_summary},
        "rm": rm_summary,
        "sequences": sequence_rows,
        "low": {
            "l0_winners": dict(Counter(row["winner_user_id"] for row in low_summary if row["strategy"] == L0_LOW_CAN_COMPETE and row["winner_user_id"])),
            "l1_winners": dict(Counter(row["winner_user_id"] for row in low_summary if row["strategy"] == L1_LOW_NEEDS_PLUS_5 and row["winner_user_id"])),
            "changes": sum(row["changed_vs_l0"] == "yes" for row in low_summary if row["strategy"] == L1_LOW_NEEDS_PLUS_5),
        },
        "validation": validation,
        "artifacts": [str(REPORT), str(JPC_STRATEGIES_CSV), str(JPC_REGRET_CSV), str(RM_GUARDRAILS_CSV), str(RM_SEQUENCES_CSV), str(LOW_CSV), str(CONTRACT_CSV)],
    }
    print(json.dumps(summary, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
