import smtplib

import pytest

from campanas import owner_campaign_live_sender as sender
from config import Config


class _LedgerCollection:
    def __init__(self):
        self.status = "READY"
        self.finished = []

    def find_one(self, *_args, **_kwargs):
        return {"send_status": self.status}


class _DB:
    def __init__(self):
        self.ledger = _LedgerCollection()

    def __getitem__(self, _name):
        return self.ledger


class _SMTP:
    def __init__(self, *, data_result=(250, b"accepted"), data_error=None, quit_error=None):
        self.data_result = data_result
        self.data_error = data_error
        self.quit_error = quit_error
        self.calls = []
        self.sock = self

    def settimeout(self, timeout):
        self.calls.append(("send_timeout", timeout))

    def starttls(self):
        self.calls.append("starttls")

    def login(self, *_args):
        self.calls.append("login")

    def mail(self, *_args):
        self.calls.append("mail")
        return 250, b"sender ok"

    def rcpt(self, *_args):
        self.calls.append("rcpt")
        return 250, b"recipient ok"

    def data(self, _content):
        self.calls.append("data")
        if self.data_error:
            raise self.data_error
        return self.data_result

    def quit(self):
        self.calls.append("quit")
        if self.quit_error:
            raise self.quit_error
        return 221, b"bye"

    def close(self):
        self.calls.append("close")


def _row(code):
    return {
        "property_code": str(code),
        "owner_email": f"owner{code}@example.com",
        "final_cc_list": ["boss@example.com", "agent@example.com"],
        "html": "<html><body>Test campaign message</body></html>",
    }


def _patch_ledger(monkeypatch, db):
    def claim(_db, _row, _campaign_id, _batch_id, _attempt_id):
        if db.ledger.status != "READY":
            return False
        db.ledger.status = "SENDING"
        return True

    def finish(_db, row, attempt_id, *, status, message_id=None, error_type=None, error_message=None):
        db.ledger.status = status
        db.ledger.finished.append({
            "property_code": row["property_code"], "attempt_id": attempt_id,
            "status": status, "message_id": message_id,
            "error_type": error_type, "error_message": error_message,
        })

    monkeypatch.setattr(sender, "_claim_send", claim)
    monkeypatch.setattr(sender, "_finish_attempt", finish)


@pytest.fixture(autouse=True)
def _smtp_credentials(monkeypatch):
    monkeypatch.setattr(Config, "GMAIL_USER", "sender@example.com")
    monkeypatch.setattr(Config, "GMAIL_PASSWORD", "test-only-secret")
    monkeypatch.setattr(Config, "COLLECTION_CAMPANAS_LOG", "campaign_ledger")


def test_batch_opens_and_closes_a_new_smtp_session_for_every_message(monkeypatch, caplog):
    caplog.set_level("INFO", logger="owner_campaign.smtp")
    sessions = []
    databases = []

    def factory(_host, _port, *, timeout):
        assert timeout == sender.SMTP_CONNECT_TIMEOUT_SECONDS
        session = _SMTP()
        sessions.append(session)
        return session

    def send_one(_db, row, campaign_id, batch_id, *, smtp_factory):
        db = _DB()
        databases.append(db)
        _patch_ledger(monkeypatch, db)
        return sender._send_one(db, row, campaign_id, batch_id, smtp_factory=smtp_factory)

    results, stop_reason, unsent = sender._send_batch(
        None, [_row("1001"), _row("1002"), _row("1003")], "campaign", "batch",
        smtp_factory=factory, send_one=send_one,
    )

    assert [result["smtp_status"] for result in results] == ["SENT"] * 3
    assert stop_reason is None
    assert unsent == 0
    assert len(sessions) == 3
    assert len({id(session) for session in sessions}) == 3
    assert all(("send_timeout", sender.SMTP_SEND_TIMEOUT_SECONDS) in session.calls for session in sessions)
    assert all(session.calls[-2:] == ["quit", "close"] for session in sessions)
    assert all(db.ledger.status == "SENT" for db in databases)
    joined_logs = "\n".join(record.getMessage() for record in caplog.records)
    for phase in ("CONNECTING", "CONNECTED", "AUTHENTICATED", "MAIL_STARTED", "DATA_STARTED", "SMTP_ACCEPTED", "CONNECTION_CLOSED"):
        assert f'"smtp_phase": "{phase}"' in joined_logs
    assert "test-only-secret" not in joined_logs


def test_smtp_server_disconnected_during_data_is_unknown_and_not_retried(monkeypatch):
    db = _DB()
    _patch_ledger(monkeypatch, db)
    sessions = []

    def factory(*_args, **_kwargs):
        session = _SMTP(data_error=smtplib.SMTPServerDisconnected("connection lost"))
        sessions.append(session)
        return session

    with pytest.raises(sender.SenderError, match="send_claim_unavailable_delivery_unknown"):
        # First attempt is handled as DELIVERY_UNKNOWN.
        outcome = sender._send_one(db, _row("1001"), "campaign", "batch", smtp_factory=factory)
        assert outcome["smtp_status"] == "DELIVERY_UNKNOWN"
        # A later invocation cannot claim the same row and never opens SMTP.
        sender._send_one(db, _row("1001"), "campaign", "batch", smtp_factory=factory)

    assert db.ledger.status == "DELIVERY_UNKNOWN"
    assert len(db.ledger.finished) == 1
    assert db.ledger.finished[0]["status"] == "DELIVERY_UNKNOWN"
    assert len(sessions) == 1
    assert sessions[0].calls[-2:] == ["quit", "close"]


def test_sent_is_recorded_only_after_positive_smtp_data_acceptance(monkeypatch):
    db = _DB()
    _patch_ledger(monkeypatch, db)
    session = _SMTP(data_result=(451, b"rejected"))

    result = sender._send_one(
        db, _row("1001"), "campaign", "batch", smtp_factory=lambda *_args, **_kwargs: session,
    )

    assert result["smtp_status"] == "FAILED"
    assert [entry["status"] for entry in db.ledger.finished] == ["FAILED"]
    assert db.ledger.status == "FAILED"
    assert session.calls[-2:] == ["quit", "close"]


def test_quit_error_after_acceptance_does_not_downgrade_sent(monkeypatch):
    db = _DB()
    _patch_ledger(monkeypatch, db)
    session = _SMTP(quit_error=smtplib.SMTPServerDisconnected("closed after accept"))

    result = sender._send_one(
        db, _row("1001"), "campaign", "batch", smtp_factory=lambda *_args, **_kwargs: session,
    )

    assert result["smtp_status"] == "SENT"
    assert [entry["status"] for entry in db.ledger.finished] == ["SENT"]
    assert db.ledger.status == "SENT"
    assert session.calls[-1] == "close"


@pytest.mark.parametrize("existing_status", ["SENT", "DELIVERY_UNKNOWN"])
def test_terminal_ledger_status_is_never_retried(monkeypatch, existing_status):
    db = _DB()
    db.ledger.status = existing_status
    sessions = []
    monkeypatch.setattr(sender, "_claim_send", lambda *_args, **_kwargs: False)

    def factory(*_args, **_kwargs):
        sessions.append(_SMTP())
        return sessions[-1]

    with pytest.raises(sender.SenderError, match=f"send_claim_unavailable_{existing_status.casefold()}"):
        sender._send_one(db, _row("1001"), "campaign", "batch", smtp_factory=factory)

    assert sessions == []
    assert db.ledger.status == existing_status
    assert db.ledger.finished == []
