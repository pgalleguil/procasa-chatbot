from __future__ import annotations

import base64
import ast
import hashlib
import hmac
import json
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import quote

import pytest

from campanas import owner_campaign_test_actions as actions
from campanas import owner_campaign_test_sender as sender


SECRET = "owner-campaign-e2e-secret-for-tests"
OWNER_EMAIL = "real-owner@example.test"


class FakeCollection:
    def __init__(self, name, db):
        self.name = name
        self.db = db
        self.docs = db.docs.setdefault(name, [])

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return dict(doc)
        return None

    def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("event_id"))

    def update_one(self, query, update, upsert=False):
        if self.name == actions.PROPERTY_COLLECTION:
            raise AssertionError("test action attempted a live property write")
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            doc = dict(query)
            doc.update(update.get("$setOnInsert", {}))
            self.docs.append(doc)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id="test-ledger")
        return SimpleNamespace(matched_count=0, modified_count=0)


class FakeDB:
    def __init__(self):
        self.docs = {}
        self.requested = []

    def __getitem__(self, name):
        self.requested.append(name)
        return FakeCollection(name, self)


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.sent = []
        self.login_args = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        pass

    def login(self, username, password):
        self.login_args = (username, password)

    def sendmail(self, sender_address, recipients, message):
        self.sent.append((sender_address, list(recipients), message))


def _case(case_id="A", **overrides):
    defaults = {
        "case_id": case_id,
        "property_code": "17400" if case_id == "A" else "18000" if case_id == "B" else "16521" if case_id == "C" else "16527",
        "intended_owner_email": OWNER_EMAIL,
        "operation": "ARRIENDO" if case_id == "D" else "VENTA",
        "evidence_segment": "PRICE_AUTHORIZATION_READY" if case_id == "A" else "MIXED_EVIDENCE" if case_id == "C" else "INSUFFICIENT_EVIDENCE" if case_id == "D" else "MIXED_EVIDENCE",
        "document_type": "INDIVIDUAL_APPRAISAL" if case_id == "A" else "COMMUNAL_MARKET_REPORT",
        "executive": "Ejecutivo PROCASA",
        "current_price": 5000.0,
        "commune": "Ñuñoa" if case_id == "B" else "Talca",
        "property_type": "Departamento" if case_id == "B" else "Casa",
        "raw_recommended_price": 4700.25 if case_id == "A" else None,
        "display_recommended_price": 4700.0 if case_id == "A" else None,
        "adjustment_pct": -6.0 if case_id == "A" else None,
    }
    defaults.update(overrides)
    return sender.OwnerCampaignTestCase(**defaults)


def _render(case):
    checks = {key: True for key in sender.REQUIRED_RENDER_CHECKS}
    if case.case_id == "D":
        checks.update({
            "rental_uf_per_month": True,
            "rental_estimate": True,
            "rental_comparables": True,
            "sale_fields_present": False,
        })
    links = []
    if case.case_id in {"A", "B"}:
        report_token = actions.issue_test_link_token(
            property_code=case.property_code,
            action=actions.REPORT_ACTION,
            document_type=case.document_type,
        )
        links.append(f'<a href="https://procasa-chatbot-yr8d.onrender.com/campana/informe?token={quote(report_token)}">Ver informe</a>')
    action = {"A": actions.ACCEPT_PRICE_ACTION, "C": actions.ADVISOR_ACTION, "D": actions.ADVISOR_ACTION}.get(case.case_id)
    if action:
        action_token = actions.issue_test_link_token(property_code=case.property_code, action=action)
        links.append(f'<a href="https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token={quote(action_token)}">Acción</a>')
    return {
        "subject": "Seguimiento comercial PROCASA",
        "html": f"<html><body>{' '.join(links)}</body></html>",
        "text": "Informe y recomendación de prueba.",
        "checks": checks,
    }


