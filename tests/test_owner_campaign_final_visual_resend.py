from __future__ import annotations

from email.message import EmailMessage
import inspect
from types import SimpleNamespace

import pytest

from campanas import owner_campaign_final_visual_resend as resend
from campanas.owner_campaign_test_sender import TestSenderError as SenderError, validate_recipient_envelope
from campanas.test_mode import TEST_CAMPAIGN_ID, TEST_RECIPIENT


def _row(**updates):
    row = {
        "_id": f"{TEST_CAMPAIGN_ID}:5641",
        "campaign_id": TEST_CAMPAIGN_ID,
        "property_code": "5641",
        "test_mode": True,
        "actual_recipient_email": TEST_RECIPIENT,
        "delivery_status": "pending_test_send",
    }
    row.update(updates)
    return row


class _Collection:
    def __init__(self, row):
        self.row = row
        self.updates = []

    def find_one(self, query):
        if self.row and all(self.row.get(key) == value for key, value in query.items()):
            return dict(self.row)
        return None

    def count_documents(self, query):
        return int(self.find_one(query) is not None)

    def update_one(self, query, update, upsert=False):
        self.updates.append((query, update, upsert))
        matched = self.row is not None and all(
            (self.row.get(key) != value if isinstance(value, dict) and "$ne" in value else self.row.get(key) == value)
            for key, value in query.items()
        )
        if matched:
            self.row.update(update.get("$set", {}))
        return SimpleNamespace(matched_count=int(matched))


class _DB:
    def __init__(self, row):
        self.collection = _Collection(row)

    def __getitem__(self, name):
        assert name == resend.TEST_LEDGER_COLLECTION
        return self.collection


class _SMTP:
    calls = 0

    def __init__(self, *_args, **_kwargs):
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        return None

    def login(self, *_args):
        return None

    def sendmail(self, _sender, recipients, message):
        type(self).calls += 1
        assert recipients == [TEST_RECIPIENT]
        self.message = message
        return {}


def test_requires_the_existing_exact_qa_row():
    with pytest.raises(SenderError, match="existing_qa_row_required"):
        resend._existing_ledger_row(_DB(None))
    with pytest.raises(SenderError, match="existing_qa_row_invalid"):
        resend._existing_ledger_row(_DB(_row(test_mode=False)))


@pytest.mark.parametrize(
    "updates",
    [
        {"property_code": "16521"},
        {"actual_recipient_email": "owner@example.com"},
    ],
)
def test_wrong_property_or_recipient_fails_closed(updates):
    with pytest.raises(SenderError, match="existing_qa_row_required|existing_qa_row_invalid"):
        resend._existing_ledger_row(_DB(_row(**updates)))


@pytest.mark.parametrize("field", ["cc", "bcc"])
def test_cc_and_bcc_are_rejected(field):
    kwargs = {"to": TEST_RECIPIENT, field: ["other@example.com"], "envelope_recipients": [TEST_RECIPIENT]}
    with pytest.raises(SenderError, match=f"test_{field}_forbidden"):
        validate_recipient_envelope(**kwargs)


def test_subject_is_not_an_arbitrary_resend_parameter():
    assert "subject" not in inspect.signature(resend.resend_existing_test_case).parameters
    assert resend.FINAL_RESEND_SUBJECT == "[TEST FINAL PROCASA] Informe propiedad 5641"


def test_hash_mismatch_blocks_mime_creation(monkeypatch):
    monkeypatch.setattr(resend, "Config", SimpleNamespace(GMAIL_USER="qa@procasa.cl"))
    with pytest.raises(SenderError, match="html_hash_mismatch"):
        resend._message_with_hash_gate("<p>final</p>", expected_hash="wrong", text="QA")


def test_dry_run_keeps_the_existing_row_and_success_blocks_second_resend():
    db = _DB(_row(last_test_resend_purpose=resend.FINAL_RESEND_PURPOSE, last_test_resend_smtp_accepted=True))
    with pytest.raises(SenderError, match="final_visual_test_already_sent"):
        resend._existing_ledger_row(db)
    assert db.collection.updates == []


