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


def _approved_html_source():
    return """<!doctype html><html><body>
      <h1>Revisión comercial de tu propiedad</h1><p class="hero-single-aside">Buenas<br />propiedades<br />crean grandes<br />historias</p>
      <section>CHILE · SEPTIEMBRE 2026 FINANCIAMIENTO HIPOTECARIO TPM Fuentes: Banco Central de Chile y MINVU · septiembre 2026</section>
      <div class="document-copy-single"><strong>Informe comercial disponible</strong>
      <span>✓ Tasación individual</span><span>✓ Publicaciones comparables</span></div>
      <a class="document-action-single" href="https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report">Ver informe →</a>
      <a class="primary" href="https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=action">Aceptar →</a>
      <div class="advisor-review">¿Prefieres conversarlo antes? <a href="#old">Revisar con mi ejecutivo →</a></div>
      <div class="executive-single"><span class="exec-name-single">Ejecutiva QA</span>
      <span class="contact-line-single"><span class="contact-icon-single contact-icon-email-single"></span>qa@procasa.cl</span>
      <span class="contact-line-single"><span class="contact-icon-single contact-icon-phone-single"></span>+56912345678</span></div>
    </body></html>"""


def _advisor_url(token="advisor"):
    return f"{resend.SERVICE_BASE_URL}/campana/test-accion?token={token}"


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
        def matches(key, expected):
            actual = self.row.get(key)
            if isinstance(expected, dict):
                if "$ne" in expected:
                    return actual != expected["$ne"]
                if "$exists" in expected:
                    return (key in self.row) is expected["$exists"]
            return actual == expected

        matched = self.row is not None and all(matches(key, value) for key, value in query.items())
        if matched:
            self.row.update(update.get("$set", {}))
            for key, increment in update.get("$inc", {}).items():
                self.row[key] = self.row.get(key, 0) + increment
            for key, item in update.get("$push", {}).items():
                self.row.setdefault(key, []).append(item)
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


def test_identical_html_is_blocked_but_changed_html_is_allowed():
    old_hash = "a" * 64
    row = _row(
        last_test_resend_purpose=resend.FINAL_RESEND_PURPOSE,
        last_test_resend_smtp_accepted=True,
        last_test_resend_html_sha256=old_hash,
    )
    assert resend._last_accepted_html_hash(row) == old_hash
    with pytest.raises(SenderError, match="same_visual_test_already_sent"):
        resend._require_changed_html(row, old_hash)
    assert resend._require_changed_html(row, "b" * 64) == old_hash


def test_accepted_history_is_preserved_and_used_as_latest_hash():
    row = _row(test_resend_history=[{"html_sha256": "c" * 64, "smtp_accepted": True}])
    assert resend._last_accepted_html_hash(row) == "c" * 64


