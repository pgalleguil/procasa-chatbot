"""Fail-closed, in-memory SLA rescue ranking for policy V2.

This selector is deliberately independent from MongoDB, scheduling,
notifications and assignment writes. The worker calls it only after its
existing canonical cycle, expiry, management, ownership and cutover checks.
The V1 selector remains available to replay historical decisions.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import isfinite
from typing import Any, Iterable, Mapping


POLICY_VERSION = "crm_sla_reassignment_v2"
WINNER_CATEGORY = "DYNAMIC_V2_WINNER_SELECTED"
NO_CANDIDATE_CATEGORY = "DYNAMIC_V2_NO_ELIGIBLE_CANDIDATE"
NO_DATA_CATEGORY = "DYNAMIC_V2_NO_VALID_PERFORMANCE_DATA"
UNVERIFIED_CATEGORY = "DYNAMIC_V2_PRECONDITIONS_UNVERIFIED"

# Exact normalized shares: 6/18, 5/18, 4/18 and 3/18. Commercial outcome is
# intentionally absent until its source events and attribution are audited.
BASE_WEIGHTS = {"sla": 6 / 18, "management": 5 / 18, "speed": 4 / 18, "capacity": 3 / 18}


@dataclass(frozen=True)
class RankingParameters:
    shrinkage_k: float = 20.0
    minimum_speed_sample: int = 5
    tie_threshold: float = 1.0
    expired_load_weight: float = 3.0
    unmanaged_load_weight: float = 2.0
    open_load_weight: float = 1.0
    hot_unmanaged_load_weight: float = 1.5
    recent_rescue_load_weight: float = 2.0
    speed_p50_weight: float = 0.70
    speed_p90_weight: float = 0.30
    winsor_lower: float = 0.05
    winsor_upper: float = 0.95


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _optional_number(value: Any) -> float | None:
    if value in (None, "", "N/A", "N/D"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def shrink_rate(personal_rate: float, sample_size: int, team_rate: float, k: float = 20.0) -> float:
    """Shrink a rate toward a comparable team rate using the configured k."""
    n = max(0.0, _number(sample_size))
    prior = max(0.0, _number(k))
    personal = _clamp(_number(personal_rate))
    team = _clamp(_number(team_rate))
    if n + prior == 0:
        return team
    return (n * personal + prior * team) / (n + prior)


def _strata(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("by_temperature") or row.get("strata") or {}
    return value if isinstance(value, Mapping) else {}


def _team_mix(team: Mapping[str, Any]) -> tuple[dict[str, float], str]:
    explicit = team.get("temperature_mix")
    counts = explicit if isinstance(explicit, Mapping) else {
        key: (_strata(team).get(key) or {}).get("sample_size", 0)
        for key in ("HOT", "NORMAL")
    }
    totals = {key: max(0.0, _number(counts.get(key))) for key in ("HOT", "NORMAL")}
    total = sum(totals.values())
    if total:
        return {key: value / total for key, value in totals.items()}, "team_observed"
    return {"HOT": 0.5, "NORMAL": 0.5}, "equal_fallback_no_team_mix"


def _metric_rate(
    candidate: Mapping[str, Any],
    team: Mapping[str, Any],
    metric: str,
    *,
    team_metric: str,
    mix: Mapping[str, float],
    params: RankingParameters,
) -> tuple[float | None, int, list[str], str]:
    person_strata = _strata(candidate)
    team_strata = _strata(team)
    person_total = 0
    score = 0.0
    weight_total = 0.0
    imputed: list[str] = []
    for temperature, weight in mix.items():
        if weight <= 0:
            continue
        own = person_strata.get(temperature) if isinstance(person_strata.get(temperature), Mapping) else {}
        cohort = team_strata.get(temperature) if isinstance(team_strata.get(temperature), Mapping) else {}
        n = max(0, int(_number(own.get("sample_size"))))
        person_total += n
        own_rate = _optional_number(own.get(metric))
        team_rate = _optional_number(cohort.get(team_metric))
        if team_rate is None:
            team_rate = _optional_number(team.get(team_metric))
            if team_rate is not None:
                imputed.append(f"{temperature}:team_overall_fallback")
        if team_rate is None:
            continue
        if n == 0 or own_rate is None:
            adjusted = _clamp(team_rate)
            imputed.append(f"{temperature}:team_rate")
        else:
            adjusted = shrink_rate(own_rate, n, team_rate, params.shrinkage_k)
        score += weight * adjusted
        weight_total += weight

    # Compatibility for explicitly injected/replay rows that predate strata.
    # The audit field makes this less reliable path visible; production V2
    # snapshots populate strata from the same bounded historical read.
    if not weight_total:
        n = max(0, int(_number(candidate.get(f"{metric}_sample_size", candidate.get("sample_size", 0)))))
        own_rate = _optional_number(candidate.get(metric))
        team_rate = _optional_number(team.get(team_metric))
        if own_rate is not None and team_rate is not None:
            person_total = n
            score = shrink_rate(own_rate, n, team_rate, params.shrinkage_k)
            weight_total = 1.0
            imputed.append("overall_unstratified_fallback")
    if not weight_total:
        return None, person_total, imputed, "unavailable"
    status = "imputed" if imputed else "observed"
    return score / weight_total, person_total, imputed, status


def _winsor_bounds(values: list[float], low_q: float, high_q: float) -> tuple[float, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    low_index = int((len(ordered) - 1) * low_q)
    high_index = int((len(ordered) - 1) * high_q + 0.999999)
    return ordered[low_index], ordered[min(len(ordered) - 1, high_index)]


def _lower_is_better(value: float | None, peers: list[float], params: RankingParameters) -> float | None:
    if value is None or not peers:
        return None
    bounds = _winsor_bounds(peers, params.winsor_lower, params.winsor_upper)
    if bounds is None:
        return None
    low, high = bounds
    if high <= low:
        return 100.0
    clipped = max(low, min(high, value))
    return max(0.0, min(100.0, 100.0 * (high - clipped) / (high - low)))


def _speed_scores(
    candidates: list[dict[str, Any]],
    team: Mapping[str, Any],
    mix: Mapping[str, float],
    params: RankingParameters,
) -> dict[str, tuple[float | None, dict[str, Any]]]:
    by_temperature = {key: _strata(team).get(key, {}) for key in ("HOT", "NORMAL")}
    prepared: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        uid = str(candidate.get("user_id") or "")
        person = _strata(candidate)
        cells: dict[str, Any] = {}
        for temperature in ("HOT", "NORMAL"):
            own = person.get(temperature) if isinstance(person.get(temperature), Mapping) else {}
            cohort = by_temperature.get(temperature) if isinstance(by_temperature.get(temperature), Mapping) else {}
            n = max(0, int(_number(own.get("sample_size"))))
            reliable = (
                n >= params.minimum_speed_sample
                and _optional_number(own.get("p50")) is not None
                and _optional_number(own.get("p90")) is not None
            )
            p50 = _optional_number(own.get("p50")) if reliable else _optional_number(cohort.get("p50"))
            p90 = _optional_number(own.get("p90")) if reliable else _optional_number(cohort.get("p90"))
            if p50 is None:
                p50 = _optional_number(team.get("team_p50_average"))
            if p90 is None:
                p90 = _optional_number(team.get("team_p90_average"))
            cells[temperature] = {
                "p50": p50, "p90": p90, "reliable": reliable,
                "source": "observed_stratum" if reliable else "team_stratum_or_global_fallback",
                "sample_size": n,
            }
        prepared[uid] = cells

    scored: dict[str, tuple[float | None, dict[str, Any]]] = {}
    for candidate in candidates:
        uid = str(candidate.get("user_id") or "")
        cell_scores: dict[str, float] = {}
        p50_scores: dict[str, float] = {}
        p90_scores: dict[str, float] = {}
        imputed: list[str] = []
        for temperature, weight in mix.items():
            if weight <= 0:
                continue
            p50_values = [row[temperature]["p50"] for row in prepared.values() if row[temperature]["p50"] is not None]
            p90_values = [row[temperature]["p90"] for row in prepared.values() if row[temperature]["p90"] is not None]
            item = prepared[uid][temperature]
            p50 = _lower_is_better(item["p50"], p50_values, params)
            p90 = _lower_is_better(item["p90"], p90_values, params)
            if p50 is None or p90 is None:
                continue
            p50_scores[temperature] = p50
            p90_scores[temperature] = p90
            cell_scores[temperature] = params.speed_p50_weight * p50 + params.speed_p90_weight * p90
            if not item["reliable"]:
                imputed.append(temperature)
        total_weight = sum(mix[key] for key in cell_scores)
        speed = sum(mix[key] * value for key, value in cell_scores.items()) / total_weight if total_weight else None
        scored[uid] = (speed, {
            "speed_score_p50_by_temperature": p50_scores,
            "speed_score_p90_by_temperature": p90_scores,
            "speed_imputed": bool(imputed),
            "speed_imputation_source": sorted(imputed),
        })
    return scored


def _capacity(candidate: Mapping[str, Any], params: RankingParameters) -> tuple[float, str | None, bool]:
    expired = _optional_number(candidate.get("simulated_expired_current", candidate.get("expired_current_policy", candidate.get("expired_backlog"))))
    unmanaged = _optional_number(candidate.get("simulated_unmanaged_current", candidate.get("unmanaged_current_policy", candidate.get("unmanaged_backlog"))))
    opened = _optional_number(candidate.get("simulated_open_current", candidate.get("open_current_policy", candidate.get("open_backlog"))))
    hot = _optional_number(candidate.get("hot_unmanaged_backlog"))
    rescues = _optional_number(candidate.get("recent_rescues_7d"))
    if expired is None or unmanaged is None or opened is None:
        return 0.0, "capacity_snapshot_incomplete", True
    imputed = hot is None or rescues is None
    pressure = (
        params.expired_load_weight * expired
        + params.unmanaged_load_weight * unmanaged
        + params.open_load_weight * opened
        + params.hot_unmanaged_load_weight * (hot or 0.0)
        + params.recent_rescue_load_weight * (rescues or 0.0)
    )
    # Verified load affects only the weighted capacity score. It must not
    # make an otherwise eligible executive ineligible by itself.
    return pressure, None, imputed


def filter_candidates(
    lead: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    branch: str,
    selection_context_verified: bool,
    admin_user_ids: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not selection_context_verified:
        return [], [{"excluded_reason": "preconditions_not_verified"}]
    current_owner = str(lead.get("owner_user_id") or "")
    previous = {str(value) for value in (lead.get("previous_owner_user_ids") or ()) if str(value)}
    admins = {str(value) for value in admin_user_ids}
    rows = [dict(row) for row in candidates]
    counts = Counter(str(row.get("user_id") or "") for row in rows if str(row.get("user_id") or ""))
    valid: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for candidate in rows:
        uid = str(candidate.get("user_id") or "")
        reasons: list[str] = []
        if not uid:
            reasons.append("missing_user_id")
        if counts.get(uid, 0) > 1:
            reasons.append("duplicate_user_id")
        if candidate.get("active") is not True:
            reasons.append("inactive_or_unverified_user")
        if str(candidate.get("role") or "").strip().lower() != "agente":
            reasons.append("role_not_agent")
        if uid in admins:
            reasons.append("administrator_excluded")
        if uid == current_owner:
            reasons.append("current_owner_excluded")
        if uid in previous:
            reasons.append("previous_owner_excluded")
        if candidate.get("cycle_conflict"):
            reasons.append("cycle_conflict")
        if candidate.get("data_issue"):
            reasons.append("data_issue")
        if candidate.get("protected_by_management"):
            reasons.append("protected_by_management")
        if candidate.get("available") is False:
            reasons.append("unavailable")
        if candidate.get("territory_valid") is False:
            reasons.append("territory_invalid")
        # The legacy JPC route was limited to named staff. V2 replaces names
        # with agents whose declared profile certifies the property's region.
        if branch == "REGION_JPC_MARIA_HERNAN" and candidate.get("same_region") != "yes":
            reasons.append("territory_not_certified")
        if reasons:
            candidate["excluded_reason"] = ";".join(dict.fromkeys(reasons))
            excluded.append(candidate)
        else:
            valid.append(candidate)
    return valid, excluded


def select_v2_winner(
    lead: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    team: Mapping[str, Any],
    branch: str,
    selection_context_verified: bool,
    admin_user_ids: Iterable[str],
    params: RankingParameters = RankingParameters(),
) -> dict[str, Any]:
    """Return explainable candidate scores; this function never writes state."""
    eligible, excluded = filter_candidates(
        lead, candidates, branch=branch,
        selection_context_verified=selection_context_verified,
        admin_user_ids=admin_user_ids,
    )
    base = {
        "policy_version": POLICY_VERSION,
        "winner": None,
        "scored": [],
        "selection_pool": [],
        "hard_excluded": excluded,
        "selection_reason": "",
        "selection_rule": "dynamic_performance_v2",
    }
    if not selection_context_verified:
        base["selection_reason"] = UNVERIFIED_CATEGORY
        return base
    if not eligible:
        base["selection_reason"] = NO_CANDIDATE_CATEGORY
        return base

    mix, mix_source = _team_mix(team)
    speed = _speed_scores(eligible, team, mix, params)
    capacity_rows: dict[str, tuple[float, str | None, bool]] = {
        str(row.get("user_id")): _capacity(row, params) for row in eligible
    }
    pressures = [value[0] for value in capacity_rows.values() if value[1] != "capacity_snapshot_incomplete"]
    low_pressure, high_pressure = (min(pressures), max(pressures)) if pressures else (None, None)
    scored: list[dict[str, Any]] = []
    for raw in eligible:
        row = dict(raw)
        uid = str(row.get("user_id") or "")
        sla_rate, sla_n, sla_imputed, sla_status = _metric_rate(
            row, team, "sla_compliance_rate", team_metric="sla_compliance_rate",
            mix=mix, params=params,
        )
        management_rate, management_n, management_imputed, management_status = _metric_rate(
            row, team, "attention_rate", team_metric="attention_rate",
            mix=mix, params=params,
        )
        pressure, capacity_exclusion, capacity_imputed = capacity_rows[uid]
        capacity_score = None
        if capacity_exclusion != "capacity_snapshot_incomplete" and low_pressure is not None and high_pressure is not None:
            capacity_score = 100.0 if high_pressure <= low_pressure else max(0.0, min(100.0, 100.0 * (high_pressure - pressure) / (high_pressure - low_pressure)))
        components: dict[str, float | None] = {
            "sla": sla_rate * 100.0 if sla_rate is not None else None,
            "management": management_rate * 100.0 if management_rate is not None else None,
            "speed": speed[uid][0],
            "capacity": capacity_score,
        }
        available_weight = sum(BASE_WEIGHTS[key] for key, value in components.items() if value is not None)
        performance_score = (
            sum(BASE_WEIGHTS[key] * value for key, value in components.items() if value is not None) / available_weight
            if available_weight > 0 else None
        )
        sample_size = max(sla_n, management_n)
        confidence = "HIGH" if sample_size >= 30 else "MEDIUM" if sample_size >= 10 else "LOW"
        row.update({
            "policy_version": POLICY_VERSION,
            "adjusted_sla_rate": sla_rate,
            "adjusted_management_rate": management_rate,
            "sla_sample_size": sla_n,
            "management_sample_size": management_n,
            "speed_sample_size": max((int(_number((_strata(row).get(key) or {}).get("sample_size"))) for key in ("HOT", "NORMAL")), default=0),
            "team_temperature_mix": dict(mix),
            "team_temperature_mix_source": mix_source,
            "metric_imputation": {
                "sla": sla_imputed,
                "management": management_imputed,
                "speed": speed[uid][1]["speed_imputation_source"],
                "capacity": "hot_or_recent_rescue_missing" if capacity_imputed else [],
            },
            "metric_status": {
                "sla": sla_status,
                "management": management_status,
                "speed": "imputed" if speed[uid][1]["speed_imputed"] else "observed",
                "capacity": "imputed" if capacity_imputed else "observed",
            },
            "sla_score": components["sla"],
            "management_score": components["management"],
            "speed_score": components["speed"],
            "speed_score_p50_by_temperature": speed[uid][1]["speed_score_p50_by_temperature"],
            "speed_score_p90_by_temperature": speed[uid][1]["speed_score_p90_by_temperature"],
            "capacity_score": components["capacity"],
            "load_pressure": pressure,
            "capacity_exclusion_reason": capacity_exclusion,
            "performance_score": performance_score,
            "performance_data_valid": performance_score is not None,
            "performance_confidence": confidence,
            "weights_applied": {key: BASE_WEIGHTS[key] / available_weight for key, value in components.items() if value is not None} if available_weight else {},
        })
        scored.append(row)
    capacity_excluded = [row for row in scored if row.get("capacity_exclusion_reason")]
    available = [row for row in scored if not row.get("capacity_exclusion_reason") and row.get("performance_data_valid")]
    if not available:
        base["scored"] = scored
        base["hard_excluded"] = excluded + capacity_excluded
        base["selection_pool"] = []
        base["selection_reason"] = NO_DATA_CATEGORY if not capacity_excluded else "DYNAMIC_V2_CAPACITY_LIMIT"
        return base

    def tie_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            _number(row.get("load_pressure")),
            -max(int(_number(row.get("sla_sample_size"))), int(_number(row.get("management_sample_size")))),
            _number(row.get("p50_first_management_business_minutes"), float("inf")),
            _number(row.get("p90_first_management_business_minutes"), float("inf")),
            _number(row.get("recent_rescues_7d"), float("inf")),
            str(row.get("user_id") or ""),
        )

    by_score = sorted(available, key=lambda row: (-_number(row.get("performance_score")), str(row.get("user_id") or "")))
    if len(by_score) > 1 and _number(by_score[0].get("performance_score")) - _number(by_score[1].get("performance_score")) < params.tie_threshold:
        first_score = _number(by_score[0].get("performance_score"))
        tied = [row for row in available if first_score - _number(row.get("performance_score")) < params.tie_threshold]
        outside = [row for row in available if row not in tied]
        by_score = sorted(tied, key=tie_key) + sorted(outside, key=lambda row: (-_number(row.get("performance_score")), str(row.get("user_id") or "")))
        tie_applied = "yes"
    else:
        tie_applied = "no"
    for rank, row in enumerate(by_score, start=1):
        row["rank"] = rank
        row["tie_break_applied"] = tie_applied if rank == 1 else "no"
    winner = by_score[0]
    base.update({
        "winner": winner,
        "scored": scored,
        "selection_pool": available,
        "hard_excluded": excluded + capacity_excluded,
        "selection_reason": "DYNAMIC_V2_SCORE_MAXIMIZED",
        "selection_rule": "dynamic_performance_v2",
        "second_place": by_score[1] if len(by_score) > 1 else None,
    })
    return base


