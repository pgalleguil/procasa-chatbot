from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_production_deploy_base.py"
CURRENT_MAIN_AT_INCIDENT = "b760443b1be9535a47cea8e92e545255afe33443"
REBASED_SLA_COMMIT = "67476bfe945a4778ccd8e7b9b88d411ad03ee357"
OLD_DIVERGENT_DEPLOY = "d6517398c24086b9caa728b6ee667211b5eb12e6"
OLD_SHADOW_BASE = "917ad30617ba2181e93da78a589a968d498c9666"


def run_guard(approved: str, head: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--approved-main-ref",
            approved,
            "--head",
            head,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("head", "expected_code", "expected_marker"),
    [
        (CURRENT_MAIN_AT_INCIDENT, 0, "DEPLOY_BASE_OK"),
        (REBASED_SLA_COMMIT, 0, "DEPLOY_BASE_OK"),
        (OLD_DIVERGENT_DEPLOY, 1, "DEPLOY_BASE_DIVERGED"),
        (OLD_SHADOW_BASE, 1, "DEPLOY_BASE_DIVERGED"),
    ],
)
def test_production_deploy_ancestry(head: str, expected_code: int, expected_marker: str) -> None:
    result = run_guard(CURRENT_MAIN_AT_INCIDENT, head)
    assert result.returncode == expected_code
    assert expected_marker in result.stdout


def test_missing_approved_ref_fails_closed() -> None:
    result = run_guard("refs/heads/does-not-exist", CURRENT_MAIN_AT_INCIDENT)
    assert result.returncode == 2
    assert "DEPLOY_BASE_ERROR" in result.stderr
