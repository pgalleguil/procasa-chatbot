from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "business_calendar", ROOT / "chatbot" / "business_calendar.py"
)
calendar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calendar)
CHILE = calendar.CHILE


def local(year, month, day, hour, minute=0):
    return CHILE.localize(datetime(year, month, day, hour, minute), is_dst=None)


def test_sunday_schedules_monday_09_chile_13_utc():
    due = calendar.next_business_slot_utc(local(2026, 7, 26, 12))
    assert due == datetime(2026, 7, 27, 13, tzinfo=timezone.utc)
    assert due.astimezone(CHILE).strftime("%Y-%m-%d %H:%M") == "2026-07-27 09:00"


def test_saturday_and_friday_after_close_schedule_monday_open():
    expected = datetime(2026, 7, 27, 13, tzinfo=timezone.utc)
    assert calendar.next_business_slot_utc(local(2026, 7, 25, 10)) == expected
    assert calendar.next_business_slot_utc(local(2026, 7, 24, 19, 1)) == expected


def test_monday_0859_opens_at_0900_and_0900_is_immediate():
    assert calendar.next_business_slot_utc(
        local(2026, 7, 27, 8, 59)
    ) == datetime(2026, 7, 27, 13, tzinfo=timezone.utc)
    at_open = local(2026, 7, 27, 9)
    assert calendar.next_business_slot_utc(at_open) == at_open.astimezone(timezone.utc)
    assert calendar.is_business_time(at_open)


def test_dst_is_resolved_by_america_santiago_not_fixed_offset():
    winter = calendar.next_business_slot_utc(local(2026, 7, 26, 12))
    summer = calendar.next_business_slot_utc(local(2026, 12, 27, 12))
    assert winter.hour == 13
    assert summer.hour == 12
    assert winter.astimezone(CHILE).hour == summer.astimezone(CHILE).hour == 9


def test_naive_datetimes_are_rejected():
    try:
        calendar.next_business_slot_utc(datetime(2026, 7, 26, 12))
    except ValueError:
        pass
    else:
        raise AssertionError("naive datetime was accepted")


def test_weekend_digest_uses_business_open_without_extra_ten_minutes():
    source = (ROOT / "chatbot" / "crm_non_hot_digest.py").read_text(encoding="utf-8")
    helper = source[source.index("def _window_due_at"):source.index(
        "def _business_period_label"
    )]
    assert "if not is_business_hours(started_at)" in helper
    assert "return get_next_business_slot(started_at)" in helper
    assert helper.index("return get_next_business_slot") < helper.index("timedelta(minutes=window)")


def test_hot_claim_requires_eligibility_and_no_previous_delivery():
    source = (ROOT / "chatbot" / "crm_hot_delivery.py").read_text(encoding="utf-8")
    worker = source[source.index("def process_one_hot_sync"):source.index(
        "async def process_one_hot"
    )]
    assert '"notification_eligible": True' in worker
    assert '"provider_message_id": {"$exists": False}' in worker
    assert '"actually_delivered": {"$ne": True}' in worker
    assert '"cycle_reason": {"$in"' in worker


def test_reconciliation_is_identity_based_and_never_calls_provider():
    source = (ROOT / "scripts" / "reconcile_weekend_notifications_20260726.py").read_text(
        encoding="utf-8"
    )
    assert '"source_event_id": event["id"]' in source
    assert '"assignment_cycle_id": cycle["assignment_cycle_id"]' in source
    assert "send_whatsapp" not in source
    assert "WASender" not in source
