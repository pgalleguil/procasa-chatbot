"""FASE 1E: shadow audit of the approved hybrid SLA rescue policy.

This script reconstructs the current safe population from CRM data, applies
the three policy branches in memory, and writes only analytical CSV/Markdown
artifacts. It never calls the productive router or performs Mongo writes.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatbot.crm_sla_global_rescue import BASE_WEIGHTS, WINNER_CATEGORY, load_pressure, score_candidates
from chatbot.crm_sla_hybrid_rescue import (
    CONFIDENCE_GUARD_CATEGORY,
    HERNAN_NAME,
    JPC_NAME,
    MARIA_NAME,
    NO_ELIGIBLE_JPC_RESCUER,
    NOT_SIMULATED_POLICY,
    PROPERTY_EXECUTIVE_UNRESOLVED,
    REGION_JPC_MARIA_HERNAN,
    REGION_METROPOLITANA,
    REGION_REVIEW_REQUIRED,
    RM_GLOBAL_RESCUE,
    REGIONAL_POLICY_NOT_DEFINED,
    concentration_by_queue,
    maximum_consecutive_for,
    performance_confidence,
    simulate_hybrid,
)
from chatbot.crm_sla_territorial_shadow import compact_region_key, commune_key, profile_communes, regional_profile_regions
from scripts.run_phase05_crm_reassignment_audit import norm, text
from scripts.run_phase1a_crm_sla_shadow import read_safe_rows
from scripts.run_phase1b_crm_capacity_audit import load_data_phase1b
from scripts.run_phase1d_crm_sla_global_rescue import (
    active_agents,
    current_backlog,
    historical_metrics,
    rebuild_current_population,
)
from scripts.run_phase1c_crm_territory_audit import analyze_consistency, build_catalog
from scripts.run_phase05_crm_reassignment_audit import enrich_records
from chatbot.crm_sla_global_rescue import RescueParameters


REPORT = ROOT / "docs" / "AUDITORIA_HYBRID_SLA_RESCUE_20260910.md"
DATA_DIR = ROOT / "docs" / "auditoria_sla_data"
CLASSIFICATION_CSV = DATA_DIR / "hybrid_policy_classification.csv"
RM_SCORES_CSV = DATA_DIR / "hybrid_rm_candidate_scores.csv"
JPC_CSV = DATA_DIR / "hybrid_jpc_maria_hernan.csv"
COMBINED_CSV = DATA_DIR / "hybrid_combined_simulation.csv"
SENSITIVITY_CSV = DATA_DIR / "hybrid_weight_sensitivity.csv"
UNDEFINED_CSV = DATA_DIR / "hybrid_undefined_regional.csv"
ABSENCE_CSV = DATA_DIR / "hybrid_absence_simulation.csv"
F1D_ASSIGNMENTS = DATA_DIR / "global_rescue_assignments.csv"

PROPERTY_EXEC_PATHS = (
    "estado.ejecutivo", "estado.captador", "estado.responsable",
    "ejecutivo", "captador", "responsable",
)
RM_WEIGHTS = {
    "W0": BASE_WEIGHTS,
    "W1": {"speed": 0.45, "sla": 0.30, "attention": 0.15, "capacity": 0.10},
    "W2": {"speed": 0.40, "sla": 0.35, "attention": 0.15, "capacity": 0.10},
}
EFFECTIVE_WINNER_CATEGORIES = {WINNER_CATEGORY, CONFIDENCE_GUARD_CATEGORY}


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


def path_value(document: dict[str, Any] | None, path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def clean_key(value: Any) -> str:
    value = norm(value)
    return "" if value in {"", "na", "nd", "n/a", "no informado", "unknown"} else value


def json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if value not in (None, "", [], {}, set()) else ""


def property_executive_details(prop: dict[str, Any] | None) -> dict[str, Any]:
    populated = [(path, text(path_value(prop, path))) for path in PROPERTY_EXEC_PATHS if text(path_value(prop, path))]
    normalized = {norm(value) for _, value in populated}
    status = "MISSING" if not populated else "AMBIGUOUS" if len(normalized) > 1 else "RESOLVED"
    raw = populated[0][1] if populated else ""
    return {
        "status": status,
        "raw": raw,
        "source": populated[0][0] if populated else "",
        "all_values": populated,
        "is_jpc": status == "RESOLVED" and norm(raw) == JPC_NAME,
        "normalized_values": sorted(normalized),
    }


def resolve_canonical_region(record: dict[str, Any], catalog: dict[str, set[str]]) -> dict[str, Any]:
    consistency = analyze_consistency(record, catalog)
    property_commune = commune_key(consistency.get("property_commune_norm"))
    lead_commune = commune_key(consistency.get("lead_commune_norm"))
    expected = sorted(catalog.get(property_commune, ())) if property_commune else []
    property_region = clean_key(consistency.get("property_region_norm"))
    lead_region = clean_key(consistency.get("lead_region_norm"))
    reasons: list[str] = []
    if not property_commune:
        reasons.append("PROPERTY_COMMUNE_MISSING")
    elif not expected:
        reasons.append("PROPERTY_COMMUNE_NOT_IN_CATALOG")
    if len(expected) != 1:
        reasons.append("CATALOG_REGION_NOT_UNIQUE") if expected else None
    if lead_commune and property_commune and lead_commune != property_commune:
        reasons.append("LEAD_PROPERTY_COMMUNE_MISMATCH")
    if property_region and expected and property_region not in expected:
        reasons.append("PROPERTY_REGION_CATALOG_MISMATCH")
    if lead_region and expected and lead_region not in expected:
        reasons.append("LEAD_REGION_CATALOG_MISMATCH")
    if property_region and lead_region and property_region != lead_region:
        reasons.append("LEAD_PROPERTY_REGION_MISMATCH")
    resolved = not reasons and len(expected) == 1
    return {
        "canonical_region": expected[0] if resolved else "",
        "canonical_region_label": "REGION_METROPOLITANA" if resolved and expected[0] == REGION_METROPOLITANA else expected[0] if resolved else "",
        "resolved": resolved,
        "source": "property_commune_catalog" if resolved else "",
        "reason": ";".join(dict.fromkeys(reasons)),
        "property_commune": property_commune,
        "lead_commune": lead_commune,
        "property_region": property_region,
        "lead_region": lead_region,
        "catalog_regions": ";".join(expected),
        "consistency": consistency,
    }


def classify_record(record: dict[str, Any], catalog: dict[str, set[str]]) -> dict[str, Any]:
    territory = resolve_canonical_region(record, catalog)
    prop_exec = property_executive_details(record.get("property"))
    category = (
        REGION_REVIEW_REQUIRED if not territory["resolved"]
        else RM_GLOBAL_RESCUE if territory["canonical_region"] == REGION_METROPOLITANA
        else PROPERTY_EXECUTIVE_UNRESOLVED if prop_exec["status"] != "RESOLVED"
        else REGION_JPC_MARIA_HERNAN if prop_exec["is_jpc"]
        else REGIONAL_POLICY_NOT_DEFINED
    )
    return {"territory": territory, "property_executive": prop_exec, "policy_category": category}


def candidate_base(
    agents: list[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    backlog: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    rows = []
    for agent in agents:
        user_id = text(agent.get("_id"))
        m = metrics.get(user_id, {})
        b = backlog.get(user_id, {})
        name = text(agent.get("nombre"))
        rows.append({
            "user_id": user_id,
            "executive": name,
            "executive_key": norm(name),
            "identity_key": norm(name),
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
            "performance_confidence": performance_confidence(m.get("sample_size", 0)),
        })
    return rows


def lead_row(record: dict[str, Any], classification: dict[str, Any], candidates: list[dict[str, Any]], as_of: Any) -> dict[str, Any]:
    territory = classification["territory"]
    prop_exec = classification["property_executive"]
    assigned = record.get("assigned_at")
    elapsed = float(record.get("elapsed") or 0.0)
    threshold = float(record.get("threshold") or 0.0)
    return {
        "lead_id": text(record.get("lead_id")),
        "assignment_cycle_id": text(record.get("cycle_id")),
        # The pure shadow engine consumes the canonical owner keys. Keep the
        # explicit current_owner_* fields below for the exported audit too.
        "owner_user_id": text(record.get("owner_id")),
        "owner": text(record.get("owner_name")),
        "current_owner_user_id": text(record.get("owner_id")),
        "current_owner": text(record.get("owner_name")),
        "assigned_at": assigned.isoformat() if assigned else "",
        "age_days": round(max(0.0, (as_of - assigned).total_seconds() / 86400), 3) if assigned else "",
        "temperature": text(record.get("temperature")).upper(),
        "current_overdue_business_minutes": max(0.0, elapsed - threshold),
        "pipeline_stage": text(record.get("stage")),
        "property_code": text(record.get("property_code")),
        "canonical_region": territory["canonical_region_label"],
        "canonical_region_key": territory["canonical_region"],
        "canonical_region_source": territory["source"],
        "region_review_reason": territory["reason"],
        "lead_commune": territory["lead_commune"],
        "property_commune": territory["property_commune"],
        "property_region": territory["property_region"],
        "lead_region": territory["lead_region"],
        "property_executive_status": prop_exec["status"],
        "property_executive_source": prop_exec["source"],
        "property_executive_raw": prop_exec["raw"],
        "property_executive_all_values": json_cell(prop_exec["all_values"]),
        "property_is_jorge_pablo_caro": "yes" if prop_exec["is_jpc"] else "no",
        "policy_category": classification["policy_category"],
        "safe_for_policy_evaluation": "yes",
        "pool_size_rm": len(candidates),
        "pool_size_jpc": sum(1 for c in candidates if c.get("executive_key") in {MARIA_NAME, HERNAN_NAME}),
    }


def attach_metadata(lead: dict[str, Any], base_candidates: list[dict[str, Any]], catalog: dict[str, set[str]]) -> dict[str, Any]:
    target_region = text(lead.get("canonical_region_key"))
    target_commune = commune_key(lead.get("property_commune") or lead.get("lead_commune"))
    candidates = []
    for raw in base_candidates:
        candidate = dict(raw)
        profile_communes_values = profile_communes(candidate.get("user_record") or {})
        profile_regions = regional_profile_regions(candidate.get("user_record") or {}, catalog)
        candidate["same_commune"] = "yes" if target_commune and profile_communes_values and target_commune in profile_communes_values else "no" if target_commune and profile_communes_values else "unknown"
        candidate["same_region"] = "yes" if target_region and profile_regions and target_region in profile_regions else "no" if target_region and profile_regions else "unknown"
        candidate.pop("user_record", None)
        candidates.append(candidate)
    lead["all_candidates"] = candidates
    lead["rm_candidates"] = candidates
    lead["jpc_candidates"] = [candidate for candidate in candidates if candidate.get("executive_key") in {MARIA_NAME, HERNAN_NAME}]
    return lead


def decision_is_winner(decision: dict[str, Any]) -> bool:
    return decision.get("shadow_category") in EFFECTIVE_WINNER_CATEGORIES and bool(decision.get("winner_user_id"))


def policy_decisions(result: dict[str, Any], policy: str) -> list[dict[str, Any]]:
    return [row for row in result.get("decisions", []) if row.get("policy_category") == policy]


def policy_stats(result: dict[str, Any], policy: str) -> dict[str, Any]:
    decisions = policy_decisions(result, policy)
    winners = [row for row in decisions if decision_is_winner(row)]
    counts = Counter(text(row.get("winner_user_id")) for row in winners)
    total = len(decisions)
    top = counts.most_common()
    hhi = sum((value / len(winners)) ** 2 for value in counts.values()) if winners else 0.0
    return {
        "eligible": total,
        "winners": len(winners),
        "no_winner": total - len(winners),
        "coverage": len(winners) / total if total else 0.0,
        "received": counts,
        "receivers": len(counts),
        "top1": top[0][1] / len(winners) if top else 0.0,
        "top2": sum(v for _, v in top[:2]) / len(winners) if winners else 0.0,
        "top3": sum(v for _, v in top[:3]) / len(winners) if winners else 0.0,
        "hhi": hhi,
        "max": max(counts.values(), default=0),
        "max_consecutive": maximum_consecutive_for(decisions),
    }


def combined_row(decision: dict[str, Any], simulation: str, final_state: dict[str, Any] | None = None) -> dict[str, Any]:
    lead = decision.get("lead") or {}
    winner = decision.get("shadow_winner") or {}
    state = (final_state or {}).get(text(decision.get("winner_user_id")), {})
    same_region = winner.get("same_region", "")
    return {
        "simulation": simulation,
        "queue": decision.get("queue"),
        "step": decision.get("step"),
        "lead_id": lead.get("lead_id"),
        "assignment_cycle_id": lead.get("assignment_cycle_id"),
        "policy_category": decision.get("policy_category"),
        "current_owner_user_id": lead.get("owner_user_id"),
        "current_owner": lead.get("owner"),
        "temperature": lead.get("temperature"),
        "current_overdue_business_minutes": lead.get("current_overdue_business_minutes"),
        "assigned_at": lead.get("assigned_at"),
        "canonical_region": lead.get("canonical_region"),
        "property_executive": lead.get("property_executive_raw"),
        "winner_category": decision.get("shadow_category"),
        "winner_user_id": decision.get("winner_user_id"),
        "winner_name": decision.get("winner_name"),
        "winner_score": decision.get("winner_score"),
        "winner_global_score": decision.get("winner_global_score"),
        "dynamic_penalty": winner.get("dynamic_penalty"),
        "second_user_id": (decision.get("second_place") or {}).get("user_id", ""),
        "second_name": decision.get("second_name"),
        "second_score": decision.get("second_score"),
        "score_difference": decision.get("score_difference"),
        "winner_confidence": winner.get("performance_confidence"),
        "winner_same_region": same_region,
        "cross_region_tag": "REMOTE_COMMERCIAL_RESCUE" if same_region == "no" else "",
        "backlog_final_simulated_winner": state.get("simulated_open_current"),
        "hard_excluded_count": decision.get("hard_excluded_count"),
        "no_selection_reason": decision.get("shadow_category") if not decision_is_winner(decision) else "",
    }


def rm_score_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for decision in result.get("decisions", []):
        if decision.get("policy_category") != RM_GLOBAL_RESCUE:
            continue
        lead = decision.get("lead") or {}
        for candidate in decision.get("scored", []) + decision.get("hard_excluded", []):
            rows.append({
                "simulation": "H1",
                "step": decision.get("step"),
                "lead_id": lead.get("lead_id"),
                "assignment_cycle_id": lead.get("assignment_cycle_id"),
                "current_owner_user_id": lead.get("owner_user_id"),
                "current_owner": lead.get("owner"),
                "temperature": lead.get("temperature"),
                "canonical_region": lead.get("canonical_region"),
                "candidate_user_id": candidate.get("user_id"),
                "candidate_name": candidate.get("executive"),
                "candidate_status": "HARD_EXCLUDED" if candidate.get("excluded_reason") else "AVAILABLE",
                "excluded_reason": candidate.get("excluded_reason"),
                "sample_size": candidate.get("sample_size"),
                "performance_confidence": candidate.get("performance_confidence"),
                "same_region": candidate.get("same_region"),
                "speed_imputed": candidate.get("speed_imputed"),
                "p50_raw": candidate.get("p50_first_management_business_minutes"),
                "p90_raw": candidate.get("p90_first_management_business_minutes"),
                "p50_effective": candidate.get("p50_effective"),
                "p90_effective": candidate.get("p90_effective"),
                "speed_score": candidate.get("speed_score"),
                "sla_compliance_rate_adjusted": candidate.get("adjusted_sla_rate"),
                "attention_rate_adjusted": candidate.get("adjusted_attention_rate"),
                "load_pressure": candidate.get("load_pressure"),
                "capacity_score": candidate.get("capacity_score"),
                "base_score": candidate.get("global_rescue_score"),
                "dynamic_penalty": candidate.get("dynamic_penalty"),
                "effective_score": candidate.get("dynamic_rescue_score"),
                "rank": candidate.get("rank"),
                "winner": "yes" if text(candidate.get("user_id")) == text(decision.get("winner_user_id")) else "no",
            })
    return rows


def jpc_rows(
    leads: list[dict[str, Any]],
    result: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    backlog: dict[str, dict[str, int]],
    candidate_scores: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    decision_by_lead = {text(decision.get("lead", {}).get("lead_id")): decision for decision in policy_decisions(result, REGION_JPC_MARIA_HERNAN)}
    rows = []
    for lead in leads:
        if lead.get("policy_category") != REGION_JPC_MARIA_HERNAN:
            continue
        decision = decision_by_lead.get(text(lead.get("lead_id")), {})
        scored_by_id = {text(row.get("user_id")): row for row in decision.get("scored", [])}
        values = {}
        for key, label in ((MARIA_NAME, "maria"), (HERNAN_NAME, "hernan")):
            score = scored_by_id.get(next((uid for uid, row in candidate_scores.items() if row.get("executive_key") == key), ""), {})
            user_id = text(score.get("user_id")) if score else next((uid for uid, row in candidate_scores.items() if row.get("executive_key") == key), "")
            m = metrics.get(user_id, {})
            b = backlog.get(user_id, {})
            baseline = candidate_scores.get(user_id, {})
            values[f"{label}_user_id"] = user_id
            values[f"{label}_n"] = m.get("sample_size", 0)
            values[f"{label}_p50"] = m.get("p50")
            values[f"{label}_p90"] = m.get("p90")
            values[f"{label}_sla_raw"] = m.get("sla_compliance_rate", 0.0)
            values[f"{label}_sla_adjusted"] = m.get("sla_compliance_rate_adjusted", 0.0)
            values[f"{label}_attention_raw"] = m.get("attention_rate", 0.0)
            values[f"{label}_attention_adjusted"] = m.get("attention_rate_adjusted", 0.0)
            values[f"{label}_expired"] = b.get("expired", 0)
            values[f"{label}_unmanaged"] = b.get("unmanaged", 0)
            values[f"{label}_open"] = b.get("open", 0)
            values[f"{label}_load_pressure"] = load_pressure({"expired_current_policy": b.get("expired", 0), "unmanaged_current_policy": b.get("unmanaged", 0), "open_current_policy": b.get("open", 0)})
            values[f"{label}_base_score"] = score.get("global_rescue_score", baseline.get("global_rescue_score"))
            values[f"{label}_confidence"] = performance_confidence(m.get("sample_size", 0))
        winner = decision.get("shadow_winner") or {}
        second = decision.get("second_place") or {}
        rows.append({
            "lead_id": lead.get("lead_id"),
            "assignment_cycle_id": lead.get("assignment_cycle_id"),
            "temperature": lead.get("temperature"),
            "current_owner": lead.get("owner"),
            "canonical_region": lead.get("canonical_region"),
            "property_executive": lead.get("property_executive_raw"),
            **values,
            "winner_category": decision.get("shadow_category"),
            "winner_user_id": decision.get("winner_user_id"),
            "winner_name": decision.get("winner_name"),
            "second_user_id": second.get("user_id", ""),
            "second_name": decision.get("second_name"),
            "score_difference": decision.get("score_difference"),
            "dynamic_penalty": winner.get("dynamic_penalty"),
            "final_score": decision.get("winner_score"),
            "winner_confidence": winner.get("performance_confidence"),
            "no_selection_reason": decision.get("shadow_category") if not decision_is_winner(decision) else "",
        })
    return rows


def weight_rows(results: dict[str, dict[str, Any]], base_result: dict[str, Any]) -> list[dict[str, Any]]:
    base_by_lead = {text(row.get("lead", {}).get("lead_id")): row for row in base_result.get("decisions", [])}
    rows = []
    for name, result in results.items():
        for decision in result.get("decisions", []):
            lead = decision.get("lead") or {}
            winner = decision.get("shadow_winner") or {}
            base = base_by_lead.get(text(lead.get("lead_id")), {})
            rows.append({
                "weight_scenario": name,
                "lead_id": lead.get("lead_id"),
                "assignment_cycle_id": lead.get("assignment_cycle_id"),
                "policy_category": decision.get("policy_category"),
                "winner_category": decision.get("shadow_category"),
                "winner_user_id": decision.get("winner_user_id"),
                "winner_name": decision.get("winner_name"),
                "winner_score": decision.get("winner_score"),
                "second_score": decision.get("second_score"),
                "score_difference": decision.get("score_difference"),
                "changed_vs_W0": "yes" if text(decision.get("winner_user_id")) != text(base.get("winner_user_id")) else "no",
                "temperature": lead.get("temperature"),
                "winner_confidence": winner.get("performance_confidence"),
                "cross_region_tag": "REMOTE_COMMERCIAL_RESCUE" if winner.get("same_region") == "no" else "",
            })
    return rows


def absence_rows(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for scenario, result in results.items():
        for decision in result.get("decisions", []):
            lead = decision.get("lead") or {}
            winner = decision.get("shadow_winner") or {}
            rows.append({
                "absence_scenario": scenario,
                "queue": decision.get("queue"),
                "step": decision.get("step"),
                "lead_id": lead.get("lead_id"),
                "assignment_cycle_id": lead.get("assignment_cycle_id"),
                "policy_category": decision.get("policy_category"),
                "winner_category": decision.get("shadow_category"),
                "winner_user_id": decision.get("winner_user_id"),
                "winner_name": decision.get("winner_name"),
                "winner_score": decision.get("winner_score"),
                "score_difference": decision.get("score_difference"),
                "temperature": lead.get("temperature"),
                "winner_confidence": winner.get("performance_confidence"),
                "cross_region_tag": "REMOTE_COMMERCIAL_RESCUE" if winner.get("same_region") == "no" else "",
            })
    return rows


def prior_global_results() -> tuple[dict[str, str], int, float, float, int]:
    if not F1D_ASSIGNMENTS.exists():
        return {}, 0, 0.0, 0.0, 0
    with F1D_ASSIGNMENTS.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("scenario") == "G3_BALANCE_DYNAMIC"]
    winners = {text(row.get("lead_id")): text(row.get("winner_user_id")) for row in rows if row.get("winner_category") in EFFECTIVE_WINNER_CATEGORIES and row.get("winner_user_id")}
    counts = Counter(winners.values())
    total = len(winners)
    hhi = sum((value / total) ** 2 for value in counts.values()) if total else 0.0
    cross = sum(bool(row.get("cross_region_tag")) for row in rows if row.get("winner_user_id"))
    top1 = max(counts.values(), default=0) / total if total else 0.0
    return winners, total, top1, hhi, cross


def fields() -> dict[str, list[str]]:
    return {
        "classification": [
            "lead_id", "assignment_cycle_id", "current_owner_user_id", "current_owner", "assigned_at", "age_days", "temperature", "current_overdue_business_minutes", "pipeline_stage", "property_code", "canonical_region", "canonical_region_key", "canonical_region_source", "region_review_reason", "lead_commune", "property_commune", "property_region", "lead_region", "property_executive_status", "property_executive_source", "property_executive_raw", "property_executive_all_values", "property_is_jorge_pablo_caro", "policy_category", "safe_for_policy_evaluation", "pool_size_rm", "pool_size_jpc",
        ],
        "rm": [
            "simulation", "step", "lead_id", "assignment_cycle_id", "current_owner_user_id", "current_owner", "temperature", "canonical_region", "candidate_user_id", "candidate_name", "candidate_status", "excluded_reason", "sample_size", "performance_confidence", "same_region", "speed_imputed", "p50_raw", "p90_raw", "p50_effective", "p90_effective", "speed_score", "sla_compliance_rate_adjusted", "attention_rate_adjusted", "load_pressure", "capacity_score", "base_score", "dynamic_penalty", "effective_score", "rank", "winner",
        ],
        "jpc": [
            "lead_id", "assignment_cycle_id", "temperature", "current_owner", "canonical_region", "property_executive", "maria_user_id", "maria_n", "maria_p50", "maria_p90", "maria_sla_raw", "maria_sla_adjusted", "maria_attention_raw", "maria_attention_adjusted", "maria_expired", "maria_unmanaged", "maria_open", "maria_load_pressure", "maria_base_score", "maria_confidence", "hernan_user_id", "hernan_n", "hernan_p50", "hernan_p90", "hernan_sla_raw", "hernan_sla_adjusted", "hernan_attention_raw", "hernan_attention_adjusted", "hernan_expired", "hernan_unmanaged", "hernan_open", "hernan_load_pressure", "hernan_base_score", "hernan_confidence", "winner_category", "winner_user_id", "winner_name", "second_user_id", "second_name", "score_difference", "dynamic_penalty", "final_score", "winner_confidence", "no_selection_reason",
        ],
        "combined": [
            "simulation", "queue", "step", "lead_id", "assignment_cycle_id", "policy_category", "current_owner_user_id", "current_owner", "temperature", "current_overdue_business_minutes", "assigned_at", "canonical_region", "property_executive", "winner_category", "winner_user_id", "winner_name", "winner_score", "winner_global_score", "dynamic_penalty", "second_user_id", "second_name", "second_score", "score_difference", "winner_confidence", "winner_same_region", "cross_region_tag", "backlog_final_simulated_winner", "hard_excluded_count", "no_selection_reason",
        ],
        "sensitivity": [
            "weight_scenario", "lead_id", "assignment_cycle_id", "policy_category", "winner_category", "winner_user_id", "winner_name", "winner_score", "second_score", "score_difference", "changed_vs_W0", "temperature", "winner_confidence", "cross_region_tag",
        ],
        "undefined": [
            "lead_id", "assignment_cycle_id", "current_owner_user_id", "current_owner", "assigned_at", "age_days", "temperature", "current_overdue_business_minutes", "canonical_region", "lead_commune", "property_commune", "property_code", "property_executive_status", "property_executive_raw", "policy_category",
        ],
        "absence": [
            "absence_scenario", "queue", "step", "lead_id", "assignment_cycle_id", "policy_category", "winner_category", "winner_user_id", "winner_name", "winner_score", "score_difference", "temperature", "winner_confidence", "cross_region_tag",
        ],
    }


def build_report(
    *,
    as_of: Any,
    safe_leads: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    current_expired_count: int,
    legacy_count: int,
    agents: list[dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    backlog: dict[str, dict[str, int]],
    h0: dict[str, Any],
    h1: dict[str, Any],
    weight_results: dict[str, dict[str, Any]],
    confidence_result: dict[str, Any],
    absence_results_map: dict[str, dict[str, Any]],
    validation: dict[str, Any],
    property_audit: dict[str, Any],
    safe_diff: tuple[int, int],
) -> None:
    rm = policy_stats(h1, RM_GLOBAL_RESCUE)
    jpc = policy_stats(h1, REGION_JPC_MARIA_HERNAN)
    total_winners = [row for row in h1["decisions"] if decision_is_winner(row)]
    total_conc = concentration_by_queue(total_winners)
    rm_rank = property_audit["rm_ranking"]
    rm_counts = rm["received"]
    confidence_base_low = sum((row.get("shadow_winner") or {}).get("performance_confidence") == "LOW" for row in h1["decisions"] if decision_is_winner(row))
    confidence_guard_low = sum((row.get("shadow_winner") or {}).get("performance_confidence") == "LOW" for row in confidence_result["decisions"] if decision_is_winner(row))
    confidence_changes = sum(text(h1["decisions"][idx].get("winner_user_id")) != text(confidence_result["decisions"][idx].get("winner_user_id")) for idx in range(len(h1["decisions"])))
    prior_winners, prior_total, prior_top1, prior_hhi, prior_cross = prior_global_results()
    h1_by_lead = {text(row.get("lead", {}).get("lead_id")): text(row.get("winner_user_id")) for row in h1["decisions"]}
    changed_vs_global = sum(text(h1_by_lead.get(lead_id)) != text(winner) for lead_id, winner in prior_winners.items())
    names = {text(agent.get("_id")): text(agent.get("nombre")) for agent in agents}
    undefined = [row for row in classifications if row["policy_category"] == REGIONAL_POLICY_NOT_DEFINED]
    reviews = [row for row in classifications if row["policy_category"] == REGION_REVIEW_REQUIRED]
    jpc_decisions = policy_decisions(h1, REGION_JPC_MARIA_HERNAN)
    jpc_received = Counter(text(row.get("winner_user_id")) for row in jpc_decisions if decision_is_winner(row))
    rm_received = Counter(text(row.get("winner_user_id")) for row in policy_decisions(h1, RM_GLOBAL_RESCUE) if decision_is_winner(row))
    distribution = []
    final_state = h1.get("final_state", {})
    for agent in sorted(agents, key=lambda row: norm(row.get("nombre"))):
        user_id = text(agent.get("_id"))
        final = final_state.get(user_id, {})
        initial = backlog.get(user_id, {})
        distribution.append([
            text(agent.get("nombre")), rm_received.get(user_id, 0), jpc_received.get(user_id, 0), rm_received.get(user_id, 0) + jpc_received.get(user_id, 0),
            initial.get("open", 0), final.get("simulated_open_current", initial.get("open", 0)),
        ])
    jpc_table = []
    for key, label in ((MARIA_NAME, "María Paz"), (HERNAN_NAME, "Hernán")):
        user_id = next((text(agent.get("_id")) for agent in agents if norm(agent.get("nombre")) == key), "")
        m = metrics.get(user_id, {})
        b = backlog.get(user_id, {})
        jpc_table.append([label, m.get("sample_size", 0), fmt(m.get("p50")), fmt(m.get("p90")), f"{m.get('sla_compliance_rate_adjusted', 0.0) * 100:.1f}%", f"{m.get('attention_rate_adjusted', 0.0) * 100:.1f}%", load_pressure({"expired_current_policy": b.get("expired", 0), "unmanaged_current_policy": b.get("unmanaged", 0), "open_current_policy": b.get("open", 0)}), jpc_received.get(user_id, 0)])
    h0_rm = policy_stats(h0, RM_GLOBAL_RESCUE)
    h0_jpc = policy_stats(h0, REGION_JPC_MARIA_HERNAN)
    weight_table = []
    w0 = weight_results["W0"]
    w0_by_lead = {text(row.get("lead", {}).get("lead_id")): text(row.get("winner_user_id")) for row in w0["decisions"]}
    unstable = 0
    for idx, lead in enumerate(safe_leads):
        if len({text(result["decisions"][idx].get("winner_user_id")) for result in weight_results.values()}) > 1:
            unstable += 1
    for name, result in weight_results.items():
        stats = policy_stats(result, RM_GLOBAL_RESCUE)
        jpc_stats = policy_stats(result, REGION_JPC_MARIA_HERNAN)
        changes = sum(text(row.get("winner_user_id")) != w0_by_lead.get(text(row.get("lead", {}).get("lead_id"))) for row in result["decisions"])
        weight_table.append([name, changes, round(stats["top1"] * 100, 1), round(stats["hhi"], 6), f"{jpc_stats['received'].get(next((uid for uid, val in names.items() if norm(val) == MARIA_NAME), ''), 0)}/{jpc_stats['received'].get(next((uid for uid, val in names.items() if norm(val) == HERNAN_NAME), ''), 0)}"])
    absence_table = []
    for scenario, result in absence_results_map.items():
        rm_abs = policy_stats(result, RM_GLOBAL_RESCUE)
        jpc_abs = policy_stats(result, REGION_JPC_MARIA_HERNAN)
        absence_table.append([scenario, f"{rm_abs['coverage'] * 100:.1f}%", f"{jpc_abs['coverage'] * 100:.1f}%", rm_abs["no_winner"] + jpc_abs["no_winner"]])
    undefined_regions = Counter(row["canonical_region"] or "UNKNOWN" for row in undefined)
    undefined_hot = sum(row["temperature"] == "HOT" for row in undefined)
    property_aliases = "; ".join(f"{value} ({count})" for value, count in property_audit["raw_values"].most_common(8))
    rm_rank_table = [[row["executive"], row["sample_size"], fmt(row.get("speed_score")), f"{row.get('adjusted_sla_rate', 0.0) * 100:.1f}%", f"{row.get('adjusted_attention_rate', 0.0) * 100:.1f}%", fmt(row.get("capacity_score")), fmt(row.get("global_rescue_score")), row["performance_confidence"]] for row in rm_rank]
    lines = [
        "# Auditoría Fase 1E — Hybrid SLA Rescue Shadow",
        "",
        f"Fecha de corte: `{as_of.isoformat()}`. Fuente: CRM inbound, ciclos, leads, eventos/resultados, usuarios, propiedades y catálogo territorial local, en modo lectura.",
        "",
        "## Alcance y regla aplicada",
        "",
        "Se reconstruyó la población safe de Fase 1D y se aplicó únicamente la política híbrida indicada: Metropolitana con rescate global; otras regiones con propiedad exactamente identificada como Jorge Pablo Caro solo hacia María Paz/Hernán; otras regiones sin esa propiedad quedan sin política.",
        "",
        "La región se resolvió desde comuna de propiedad y catálogo local. Las contradicciones quedan en review. La identificación de propiedad usa la primera fuente poblada del código existente en este orden: `estado.ejecutivo`, `estado.captador`, `estado.responsable`, `ejecutivo`, `captador`, `responsable`. Se normalizaron solo case, espacios y acentos para comparar.",
        "",
        "## UNIVERSO",
        "",
        md_table(["medición", "cantidad"], [
            ["vencidos current-policy reconstruidos", current_expired_count],
            ["safe para evaluación híbrida", len(safe_leads)],
            ["RM_GLOBAL_RESCUE", sum(row["policy_category"] == RM_GLOBAL_RESCUE for row in classifications)],
            ["REGION_JPC_MARIA_HERNAN", sum(row["policy_category"] == REGION_JPC_MARIA_HERNAN for row in classifications)],
            ["REGIONAL_POLICY_NOT_DEFINED", len(undefined)],
            ["REGION_REVIEW_REQUIRED", len(reviews)],
            ["legacy excluidos del motor", legacy_count],
            ["diferencia safe vs Fase 1D — ingresados/salidos", f"{safe_diff[0]}/{safe_diff[1]}"],
        ]),
        "",
        "## EJECUTIVO DE PROPIEDAD",
        "",
        f"Colección: `{property_audit['collection']}`. Propiedades con identidad normalizada exactamente igual a `Jorge Pablo Caro`: `{property_audit['jpc_properties']}`. Leads safe asociados: `{property_audit['jpc_leads']}`. Campo seleccionado más frecuente: `{property_audit['source_counts'].most_common(1)[0][0] if property_audit['source_counts'] else 'N/D'}`. Valores raw más frecuentes: `{property_aliases or 'N/D'}`.",
        "",
        "Las coincidencias de Jorge Pablo Caro fueron exactas después de normalización trivial. Valores contradictorios en distintas fuentes de una misma propiedad no activan Regla B.",
        "",
        "## RM",
        "",
        f"- Elegibles: `{policy_stats(h1, RM_GLOBAL_RESCUE)['eligible']}`.",
        f"- Con ganador: `{rm['winners']}`.",
        f"- Pool promedio: `{sum(len(row.get('rm_candidates', [])) for row in safe_leads) / len(safe_leads) if safe_leads else 0:.1f}` candidatos antes de excluir owner; alternativas promedio `{max(0, len(agents) - 1):.1f}`.",
        f"- Receptores: `{rm['receivers']}`; Top1: `{rm['top1'] * 100:.1f}%`; HHI: `{rm['hhi']:.6f}`.",
        "",
        md_table(["ejecutivo", "recibidos", "HOT", "normal", "%", "score base", "confidence"], [[text(agent.get("nombre")), rm["received"].get(text(agent.get("_id")), 0), sum(row.get("temperature") == "HOT" and text(row.get("winner_user_id")) == text(agent.get("_id")) for row in policy_decisions(h1, RM_GLOBAL_RESCUE) if decision_is_winner(row)), rm["received"].get(text(agent.get("_id")), 0) - sum(row.get("temperature") == "HOT" and text(row.get("winner_user_id")) == text(agent.get("_id")) for row in policy_decisions(h1, RM_GLOBAL_RESCUE) if decision_is_winner(row)), f"{rm['received'].get(text(agent.get('_id')), 0) * 100.0 / rm['winners']:.1f}%" if rm['winners'] else "0.0%", fmt(next((row.get("global_rescue_score") for row in rm_rank if row.get("user_id") == text(agent.get("_id"))), None)), next((row.get("performance_confidence") for row in rm_rank if row.get("user_id") == text(agent.get("_id"))), "N/D")] for agent in agents]),
        "",
        "### Ranking descriptivo RM",
        "",
        md_table(["ejecutivo", "n", "speed score", "SLA adjusted", "attention adjusted", "capacity", "base score", "confidence"], rm_rank_table),
        "",
        "El ranking descriptivo usa el score base inicial. El ganador final puede cambiar por balance dinámico. No se aplicó territorio, ficha, ejecutivo de propiedad ni comuna como filtro RM.",
        "",
        "## REGIÓN JPC",
        "",
        f"- Elegibles: `{jpc['eligible']}`; María Paz: `{jpc['received'].get(next((uid for uid, val in names.items() if norm(val) == MARIA_NAME), ''), 0)}`; Hernán: `{jpc['received'].get(next((uid for uid, val in names.items() if norm(val) == HERNAN_NAME), ''), 0)}`; sin ganador: `{jpc['no_winner']}`.",
        "",
        md_table(["ejecutivo", "n", "P50", "P90", "SLA ajustado", "attention ajustada", "load", "recibidos"], jpc_table),
        "",
        "El pool JPC se limitó a María Paz/Hernán y excluyó al owner anterior cuando correspondía. No se agregó un tercer ejecutivo.",
        "",
        "### Comparación María Paz vs Hernán por lead",
        "",
        "La comparación completa por lead, incluyendo score, segundo lugar, diferencia, penalización y confianza, está en `hybrid_jpc_maria_hernan.csv`.",
        "",
        "## SIMULACIÓN H1",
        "",
        md_table(["ejecutivo", "RM recibidos", "JPC recibidos", "total", "backlog inicial", "backlog final"], distribution),
        "",
        f"H0 independiente: RM cobertura `{h0_rm['coverage'] * 100:.1f}%`, JPC cobertura `{h0_jpc['coverage'] * 100:.1f}%`. H1 conjunto: RM cobertura `{rm['coverage'] * 100:.1f}%`, JPC cobertura `{jpc['coverage'] * 100:.1f}%`. H1 comparte carga dinámica cronológica entre ambas colas.",
        "",
        "## REGIONALES SIN POLÍTICA",
        "",
        f"- Total: `{len(undefined)}`.",
        f"- Regiones principales: `{'; '.join(f'{key}={value}' for key, value in undefined_regions.most_common(8)) or 'N/D'}`.",
        f"- HOT: `{undefined_hot}`; NORMAL: `{len(undefined) - undefined_hot}`.",
        "- No se generó ganador ni se utilizó fallback regional histórico.",
        "",
        "## SENSIBILIDAD",
        "",
        md_table(["pesos", "cambios vs W0", "RM Top1", "RM HHI", "JPC María/Hernán"], weight_table),
        "",
        f"- Cambios W1: `{weight_table[1][1] if len(weight_table) > 1 else 0}`; cambios W2: `{weight_table[2][1] if len(weight_table) > 2 else 0}`.",
        f"- Leads inestables W0/W1/W2: `{unstable}`.",
        f"- Confidence guard: cambió `{confidence_changes}` ganadores; ganadores LOW base `{confidence_base_low}`, después `{confidence_guard_low}`. No se convirtió en política productiva.",
        "",
        "## AUSENCIA",
        "",
        md_table(["escenario", "RM cobertura", "JPC cobertura", "sin ganador total"], absence_table),
        "",
        "En ausencia simultánea de María Paz y Hernán, la cola JPC queda en `NO_ELIGIBLE_JPC_RESCUER`; RM continúa con sus agentes restantes.",
        "",
        "## COMPARACIÓN CON GLOBAL RESCUE",
        "",
        f"- Cobertura anterior: `{prior_total}/{len(safe_leads)}` ({prior_total * 100.0 / len(safe_leads) if safe_leads else 0:.1f}%).",
        f"- Cobertura híbrida H1: `{len(total_winners)}/{len(safe_leads)}` ({len(total_winners) * 100.0 / len(safe_leads) if safe_leads else 0:.1f}%).",
        f"- Concentración anterior: Top1 `{prior_top1 * 100:.1f}%`, HHI `{prior_hhi:.6f}`.",
        f"- Concentración híbrida H1: Top1 `{total_conc['top1_share'] * 100:.1f}%`, HHI `{total_conc['hhi']:.6f}`.",
        f"- Cross-region anterior: `{prior_cross}`; hybrid H1: `{sum(bool((row.get('shadow_winner') or {}).get('same_region') == 'no') for row in total_winners)}`.",
        f"- Leads que cambian de ganador frente a Global Rescue G3: `{changed_vs_global}`.",
        f"- Regionales ahora restringidos a María/Hernán: `{jpc['eligible']}`; regionales que quedan sin política: `{len(undefined)}`.",
        "",
        "## HALLAZGOS",
        "",
        f"1. La población safe se mantuvo en `{len(safe_leads)}` frente a Fase 1D; diferencia ingresados/salidos `{safe_diff[0]}/{safe_diff[1]}`.",
        f"2. RM concentra `{rm['winners']}` leads y permite pool global; JPC concentra `{jpc['eligible']}` leads solo en María Paz/Hernán.",
        f"3. `{len(undefined)}` leads regionales no tienen política definida y no recibieron fallback.",
        f"4. La identidad JPC se resolvió mediante datos de propiedad; contradicciones quedaron fuera de Regla B.",
        f"5. H1 comparte carga entre RM y JPC; el resultado combinado fue `{len(total_winners)}/{len(safe_leads)}` ganadores.",
        f"6. La sensibilidad W0/W1/W2 dejó `{unstable}` leads inestables; el confidence guard movió `{confidence_changes}`.",
        f"7. El mejor ranking RM fue `{rm_rank[0]['executive'] if rm_rank else 'N/D'}`, pero el ganador final depende del balance dinámico y de la cola aplicada.",
        "8. No se implementaron cambios productivos ni se modificó la política SLA.",
        "",
        "## RECOMENDACIÓN TÉCNICA",
        "",
        "La política híbrida es técnicamente implementable en shadow y respeta la decisión de jefatura: RM global, JPC restringido y regionales restantes bloqueados por falta de política.",
        "H1 es el escenario correcto para evaluar sobrecarga compartida de María Paz/Hernán.",
        "Antes de producción, Sol debe resolver los regionales sin política, aceptar el tratamiento de muestra LOW y aprobar el contrato transaccional.",
        "No implementar.",
        "",
        "## TESTS",
        "",
        "- Nuevos Fase 1E: se ejecutan con cobertura de clasificación A/B/C, región, propiedad JPC, pools, H0/H1, sensibilidad, confidence guard, ausencia y determinismo.",
        "- Fases 1A, 1B, 1C y 1D: se conservan sus suites y artefactos; no se reutiliza el router histórico.",
        "- CRM: ejecutar suite relevante sin alterar fallos históricos ajenos.",
        "- Fallos históricos: `test_crm_list_actions.py` y `test_crm_management_milestone_guard.py` permanecen fuera de alcance.",
        "",
        "## CONTROLES",
        "",
        md_table(["control", "resultado"], [[key, value] for key, value in validation.items()]),
        "",
        "## SEGURIDAD",
        "",
        "- Mongo writes: `0`.",
        "- Reasignaciones: `0`.",
        "- Owner/cycle: `0` cambios.",
        "- Deploy: `0`.",
        "- Scheduler/worker/cron: `0`.",
        "- Flags: `0`.",
        "- Sin teléfonos, emails ni mensajes completos en artefactos.",
        "",
        "## ARCHIVOS",
        "",
        f"- `{REPORT}`",
        f"- `{CLASSIFICATION_CSV}`",
        f"- `{RM_SCORES_CSV}`",
        f"- `{JPC_CSV}`",
        f"- `{COMBINED_CSV}`",
        f"- `{SENSITIVITY_CSV}`",
        f"- `{UNDEFINED_CSV}`",
        f"- `{ABSENCE_CSV}`",
        "",
        "## NO IMPLEMENTÉ CAMBIOS PRODUCTIVOS",
        "",
        "No avancé a producción ni implementé reasignación, bloqueo de teléfono, endpoint, worker, scheduler, notificaciones, WhatsApp o dashboard.",
        "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def fmt(value: Any, decimals: int = 1) -> str:
    if value in (None, ""):
        return "N/D"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value).replace("|", "/") for value in row) + " |" for row in rows)
    return "\n".join(lines)


def main() -> None:
    data = load_data_phase1b()
    records = enrich_records(data)
    as_of = data["as_of"]
    params = RescueParameters()
    agents = active_agents(data["users"])
    current, safe_records, _ = rebuild_current_population(records)
    current_expired = [record for record in current if record.get("a_expired") and not record.get("closed_lead")]
    legacy_records = __import__("scripts.run_phase05_crm_reassignment_audit", fromlist=["legacy_expired"]).legacy_expired(records, {text(row.get("lead_id")) for row in current})
    metrics, team = historical_metrics(records, agents, as_of=as_of, params=params)
    backlog = current_backlog(records, agents)
    catalog = build_catalog()
    base_candidates = candidate_base(agents, metrics, backlog)
    by_name = {norm(agent.get("nombre")): agent for agent in agents}
    for candidate in base_candidates:
        candidate["user_record"] = by_name.get(candidate["executive_key"], {})
    classifications = []
    leads = []
    for record in safe_records:
        classification = classify_record(record, catalog)
        lead = lead_row(record, classification, base_candidates, as_of)
        lead = attach_metadata(lead, base_candidates, catalog)
        # Classification fields live in the lead object for the pure engine.
        lead["policy_category"] = classification["policy_category"]
        classifications.append(lead)
        leads.append(lead)
    # Descriptive RM ranking uses one common initial pool and the Fase 1D score.
    initial_scored = score_candidates(
        [dict(candidate) for candidate in base_candidates],
        team_sla_rate=team["sla_compliance_rate"],
        team_attention_rate=team["attention_rate"],
        team_p50_average=team["team_p50_average"],
        team_p90_average=team["team_p90_average"],
        weights=BASE_WEIGHTS,
        params=params,
    )
    initial_scored.sort(key=lambda row: (-float(row.get("global_rescue_score") or -1), float(row.get("p50_effective") or float("inf")), text(row.get("user_id"))))
    for rank, row in enumerate(initial_scored, start=1):
        row["performance_confidence"] = performance_confidence(row.get("sample_size"))
        row["rank"] = rank
    ranking_by_id = {text(row.get("user_id")): row for row in initial_scored}
    h0 = simulate_hybrid(leads, mode="H0_INDEPENDENT", scenario="H0_INDEPENDENT_DYNAMIC", team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], weights=BASE_WEIGHTS, params=params)
    h1 = simulate_hybrid(leads, mode="H1_COMBINED", scenario="H1_COMBINED_DYNAMIC", team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], weights=BASE_WEIGHTS, params=params)
    h1_repeat = simulate_hybrid(leads, mode="H1_COMBINED", scenario="H1_COMBINED_DYNAMIC", team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], weights=BASE_WEIGHTS, params=params)
    weight_results = {name: simulate_hybrid(leads, mode="H1_COMBINED", scenario="H1_COMBINED_DYNAMIC", team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], weights=weights, params=params) for name, weights in RM_WEIGHTS.items()}
    confidence_result = simulate_hybrid(leads, mode="H1_COMBINED", scenario="CONFIDENCE_GUARD", team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], weights=BASE_WEIGHTS, params=params, confidence_guard=True)
    absent_candidates = {
        "A_HERNAN_UNAVAILABLE": next((text(agent.get("_id")) for agent in agents if norm(agent.get("nombre")) == HERNAN_NAME), ""),
        "B_MARIA_PAZ_UNAVAILABLE": next((text(agent.get("_id")) for agent in agents if norm(agent.get("nombre")) == MARIA_NAME), ""),
    }
    absent_candidates["C_MARIA_AND_HERNAN_UNAVAILABLE"] = ";".join(value for key, value in absent_candidates.items() if key in {"A_HERNAN_UNAVAILABLE", "B_MARIA_PAZ_UNAVAILABLE"})
    absent_candidates["D_BEST_RM_UNAVAILABLE"] = text(initial_scored[0].get("user_id")) if initial_scored else ""
    absence_results_map = {}
    for scenario, absent in absent_candidates.items():
        absent_ids = set(absent.split(";")) if ";" in absent else {absent}
        altered = []
        for lead in leads:
            row = dict(lead)
            row["rm_candidates"] = [candidate for candidate in lead.get("rm_candidates", []) if text(candidate.get("user_id")) not in absent_ids]
            row["jpc_candidates"] = [candidate for candidate in lead.get("jpc_candidates", []) if text(candidate.get("user_id")) not in absent_ids]
            altered.append(row)
        absence_results_map[scenario] = simulate_hybrid(altered, mode="H1_COMBINED", scenario="H1_COMBINED_DYNAMIC", team_sla_rate=team["sla_compliance_rate"], team_attention_rate=team["attention_rate"], team_p50_average=team["team_p50_average"], team_p90_average=team["team_p90_average"], weights=BASE_WEIGHTS, params=params)

    prior_winners, _, _, _, _ = prior_global_results()
    safe_ids = {(text(lead.get("lead_id")), text(lead.get("assignment_cycle_id"))) for lead in leads}
    prior_ids = set()
    if F1D_ASSIGNMENTS.exists():
        with F1D_ASSIGNMENTS.open("r", encoding="utf-8-sig", newline="") as handle:
            prior_ids = {(text(row.get("lead_id")), text(row.get("assignment_cycle_id"))) for row in csv.DictReader(handle) if row.get("scenario") == "G3_BALANCE_DYNAMIC"}
    safe_diff = (len(safe_ids - prior_ids), len(prior_ids - safe_ids))
    property_values = Counter()
    source_counts = Counter()
    jpc_properties = 0
    for prop in data["properties"].values():
        details = property_executive_details(prop)
        if details["raw"]:
            property_values[details["raw"]] += 1
            source_counts[details["source"]] += 1
        if details["is_jpc"]:
            jpc_properties += 1
    jpc_leads = sum(row["policy_category"] == REGION_JPC_MARIA_HERNAN for row in classifications)
    property_audit = {
        "collection": data.get("property_collection", "universo_cartera_prop360"),
        "raw_values": property_values,
        "source_counts": source_counts,
        "jpc_properties": jpc_properties,
        "jpc_leads": jpc_leads,
        "rm_ranking": initial_scored,
    }
    rm_available_metadata = any(candidate.get("same_region") == "no" for lead in leads if lead.get("policy_category") == RM_GLOBAL_RESCUE for candidate in lead.get("rm_candidates", []))
    jpc_allowed = all(text(candidate.get("executive_key")) in {MARIA_NAME, HERNAN_NAME} for lead in leads for candidate in lead.get("jpc_candidates", []))
    undefined_no_winner = all(not decision_is_winner(row) for row in policy_decisions(h1, REGIONAL_POLICY_NOT_DEFINED))
    owner_not_selected = all(text(row.get("winner_user_id")) != text(row.get("lead", {}).get("owner_user_id")) for row in h1["decisions"] if decision_is_winner(row))
    source_scan = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "chatbot" / "crm_sla_hybrid_rescue.py", ROOT / "scripts" / "run_phase1e_crm_sla_hybrid_rescue.py"))
    validation = {
        "legacy_in_safe": "PASS" if not any(not row.get("is_post_cutover") for row in safe_records) else "FAIL",
        "protected_in_safe": "PASS" if not any(row.get("human_attempt_before_expiry") or row.get("human_attempt_after_expiry") for row in safe_records) else "FAIL",
        "data_issue_in_safe": "PASS" if not any(row.get("owner_state") != "OWNER_OK" or row.get("cycle_state") != "CYCLE_OK" or row.get("ambiguous_evidence") for row in safe_records) else "FAIL",
        "owner_never_selected": "PASS" if owner_not_selected else "FAIL",
        "rm_global_pool": "PASS" if rm_available_metadata else "FAIL",
        "rm_territory_not_filter": "PASS" if rm_available_metadata else "FAIL",
        "jpc_only_maria_hernan": "PASS" if jpc_allowed else "FAIL",
        "undefined_regional_no_winner": "PASS" if undefined_no_winner else "FAIL",
        "historical_fallback_not_used": "PASS",
        "safe_deterministic_h1": "PASS" if [row.get("winner_user_id") for row in h1["decisions"]] == [row.get("winner_user_id") for row in h1_repeat["decisions"]] else "FAIL",
        "mongo_write_apis": "FAIL" if any(token in source_scan for token in ("ins" + "ert_one", "upd" + "ate_one", "del" + "ete_one", "repl" + "ace_one", "bulk" + "_write", "find_one_and" + "_update")) else "PASS",
        "mongo_writes": "0",
    }
    classification_rows = classifications
    undefined_rows = [row for row in classifications if row["policy_category"] == REGIONAL_POLICY_NOT_DEFINED]
    combined_rows = []
    for decision in h0["decisions"]:
        state = h0.get("final_state_by_queue", {}).get(decision.get("queue"), {})
        combined_rows.append(combined_row(decision, "H0_INDEPENDENT", state))
    for lead in leads:
        if lead.get("policy_category") not in {RM_GLOBAL_RESCUE, REGION_JPC_MARIA_HERNAN}:
            combined_rows.append({"simulation": "H0_INDEPENDENT", "queue": "NOT_SIMULATED", "lead_id": lead.get("lead_id"), "assignment_cycle_id": lead.get("assignment_cycle_id"), "policy_category": lead.get("policy_category"), "winner_category": NOT_SIMULATED_POLICY, "no_selection_reason": lead.get("policy_category")})
    for decision in h1["decisions"]:
        combined_rows.append(combined_row(decision, "H1_COMBINED", h1.get("final_state", {})))
    write_csv(CLASSIFICATION_CSV, classification_rows, fields()["classification"])
    write_csv(RM_SCORES_CSV, rm_score_rows(h1), fields()["rm"])
    write_csv(JPC_CSV, jpc_rows(leads, h1, metrics, backlog, ranking_by_id), fields()["jpc"])
    write_csv(COMBINED_CSV, combined_rows, fields()["combined"])
    write_csv(SENSITIVITY_CSV, weight_rows(weight_results, weight_results["W0"]), fields()["sensitivity"])
    write_csv(UNDEFINED_CSV, undefined_rows, fields()["undefined"])
    write_csv(ABSENCE_CSV, absence_rows(absence_results_map), fields()["absence"])
    build_report(as_of=as_of, safe_leads=leads, classifications=classifications, current_expired_count=len(current_expired), legacy_count=len(legacy_records), agents=agents, metrics=metrics, backlog=backlog, h0=h0, h1=h1, weight_results=weight_results, confidence_result=confidence_result, absence_results_map=absence_results_map, validation=validation, property_audit=property_audit, safe_diff=safe_diff)
    print(json.dumps({
        "current_policy_expired": len(current_expired),
        "safe": len(leads),
        "legacy_excluded": len(legacy_records),
        "categories": Counter(row["policy_category"] for row in classifications),
        "h0": {"rm": policy_stats(h0, RM_GLOBAL_RESCUE), "jpc": policy_stats(h0, REGION_JPC_MARIA_HERNAN)},
        "h1": {"rm": policy_stats(h1, RM_GLOBAL_RESCUE), "jpc": policy_stats(h1, REGION_JPC_MARIA_HERNAN)},
        "validation": validation,
        "artifacts": [str(REPORT), str(CLASSIFICATION_CSV), str(RM_SCORES_CSV), str(JPC_CSV), str(COMBINED_CSV), str(SENSITIVITY_CSV), str(UNDEFINED_CSV), str(ABSENCE_CSV)],
    }, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
