"""CRM chat chronology must display explicit instants in Santiago time."""

from datetime import datetime

from api_crm import process_chat_timeline


def test_explicit_utc_and_chilean_offsets_render_identically():
    rows = process_chat_timeline([
        {"role": "user", "timestamp": "2026-11-18T12:30:00+00:00"},
        {"role": "assistant", "timestamp": "2026-11-18T12:30:00Z"},
        {"role": "agent", "timestamp": "2026-11-18T09:30:00-03:00"},
    ])
    assert len(rows) == 3
    assert all(x["timestamp"].strftime("%Y-%m-%d %H:%M") == "2026-11-18 09:30" for x in rows)
    assert all(x["timestamp"].utcoffset().total_seconds() == -10800 for x in rows)


def test_legacy_unzoned_string_is_not_silently_interpreted_as_utc():
    row = process_chat_timeline([{"timestamp": "2026-11-18T09:30:00.000"}])[0]
    assert row["timestamp"].hour == 9
    assert row["timestamp"].tzinfo is None


def test_bson_naive_datetime_is_utc_for_crm_display():
    row = process_chat_timeline([{"timestamp": datetime(2026, 11, 18, 12, 30)}])[0]
    assert row["timestamp"].strftime("%H:%M") == "09:30"
    assert row["timestamp"].tzinfo is not None
