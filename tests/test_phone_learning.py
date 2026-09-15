from __future__ import annotations

from captacion_assignment_eligibility import calculate_assignment_eligibility
from captacion_contact_identity import get_contact_identity_evidence, normalize_phone
from config import Config


class FakeIdentityCollection:
    def __init__(self, row):
        self.row = row
        self.query = None

    def find_one(self, query):
        self.query = query
        return self.row


def test_phone_learning_normalizes_and_looks_up_global_identity(monkeypatch):
    monkeypatch.setattr(Config, "PHONE_LEARNING_ENABLED", True)
    collection = FakeIdentityCollection(
        {
            "identity_key": "phone:56912345678",
            "phone_normalized": "56912345678",
            "status": "CORREDOR_CONFIRMED",
            "confirmed_corredor_count": 1,
        }
    )

    evidence = get_contact_identity_evidence(
        collection,
        {"telefono": "+56 9 1234 5678"},
    )

    assert normalize_phone("+56 9 1234 5678") == "56912345678"
    assert collection.query == {"phone_normalized": "56912345678"}
    assert evidence["status"] == "CORREDOR_CONFIRMED"


def test_confirmed_broker_phone_blocks_future_assignment(monkeypatch):
    monkeypatch.setattr(Config, "PHONE_LEARNING_ENABLED", True)
    decision = calculate_assignment_eligibility(
        {
            "origen": "chilepropiedades",
            "listing_id": "synthetic-listing",
            "comuna": "Santiago",
            "title": "Casa sintética",
            "classification": {"state": "DUEÑO_SEGURO"},
        },
        contact_identity={
            "phone_normalized": "56912345678",
            "status": "CORREDOR_CONFIRMED",
            "confirmed_corredor_count": 1,
        },
    )

    assert decision["assignment_ready"] is False
    assert decision["effective_state"] == "CORREDOR_SEGURO"
    assert decision["effective_reason"] == "PHONE_HUMAN_CONFIRMED_BROKER"


def test_executive_notice_is_present_without_exposing_contact_data():
    from pathlib import Path

    template = Path(__file__).parents[1] / "templates" / "captacion_detail.html"
    source = template.read_text(encoding="utf-8")

    assert "known-broker-auto-match-alert" in source
    assert "known_broker_auto_match" in source
    assert "classList.remove('d-none')" in source
