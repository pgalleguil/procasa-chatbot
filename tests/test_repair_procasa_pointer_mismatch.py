from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

import pytest

from maintenance import repair_procasa_pointer_mismatch as repair


def _snapshot(case_name):
    case = repair.CASES[case_name]
    assigned_at = datetime(2026, 10, 19, 12, 0, 34, tzinfo=timezone.utc)
    sla_started_at = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    management = []
    first_by_cycle = {}
    for record_id, expected in case["management"].items():
        management.append({
            "_id": record_id,
            "assignment_cycle_id": expected["cycle_id"],
            "actor_user_id": case["owner_user_id"],
            "result_type": expected["result_type"],
            "status": "completed",
            "occurred_at": expected["occurred_at"],
        })
        first_by_cycle.setdefault(expected["cycle_id"], []).append(expected["occurred_at"])
    cycles = []
    for cycle_id, status, cycle_assigned_at, unassigned_at in (
        (case["closed_cycle_id"], "closed", datetime(2026, 10, 18, 15, 21, 51, tzinfo=timezone.utc), assigned_at),
        (case["active_cycle_id"], "active", assigned_at, None),
    ):
        cycles.append({
            "assignment_cycle_id": cycle_id,
            "assigned_to_user_id": case["owner_user_id"],
            "assigned_to_display_name": case["owner_name"],
            "cycle_status": status,
            "assigned_at": cycle_assigned_at,
            "unassigned_at": unassigned_at,
            "sla_started_at": sla_started_at,
            "sla_first_management_status": "completed",
            "first_valid_management_at": min(first_by_cycle[cycle_id]),
            "source_event_id": case["source_event_id"] if cycle_id == case["active_cycle_id"] else "original-inbound-event",
            "reason": "lead_created" if cycle_id == case["active_cycle_id"] else "inbound_message",
        })
    return {
        "lead": [{
            "_id": case["lead_id"],
            "ejecutivo_asignado": case["owner_name"],
            "prospecto": {"codigo": case["property_code"], "ejecutivo": case["owner_name"]},
            "sla_status": "fulfilled",
            "lifecycle": {
                "current_assignment_cycle_id": case["active_cycle_id"],
                "assignment_cycle_id": case["closed_cycle_id"],
                "sla_started_at": sla_started_at,
            },
        }],
        "cycles": cycles,
        "management": management,
        "events": [],
        "alerts": [{
            "_id": case["alert_id"],
            "status": "open",
            "evidence": {"issues": ["cycle_pointer_mismatch"]},
        }],
        "notifications": [],
        "sla_alerts": [],
        "pending_notifications": [],
        "active_jobs": [],
    }


@pytest.mark.parametrize("case_name", ["17252-other", "5806"])
def test_preflight_accepts_only_the_individually_audited_history(case_name):
    snapshot = _snapshot(case_name)

    repair._validate_pre_repair(case_name, snapshot)

    assert len(snapshot["cycles"]) == 2
    assert len(snapshot["management"]) == len(repair.CASES[case_name]["management"])


def test_17252_secondary_preserves_both_effective_contacts():
    snapshot = _snapshot("17252-other")

    repair._validate_pre_repair("17252-other", snapshot)

    assert {row["result_type"] for row in snapshot["management"]} == {"EFFECTIVE_CONTACT"}
    assert len(snapshot["management"]) == 2


def test_5806_preserves_all_four_follow_up_requests():
    snapshot = _snapshot("5806")

    repair._validate_pre_repair("5806", snapshot)

    assert {row["result_type"] for row in snapshot["management"]} == {"FOLLOW_UP_REQUESTED"}
    assert len(snapshot["management"]) == 4


def test_preflight_rejects_a_pointer_or_cycle_change():
    snapshot = _snapshot("5806")
    snapshot["lead"][0]["lifecycle"]["assignment_cycle_id"] = "unexpected-cycle"

    with pytest.raises(repair.RepairSafetyError, match="cycle pointers changed"):
        repair._validate_pre_repair("5806", snapshot)

    snapshot = _snapshot("17252-other")
    snapshot["cycles"].append(dict(snapshot["cycles"][0]))
    with pytest.raises(repair.RepairSafetyError, match="exactly the two"):
        repair._validate_pre_repair("17252-other", snapshot)


def test_execution_requires_case_specific_confirmation():
    for case_name, case in repair.CASES.items():
        with pytest.raises(SystemExit):
            repair.parse_args([
                "--case", case_name, "--execute", "--backup", "/tmp/backup.json",
                "--preflight", "/tmp/preflight.json", "--actor", "Pablo Galleguillos",
                "--confirm-incident", "wrong-token",
            ])

        args = repair.parse_args([
            "--case", case_name, "--execute", "--backup", "/tmp/backup.json",
            "--preflight", "/tmp/preflight.json", "--actor", "Pablo Galleguillos",
            "--confirm-incident", case["confirmation"],
        ])
        assert args.execute is True


def test_cli_bootstraps_repository_package_for_postcondition_owner_lookup():
    script = Path(repair.__file__).resolve()
    repo_root = script.parents[1]
    isolated_import = """
import importlib.util
import sys
from pathlib import Path

script = Path(sys.argv[1]).resolve()
repo_root = script.parents[1]
sys.path[:] = [entry for entry in sys.path if Path(entry or '.').resolve() != repo_root]
sys.path.insert(0, str(script.parent))
spec = importlib.util.spec_from_file_location('repair_cli', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from chatbot.crm_assignment_integrity import assignment_context
assert callable(assignment_context)
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-c", isolated_import, str(script)],
        cwd=repo_root / "maintenance",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
