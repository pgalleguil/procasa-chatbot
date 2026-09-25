"""Independent CLI for the fixed-recipient owner-campaign E2E tests.

Examples::

    python -m analytics.owner_campaign_test_sender --cases A,B,D --test-mode --dry-run
    python -m analytics.owner_campaign_test_sender --cases A,B,D --test-mode
    python -m analytics.owner_campaign_test_sender --cases C,E --test-mode
    python -m analytics.owner_campaign_test_sender --cases A,B,C,D,E --test-mode --dry-run
    python -m analytics.owner_campaign_test_sender --cases A,B,C,D,E --test-mode

There is deliberately no recipient, property-code, campaign-id, or price
override. Live data is read from Mongo; test ledger writes and SMTP delivery
are delegated to the isolated campaign test sender.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from campanas.test_mode import TEST_CAMPAIGN_ID, TEST_RECIPIENT, test_mode_enabled
from campanas.owner_campaign_test_runtime import (
    LiveTestCaseBuildError,
    build_owner_campaign_test_cases_live,
)
from campanas.owner_campaign_test_sender import (
    SERVICE_BASE_URL,
    TEST_RUN_ID,
    TEST_LEDGER_COLLECTION,
    OwnerCampaignTestCase,
    PreparedTestMessage,
    prepare_test_messages,
    send_test_messages,
)


MASS_SEND_ENV = "OWNER_CAMPAIGN_MASS_SEND_ENABLED"
TRIGGER_SECRET_ENV = "OWNER_CAMPAIGN_TEST_TRIGGER_SECRET"
DELIVERY_UNKNOWN_BASELINE = 6
INITIAL_CASES = ("A", "B", "D")
REMAINING_CASES = ("C", "E")
ALL_CASES = ("A", "B", "C", "D", "E")
INITIAL_PROPERTY_CODES = frozenset({"5641", "16521", "16527"})
ALLOWED_BATCHES = frozenset({INITIAL_CASES, REMAINING_CASES, ALL_CASES})


class TestCampaignCLIError(ValueError):
    """A fail-closed error in the explicit E2E command."""


def parse_cases(value: str) -> tuple[str, ...]:
    parts = str(value or "").split(",")
    if any(not part.strip() for part in parts):
        raise TestCampaignCLIError("cases_must_be_A_B_D_C_E_or_ALL")
    cases = tuple(part.strip().upper() for part in parts)
    if cases not in ALLOWED_BATCHES:
        raise TestCampaignCLIError("cases_must_be_A_B_D_C_E_or_ALL")
    return cases


def parse_trigger_request(payload: Any) -> tuple[tuple[str, ...], bool]:
    """Parse the deliberately tiny HTTP trigger contract; reject all overrides."""
    if not isinstance(payload, Mapping) or set(payload) != {"batch", "mode"}:
        raise TestCampaignCLIError("trigger_payload_invalid")
    batch = payload.get("batch")
    mode = payload.get("mode")
    batches = {"ABD": INITIAL_CASES, "CE": REMAINING_CASES, "ALL": ALL_CASES}
    if not isinstance(batch, str) or batch not in batches:
        raise TestCampaignCLIError("trigger_batch_invalid")
    if not isinstance(mode, str) or mode not in {"dry-run", "send"}:
        raise TestCampaignCLIError("trigger_mode_invalid")
    return batches[batch], mode == "dry-run"


def trigger_secret_matches(provided: str | None, expected: str | None = None) -> bool:
    """Fail closed unless the independent server-side trigger secret is present and matches."""
    expected = os.getenv(TRIGGER_SECRET_ENV, "") if expected is None else expected
    if not isinstance(provided, str) or not isinstance(expected, str) or len(expected) < 32:
        return False
    try:
        return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
    except UnicodeError:
        return False


def mass_send_enabled() -> bool:
    return os.getenv(MASS_SEND_ENV, "false").strip().casefold() in {"true", "1", "yes", "on"}


def delivery_unknown_count() -> int:
    request = Request(
        f"{SERVICE_BASE_URL}/health",
        method="GET",
        headers={"User-Agent": "PROCASA-owner-campaign-test/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            if int(response.status) != 200:
                raise TestCampaignCLIError("health_check_not_200")
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
    except HTTPError as exc:
        raise TestCampaignCLIError("health_check_not_200") from exc
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestCampaignCLIError("health_check_unavailable") from exc

    values: list[int] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).casefold() == "delivery_unknown" and isinstance(child, int) and not isinstance(child, bool):
                    values.append(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    if not values or len(set(values)) != 1:
        raise TestCampaignCLIError("delivery_unknown_metric_not_unambiguous")
    return values[0]


def _database(db: Any = None) -> Any:
    if db is not None:
        return db
    from chatbot.storage import get_db

    return get_db()


def _validate_phase(db: Any, cases: tuple[str, ...]) -> None:
    rows = list(db[TEST_LEDGER_COLLECTION].find({"campaign_id": TEST_CAMPAIGN_ID}))
    if any(row.get("test_run_id") == TEST_RUN_ID for row in rows):
        raise TestCampaignCLIError("test_run_already_registered")
    if any(
        row.get("test_mode") is not True
        or str(row.get("actual_recipient_email") or "").strip().casefold() != TEST_RECIPIENT
        for row in rows
    ):
        raise TestCampaignCLIError("campaign_ledger_recipient_or_mode_mismatch")
    codes = [str(row.get("property_code") or "").strip() for row in rows]
    if len(codes) != len(set(codes)):
        raise TestCampaignCLIError("campaign_ledger_duplicate_property")
    if any(row.get("delivery_status") != "test_sent" or row.get("smtp_accepted") is not True for row in rows):
        raise TestCampaignCLIError("campaign_has_incomplete_previous_send")

    if cases == INITIAL_CASES:
        if rows:
            raise TestCampaignCLIError("initial_batch_already_registered")
        return
    if cases == ALL_CASES:
        if rows:
            raise TestCampaignCLIError("full_batch_already_registered")
        return
    if cases == REMAINING_CASES and set(codes) == INITIAL_PROPERTY_CODES:
        return
    raise TestCampaignCLIError("remaining_batch_requires_completed_A_B_D")


def _safe_prepared_summary(prepared: Sequence[PreparedTestMessage]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": item.case.case_id,
            "property_codes": [str(case.property_code) for case in (item.property_cases or (item.case,))],
            "operation": item.case.operation,
            "segment": item.case.evidence_segment,
            "cta": item.case.cta_type,
            "subject": item.subject,
            "report_url_present": bool(item.report_token),
            "action_url_present": bool(item.action_token),
        }
        for item in prepared
    ]


def run_test_batch(
    cases: tuple[str, ...] | str,
    *,
    test_mode: bool,
    dry_run: bool = False,
    db: Any = None,
    health_reader=delivery_unknown_count,
    require_delivery_unknown_baseline: bool = False,
) -> dict[str, Any]:
    selected = parse_cases(cases) if isinstance(cases, str) else cases
    if selected not in ALLOWED_BATCHES:
        raise TestCampaignCLIError("cases_must_be_A_B_D_C_E_or_ALL")
    if test_mode is not True or not test_mode_enabled():
        raise TestCampaignCLIError("test_mode_required_and_must_be_enabled_in_environment")
    if mass_send_enabled():
        raise TestCampaignCLIError("mass_send_must_remain_disabled")

    database = _database(db)
    _validate_phase(database, selected)
    delivery_before: int | None = None
    delivery_before_error: str | None = None
    try:
        delivery_before = health_reader()
    except TestCampaignCLIError as exc:
        delivery_before_error = str(exc)
    if require_delivery_unknown_baseline and delivery_before != DELIVERY_UNKNOWN_BASELINE:
        raise TestCampaignCLIError("delivery_unknown_baseline_not_confirmed")
    if delivery_before is not None and delivery_before > DELIVERY_UNKNOWN_BASELINE:
        raise TestCampaignCLIError("new_delivery_unknown_present_before_send")

    try:
        live_cases = build_owner_campaign_test_cases_live(database, case_ids=selected)
    except LiveTestCaseBuildError as exc:
        raise TestCampaignCLIError(f"live_case_build_failed:{exc}") from exc
    if tuple(case.case_id for case in live_cases) != selected:
        raise TestCampaignCLIError("live_case_batch_incomplete")
    prepared = prepare_test_messages(live_cases)
    if tuple(item.case.case_id for item in prepared) != selected:
        raise TestCampaignCLIError("rendered_case_batch_incomplete")

    common = {
        "campaign_id": TEST_CAMPAIGN_ID,
        "test_run_id": TEST_RUN_ID,
        "test_mode": True,
        "actual_recipient_email": TEST_RECIPIENT,
        "cc": [],
        "bcc": [],
        "case_ids": list(selected),
        "preflight": {
            "render_ok": True,
            "signed_urls_valid": True,
            "recipient_fixed": TEST_RECIPIENT,
            "owner_recipient_blocked": True,
            "live_price_mutation_blocked": True,
            "mass_send_enabled": False,
        },
        "cases": _safe_prepared_summary(prepared),
        "delivery_unknown_before": delivery_before,
        "delivery_monitor_error_before": delivery_before_error,
        "owner_emails_sent": 0,
        "live_property_price_changed": False,
    }
    if dry_run:
        return {**common, "status": "preflight_passed_no_send", "test_emails_sent": 0}

    results = send_test_messages(live_cases, db=database)
    if len(results) != len(selected) or any(
        row.get("status") != "sent_to_test_recipient"
        or row.get("smtp_accepted") is not True
        or row.get("recipient") != TEST_RECIPIENT
        for row in results
    ):
        raise TestCampaignCLIError("smtp_batch_incomplete_or_recipient_mismatch")

    try:
        delivery_after: int | None = health_reader()
        monitor_error = None
    except TestCampaignCLIError as exc:
        delivery_after = None
        monitor_error = str(exc)
    new_unknown = max(0, delivery_after - DELIVERY_UNKNOWN_BASELINE) if delivery_after is not None else None
    if monitor_error is not None:
        status = "sent_delivery_monitor_unavailable"
    elif new_unknown:
        status = "sent_delivery_monitor_review"
    else:
        status = "test_messages_sent"
    return {
        **common,
        "status": status,
        "test_emails_sent": len(results),
        "results": results,
        "delivery_unknown_after": delivery_after,
        "new_delivery_unknown": new_unknown,
        "delivery_monitor_error": monitor_error,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run only the fixed-recipient PROCASA owner-campaign E2E tests.")
    parser.add_argument("--cases", required=True, help="Exactly A,B,D; C,E after A,B,D; or the full A,B,C,D,E batch.")
    parser.add_argument("--test-mode", action="store_true", help="Required; environment test mode must also be enabled.")
    parser.add_argument("--dry-run", action="store_true", help="Render and validate live cases without ledger writes or SMTP.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_test_batch(args.cases, test_mode=args.test_mode, dry_run=args.dry_run)
    except Exception as exc:
        code = str(exc) if isinstance(exc, TestCampaignCLIError) else "runner_failed_closed"
        print(json.dumps({
            "status": "blocked",
            "error": code,
            "campaign_id": TEST_CAMPAIGN_ID,
            "test_mode": bool(args.test_mode),
            "actual_recipient_email": TEST_RECIPIENT,
            "owner_emails_sent": 0,
            "live_property_price_changed": False,
        }, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("status") in {
        "preflight_passed_no_send", "test_messages_sent", "sent_delivery_monitor_unavailable"
    } else 3


if __name__ == "__main__":
    raise SystemExit(main())
