"""Rescue newly arriving leads when the property's executive is inactive.

Reuses the frozen CRM SLA reassignment scorer and selector. Candidate
performance is read from recently committed SLA reassignment snapshots,
not from a second set of weights or a static executive list. The original
property/round-robin routing remains unchanged for available owners.

This module does not create assignment cycles or send notifications.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable, Mapping

from .crm_sla_global_rescue import RescueParameters
from .crm_sla_hybrid_stabilization import L1_LOW_NEEDS_PLUS_5, R2_ROLLING_SHARE
from .crm_sla_policy_freeze import RM_ADMIN_EXCLUDED_USER_IDS, _rm_selection, _state_for

logger = logging.getLogger(__name__)

# A recent committed decision already contains the exact historical SLA
# performance inputs used by the production reassignment worker.
MAX_SCORE_AGE = timedelta(hours=48)
MAX_SNAPSHOTS = 40


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def _team_metrics(rows: list[dict[str, Any]], params: RescueParameters) -> dict[str, Any]:
    samples = sum(max(0, int(_num(row.get("sample_size")))) for row in rows)
    sla = sum(max(0, int(_num(row.get("sample_size")))) * _num(row.get("sla_compliance_rate")) for row in rows)
    attention = sum(max(0, int(_num(row.get("sample_size")))) * _num(row.get("attention_rate")) for row in rows)
    reliable = [
        row for row in rows
        if _num(row.get("sample_size")) >= params.minimum_speed_sample
        and row.get("p50_first_management_business_minutes") is not None
        and row.get("p90_first_management_business_minutes") is not None
    ]
    speed_rows = reliable or [
        row for row in rows
        if row.get("p50_first_management_business_minutes") is not None
        and row.get("p90_first_management_business_minutes") is not None
    ]
    def mean(key: str):
        values = [_num(row[key]) for row in speed_rows if row.get(key) is not None]
        return sum(values) / len(values) if values else None
    return {
        "sla_compliance_rate": sla / samples if samples else 0.0,
        "attention_rate": attention / samples if samples else 0.0,
        "team_p50_average": mean("p50_first_management_business_minutes"),
        "team_p90_average": mean("p90_first_management_business_minutes"),
    }


def _capacity_backup(db: Any, eligible: Mapping[str, Mapping[str, Any]]) -> tuple[str, str]:
    """Last resort: an active notified agent with the smallest current backlog.

    This is deliberately marked as a degraded capacity-only decision. It is
    used only when no eligible scored SLA rescue decision can be produced.
    It never uses a hardcoded executive or an inactive agent.
    """
    loads = {uid: [0, 0] for uid in eligible}
    for cycle in db["crm_assignment_cycles"].find(
        {"cycle_status": "active", "unassigned_at": None},
        {"assigned_to_user_id": 1, "sla_first_management_status": 1},
    ):
        uid = str(cycle.get("assigned_to_user_id") or "")
        if uid not in loads:
            continue
        loads[uid][1] += 1
        if cycle.get("sla_first_management_status") != "completed":
            loads[uid][0] += 1
    winner_id = min(eligible, key=lambda uid: (loads[uid][0], loads[uid][1], uid))
    return str(eligible[winner_id]["nombre"]), "capacity_backup_no_sla_score"


def select_active_sla_rescuer(
    db: Any,
    *,
    excluded_name: str,
    is_temporarily_inactive: Callable[[str], bool],
    normalize: Callable[[str], str],
    now: datetime | None = None,
) -> tuple[str, str]:
    """Select globally using the frozen SLA policy when an owner is unavailable.

    Active/role/phone checks are live; prior performance scores are bounded to
    48 hours. When no scored option exists, capacity determines a staffed
    fallback so a lead is not abandoned solely because territory is uncovered.
    """
    as_of = now or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    eligible: dict[str, dict[str, Any]] = {}
    for user in db["usuarios"].find(
        {"is_active": True, "rol": "agente"},
        {"_id": 1, "nombre": 1, "telefono": 1, "tel": 1, "movil": 1},
    ):
        name = str(user.get("nombre") or "").strip()
        uid = str(user.get("_id") or "")
        phone = user.get("telefono") or user.get("tel") or user.get("movil")
        if (not uid or uid in RM_ADMIN_EXCLUDED_USER_IDS or not name or not phone
                or normalize(name) == normalize(excluded_name)
                or is_temporarily_inactive(name)):
            continue
        eligible[uid] = dict(user)
    if not eligible:
        logger.error("[ROUTER_SLA_RESCUE] No active commercial agents with phone")
        return "", "no_eligible_active_agent"

    latest: dict[str, dict[str, Any]] = {}
    snapshots = db["crm_assignment_cycles"].find(
        {
            "assigned_by": "sla_reassignment",
            "candidate_scores_snapshot.0": {"$exists": True},
            "assigned_at": {"$gte": as_of - MAX_SCORE_AGE},
        },
        {"assigned_at": 1, "candidate_scores_snapshot": 1},
    ).sort("assigned_at", -1).limit(MAX_SNAPSHOTS)
    for cycle in snapshots:
        for raw in cycle.get("candidate_scores_snapshot") or []:
            uid = str(raw.get("user_id") or "")
            if uid in eligible and uid not in latest:
                latest[uid] = dict(raw)

    if latest:
        candidates: list[dict[str, Any]] = []
        for uid, raw in latest.items():
            row = dict(raw)
            row.update({
                "user_id": uid,
                "executive": str(eligible[uid]["nombre"]),
                "identity_key": normalize(str(eligible[uid]["nombre"])),
                "executive_key": normalize(str(eligible[uid]["nombre"])),
                "active": True, "role": "agente",
                "legacy": False, "protected_by_management": False,
                "data_issue": False, "closed_lead": False,
                "not_currently_expired": False,
            })
            candidates.append(row)
        params = RescueParameters()
        lead = {
            "owner_user_id": "",
            "previous_sla_owner_ids": [],
            "rm_candidates": candidates,
        }
        state = _state_for([lead], keys=("rm_candidates",))
        history = [
            str(cycle.get("assigned_to_user_id") or "")
            for cycle in db["crm_assignment_cycles"].find(
                {
                    "assigned_by": "sla_reassignment",
                    "policy_branch": {"$in": ["RM_GLOBAL_RESCUE", "REGIONAL_GLOBAL_RESCUE"]},
                },
                {"assigned_to_user_id": 1},
            ).sort("assigned_at", -1).limit(20)
        ]
        decision = _rm_selection(
            lead, state, history,
            scenario=R2_ROLLING_SHARE,
            low_policy=L1_LOW_NEEDS_PLUS_5,
            team=_team_metrics(candidates, params),
            params=params,
        )
        winner = decision.get("winner") or {}
        winner_id = str(winner.get("user_id") or "")
        if winner_id in eligible:
            name = str(eligible[winner_id]["nombre"])
            logger.info(
                "[ROUTER_SLA_RESCUE] selected=%s score=%s rule=%s candidates=%d",
                name, winner.get("dynamic_rescue_score"),
                decision.get("selection_rule"), len(candidates),
            )
            return name, "sla_" + str(decision.get("selection_rule") or "RM_R2")

    name, reason = _capacity_backup(db, eligible)
    logger.warning("[ROUTER_SLA_RESCUE] score unavailable/guarded; selected=%s reason=%s", name, reason)
    return name, reason
