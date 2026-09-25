"""Runtime bridge for real owner-campaign test previews.

The live source normalization stays in ``campanas.owner_campaign_test_runtime``
and rendering stays in the approved V2 email renderer. This module gives the
temporary admin panel one explicit, read-only entry point for A–E previews.
"""

from __future__ import annotations

import logging
import re
import traceback
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

logger = logging.getLogger(__name__)
_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(token|secret|password|private[_ -]?key|authorization)\b(\s*[:=]\s*|\s+Bearer\s+)\S+"
)


def _safe_diagnostic_text(message: str) -> str:
    message = _EMAIL_PATTERN.sub("<email-redacted>", message)
    message = _JWT_PATTERN.sub("<token-redacted>", message)
    message = _CREDENTIAL_PATTERN.sub(r"\1\2<redacted>", message)
    return message


def _safe_exception_message(exc: Exception) -> str:
    return _safe_diagnostic_text(str(exc))[:500]


def _portfolio_diagnostic(case_id: str, live_cases: list[Any] | None) -> tuple[str, int, str]:
    if case_id != "E" or not live_cases:
        return "NOT_SELECTED", 0, ""
    case = live_cases[0]
    parts = tuple(case.portfolio_cases) if getattr(case, "case_id", None) == "E" else ()
    source = str((getattr(case, "render_context", {}) or {}).get("owner_email_source") or "NOT_REPORTED")
    codes = ",".join(str(getattr(part, "property_code", "")) for part in parts)
    return source, len(parts), codes


@dataclass(frozen=True)
class PreviewResult:
    case_id: str
    prepared: PreparedTestMessage | None
    error_code: str | None = None


def build_rendered_test_previews(db: Any) -> tuple[PreviewResult, ...]:
    """Build independently reportable, live-data A–E previews without writes."""
    results: list[PreviewResult] = []
    for case_id in ALL_CASES:
        phase = "build_live_cases"
        live_cases: list[Any] | None = None
        try:
            live_cases = build_owner_campaign_test_cases_live(db, case_ids=(case_id,))
            if len(live_cases) != 1 or live_cases[0].case_id != case_id:
                raise ValueError("live_case_batch_incomplete")
            phase = "prepare_render_and_links"
            prepared = prepare_test_messages(live_cases)
            if len(prepared) != 1 or prepared[0].case.case_id != case_id:
                raise ValueError("rendered_case_batch_incomplete")
            results.append(PreviewResult(case_id, prepared[0]))
        except Exception as exc:
            # Keep the web response symbolic, but do not silently discard E's
            # exception: operators need a server-side traceback to repair the
            # multiproperty render. Never include stack locals or raw emails.
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
            if case_id == "E":
                email_source, property_count, property_codes = _portfolio_diagnostic(case_id, live_cases)
                stack = _safe_diagnostic_text("".join(traceback.format_tb(exc.__traceback__)))
                logger.error(
                    "OWNER_CAMPAIGN_E_PREVIEW_EXCEPTION phase=%s exception_type=%s message=%s "
                    "owner_email_source=%s property_count=%s property_codes=%s traceback=\n%s",
                    phase, type(exc).__name__, _safe_exception_message(exc), email_source,
                    property_count, property_codes, stack,
                )
            results.append(PreviewResult(case_id, None, code))
    return tuple(results)
