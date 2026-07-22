import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from chatbot.document_message_guard import (
    build_phone_suffix_regex,
    find_active_document_guard,
    normalize_phone_digits,
)


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents

    def find_one(self, query, projection=None):
        for document in self.documents:
            if not query["phone"].search(document.get("phone", "")):
                continue
            if document.get("status") not in query["status"]["$in"]:
                continue
            expiry = (document.get("security") or {}).get("token_expiry")
            if expiry and expiry > query["security.token_expiry"]["$gt"]:
                return document
        return None


def fake_db(contracts=None, visitas=None):
    return {
        "contracts": FakeCollection(contracts or []),
        "visitas": FakeCollection(visitas or []),
    }


def test_phone_normalization_and_flexible_suffix():
    assert normalize_phone_digits("9 1234-5678") == "56912345678"
    pattern = build_phone_suffix_regex("+56 9 1234 5678")
    assert isinstance(pattern, re.Pattern)
    assert pattern.search("+56 9 1234-5678")
    assert not pattern.search("+56 9 8765-4321")


def test_active_contract_blocks_until_its_real_expiry():
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    db = fake_db(contracts=[{
        "phone": "+56 9 1234-5678",
        "contract_code": "C-1",
        "status": "sent",
        "security": {"token_expiry": now + timedelta(hours=24)},
    }])

    guard = find_active_document_guard(db, "56912345678", now)

    assert guard["document_type"] == "contract"
    assert guard["document_code"] == "C-1"


def test_paula_case_blocks_only_her_exact_phone_during_24_hours():
    sent_at = datetime(2026, 7, 21, 19, 40, tzinfo=timezone.utc)
    client_message_at = datetime(2026, 7, 22, 4, 56, tzinfo=timezone.utc)
    db = fake_db(contracts=[{
        "phone": "+56988085735",
        "contract_code": "PROC-2026-5D79",
        "status": "sent",
        "security": {"token_expiry": sent_at + timedelta(hours=24)},
    }])

    assert find_active_document_guard(db, "+56 9 8808 5735", client_message_at)
    assert find_active_document_guard(db, "+56 9 8808 5736", client_message_at) is None


def test_active_visita_also_blocks():
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    db = fake_db(visitas=[{
        "phone": "56912345678",
        "visita_code": "V-1",
        "status": "otp_requested",
        "security": {"token_expiry": now + timedelta(minutes=1)},
    }])

    assert find_active_document_guard(db, "+56912345678", now)["document_code"] == "V-1"


def test_expired_or_not_sent_document_does_not_block():
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    documents = [
        {
            "phone": "56912345678",
            "contract_code": "expired",
            "status": "sent",
            "security": {"token_expiry": now - timedelta(seconds=1)},
        },
        {
            "phone": "56912345678",
            "contract_code": "draft",
            "status": "created",
            "security": {"token_expiry": now + timedelta(hours=24)},
        },
    ]

    assert find_active_document_guard(fake_db(contracts=documents), "56912345678", now) is None


def test_webhook_cannot_disconnect_document_guard_again():
    webhook_path = Path(__file__).resolve().parents[1] / "webhook.py"
    tree = ast.parse(webhook_path.read_text(encoding="utf-8-sig"))

    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "chatbot.document_message_guard"
        and any(alias.name == "find_active_document_guard" for alias in node.names)
        for node in ast.walk(tree)
    )
    guard_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "find_active_document_guard"
    ]

    assert imported
    # Recepción, post-debounce y antes de enviar una respuesta tardía.
    assert len(guard_calls) >= 3
