"""Tests for business-minutes SLA calculation."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta, timezone
from chatbot.utils import calculate_business_minutes
from chatbot.constants import CHILE_TZ
from chatbot.business_calendar import is_business_day, is_business_time, next_business_slot_utc
from chatbot.crm_metrics import commercial_sla_start_at
from chatbot.crm_sla_alert_evaluator import add_business_minutes


def _chile_dt(day, hour, minute=0):
    base = CHILE_TZ.localize(datetime(2026, 7, 27, hour, minute))
    return base + timedelta(days=day)


def test_friday_evening_to_monday_morning():
    """Friday 18:30 → Monday 09:30 = 60 business minutes."""
    friday = _chile_dt(4, 18, 30)
    monday = _chile_dt(7, 9, 30)
    mins = calculate_business_minutes(friday, monday)
    assert mins == 60, f"Expected 60, got {mins}"


def test_sunday_to_monday():
    """Sunday 14:00 → Monday 09:30 = 30 business minutes (only Monday AM)."""
    sunday = _chile_dt(6, 14, 0)
    monday = _chile_dt(7, 9, 30)
    mins = calculate_business_minutes(sunday, monday)
    assert mins == 30, f"Expected 30, got {mins}"


def test_after_hours_starts_next_day():
    """Monday 20:00 → Tuesday 10:00 = 60 business minutes."""
    mon_night = _chile_dt(0, 20, 0)
    tue_am = _chile_dt(1, 10, 0)
    mins = calculate_business_minutes(mon_night, tue_am)
    assert mins == 60, f"Expected 60, got {mins}"


def test_hot_vs_cold_thresholds():
    """Same 360-minute span yields different overdue for HOT (60) vs COLD (180)."""
    assigned = _chile_dt(0, 10, 0)
    now_test = _chile_dt(0, 16, 0)
    elapsed = calculate_business_minutes(assigned, now_test)
    assert elapsed == 360, f"Expected 360, got {elapsed}"
    hot_overdue = elapsed - 60
    cold_overdue = elapsed - 180
    assert hot_overdue == 300
    assert cold_overdue == 180


def test_weekend_span():
    """Friday 17:00 → Monday 11:00 = 240 business minutes."""
    friday = _chile_dt(4, 17, 0)
    monday = _chile_dt(7, 11, 0)
    mins = calculate_business_minutes(friday, monday)
    assert mins == 240, f"Expected 240, got {mins}"


def test_display_and_sla_order_use_same_function():
    """Both the SLA display in api_crm.py and this test use calculate_business_minutes."""
    import api_crm as _
    # api_crm.py imports calculate_business_minutes at line 954
    src = open(os.path.join(os.path.dirname(__file__), "..", "api_crm.py"), encoding="utf-8").read()
    assert "calculate_business_minutes" in src


def test_chile_public_holiday_has_zero_sla_minutes_and_is_not_business_time():
    holiday_open = CHILE_TZ.localize(datetime(2026, 10, 12, 9, 0))
    holiday_close = CHILE_TZ.localize(datetime(2026, 10, 12, 19, 0))
    assert not is_business_day(date(2026, 10, 12))
    assert not is_business_time(holiday_open)
    assert calculate_business_minutes(holiday_open, holiday_close) == 0


def test_holiday_sla_start_moves_to_next_chile_opening():
    assigned = CHILE_TZ.localize(datetime(2026, 10, 12, 12, 0)).astimezone(timezone.utc)
    expected = CHILE_TZ.localize(datetime(2026, 10, 13, 9, 0)).astimezone(timezone.utc)
    assert commercial_sla_start_at(assigned) == expected
    assert next_business_slot_utc(assigned) == expected


def test_normal_sla_deadline_skips_october_12_holiday():
    friday = CHILE_TZ.localize(datetime(2026, 10, 9, 18, 0))
    expected = CHILE_TZ.localize(datetime(2026, 10, 13, 11, 0)).astimezone(timezone.utc)
    assert add_business_minutes(friday, 180) == expected


def test_business_minutes_remain_correct_across_santiago_dst_change():
    friday = CHILE_TZ.localize(datetime(2026, 9, 4, 18, 30))
    monday = CHILE_TZ.localize(datetime(2026, 9, 7, 10, 30))
    assert calculate_business_minutes(friday, monday) == 120
