from datetime import datetime
from unittest.mock import patch

from chatbot.constants import CHILE_TZ
from chatbot import storage


def matches(doc, query):
    for key, expected in query.items():
        value = doc
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(expected, dict) and "$in" in expected:
            if value not in expected["$in"]: return False
        elif value != expected: return False
    return True


class Collection:
    def __init__(self, docs=None): self.docs = list(docs or [])
    def find_one(self, query, *args, **kwargs): return next((d for d in self.docs if matches(d, query)), None)
    def find(self, query, projection=None): return [d for d in self.docs if matches(d, query)]
    def insert_one(self, doc): self.docs.append(dict(doc))
    def update_one(self, query, update):
        doc = self.find_one(query)
        if doc: doc.update(update.get("$set", {}))


def db_with(**collections):
    return {name: Collection(docs) for name, docs in collections.items()}


def test_sent_notification_is_permanent_and_never_requeued():
    db = db_with(pending_notifications=[{
        "notification_key": "phone:5691|property:p1", "status": "sent",
        "lead_data": {"phone": "+5691", "property_code": "P1"},
    }])
    with patch("chatbot.storage.get_db", return_value=db), \
         patch("chatbot.storage.Config.LEAD_HOT_RECONCILIATION_ENABLED", True):
        storage.save_pending_notification({"phone": "+5691", "property_code": "P1"})
    assert len(db["pending_notifications"].docs) == 1
    assert db["pending_notifications"].docs[0]["status"] == "sent"


def test_post_cutover_hot_lead_missing_from_queue_is_recovered_once():
    lead = {
        "_id": "lead-1", "phone": "+5691",
        "created_at": CHILE_TZ.localize(datetime(2026, 7, 20, 10)),
        "ejecutivo_asignado": "Activa", "lead_temperature_effective": "HOT",
        "pipeline_stage": "NEW", "prospecto": {"codigo": "P1"},
    }
    db = db_with(
        leads=[lead], usuarios=[{"_id": "user-activa", "nombre": "Activa", "is_active": True, "telefono": "+5692"}],
        pending_notifications=[], crm_notifications_v1=[], crm_assignment_cycles=[],
        crm_events=[], crm_management_results=[],
    )
    storage._HOT_RECONCILIATION_LAST_RUN = None
    with patch("chatbot.storage.get_db", return_value=db), \
         patch("chatbot.storage.Config.LEAD_HOT_RECONCILIATION_ENABLED", True), \
         patch("chatbot.storage.Config.LEAD_HOT_NOTIFICATIONS_ENABLED", True):
        storage._reconcile_missing_hot_notifications(db)
        storage._HOT_RECONCILIATION_LAST_RUN = None
        storage._reconcile_missing_hot_notifications(db)
    # Reconciliation uses canonical path (crm_notifications_v1), not pending_notifications
    assert len(db["crm_notifications_v1"].docs) == 1
    assert db["crm_notifications_v1"].docs[0]["notification_type"] == "lead_assignment_hot"