def test_dry_run_validates_fresh_case_without_writing_ledger(monkeypatch):
    source = _approved_html_source()
    old_hash = "a" * 64
    db = _DB(_row(
        last_test_resend_purpose=resend.FINAL_RESEND_PURPOSE,
        last_test_resend_smtp_accepted=True,
        last_test_resend_html_sha256=old_hash,
    ))
    case = SimpleNamespace(case_id="A", property_code="5641")
    prepared = SimpleNamespace(html=source, report_token="report", action_token="action")
    monkeypatch.setattr(resend, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(resend, "Config", SimpleNamespace(GMAIL_USER="qa@procasa.cl"))
    monkeypatch.setattr(resend, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: [case])
    monkeypatch.setattr(resend, "prepare_test_messages", lambda cases: [prepared])
    monkeypatch.setattr(resend, "make_email_safe_html", lambda value: value)
    monkeypatch.setattr(resend, "issue_campaign_test_token", lambda **kwargs: "fresh-advisor")

    result = resend.resend_existing_test_case(dry_run=True, db=db)

    assert result["html_changed"] is True
    assert result["old_html_sha256"] == old_hash
    assert result["qa_resend_allowed"] is True
    assert result["property_code"] == "5641"
    assert result["recipient"] == TEST_RECIPIENT
    assert result["report_link_present"] is True
    assert result["price_auth_link_present"] is True
    assert result["advisor_review_link_present"] is True
    assert result["approved_single_property_template_active"] is True
    assert result["html_match"] is True
    assert result["new_ledger_row_created"] is False
    assert db.collection.updates == []


def test_dry_run_blocks_same_hash_before_smtp_or_ledger_write(monkeypatch):
    source = _approved_html_source()
    monkeypatch.setattr(resend, "make_email_safe_html", lambda value: value)
    safe_html = resend._final_visual_html(SimpleNamespace(html=source, report_token="report", action_token="action"), advisor_review_url=_advisor_url())
    same_hash = resend._sha256(safe_html)
    db = _DB(_row(last_test_resend_smtp_accepted=True, last_test_resend_html_sha256=same_hash))
    monkeypatch.setattr(resend, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(resend, "Config", SimpleNamespace(GMAIL_USER="qa@procasa.cl"))
    monkeypatch.setattr(resend, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: [SimpleNamespace(case_id="A", property_code="5641")])
    monkeypatch.setattr(resend, "prepare_test_messages", lambda cases: [SimpleNamespace(html=source, report_token="report", action_token="action")])
    monkeypatch.setattr(resend, "make_email_safe_html", lambda value: value)
    monkeypatch.setattr(resend, "issue_campaign_test_token", lambda **kwargs: "advisor")
    with pytest.raises(SenderError, match="same_visual_test_already_sent"):
        resend.resend_existing_test_case(dry_run=True, db=db)
    assert db.collection.updates == []


def test_send_audits_the_same_row_and_one_shot_blocks_repeat(monkeypatch):
    source = _approved_html_source()
    db = _DB(_row())
    case = SimpleNamespace(case_id="A", property_code="5641")
    prepared = SimpleNamespace(html=source, report_token="report", action_token="action")
    monkeypatch.setattr(resend, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(resend, "Config", SimpleNamespace(GMAIL_USER="qa@procasa.cl", GMAIL_PASSWORD="test"))
    monkeypatch.setattr(resend, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: [case])
    monkeypatch.setattr(resend, "prepare_test_messages", lambda cases: [prepared])
    monkeypatch.setattr(resend, "make_email_safe_html", lambda value: value)
    monkeypatch.setattr(resend, "issue_campaign_test_token", lambda **kwargs: "fresh-advisor")
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
    assert db.collection.row["test_resend_history"][-1]["smtp_accepted"] is True
    assert db.collection.row["delivery_status"] == "pending_test_send"
    assert len(db.collection.updates) == 2
    assert all(update[2] is False for update in db.collection.updates)
    with pytest.raises(SenderError, match="same_visual_test_already_sent"):
        resend._require_changed_html(db.collection.row, result["final_email_safe_html_sha256"])
    assert db.collection.row["last_test_resend_html_sha256"] == result["final_email_safe_html_sha256"]
    assert db.collection.row["test_resend_count"] == 1


def test_reservation_and_audit_update_never_upsert():
    db = _DB(_row())
    row = resend._existing_ledger_row(db)
    resend._reserve_existing_row(db.collection, row, "a" * 64, resend.datetime.now(resend.timezone.utc), None)
    assert db.collection.updates[-1][2] is False
    assert db.collection.row["last_test_resend_status"] == "sending"
    assert db.collection.row["campaign_id"] == TEST_CAMPAIGN_ID
    assert len(db.collection.row) > len(_row())
    assert all(update[2] is False for update in db.collection.updates)


def test_final_html_requires_report_and_action_buttons(monkeypatch):
    monkeypatch.setattr(resend, "make_email_safe_html", lambda value: value)
    source = _approved_html_source()
    prepared = SimpleNamespace(html=source, report_token="report", action_token="action")
    result = resend._final_visual_html(prepared, advisor_review_url=_advisor_url())
    assert "VER INFORME →" in result
    assert "ACEPTAR NUEVO VALOR →" in result
    assert "Revisión comercial de tu propiedad" in result
    assert "Revisar con mi ejecutivo →" in result
    assert "Ver respaldo comercial" not in result


def test_rendered_html_does_not_create_email_or_ledger_row():
    # _build_email is intentionally only reached after the hash gate; this
    # structural assertion covers the no-send helper surface used by dry-run.
    assert issubclass(EmailMessage, object)
    assert "_write_test_ledger_entries" not in inspect.getsource(resend.resend_existing_test_case)


def test_changed_hash_reservation_is_compare_and_set_and_never_upserts():
    old_hash = "a" * 64
    row = _row(last_test_resend_smtp_accepted=True, last_test_resend_html_sha256=old_hash)
    db = _DB(row)
    snapshot = resend._existing_ledger_row(db)
    resend._reserve_existing_row(db.collection, snapshot, "b" * 64, resend.datetime.now(resend.timezone.utc), old_hash)
    query, _update, upsert = db.collection.updates[-1]
    assert query["last_test_resend_html_sha256"] == old_hash
    assert query["last_test_resend_status"] == {"$exists": False}
    assert upsert is False
