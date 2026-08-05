"""Tests for captacion_sla_release_loop scheduling logic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _is_due(now_local, weekday=6, hour=4):
    return (now_local.weekday() == weekday
            and now_local.hour == hour
            and 0 <= now_local.minute < 10)


from datetime import datetime
import pytz

CHILE = pytz.timezone("Chile/Continental")


def _chile(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=CHILE)


def test_due_sunday_4am():
    assert _is_due(_chile(2026, 8, 9, 4, 5)) is True  # domingo


def test_not_due_sunday_3am():
    assert _is_due(_chile(2026, 8, 9, 3, 5)) is False


def test_not_due_sunday_5am():
    assert _is_due(_chile(2026, 8, 9, 5, 5)) is False


def test_not_due_monday_4am():
    assert _is_due(_chile(2026, 8, 10, 4, 5)) is False


def test_not_due_sunday_4_15():
    assert _is_due(_chile(2026, 8, 9, 4, 15)) is False


def test_not_due_saturday_4am():
    assert _is_due(_chile(2026, 8, 8, 4, 5)) is False


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
