"""No-network tests for Mariela's single-use, privacy-preserving WhatsApp probe."""
from datetime import datetime, timezone
from types import SimpleNamespace

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from chatbot import executive_whatsapp_e2e_probe as probe


SUPERVISOR = ObjectId("69796bc5bbebf240378eb73a")
PHONE = "+56983219804"


class FakeUsers:
    def find_one(self, query, projection=None):
        if query.get("_id") == SUPERVISOR and query.get("rol") == "supervisor":
            return {"telefono": PHONE}
        return None


class FakeProbes:
    def __init__(self):
        self.records = {}

    def insert_one(self, doc):
        if doc["_id"] in self.records:
            raise DuplicateKeyError("duplicate")
        self.records[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    def find_one(self, query, projection=None):
        r = self.records.get(query.get("_id"))
        if not r:
            return None
        for k, v in query.items():
            if k == "_id":
                continue
            if isinstance(v, dict) and "$in" in v:
                if r.get(k) not in v["$in"]:
                    return None
            elif isinstance(v, dict) and "$gte" in v:
                if r.get(k) is None or r[k] < v["$gte"]:
                    return None
            elif r.get(k) != v:
                return None
        return {"_id": r["_id"]}

    def update_one(self, query, mutation):
        r = self.records.get(query["_id"])
        if not r:
            return SimpleNamespace(modified_count=0)
        for key, cond in query.items():
            if key == "_id":
                continue
            if isinstance(cond, dict) and cond.get("$exists") is False:
                if key in r:
                    return SimpleNamespace(modified_count=0)
            elif r.get(key) != cond:
                return SimpleNamespace(modified_count=0)
        r.update(mutation.get("$set", {}))
        return SimpleNamespace(modified_count=1)


def _setup(monkeypatch):
    for key, val in {
        "PROCASA_EXEC_E2E_PROBE_ENABLED": "1",
        "PROCASA_EXEC_E2E_PROBE_SESSION_ID": "133526",
        "PROCASA_EXEC_E2E_PROBE_NONCE": "testnonce-20261119",
        "PROCASA_EXEC_E2E_PROBE_SUPERVISOR_USER_ID": str(SUPERVISOR),
        "PROCASA_EXEC_E2E_PROBE_SUPERVISOR_PHONE_LAST4": "9804",
        "PROCASA_EXEC_AUDIT_WRITE_ENABLED": "1",
    }.items():
        monkeypatch.setenv(key, val)
    db = {"usuarios": FakeUsers(), probe.COLLECTION: FakeProbes()}
    session = {
        "session_id": "133526", "user_id": probe.MARIELA_USER_ID,
        "capture_enabled": True, "token": "unit-token-not-real",
    }
    return db, session


def test_one_shot_send_and_signed_in_out_event_metadata(monkeypatch):
    db, session = _setup(monkeypatch)
    requests_seen = []

    def post(url, **kwargs):
        requests_seen.append((url, kwargs))
        return SimpleNamespace(
            status_code=200, content=b"{}",
            json=lambda: {"success": True, "data": {"messageId": "id-from-provider"}},
        )

    monkeypatch.setattr(probe.requests, "post", post)
    probe.run_once(db, lambda _db, _sid: session)
    probe.run_once(db, lambda _db, _sid: session)
    assert len(requests_seen) == 1
    assert requests_seen[0][0].endswith("/send-message")
    assert requests_seen[0][1]["json"]["to"] == PHONE
    assert requests_seen[0][1]["json"]["text"] == probe.TEXT
    assert "unit-token-not-real" in requests_seen[0][1]["headers"]["Authorization"]
    assert list(db[probe.COLLECTION].records.values())[0]["state"] == "provider_accepted"
    doc = next(iter(db[probe.COLLECTION].records.values()))
    assert PHONE not in repr(doc)
    assert probe.TEXT not in repr(doc)

    probe.observe_signed_event(db, session, PHONE, from_me=True)
    probe.observe_signed_event(db, session, PHONE, from_me=True)
    probe.observe_signed_event(db, session, "+56977777777", from_me=False)
    probe.observe_signed_event(db, session, PHONE, from_me=False)
    assert isinstance(doc["outbound_webhook_at"], datetime)
    assert isinstance(doc["inbound_webhook_at"], datetime)
    assert doc["outbound_webhook_at"].tzinfo == timezone.utc


def test_rejects_unapproved_recipient_or_wrong_session_or_global_guard(monkeypatch):
    db, session = _setup(monkeypatch)
    calls = []
    monkeypatch.setattr(probe.requests, "post", lambda *a, **kw: calls.append(1))
    monkeypatch.setenv("PROCASA_EXEC_E2E_PROBE_SUPERVISOR_PHONE_LAST4", "1234")
    probe.run_once(db, lambda _db, _sid: session)
    assert not calls
    monkeypatch.setenv("PROCASA_EXEC_E2E_PROBE_SUPERVISOR_PHONE_LAST4", "9804")
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "0")
    probe.run_once(db, lambda _db, _sid: session)
    assert not calls
    monkeypatch.setenv("PROCASA_EXEC_AUDIT_WRITE_ENABLED", "1")
    probe.run_once(db, lambda _db, _sid: {**session, "user_id": "wrong-owner"})
    assert not calls
    monkeypatch.setenv("PROCASA_EXEC_E2E_PROBE_SESSION_ID", "133517")
    probe.run_once(db, lambda _db, _sid: session)
    assert not calls
    assert db[probe.COLLECTION].records == {}


def test_provider_error_is_single_attempt_never_retried(monkeypatch):
    db, session = _setup(monkeypatch)
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=401, content=b"{}", json=lambda: {"success": False})

    monkeypatch.setattr(probe.requests, "post", post)
    probe.run_once(db, lambda _db, _sid: session)
    probe.run_once(db, lambda _db, _sid: session)
    assert len(calls) == 1
    doc = next(iter(db[probe.COLLECTION].records.values()))
    assert doc["state"] == "provider_auth_rejected"
    probe.observe_signed_event(db, session, PHONE, from_me=False)
    assert "inbound_webhook_at" not in doc
