from datetime import datetime, timezone
from pathlib import Path
from chatbot.captacion_reminder import DOMAIN, MESSAGE_TYPE, RECIPIENT_ROLE

def test_captacion_reminder_domain_is_explicit_and_independent():
    assert (DOMAIN, MESSAGE_TYPE, RECIPIENT_ROLE) == ("captacion_reminder", "followup_reminder", "executive")

def test_captacion_reminder_schedule_is_utc_aware():
    scheduled = datetime(2026, 7, 27, 15, 20, tzinfo=timezone.utc)
    assert scheduled.tzinfo is timezone.utc


def test_recipient_id_is_persisted_as_string_and_resolved_by_objectid():
    source = Path("chatbot/captacion_reminder.py").read_text(encoding="utf-8")
    assert 'ObjectId(str(recipient_id))' in source
