"""Runtime bridge for real owner-campaign test previews.

The live source normalization stays in ``campanas.owner_campaign_test_runtime``
and rendering stays in the approved V2 email renderer. This module gives the
temporary admin panel one explicit, read-only entry point for A–E previews.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.owner_campaign_test_sender import ALL_CASES
from campanas.owner_campaign_test_runtime import (
    LiveTestCaseBuildError,
    build_owner_campaign_test_cases_live,
)
from campanas.owner_campaign_test_sender import (
    PreparedTestMessage,
    TestSenderError,
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
            # Only builder-owned symbolic codes are safe to expose. Arbitrary
            # exception text/types can contain source data or configuration.
            code = "preview_build_failed"
            candidate = ""
            if isinstance(exc, LiveTestCaseBuildError):
                candidate = str(exc.error_code or "")
            elif isinstance(exc, TestSenderError):
                # Sender errors are raised only with internal symbolic codes;
                # still validate their shape before showing them in the panel.
                candidate = str(exc)
            is_safe_symbol = (
                len(candidate) <= 64
                and candidate.isascii()
                and candidate.replace("_", "").isalnum()
                and candidate[:1].isalpha()
            )
            if is_safe_symbol:
                code = candidate
            results.append(PreviewResult(case_id, None, code))
    return tuple(results)
