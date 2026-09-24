"""Runtime bridge for real owner-campaign test previews.

The live source normalization stays in ``campanas.owner_campaign_test_runtime``
and rendering stays in the approved V2 email renderer. This module gives the
temporary admin panel one explicit, read-only entry point for A–E previews.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.owner_campaign_test_sender import ALL_CASES
from campanas.owner_campaign_test_runtime import build_owner_campaign_test_cases_live
from campanas.owner_campaign_test_sender import (
    PreparedTestMessage,
    prepare_test_messages,
)


@dataclass(frozen=True)
class PreviewResult:
    case_id: str
    prepared: PreparedTestMessage | None
    error_code: str | None = None


def build_rendered_test_previews(db: Any) -> tuple[PreviewResult, ...]:
    """Build independently reportable, live-data A–E previews without writes."""
    results: list[PreviewResult] = []
    for case_id in ALL_CASES:
        try:
            live_cases = build_owner_campaign_test_cases_live(db, case_ids=(case_id,))
            if len(live_cases) != 1 or live_cases[0].case_id != case_id:
                raise ValueError("live_case_batch_incomplete")
            prepared = prepare_test_messages(live_cases)
            if len(prepared) != 1 or prepared[0].case.case_id != case_id:
                raise ValueError("rendered_case_batch_incomplete")
            results.append(PreviewResult(case_id, prepared[0]))
        except Exception as exc:
            # The admin UI needs a safe pass/fail, not exception details that
            # might contain source data or configuration.
            results.append(PreviewResult(case_id, None, type(exc).__name__))
    return tuple(results)