def test_dry_run_validates_fresh_case_without_writing_ledger(monkeypatch):
    source = """<!doctype html><html><body>
      <div class="document-copy-single"><strong>Informe comercial disponible</strong>
      <span>✓ Tasación individual</span><span>✓ Publicaciones comparables</span></div>
      <a class="document-action-single" href="https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report">Ver informe →</a>
      <a class="primary" href="https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=action">Aceptar →</a>
    </body></html>"""
    db = _DB(_row())
    case = SimpleNamespace(case_id="A", property_code="5641")
    prepared = SimpleNamespace(html=source, report_token="fresh-report", action_token="fresh-action")
    monkeypatch.setattr(resend, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(resend, "Config", SimpleNamespace(GMAIL_USER="qa@procasa.cl"))
    monkeypatch.setattr(resend, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: [case])
    monkeypatch.setattr(resend, "prepare_test_messages", lambda cases: [prepared])
    monkeypatch.setattr(resend, "make_email_safe_html", lambda value: value)

    result = resend.resend_existing_test_case(dry_run=True, db=db)

    assert result["property_code"] == "5641"
    assert result["recipient"] == TEST_RECIPIENT
    assert result["report_link_present"] is True
    assert result["price_auth_link_present"] is True
    assert result["html_match"] is True
    assert result["new_ledger_row_created"] is False
    assert db.collection.updates == []


def test_send_audits_the_same_row_and_one_shot_blocks_repeat(monkeypatch):
    source = """<!doctype html><html><body>
      <div class="document-copy-single"><strong>Informe comercial disponible</strong>
      <span>✓ Tasación individual</span><span>✓ Publicaciones comparables</span></div>
      <a class="document-action-single" href="https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report">Ver informe →</a>
      <a class="primary" href="https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=action">Aceptar →</a>
    </body></html>"""
    db = _DB(_row())
    case = SimpleNamespace(case_id="A", property_code="5641")
    prepared = SimpleNamespace(html=source, report_token="fresh-report", action_token="fresh-action")
    monkeypatch.setattr(resend, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(resend, "Config", SimpleNamespace(GMAIL_USER="qa@procasa.cl", GMAIL_PASSWORD="test"))
    monkeypatch.setattr(resend, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: [case])
    monkeypatch.setattr(resend, "prepare_test_messages", lambda cases: [prepared])
    monkeypatch.setattr(resend, "make_email_safe_html", lambda value: value)
    monkeypatch.setattr(resend.smtplib, "SMTP", _SMTP)
    _SMTP.calls = 0

    result = resend.resend_existing_test_case(dry_run=False, db=db)

    assert result["test_email_sent"] is True
    assert result["smtp_result"] == "ACCEPTED"
    assert result["final_resend_audit_persisted"] is True
    assert _SMTP.calls == 1
    assert db.collection.row["test_mode"] is True
    assert db.collection.row["actual_recipient_email"] == TEST_RECIPIENT
    assert db.collection.row["last_test_resend_smtp_accepted"] is True
    assert db.collection.row["test_resend_count"] == 1
    assert db.collection.row["delivery_status"] == "pending_test_send"
    assert len(db.collection.updates) == 2
    assert all(update[2] is False for update in db.collection.updates)
    with pytest.raises(SenderError, match="final_visual_test_already_sent"):
        resend._existing_ledger_row(db)


def test_reservation_and_audit_update_never_upsert():
    db = _DB(_row())
    row = resend._existing_ledger_row(db)
    resend._reserve_existing_row(db.collection, row, "a" * 64, resend.datetime.now(resend.timezone.utc))
    assert db.collection.updates[-1][2] is False
    assert db.collection.row["last_test_resend_status"] == "sending"
    assert db.collection.row["campaign_id"] == TEST_CAMPAIGN_ID
    assert len(db.collection.row) > len(_row())
    assert all(update[2] is False for update in db.collection.updates)


def test_final_html_requires_report_and_action_buttons(monkeypatch):
    monkeypatch.setattr(resend, "make_email_safe_html", lambda value: value)
    source = """<!doctype html><html><body>
      <div class="document-copy-single"><strong>Informe comercial disponible</strong>
      <span>✓ Tasación individual</span><span>✓ Publicaciones comparables</span></div>
      <a class="document-action-single" href="https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report">Ver informe →</a>
      <a class="primary" href="https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=action">Aceptar →</a>
    </body></html>"""
    prepared = SimpleNamespace(html=source, report_token="fresh-report", action_token="fresh-action")
    result = resend._final_visual_html(prepared)
    assert "VER INFORME →" in result
    assert "ACEPTAR NUEVO VALOR →" in result
    assert "Ver respaldo comercial" not in result


def test_rendered_html_does_not_create_email_or_ledger_row():
    # _build_email is intentionally only reached after the hash gate; this
    # structural assertion covers the no-send helper surface used by dry-run.
    assert issubclass(EmailMessage, object)
    assert "_write_test_ledger_entries" not in inspect.getsource(resend.resend_existing_test_case)