@pytest.fixture(autouse=True)
def test_environment(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    monkeypatch.setattr(sender.Config, "GMAIL_USER", "sender@procasa.example")
    monkeypatch.setattr(sender.Config, "GMAIL_PASSWORD", "unit-test-password")
    FakeSMTP.instances.clear()


def test_recipient_policy_allows_only_fixed_test_mailbox():
    sender.validate_recipient_envelope(
        to=sender.TEST_RECIPIENT,
        cc=(),
        bcc=(),
        envelope_recipients=[sender.TEST_RECIPIENT],
    )


@pytest.mark.parametrize("address", [OWNER_EMAIL, "jpcaro@procasa.cl"])
def test_arbitrary_owner_or_internal_recipient_aborts(address):
    with pytest.raises(sender.TestSenderError):
        sender.validate_recipient_envelope(to=address, envelope_recipients=[address])


def test_any_cc_or_bcc_aborts():
    with pytest.raises(sender.TestSenderError):
        sender.validate_recipient_envelope(to=sender.TEST_RECIPIENT, cc=["exec@procasa.cl"])
    with pytest.raises(sender.TestSenderError):
        sender.validate_recipient_envelope(to=sender.TEST_RECIPIENT, bcc=[sender.TEST_RECIPIENT])


def test_unexpected_smtp_envelope_aborts():
    with pytest.raises(sender.TestSenderError):
        sender.validate_recipient_envelope(
            to=sender.TEST_RECIPIENT,
            envelope_recipients=[sender.TEST_RECIPIENT, OWNER_EMAIL],
        )


def test_more_than_four_or_non_explicit_cases_abort():
    with pytest.raises(sender.TestSenderError, match="test_case_count_invalid"):
        sender.validate_explicit_cases([_case("A")] * 5)
    with pytest.raises(sender.TestSenderError, match="explicit_test_cases_required"):
        sender.validate_explicit_cases("all")


def test_only_rendered_valid_test_links_pass_preflight():
    cases = [_case("A"), _case("B"), _case("C"), _case("D")]
    prepared = sender.prepare_test_messages(cases, render_case=_render)
    assert len(prepared) == 4
    assert all(item.subject.startswith("[PRUEBA E2E]") for item in prepared)
    assert all(OWNER_EMAIL not in item.html for item in prepared)


def test_link_builder_issues_only_fixed_test_campaign_links():
    case = _case("A")
    links = sender.build_test_links(case)
    report_claims = actions.verify_test_link_token(
        links["report"].split("token=", 1)[1],
        allowed_actions=frozenset({actions.REPORT_ACTION}),
    )
    action_claims = actions.verify_test_link_token(
        links["action"].split("token=", 1)[1],
        allowed_actions=frozenset({actions.ACCEPT_PRICE_ACTION}),
    )
    assert report_claims["property_code"] == case.property_code
    assert report_claims["document_type"] == "INDIVIDUAL_APPRAISAL"
    assert action_claims["property_code"] == case.property_code
    assert action_claims["recipient"] == sender.TEST_RECIPIENT


def test_render_attempting_another_recipient_aborts_before_smtp_or_ledger(monkeypatch):
    db = FakeDB()
    malicious = dict(_render(_case("A")), to=OWNER_EMAIL)
    with pytest.raises(sender.TestSenderError, match="test_recipient_mismatch"):
        sender.send_test_messages(
            [_case("A")], render_case=lambda _case: malicious, db=db, smtp_factory=FakeSMTP
        )
    assert FakeSMTP.instances == []
    assert db.docs.get(sender.TEST_LEDGER_COLLECTION, []) == []


def test_case_d_sale_fields_are_a_hard_failure_before_smtp():
    rendered = _render(_case("D"))
    rendered["checks"]["sale_fields_present"] = True
    with pytest.raises(sender.TestSenderError, match="test_case_d_sale_or_rent_check_failed"):
        sender.prepare_test_messages([_case("D")], render_case=lambda _case: rendered)


def test_sender_delivers_one_explicit_case_only_to_fixed_to_and_envelope():
    db = FakeDB()
    result = sender.send_test_messages(
        [_case("A")], render_case=_render, db=db, smtp_factory=FakeSMTP
    )
    assert result == [{"case_id": "A", "property_code": "17400", "status": "sent_to_test_recipient"}]
    smtp = FakeSMTP.instances[0]
    assert len(smtp.sent) == 1
    _, recipients, raw_message = smtp.sent[0]
    assert recipients == [sender.TEST_RECIPIENT]
    assert "To: pgalleguillos@procasa.cl" in raw_message
    assert "Cc:" not in raw_message
    assert "Bcc:" not in raw_message
    assert OWNER_EMAIL not in raw_message
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["actual_recipient_email"] == sender.TEST_RECIPIENT
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["intended_owner_email"] == OWNER_EMAIL
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["delivery_status"] == "test_sent"


def _test_ledger(*, code, cta_type="PRICE_AUTHORIZATION", evidence_segment="PRICE_AUTHORIZATION_READY"):
    return {
        "campaign_id": actions.TEST_CAMPAIGN_ID,
        "property_code": code,
        "test_mode": True,
        "actual_recipient_email": actions.TEST_RECIPIENT,
        "intended_owner_email": OWNER_EMAIL,
        "executive": "Ejecutivo PROCASA",
        "cta_type": cta_type,
        "evidence_segment": evidence_segment,
        "display_recommended_price": 4700.0,
    }


def _action_db(code="16521", *, cta_type="PRICE_AUTHORIZATION", evidence_segment="PRICE_AUTHORIZATION_READY"):
    db = FakeDB()
    db.docs[actions.LEDGER_COLLECTION] = [_test_ledger(code=code, cta_type=cta_type, evidence_segment=evidence_segment)]
    db.docs[actions.PROPERTY_COLLECTION] = [{
        "codigo": code,
        "tipo_operacion": {
            "tipo": "Venta",
            "precio_venta": {"precio_uf": 5000, "precio_clp": 190000000},
        },
        "estado": {"ejecutivo": "Ejecutivo PROCASA"},
    }]
    return db


def _action_token(code="16521", action=actions.ACCEPT_PRICE_ACTION, *, recipient=actions.TEST_RECIPIENT):
    claims = {
        "campaign_id": actions.TEST_CAMPAIGN_ID,
        "property_code": code,
        "action": action,
        "recipient": recipient,
        "test_mode": True,
        "exp": 2_000_000_000,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"t1.{encoded}.{signature}"


def test_test_price_authorization_appends_events_and_keeps_exact_live_price():
    db = _action_db()
    before = dict(db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_venta"])
    result = actions.process_test_action(_action_token(), db=db)
    after = db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_venta"]
    events = db.docs[actions.EVENT_COLLECTION]
    assert result["event"] == "price_authorized"
    assert result["test_mode"] is True
    assert result["test_price_mutation"] is False
    assert result["live_price_before"] == result["live_price_after"] == before
    assert after == before
    assert [event["event_type"] for event in events] == ["cta_clicked", "price_authorized"]
    assert all(event["test_mode"] is True and event["property_code"] == "16521" for event in events)
    assert set(db.requested) <= {actions.LEDGER_COLLECTION, actions.PROPERTY_COLLECTION, actions.EVENT_COLLECTION}


def test_advisor_cta_writes_only_test_events_and_no_legacy_response_collections():
    db = _action_db(cta_type="ADVISOR_REVIEW", evidence_segment="MIXED_EVIDENCE")
    result = actions.process_test_action(_action_token(action=actions.ADVISOR_ACTION), db=db)
    assert result["event"] == "advisor_review_requested"
    assert [event["event_type"] for event in db.docs[actions.EVENT_COLLECTION]] == [
        "advisor_review_requested"
    ]
    assert all(event["executive"] == "Ejecutivo PROCASA" for event in db.docs[actions.EVENT_COLLECTION])
    assert "contactos" not in db.requested
    assert "price_updates" not in db.requested
    assert actions.PROPERTY_COLLECTION not in db.requested


def test_report_open_records_only_report_opened_test_event():
    db = _action_db(cta_type="REPORT_ONLY", evidence_segment="MIXED_EVIDENCE")
    event_id = actions.record_test_report_opened("16521", token="signed-report-token", db=db)
    events = db.docs[actions.EVENT_COLLECTION]
    assert len(events) == 1
    assert events[0]["event_id"] == event_id
    assert events[0]["event_type"] == "report_opened"
    assert events[0]["test_mode"] is True
    assert "contactos" not in db.requested
    assert "price_updates" not in db.requested
    assert actions.PROPERTY_COLLECTION not in db.requested


def test_live_property_price_write_is_not_part_of_test_action_handler():
    source = (sender.__file__.replace("owner_campaign_test_sender.py", "owner_campaign_test_actions.py"))
    from pathlib import Path
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    forbidden_collections = {"contactos", "price_updates", "universo_cartera_prop360"}
    assert not any(
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in forbidden_collections
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"update_one", "update_many", "replace_one", "find_one_and_update"}
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Name) and node.id in {"send_whatsapp", "create_task", "schedule_followup"}
        for node in ast.walk(tree)
    )


def test_test_mode_off_rejects_before_touching_database(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "false")
    db = FakeDB()
    with pytest.raises(actions.OwnerCampaignTestError, match="test_action_token_invalid"):
        actions.process_test_action(_action_token(), db=db)
    assert db.requested == []


def test_public_test_action_route_is_token_only_and_bypasses_legacy_handler():
    source = Path(__file__).parents[1] / "webhook.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    route = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "campana_test_accion"
    )
    body = ast.unparse(route)
    assert "request.query_params" in body
    assert "handle_test_action" in body
    assert "handle_campana_respuesta" not in body
    assert "_require_captacion_report_admin" not in body
