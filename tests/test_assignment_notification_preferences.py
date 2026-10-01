from datetime import datetime, timezone

from chatbot.crm_delivery import executive_wants_immediate_assignment_notification
from chatbot.crm_metrics import calculate_sla
from chatbot.constants import CHILE_TZ


class _Users:
    def __init__(self, users):
        self.users = users

    def find_one(self, query, projection=None):
        for user in self.users:
            if all(user.get(key) == value for key, value in query.items()):
                return user
        return None


class _DB:
    def __init__(self, users):
        self.users = _Users(users)

    def __getitem__(self, name):
        assert name == "usuarios"
        return self.users


def test_preference_defaults_off_and_is_resolved_by_id_or_exact_name():
    db = _DB([
        {"_id": "mariela-id", "nombre": "Mariela Arriagada",
         "notification_preferences": {"assignment_immediate_outside_business_hours": True}},
        {"_id": "other-id", "nombre": "Otra Ejecutiva"},
        {"_id": "null-id", "nombre": "Null", "notification_preferences": None},
        {"_id": "false-id", "nombre": "False", "notification_preferences": {
            "assignment_immediate_outside_business_hours": False,
        }},
    ])

    assert executive_wants_immediate_assignment_notification(db, "mariela-id") is True
    assert executive_wants_immediate_assignment_notification(db, "Mariela Arriagada") is True
    assert executive_wants_immediate_assignment_notification(db, "other-id") is False
    assert executive_wants_immediate_assignment_notification(db, "null-id") is False
    assert executive_wants_immediate_assignment_notification(db, "false-id") is False
    assert executive_wants_immediate_assignment_notification(db, "missing") is False


def test_sunday_after_hours_notice_does_not_change_sla_calculation():
    # Sunday 22:30 in America/Santiago; the notification preference is not an
    # input to calculate_sla and cannot satisfy or restart the SLA clock.
    assigned = CHILE_TZ.localize(datetime(2026, 9, 27, 22, 30)).astimezone(timezone.utc)
    now = CHILE_TZ.localize(datetime(2026, 9, 28, 10, 0)).astimezone(timezone.utc)
    expected = calculate_sla(assigned_at=assigned, now=now, temperature="HOT")
    db = _DB([{"_id": "mariela-id", "nombre": "Mariela Arriagada",
               "notification_preferences": {"assignment_immediate_outside_business_hours": True}}])

    assert executive_wants_immediate_assignment_notification(db, "mariela-id") is True
    assert calculate_sla(assigned_at=assigned, now=now, temperature="HOT") == expected
