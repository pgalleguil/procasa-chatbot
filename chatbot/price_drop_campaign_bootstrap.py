"""Deprecated V1 bootstrap retained as a fail-closed compatibility stub."""
from __future__ import annotations


def run_bootstrap(_db=None) -> dict[str, str | int]:
    return {
        "status": "BLOCKED",
        "reason": "legacy_v1_bootstrap_removed_use_scripts_enviar_piloto_rebajas",
        "writes": 0,
        "sends": 0,
    }
