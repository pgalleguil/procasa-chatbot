from __future__ import annotations

import base64
import ast
import hashlib
import hmac
import json
import re
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import quote

import pytest

from campanas import owner_campaign_test_actions as actions
from campanas import owner_campaign_test_runtime as runtime
from campanas import owner_campaign_test_sender as sender
from campanas import owner_campaign_test_runner as runner
from analytics import owner_campaign_test_sender as cli
from analytics import owner_campaign_email_v2 as email_v2


SECRET = "owner-campaign-e2e-secret-for-tests"
OWNER_EMAIL = "real-owner@example.test"


class FakeCollection:
    def __init__(self, name, db):
        self.name = name
        self.db = db
        self.docs = db.docs.setdefault(name, [])

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if self._matches(doc, query):
                return dict(doc)
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        return [dict(doc) for doc in self.docs if self._matches(doc, query)]

    @staticmethod
    def _matches(doc, query):
        for key, expected in query.items():
            actual = doc
            for part in key.split("."):
                actual = actual.get(part) if isinstance(actual, dict) else None
            if isinstance(expected, dict) and "$nin" in expected:
                if any(value in (actual or []) for value in expected["$nin"]):
                    return False
            elif isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif isinstance(expected, dict) and "$exists" in expected:
                if (actual is not None) != expected["$exists"]:
                    return False
            elif actual != expected:
                return False
        return True

    @staticmethod
    def _set_path(doc, dotted, value):
        parts = dotted.split(".")
        target = doc
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("event_id"))

    def update_one(self, query, update, upsert=False):
        if self.name == actions.PROPERTY_COLLECTION:
            raise AssertionError("test action attempted a live property write")
        for doc in self.docs:
            if self._matches(doc, query):
                for key, value in update.get("$set", {}).items():
                    self._set_path(doc, key, value)
                modified = bool(update.get("$set"))
                for key, spec in update.get("$addToSet", {}).items():
                    values = spec.get("$each", []) if isinstance(spec, dict) else [spec]
                    current = doc.setdefault(key, [])
                    for value in values:
                        if value not in current:
                            current.append(value)
                            modified = True
                for key, spec in update.get("$push", {}).items():
                    values = spec.get("$each", []) if isinstance(spec, dict) else [spec]
                    doc.setdefault(key, []).extend(values)
                    modified = modified or bool(values)
                for operator in ("$min", "$max"):
                    for key, value in update.get(operator, {}).items():
                        current = doc
                        parts = key.split(".")
                        for part in parts[:-1]:
                            current = current.setdefault(part, {})
                        old = current.get(parts[-1])
                        if old is None or (operator == "$min" and value < old) or (operator == "$max" and value > old):
                            current[parts[-1]] = value
                            modified = True
                return SimpleNamespace(matched_count=1, modified_count=int(modified))
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
    is_authorized = case_id == "A"
    codes = {"A": "5641", "B": "16521", "C": "16486", "D": "16527", "E": "70001"}
    defaults = {
        "case_id": case_id,
        "property_code": codes[case_id],
        "intended_owner_email": OWNER_EMAIL,
        "operation": "ARRIENDO" if case_id == "D" else "VENTA",
        "evidence_segment": "PRICE_AUTHORIZATION_READY" if is_authorized else "MIXED_EVIDENCE" if case_id == "B" else "INSUFFICIENT_EVIDENCE" if case_id == "C" else "MIXED_EVIDENCE",
        "document_type": "COMMUNAL_MARKET_REPORT" if case_id == "B" else "NONE" if case_id == "D" else "INDIVIDUAL_APPRAISAL",
        "executive": "Ejecutivo PROCASA",
        "current_price": 21.0 if case_id == "D" else 5000.0,
        "cta_type": "PRICE_AUTHORIZATION" if is_authorized else "ADVISOR_REVIEW",
        "commune": "Estación Central" if case_id == "B" else "Talca",
        "property_type": "Departamento" if case_id == "B" else "Casa",
        "raw_recommended_price": 4700.25 if is_authorized else None,
        "display_recommended_price": 4700.0 if is_authorized else None,
        "adjustment_pct": -6.0 if is_authorized else None,
    }
    defaults.update(overrides)
    return sender.OwnerCampaignTestCase(**defaults)


def _render(case):
    checks = {key: True for key in sender.REQUIRED_RENDER_CHECKS}
    if case.operation == "ARRIENDO":
        checks.update({
            "rental_uf_per_month": True,
            "rental_estimate": True,
            "rental_comparables": True,
            "sale_fields_present": False,
        })
    links = []
    if case.document_type in {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}:
        report_token = actions.issue_test_link_token(
            property_code=case.property_code,
            action=actions.REPORT_ACTION,
            document_type=case.document_type,
        )
        links.append(f'<a href="https://procasa-chatbot-yr8d.onrender.com/campana/informe?token={quote(report_token)}">Ver informe</a>')
    action = {
        "PRICE_AUTHORIZATION": actions.ACCEPT_PRICE_ACTION,
        "ADVISOR_REVIEW": actions.ADVISOR_ACTION,
    }.get(case.cta_type)
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
    assert actions.TEST_RECIPIENT == "p.galleguil@gmail.com"
    assert sender.TEST_RECIPIENT == actions.TEST_RECIPIENT
    sender.validate_recipient_envelope(
        to=sender.TEST_RECIPIENT,
        cc=(),
        bcc=(),
        envelope_recipients=[sender.TEST_RECIPIENT],
    )


def test_corrected_test_recipient_is_the_only_test_address_in_harness_sources():
    old_test_address = "jpcaro" + "@procasa.cl"
    root = Path(__file__).parents[1]
    sources = (
        "campanas/owner_campaign_test_actions.py",
        "campanas/owner_campaign_test_sender.py",
        "campanas/owner_campaign_test_runner.py",
        "campanas/owner_campaign_test_runtime.py",
        "campanas/private_report.py",
        "tests/test_owner_campaign_test_safety.py",
    )
    source_text = {relative: (root / relative).read_text(encoding="utf-8") for relative in sources}
    assert "from .test_mode import" in source_text["campanas/owner_campaign_test_actions.py"]
    assert all(old_test_address not in text for text in source_text.values())


def test_live_case_builder_can_limit_first_phase_to_a_b_d(monkeypatch):
    loaded = {}
    monkeypatch.setattr(runtime, "_load_code_docs", lambda _db, codes: loaded.update(codes=codes) or {code: {"code": code} for code in codes})
    monkeypatch.setattr(runtime, "_build_case", lambda _db, doc, case_id, **_kwargs: SimpleNamespace(case_id=case_id, doc=doc))
    monkeypatch.setattr(runtime, "_build_portfolio", lambda *_args: pytest.fail("E must not block the first phase"))
    cases = runtime.build_owner_campaign_test_cases_live(object(), case_ids=("A", "B", "D"))
    assert [case.case_id for case in cases] == ["A", "B", "D"]
    assert loaded["codes"] == {"5641", "16521", "16527"}


@pytest.mark.parametrize(("document", "expected"), [
    ({"tipo_operacion": {"venta": True, "arriendo": False}}, runtime.VENTA),
    ({"tipo_operacion": {"venta": False, "arriendo": True}}, runtime.ARRIENDO),
    ({"resumen": {"snapshot_listado": {"operacion": "Arriendo"}}}, runtime.ARRIENDO),
    ({"tipo_operacion": {"venta": True, "arriendo": True}}, runtime.VENTA_ARRIENDO),
    ({"tipo_operacion": {"venta": False, "arriendo": False}}, runtime.UNKNOWN_OPERATION),
])
def test_live_builder_operation_resolution_does_not_import_ignored_scripts(document, expected):
    assert runtime.resolve_property_operation(document) == expected


def test_live_builder_selects_only_requested_operation_price_block():
    document = {"tipo_operacion": {
        "venta": True,
        "arriendo": True,
        "precio_venta": {"precio_uf": 5000},
        "precio_arriendo": {"precio_uf": 25},
    }}
    assert runtime.operation_price_block(document, requested_operation="VENTA")["precio_uf"] == 5000
    assert runtime.operation_price_block(document, requested_operation="ARRIENDO")["precio_uf"] == 25
    sale_only = {"tipo_operacion": {"venta": True, "arriendo": False, "precio_venta": {"precio_uf": 5000}}}
    assert runtime.operation_price_block(sale_only, requested_operation="ARRIENDO") is None


@pytest.mark.parametrize(("value", "expected"), [
    ("A,B,D", ("A", "B", "D")),
    ("C,E", ("C", "E")),
    ("A,B,C,D,E", ("A", "B", "C", "D", "E")),
])
def test_independent_cli_accepts_only_fixed_batches(value, expected):
    assert cli.parse_cases(value) == expected


@pytest.mark.parametrize("value", ["A", "A,B", "A,B,C,D", "C,E,A", "A,,B,D", "5641,16521,16527"])
def test_independent_cli_rejects_other_case_batches(value):
    with pytest.raises(cli.TestCampaignCLIError, match="cases_must_be_A_B_D_C_E_or_ALL"):
        cli.parse_cases(value)


@pytest.mark.parametrize(("payload", "expected"), [
    ({"batch": "ABD", "mode": "dry-run"}, ("A", "B", "D")),
    ({"batch": "ABD", "mode": "send"}, ("A", "B", "D")),
    ({"batch": "CE", "mode": "dry-run"}, ("C", "E")),
    ({"batch": "CE", "mode": "send"}, ("C", "E")),
    ({"batch": "ALL", "mode": "dry-run"}, ("A", "B", "C", "D", "E")),
    ({"batch": "ALL", "mode": "send"}, ("A", "B", "C", "D", "E")),
])
def test_http_trigger_accepts_only_named_batches_and_modes(payload, expected):
    cases, _dry_run = cli.parse_trigger_request(payload)
    assert cases == expected


@pytest.mark.parametrize("payload", [
    {"batch": "ABD", "mode": "send", "recipient": OWNER_EMAIL},
    {"batch": "ABD", "mode": "send", "property_code": "5641"},
    {"batch": "ABD", "mode": "send", "campaign_id": "other"},
    {"batch": "A", "mode": "send"},
    {"batch": "ABD", "mode": "execute"},
    {"batch": "ABD", "mode": []},
    {"batch": "ABD", "mode": "dry-run", "email": OWNER_EMAIL},
])
def test_http_trigger_rejects_overrides_and_unsupported_actions(payload):
    with pytest.raises(cli.TestCampaignCLIError):
        cli.parse_trigger_request(payload)


def test_http_trigger_secret_uses_constant_time_comparison_and_fails_closed(monkeypatch):
    monkeypatch.setenv(cli.TRIGGER_SECRET_ENV, "a" * 64)
    assert cli.trigger_secret_matches("a" * 64)
    assert not cli.trigger_secret_matches("b" * 64)
    assert not cli.trigger_secret_matches(None)
    monkeypatch.delenv(cli.TRIGGER_SECRET_ENV)
    assert not cli.trigger_secret_matches("a" * 64)


def test_fixed_recipient_abd_cases_do_not_require_owner_email_but_portfolios_do():
    assert runtime._email_from_property({}, required=False) == ""
    with pytest.raises(runtime.LiveTestCaseBuildError, match="owner_email_unavailable"):
        runtime._email_from_property({})

    prepared = sender.prepare_test_messages([_case("A", intended_owner_email="")], render_case=_render)
    assert len(prepared) == 1

    portfolio = tuple(
        _case("E", property_code=code, intended_owner_email="")
        for code in ("70001", "70002", "70003")
    )
    wrapper = replace(portfolio[0], case_id="E", portfolio_cases=portfolio)
    with pytest.raises(sender.TestSenderError, match="test_case_e_portfolio_invalid"):
        sender.validate_explicit_cases([wrapper])


def test_test_executive_resolves_active_crm_contact_by_assigned_name():
    class Users:
        def __init__(self, records):
            self.records = records
            self.query = None

        def find_one(self, query, projection):
            self.query = (query, projection)
            return next((item for item in self.records if all(item.get(key) == value for key, value in query.items())), None)

        def find(self, query, projection):
            self.query = (query, projection)
            return [item for item in self.records if all(item.get(key) == value for key, value in query.items())]

    class CRM:
        def __init__(self, records=()):
            self.users = Users(list(records))

        def __getitem__(self, name):
            assert name == "usuarios"
            return self.users

    db = CRM([{
        "nombre": "Ejecutiva desde propiedad", "rol": "agente", "is_active": True,
        "email": "ejecutiva@procasa.cl", "phone": "+56912345678",
    }])
    executive = runtime._resolve_executive(db, {
        "estado": {"ejecutivo": "Ejecutiva desde propiedad"},
    })
    assert executive == {
        "name": "Ejecutiva desde propiedad", "directory_name": "Ejecutiva desde propiedad",
        "email": "ejecutiva@procasa.cl", "phone": "+56912345678",
        "initials": "", "source": "usuarios", "match_type": "EXACT_NAME", "match_unique": True,
    }
    assert db.users.query[0] == {"rol": "agente", "is_active": True}
    fallback = runtime._resolve_executive(CRM(), {"estado": {"ejecutivo": "Vacante"}})
    assert fallback["name"] == "Equipo PROCASA"
    assert fallback["email"] == fallback["phone"] == ""


def test_executive_contact_prefers_complete_property_contact_without_directory_lookup():
    class NoDirectory:
        def __getitem__(self, _name):
            pytest.fail("complete property contact should not query the CRM directory")

    executive = runtime._resolve_executive(NoDirectory(), {
        "estado": {"ejecutivo": "Ejecutiva asignada"},
        "email_ejecutivo": "Ejecutiva@procasa.cl",
        "movil_ejecutivo": "9 1234 5678",
    })

    assert executive["name"] == "Ejecutiva asignada"
    assert executive["email"] == "ejecutiva@procasa.cl"
    assert executive["phone"] == "+56912345678"
    assert executive["source"] == "property"
    assert executive["match_type"] == "PROPERTY_CONTACT"


def test_executive_directory_lookup_uses_exact_normalized_name_and_completes_partial_property_contact():
    class Users:
        def __init__(self, records):
            self.records = records

        def find_one(self, query, projection):
            return next((item for item in self.records if all(item.get(key) == value for key, value in query.items())), None)

        def find(self, query, projection):
            return [item for item in self.records if all(item.get(key) == value for key, value in query.items())]

    class CRM:
        def __init__(self, records):
            self.users = Users(records)

        def __getitem__(self, name):
            assert name == "usuarios"
            return self.users

    db = CRM([{
        "nombre": "José Pérez", "rol": "agente", "is_active": True,
        "email": "jose.perez@procasa.cl", "movil": "+56 9 8765 4321",
    }])
    executive = runtime._resolve_executive(db, {
        "estado": {"ejecutivo": "Jose Perez"},
        "email_ejecutivo": "directo@procasa.cl",
    })

    assert executive["name"] == "Jose Perez"
    assert executive["email"] == "directo@procasa.cl"
    assert executive["phone"] == "+56987654321"
    assert executive["source"] == "property+usuarios"


def test_executive_directory_matches_property_name_with_unique_extra_surname():
    class Users:
        def find(self, query, projection):
            assert query == {"rol": "agente", "is_active": True}
            return [{
                "nombre": "Erika Garrido", "rol": "agente", "is_active": True,
                "email": "egarrido@procasa.cl", "telefono": "+56991951317",
            }]

    class CRM:
        def __getitem__(self, name):
            assert name == "usuarios"
            return Users()

    executive = runtime._resolve_executive(CRM(), {
        "estado": {"ejecutivo": "Erika Garrido Varela"},
    })

    assert executive["name"] == "Erika Garrido Varela"
    assert executive["directory_name"] == "Erika Garrido"
    assert executive["email"] == "egarrido@procasa.cl"
    assert executive["phone"] == "+56991951317"
    assert executive["match_type"] == "UNIQUE_NAME_SUBSET"
    assert executive["match_unique"] is True


def test_exact_normalized_full_name_match_precedes_name_subset():
    class Users:
        def find(self, _query, _projection):
            return [
                {"nombre": "Jose Perez", "rol": "agente", "is_active": True,
                 "email": "jose@procasa.cl", "telefono": "+56912345678"},
                {"nombre": "Jose Perez Soto", "rol": "agente", "is_active": True,
                 "email": "other@procasa.cl", "telefono": "+56987654321"},
            ]

    class CRM:
        def __getitem__(self, name):
            assert name == "usuarios"
            return Users()

    executive = runtime._resolve_executive(CRM(), {"estado": {"ejecutivo": "José Pérez"}})
    assert executive["directory_name"] == "Jose Perez"
    assert executive["match_type"] == "EXACT_NAME"
    assert executive["email"] == "jose@procasa.cl"


def test_ambiguous_partial_executive_matches_block_contact_resolution():
    class Users:
        def find(self, _query, _projection):
            return [
                {"nombre": "Erika Garrido", "rol": "agente", "is_active": True,
                 "email": "one@procasa.cl", "telefono": "+56912345678"},
                {"nombre": "Erika Varela", "rol": "agente", "is_active": True,
                 "email": "two@procasa.cl", "telefono": "+56987654321"},
            ]

    class CRM:
        def __getitem__(self, name):
            assert name == "usuarios"
            return Users()

    executive = runtime._resolve_executive(CRM(), {
        "estado": {"ejecutivo": "Erika Garrido Varela"},
    })
    assert executive["email"] == executive["phone"] == ""
    assert executive["match_type"] == "AMBIGUOUS_NAME_SUBSET"
    assert executive["match_unique"] is False


def test_inactive_and_missing_executive_directory_matches_block_contact_resolution():
    class Users:
        def __init__(self, records):
            self.records = records

        def find(self, query, _projection):
            return [item for item in self.records if all(item.get(key) == value for key, value in query.items())]

    class CRM:
        def __init__(self, records):
            self.users = Users(records)

        def __getitem__(self, name):
            assert name == "usuarios"
            return self.users

    inactive = runtime._resolve_executive(CRM([{
        "nombre": "Erika Garrido", "rol": "agente", "is_active": False,
        "email": "inactive@procasa.cl", "telefono": "+56912345678",
    }]), {"estado": {"ejecutivo": "Erika Garrido Varela"}})
    missing = runtime._resolve_executive(CRM([{
        "nombre": "Erika Alvarado", "rol": "agente", "is_active": True,
        "email": "other@procasa.cl", "telefono": "+56912345678",
    }]), {"estado": {"ejecutivo": "Erika Garrido Varela"}})

    assert inactive["email"] == inactive["phone"] == ""
    assert inactive["match_type"] == "NO_MATCH"
    assert missing["email"] == missing["phone"] == ""
    assert missing["match_type"] == "NO_MATCH"


def test_executive_directory_normalized_name_ambiguity_fails_closed():
    class Users:
        def find_one(self, _query, _projection):
            return None

        def find(self, _query, _projection):
            return [
                {"nombre": "Jose Perez", "rol": "agente", "is_active": True, "email": "one@procasa.cl", "phone": "+56912345678"},
                {"nombre": "José Pérez", "rol": "agente", "is_active": True, "email": "two@procasa.cl", "phone": "+56987654321"},
            ]

    class CRM:
        def __getitem__(self, name):
            assert name == "usuarios"
            return Users()

    executive = runtime._resolve_executive(CRM(), {"estado": {"ejecutivo": "JOSE PEREZ"}})
    assert executive["email"] == executive["phone"] == ""
    assert executive["source"] == "estado.ejecutivo"


def test_independent_cli_phase_gate_requires_completed_initial_batch():
    db = FakeDB()
    cli._validate_phase(db, cli.INITIAL_CASES)
    with pytest.raises(cli.TestCampaignCLIError, match="remaining_batch_requires_completed_A_B_D"):
        cli._validate_phase(db, cli.REMAINING_CASES)
    db.docs[sender.TEST_LEDGER_COLLECTION] = [
        {
            "campaign_id": actions.TEST_CAMPAIGN_ID,
            "property_code": code,
            "test_mode": True,
            "actual_recipient_email": actions.TEST_RECIPIENT,
            "delivery_status": "test_sent",
            "smtp_accepted": True,
        }
        for code in ("5641", "16521", "16527")
    ]
    cli._validate_phase(db, cli.REMAINING_CASES)


def test_independent_cli_full_batch_is_allowed_only_before_any_test_send():
    db = FakeDB()
    cli._validate_phase(db, cli.ALL_CASES)
    db.docs[sender.TEST_LEDGER_COLLECTION] = [{
        "campaign_id": actions.TEST_CAMPAIGN_ID,
        "property_code": "5641",
        "test_mode": True,
        "actual_recipient_email": actions.TEST_RECIPIENT,
        "delivery_status": "test_sent",
        "smtp_accepted": True,
    }]
    with pytest.raises(cli.TestCampaignCLIError, match="full_batch_already_registered"):
        cli._validate_phase(db, cli.ALL_CASES)


def test_independent_cli_dry_run_never_calls_smtp_or_writes_ledger(monkeypatch):
    db = FakeDB()
    cases = [_case(case_id) for case_id in cli.INITIAL_CASES]
    prepared = [
        sender.PreparedTestMessage(case, "[TEST PROCASA] test", "<html/>", "test", "report", "action")
        for case in cases
    ]
    monkeypatch.setattr(cli, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(cli, "mass_send_enabled", lambda: False)
    monkeypatch.setattr(cli, "build_owner_campaign_test_cases_live", lambda *_a, **_k: cases)
    monkeypatch.setattr(cli, "prepare_test_messages", lambda _cases: prepared)
    monkeypatch.setattr(cli, "send_test_messages", lambda *_a, **_k: pytest.fail("dry run must not send"))
    result = cli.run_test_batch(cli.INITIAL_CASES, test_mode=True, dry_run=True, db=db, health_reader=lambda: 6)
    assert result["status"] == "preflight_passed_no_send"
    assert result["actual_recipient_email"] == actions.TEST_RECIPIENT
    assert result["preflight"]["owner_recipient_blocked"] is True
    assert result["preflight"]["live_price_mutation_blocked"] is True
    assert result["test_emails_sent"] == 0
    assert not db.docs.get(sender.TEST_LEDGER_COLLECTION)


def test_independent_cli_full_batch_preflights_all_five_without_writing_or_sending(monkeypatch):
    db = FakeDB()
    cases = [_case(case_id) for case_id in cli.ALL_CASES]
    prepared = [
        sender.PreparedTestMessage(case, "[TEST PROCASA] test", "<html/>", "test", "report", "action")
        for case in cases
    ]
    monkeypatch.setattr(cli, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(cli, "mass_send_enabled", lambda: False)
    monkeypatch.setattr(cli, "build_owner_campaign_test_cases_live", lambda _db, case_ids: [
        case for case in cases if case.case_id in case_ids
    ])
    monkeypatch.setattr(cli, "prepare_test_messages", lambda _cases: prepared)
    monkeypatch.setattr(cli, "send_test_messages", lambda *_a, **_k: pytest.fail("dry run must not send"))

    result = cli.run_test_batch(cli.ALL_CASES, test_mode=True, dry_run=True, db=db, health_reader=lambda: 6)

    assert result["status"] == "preflight_passed_no_send"
    assert result["case_ids"] == list(cli.ALL_CASES)
    assert result["actual_recipient_email"] == actions.TEST_RECIPIENT
    assert result["test_emails_sent"] == 0
    assert not db.docs.get(sender.TEST_LEDGER_COLLECTION)


def test_independent_cli_dry_run_continues_when_delivery_unknown_metric_is_unavailable(monkeypatch):
    db = FakeDB()
    cases = [_case(case_id) for case_id in cli.INITIAL_CASES]
    prepared = [
        sender.PreparedTestMessage(case, "[TEST PROCASA] test", "<html/>", "test", "report", "action")
        for case in cases
    ]
    monkeypatch.setattr(cli, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(cli, "mass_send_enabled", lambda: False)
    monkeypatch.setattr(cli, "build_owner_campaign_test_cases_live", lambda *_a, **_k: cases)
    monkeypatch.setattr(cli, "prepare_test_messages", lambda _cases: prepared)
    monkeypatch.setattr(cli, "send_test_messages", lambda *_a, **_k: pytest.fail("dry run must not send"))

    def unavailable_metric():
        raise cli.TestCampaignCLIError("delivery_unknown_metric_not_unambiguous")

    result = cli.run_test_batch(
        cli.INITIAL_CASES, test_mode=True, dry_run=True, db=db, health_reader=unavailable_metric,
    )
    assert result["status"] == "preflight_passed_no_send"
    assert result["delivery_unknown_before"] is None
    assert result["delivery_monitor_error_before"] == "delivery_unknown_metric_not_unambiguous"
    assert not db.docs.get(sender.TEST_LEDGER_COLLECTION)


def test_independent_cli_reports_only_controlled_live_builder_error_codes(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(cli, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(cli, "mass_send_enabled", lambda: False)
    monkeypatch.setattr(
        cli, "build_owner_campaign_test_cases_live",
        lambda *_a, **_k: (_ for _ in ()).throw(runtime.LiveTestCaseBuildError("property_image_unresolved")),
    )
    with pytest.raises(cli.TestCampaignCLIError, match="live_case_build_failed:property_image_unresolved"):
        cli.run_test_batch(
            cli.INITIAL_CASES, test_mode=True, dry_run=True, db=db, health_reader=lambda: 6,
        )
    assert not db.docs.get(sender.TEST_LEDGER_COLLECTION)


def test_independent_cli_send_does_not_fail_after_smtp_when_delivery_metric_is_unavailable(monkeypatch):
    db = FakeDB()
    cases = [_case(case_id) for case_id in cli.INITIAL_CASES]
    prepared = [
        sender.PreparedTestMessage(case, "[TEST PROCASA] test", "<html/>", "test", "report", "action")
        for case in cases
    ]
    monkeypatch.setattr(cli, "test_mode_enabled", lambda: True)
    monkeypatch.setattr(cli, "mass_send_enabled", lambda: False)
    monkeypatch.setattr(cli, "build_owner_campaign_test_cases_live", lambda *_a, **_k: cases)
    monkeypatch.setattr(cli, "prepare_test_messages", lambda _cases: prepared)
    monkeypatch.setattr(cli, "send_test_messages", lambda selected, **_k: [
        {
            "case_id": case.case_id,
            "status": "sent_to_test_recipient",
            "smtp_accepted": True,
            "recipient": actions.TEST_RECIPIENT,
        }
        for case in selected
    ])

    def unavailable_metric():
        raise cli.TestCampaignCLIError("delivery_unknown_metric_not_unambiguous")

    result = cli.run_test_batch(
        cli.INITIAL_CASES, test_mode=True, dry_run=False, db=db, health_reader=unavailable_metric,
    )
    assert result["status"] == "sent_delivery_monitor_unavailable"
    assert result["test_emails_sent"] == 3
    assert result["delivery_unknown_after"] is None
    assert result["new_delivery_unknown"] is None


def test_runner_uses_a_b_d_then_c_e_and_blocks_partial_batches():
    db = FakeDB()
    assert runner._next_test_phase(db) == ("INITIAL", ("A", "B", "D"))
    db.docs[sender.TEST_LEDGER_COLLECTION] = [
        {
            "campaign_id": actions.TEST_CAMPAIGN_ID,
            "test_mode": True,
            "actual_recipient_email": actions.TEST_RECIPIENT,
            "property_code": code,
            "delivery_status": "test_sent",
        }
        for code in ("5641", "16521", "16527")
    ]
    assert runner._next_test_phase(db) == ("REMAINING", ("C", "E"))
    db.docs[sender.TEST_LEDGER_COLLECTION].pop()
    with pytest.raises(runner.TestRunnerError, match="test_campaign_phase_state_unexpected"):
        runner._next_test_phase(db)


@pytest.mark.parametrize("action", sorted(runner.ALLOWED_ACTIONS))
def test_runner_accepts_only_the_fixed_admin_ui_actions(action):
    assert runner.validate_runner_request(
        {"action": action}, test_mode=True, mass_send_enabled=False,
    ) == action


@pytest.mark.parametrize("payload", [
    {"action": "SEND_TEST_EMAILS_ABD", "recipient": OWNER_EMAIL},
    {"action": "SEND_TEST_EMAILS_ABD", "property_code": "5641"},
    {"action": "SEND_TEST_EMAILS_ABD", "campaign_id": "other"},
    {"action": "SEND_TEST_EMAILS_ABD", "test_mode": False},
    {"action": "SEND_TEST_EMAILS_ABD", "proposed_price": 1},
    {"action": "SEND_ALL_CAMPAIGN_EMAILS"},
])
def test_runner_rejects_editable_or_non_whitelisted_parameters(payload):
    with pytest.raises(runner.TestRunnerError):
        runner.validate_runner_request(payload, test_mode=True, mass_send_enabled=False)


def test_runner_phase_buttons_cannot_skip_or_repeat_batches():
    db = FakeDB()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: [_case(case_id) for case_id in case_ids])
    monkeypatch.setattr(runner, "prepare_test_messages", lambda cases: [SimpleNamespace(case=case, html="<p>preview</p>") for case in cases])
    try:
        with pytest.raises(runner.TestRunnerError, match="test_campaign_phase_not_ready"):
            runner.generate_previews(db, expected_phase="REMAINING")
        assert runner.generate_previews(db, expected_phase="INITIAL")[1]["case_ids"] == ["A", "B", "D"]
    finally:
        monkeypatch.undo()


def test_runner_page_is_non_editable_admin_ui_and_send_is_post_only():
    response = runner.render_admin_runner_ui()
    body = response.body.decode("utf-8")
    assert response.headers["cache-control"] == "private, no-store"
    assert actions.TEST_RECIPIENT in body
    assert f"window.confirm('Enviar exclusivamente a {actions.TEST_RECIPIENT}')" in body
    assert "method:'POST'" in body
    assert "fetch('/captacion/test-runner'" in body
    assert "GENERATE_PREVIEWS_ABD" in body and "SEND_TEST_EMAILS_ABD" in body
    assert "GENERATE_PREVIEWS_CE" in body and "SEND_TEST_EMAILS_CE" in body
    assert "VERIFY_TEST_EVENTS" in body
    assert "<input" not in body.casefold()
    assert "recipient:" not in body and "property_code:" not in body
    preview = runner._preview_page([SimpleNamespace(case=_case("A"), html="<p>preview</p>")], phase="INITIAL")
    for label in ("RENDER_OK", "DATA_VALIDATION_OK", "SIGNED_URLS_OK", "RECIPIENT_GUARD_OK", "PRICE_MUTATION_GUARD_OK"):
        assert f"{label}: PASS" in preview


def test_captacion_runner_page_is_not_exposed():
    source = Path(__file__).parents[1] / "webhook.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    assert not any(
        isinstance(node, ast.AsyncFunctionDef) and node.name == "view_owner_campaign_test_runner"
        for node in tree.body
    )
    assert "@app.get(\"/captacion/test-runner\"" not in source.read_text(encoding="utf-8-sig")


def test_runner_refuses_campaign_ledger_rows_for_another_test_recipient():
    db = FakeDB()
    db.docs[sender.TEST_LEDGER_COLLECTION] = [{
        "campaign_id": actions.TEST_CAMPAIGN_ID,
        "test_mode": True,
        "actual_recipient_email": "someone-else@procasa.cl",
        "property_code": "5641",
        "delivery_status": "test_sent",
    }]
    with pytest.raises(runner.TestRunnerError, match="test_campaign_other_recipient_present"):
        runner._next_test_phase(db)


def test_runner_does_not_block_on_historical_delivery_unknown_six(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(runner, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: list(case_ids))
    monkeypatch.setattr(
        runner,
        "prepare_test_messages",
        lambda cases: [SimpleNamespace(case=SimpleNamespace(case_id=case_id)) for case_id in cases],
    )
    monkeypatch.setattr(
        runner,
        "send_test_messages",
        lambda cases, *, db: [{"case_id": case_id, "status": "sent_to_test_recipient"} for case_id in cases],
    )
    monkeypatch.setattr(runner, "_health_delivery_unknown", lambda: 6)
    result = runner.send_tests(db)
    assert result["phase"] == "INITIAL"
    assert result["case_ids"] == ["A", "B", "D"]
    assert result["test_emails_sent"] == 3
    assert result["delivery_unknown_before"] == result["delivery_unknown_after"] == 6
    assert result["new_delivery_unknown"] == 0


def test_delivery_unknown_limit_is_fixed_at_six_not_relative_to_before(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(runner, "build_owner_campaign_test_cases_live", lambda _db, *, case_ids: list(case_ids))
    monkeypatch.setattr(runner, "prepare_test_messages", lambda cases: [SimpleNamespace(case=SimpleNamespace(case_id=case)) for case in cases])
    monkeypatch.setattr(runner, "send_test_messages", lambda cases, *, db: [{"case_id": c, "status": "sent_to_test_recipient"} for c in cases])
    counts = iter((6, 7))
    monkeypatch.setattr(runner, "_health_delivery_unknown", lambda: next(counts))
    result = runner.send_tests(db)
    assert result["critical_stop"] is True
    assert result["status"] == "delivery_unknown_limit_exceeded"
    assert result["new_delivery_unknown"] == 1


@pytest.mark.parametrize("address", [OWNER_EMAIL, "alternate-test-recipient@procasa.cl"])
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


def test_more_than_five_or_non_explicit_cases_abort():
    with pytest.raises(sender.TestSenderError, match="test_case_count_invalid"):
        sender.validate_explicit_cases([_case("A")] * 6)
    with pytest.raises(sender.TestSenderError, match="explicit_test_cases_required"):
        sender.validate_explicit_cases("all")


def test_only_rendered_valid_test_links_pass_preflight():
    cases = [_case("A"), _case("B"), _case("C"), _case("D")]
    prepared = sender.prepare_test_messages(cases, render_case=_render)
    assert len(prepared) == 4
    assert all(item.subject.startswith("[TEST PROCASA]") for item in prepared)
    assert all(OWNER_EMAIL not in item.html for item in prepared)


def _approved_property_model(case):
    rental = case.operation == "ARRIENDO"
    unit = "UF/m²/mes" if rental else "UF/m² construido"
    action_label = "ACEPTAR NUEVO VALOR" if case.cta_type == "PRICE_AUTHORIZATION" else "Revisar recomendación con mi asesor"
    target = case.display_recommended_price
    price = f"{case.current_price:g} UF / mes" if rental else "5.000 UF"
    return {
        "code": case.property_code,
        "property_type": case.property_type,
        "commune": case.commune,
        "property_heading": f"{case.property_type} · {case.commune}",
        "operation_label": case.operation.title(),
        "operation_raw": case.operation,
        "is_rental": rental,
        "price_label": price,
        "feature_cards": [{"kind": "area", "label": "80 m² construidos"}],
        "context_note": "Contexto de mercado aprobado para la propiedad.",
        "image": {"available": True, "url": f"https://images.example.test/{case.property_code}.jpg", "source": "PROCASA_PUBLICATION"},
        "comparable": {
            "visible": True,
            "scope_label": "Publicaciones similares observadas.",
            "badge_label": "12 publicaciones similares analizadas",
            "display_status": "OK",
            "property_value_label": f"{23 if not rental else 0.25} {unit}",
            "median_label": f"{21 if not rental else 0.22} {unit}",
            "positioning_mode": "PRICE_M2",
            "positioning_property_value": 23,
            "positioning_reference_value": 21,
            "positioning_unit_label": unit,
            "positioning_property_label": f"23 {unit}",
            "positioning_reference_label": f"21 {unit}",
            "positioning_graph": {"cells": [{"reference": index == 2, "marker": index == 3} for index in range(5)]},
            "selected_n": 12,
            "universe_n": 80,
            "position_label": "en la zona media",
            "percentile_label": "50 percentil",
            "interpretation": "Referencias de publicaciones actuales.",
            "top3": [{"portal": "Yapo", "surface_label": "80 m² construidos", "rooms_label": "3 dorm.", "baths_label": "2 baño(s)", "price_label": "4.900 UF", "unit_label": unit, "listing_id": f"listing-{index}"} for index in range(3)],
            "land_top3": [],
            "land_reference_visible": False,
            "graph": {"cells": [{"active": index < 3, "marker": index == 3, "reference": index == 2} for index in range(5)]},
            "effective_type": "HOUSE",
            "evidence_level": "HIGH",
        },
        "diagnostic_text": "Referencias disponibles para revisión comercial.",
        "single_diagnostic_text": "La referencia individual y las publicaciones similares aportan contexto.",
        "recommendation_title": "Ajuste de arriendo sugerido" if rental and case.cta_type == "PRICE_AUTHORIZATION" else "Ajuste de precio sugerido" if case.cta_type == "PRICE_AUTHORIZATION" else "Revisar el posicionamiento con tu asesor",
        "recommendation_text": "Las referencias respaldan revisar el posicionamiento." if case.cta_type == "PRICE_AUTHORIZATION" else "Las señales deben revisarse con tu asesor.",
        "campaign_segment": case.evidence_segment,
        "position_summary": "Referencias estructurales aprobadas.",
        "recommended_price_label": (f"{target:g}".replace(".", ",") + " UF / mes" if rental else f"{target:,.0f} UF".replace(",", ".")) if target else None,
        "display_adjustment_label": "-8,1%" if rental and target else "-6,0%" if target else None,
        "document": {"visible": True, "copy": "Documento de respaldo disponible.", "type": case.document_type},
        "appraisal": {"visible": True, "kind": "INDIVIDUAL_APPRAISAL", "low_label": "4.700 UF", "mid_label": "4.850 UF" if not rental else "19,8 UF / mes", "high_label": "5.000 UF", "range_label": "4.700–5.000 UF", "gap_label": "+3,1%", "gap_amount_label": "150 UF", "position_label": "Cercano al rango", "adjustment_label": "-6,0%" if target and not rental else "-8,1%" if target else "", "metrics": []},
        "cta": {"primary_url": "", "primary_label": action_label, "secondary_url": "", "secondary_label": "Ver respaldo comercial"},
        "level": "HIGH",
        "source_recommendation": "con ajuste de precio sustentado" if target else "revisión con asesor",
        "recommendation": "con ajuste de precio sustentado" if target else "revisión con asesor",
        "diagnostic": "ALIGNED" if target else "REVIEW",
        "executive": {"name": case.executive, "email": "executive@procasa.cl", "phone": "+56 9 1234 5678", "initials": "EP"},
        "activity_90d": {"state": "KNOWN_POSITIVE", "total_leads": 2, "portals": [{"name": "Yapo", "count": 2}], "conversations": 1, "visits": 0},
    }


def test_default_adapter_renders_with_approved_v2_template_and_signed_public_links():
    cases = []
    for case in [_case("A"), _case("B"), _case("C"), _case("D")]:
        cases.append(replace(case, render_context={
            "property_model": _approved_property_model(case),
            "executives": [{"name": case.executive, "email": "executive@procasa.cl", "phone": "+56 9 1234 5678"}],
            "source_checks": {key: True for key in {
                "real_property_image", "executive_name", "executive_email", "executive_phone",
                "reference_correct", "comparables_compatible", "own_listing_excluded", "cta_correct",
            }},
        }))
    rendered = sender.prepare_test_messages(cases)
    assert len(rendered) == 4
    for item in rendered:
        assert 'class="shell shell-single' in item.html
        if item.case.case_id == "D":
            assert 'class="hero"' in item.html
            assert "Estamos preparando tu propiedad para un nuevo escenario de arriendo" in item.text
        else:
            assert 'class="hero-single"' in item.html
        if item.report_token:
            assert "https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=" in item.html
        assert "https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=" in item.html
        assert "localhost" not in item.html
        style = re.search(r"<style>(.*?)</style>", item.html, re.DOTALL)
        assert style is not None
        assert hashlib.sha256(style.group(1).encode("utf-8")).hexdigest() == "bdb24d7c3fddba0e22be5f5f27ef45c19e21f62877b58bffec72ab07ceabf307"
        assert "Actividad comercial" in item.text
        assert "Yapo 2" in item.text
    assert "tasación" not in rendered[3].text.casefold()


def test_single_test_render_does_not_require_executive_email_or_phone():
    case = _case("B")
    model = _approved_property_model(case)
    model["executive"] = {"name": case.executive, "email": "", "phone": "", "initials": "EP"}
    case = replace(case, render_context={
        "property_model": model,
        "executives": [{"name": case.executive, "email": "", "phone": ""}],
        "source_checks": {key: True for key in {
            "real_property_image", "executive_name", "reference_correct",
            "comparables_compatible", "own_listing_excluded", "cta_correct",
        }},
    })
    rendered = sender.prepare_test_messages([case])[0]
    assert "Tu ejecutivo PROCASA" in rendered.text
    assert case.executive in rendered.text


def test_missing_subject_metric_hides_comparison_and_downgrades_numeric_cta():
    property_model = email_v2._property_context(
        {
            "codigo_propiedad": "16521",
            "tipo_propiedad": "Casa",
            "comuna": "Talca",
            "operacion": "VENTA",
            "precio_publicado_uf": 1713.7,
            "superficie_construida": None,
            "superficie_util": None,
            "superficie_terreno": 200,
        },
        {
            "codigo": "16521",
            "tipo": "Casa",
            "comuna": "Talca",
            "operacion": "VENTA",
            "comparables_quality": "HIGH",
            "recommendation": "con ajuste de precio sustentado",
            "diagnostic": "ALIGNED",
            "nuevo_precio_objetivo_uf": 1600,
            "document_type": "INDIVIDUAL_APPRAISAL",
            "attachment": {"status": "ok"},
            "cta": {"primary_url": "https://example.test/action"},
        },
        {
            "client_validation_v3": {
                "comparable_display_status": "FULL",
                "effective_type": "HOUSE",
                "primary_surface": "built_m2",
                "integral_comparables": [{
                    "listing_id": "external-1",
                    "price_uf": 2000,
                    "built_m2": 64,
                    "price_m2_built": 31.25,
                    "price_m2": 31.25,
                }],
            },
            "supporting_evidence": {
                "appraisal": {
                    "estimated_low_uf": 1400,
                    "estimated_mid_uf": 1500,
                    "estimated_high_uf": 1800,
                    "current_price_uf": 1713.7,
                    "position_vs_appraisal": "WITHIN_RANGE",
                },
            },
        },
        {"name": "Ejecutivo PROCASA"},
    )

    assert property_model["comparable"]["visible"] is False
    assert property_model["comparable"]["hidden_reason"] == "PROPERTY_METRIC_MISSING"
    assert property_model["recommendation"] == "revisión con asesor"
    assert property_model["recommended_price_label"] is None
    assert [item["label"] for item in property_model["summary_metrics"]] == ["Precio publicado"]


def test_unique_sii_appraisal_area_flows_to_value_model_and_approved_renderer():
    appraisal_doc = {
        "codigo_propiedad": "16521",
        "tasacion_online": {
            "valor_comercial": {"uf": 1442},
            "valor_minimo_maximo": {"precio_minimo_uf": 1009, "precio_maximo_uf": 1809},
            "total_construccion_m2": 64,
        },
    }

    class AppraisalCollection:
        def find(self, query, _projection=None):
            assert query == {"codigo_propiedad": {"$in": ["16521", 16521]}}
            return [appraisal_doc]

    class AppraisalDB:
        def __getitem__(self, name):
            assert name == runtime.APPRAISAL_COLLECTION
            return AppraisalCollection()

    master = {
        "codigo": "16521",
        "caracteristicas": {"superficie_terreno": 200},
    }
    appraisal = runtime._appraisal(AppraisalDB(), master, runtime.VENTA, 1713.7)
    assert appraisal is not None
    assert appraisal["property_built_m2"] == 64
    assert appraisal["property_built_m2_source"] == "tasaciones.tasacion_online.total_construccion_m2:SII"

    dimensions = runtime._dimensions(master, appraisal)
    assert dimensions["built"] == 64
    assert dimensions["built_source"] == appraisal["property_built_m2_source"]
    assert dimensions["land"] == 200
    assert dimensions["useful"] is None and dimensions["total"] is None

    prop = {
        "codigo_propiedad": "16521", "tipo_propiedad": "Casa", "comuna": "Talca",
        "operacion": "VENTA", "precio_publicado_uf": 1713.7,
        "superficie_construida": dimensions["built"],
        "superficie_construida_m2": dimensions["built"],
        "superficie_construida_source": dimensions["built_source"],
        "superficie_terreno": dimensions["land"],
    }
    evidence = {
        "client_validation_v3": {
            "comparable_display_status": "FULL", "effective_type": "HOUSE",
            "primary_surface": "built_m2", "evidence_level": "HIGH",
            "integral_comparables": [
                {"listing_id": f"external-{index}", "price_uf": 2200 + index,
                 "built_m2": 70 + index, "price_m2_built": 30 + index,
                 "price_m2": 30 + index}
                for index in range(12)
            ],
            "land_references": [],
        },
        "supporting_evidence": {"appraisal": appraisal},
    }
    qa_row = {
        "codigo": "16521", "tipo": "Casa", "comuna": "Talca", "operacion": "VENTA",
        "comparables_quality": "HIGH", "recommendation": "revisión con asesor",
        "diagnostic": "REVIEW", "document_type": "INDIVIDUAL_APPRAISAL",
        "attachment": {"status": "ok"},
        "cta": {"primary_label": "Revisar recomendación con mi asesor"},
    }
    executive = {"name": "Ejecutivo PROCASA", "email": "", "phone": ""}
    property_model = email_v2._property_context(prop, qa_row, evidence, executive)
    assert property_model["property_built_m2"] == 64
    assert property_model["property_built_m2_source"] == appraisal["property_built_m2_source"]
    assert property_model["comparable"]["visible"] is True
    assert property_model["comparable"]["positioning_property_value"] == pytest.approx(1713.7 / 64)
    assert property_model["comparable"]["positioning_unit_label"] == "UF/m² construido"
    assert "64 m² construidos" in property_model["feature_cards"][0]["label"]

    html = email_v2.render_owner_campaign_email_v2(
        [property_model], email=actions.TEST_RECIPIENT, executives=[executive],
        base_url="https://procasa-chatbot-yr8d.onrender.com",
    )
    assert "64 m² construidos" in html
    assert "UF/m² construido" in html
    assert "26,8" in html


def test_unknown_activity_is_not_rendered_as_zero():
    case = _case("A")
    model = _approved_property_model(case)
    model["activity_90d"] = {"state": "UNKNOWN", "total_leads": None, "portals": [], "conversations": None, "visits": None}
    case = replace(case, render_context={
        "property_model": model,
        "executives": [{"name": case.executive, "email": "executive@procasa.cl", "phone": "+56 9 1234 5678"}],
        "source_checks": {key: True for key in {
            "real_property_image", "executive_name", "executive_email", "executive_phone",
            "reference_correct", "comparables_compatible", "own_listing_excluded", "cta_correct",
        }},
    })
    rendered = sender.prepare_test_messages([case])[0]
    assert "0 leads" not in rendered.text.casefold()
    assert "actividad comercial · últimos 90 días" not in rendered.text.casefold()


def test_mixed_evidence_cannot_be_prepared_with_price_authorization():
    case = _case(
        "C",
        cta_type="PRICE_AUTHORIZATION",
        raw_recommended_price=1600,
        display_recommended_price=1600,
        current_price=1700,
    )
    with pytest.raises(sender.TestSenderError, match="price_authorization_case_not_supported"):
        sender.validate_explicit_cases([case])


def test_renderer_adapter_fails_closed_without_prevalidated_image_or_executive():
    case = _case("A")
    model = _approved_property_model(case)
    model["image"] = {"available": False, "url": "", "source": "NONE"}
    case = replace(case, render_context={
        "property_model": model,
        "executives": [{"name": case.executive, "email": "executive@procasa.cl", "phone": "+56 9 1234 5678"}],
        "source_checks": {key: True for key in {
            "real_property_image", "executive_name", "executive_email", "executive_phone",
            "reference_correct", "comparables_compatible", "own_listing_excluded", "cta_correct",
        }},
    })
    with pytest.raises(sender.TestSenderError, match="real_property_image_required"):
        sender.prepare_test_messages([case])


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
    assert len(result) == 1
    assert result[0]["case_id"] == "A"
    assert result[0]["status"] == "sent_to_test_recipient"
    assert result[0]["delivery_status"] == "accepted_by_smtp_relay"
    assert result[0]["smtp_accepted"] is True
    assert result[0]["recipient"] == sender.TEST_RECIPIENT
    assert result[0]["rfc_message_id"].startswith("<")
    assert result[0]["sent_at"]
    smtp = FakeSMTP.instances[0]
    assert len(smtp.sent) == 1
    _, recipients, raw_message = smtp.sent[0]
    assert recipients == [sender.TEST_RECIPIENT]
    assert f"To: {sender.TEST_RECIPIENT}" in raw_message
    assert "Cc:" not in raw_message
    assert "Bcc:" not in raw_message
    assert OWNER_EMAIL not in raw_message
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["actual_recipient_email"] == sender.TEST_RECIPIENT
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["intended_owner_email"] == OWNER_EMAIL
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["delivery_status"] == "test_sent"
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["smtp_accepted"] is True
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["rfc_message_id"] == result[0]["rfc_message_id"]
    assert db.docs[sender.TEST_LEDGER_COLLECTION][0]["recipient"] == sender.TEST_RECIPIENT


def test_smtp_explicit_recipient_refusal_is_recorded_as_not_accepted():
    class RefusingSMTP(FakeSMTP):
        def sendmail(self, sender_address, recipients, message):
            self.sent.append((sender_address, list(recipients), message))
            return {sender.TEST_RECIPIENT: (550, b"refused")}

    db = FakeDB()
    with pytest.raises(sender.TestSenderError, match="test_recipient_refused_by_smtp"):
        sender.send_test_messages([_case("A")], render_case=_render, db=db, smtp_factory=RefusingSMTP)
    row = db.docs[sender.TEST_LEDGER_COLLECTION][0]
    assert row["smtp_accepted"] is False
    assert row["recipient"] == sender.TEST_RECIPIENT
    assert row["delivery_status"] == "test_recipient_refused"


def test_test_ledger_id_is_deterministic_and_concurrent_duplicate_aborts_before_smtp():
    class RaceDB(FakeDB):
        race_collection = None

        def __getitem__(self, name):
            if name == sender.TEST_LEDGER_COLLECTION and self.race_collection is not None:
                return self.race_collection
            return super().__getitem__(name)

    db = RaceDB()
    original_collection = FakeCollection(sender.TEST_LEDGER_COLLECTION, db)

    class ConcurrentInsertCollection:
        def find_one(self, query, projection=None):
            return original_collection.find_one(query, projection)

        def update_one(self, query, update, upsert=False):
            assert query == {"_id": f"{actions.TEST_CAMPAIGN_ID}:5641"}
            return SimpleNamespace(matched_count=1, modified_count=0, upserted_id=None)

    race_collection = ConcurrentInsertCollection()
    db.race_collection = race_collection
    with pytest.raises(sender.TestSenderError, match="test_campaign_case_already_registered"):
        sender.send_test_messages([_case("A")], render_case=_render, db=db, smtp_factory=FakeSMTP)
    assert FakeSMTP.instances == []


def _test_ledger(*, code, cta_type="PRICE_AUTHORIZATION", evidence_segment="PRICE_AUTHORIZATION_READY", operation="VENTA"):
    return {
        "campaign_id": actions.TEST_CAMPAIGN_ID,
        "campaign_version": actions.TEST_CAMPAIGN_VERSION,
        "property_code": code,
        "test_mode": True,
        "actual_recipient_email": actions.TEST_RECIPIENT,
        "intended_owner_email": OWNER_EMAIL,
        "executive": "Ejecutivo PROCASA",
        "operation": operation,
        "current_price_at_send": 5000.0 if operation == "VENTA" else 21.0,
        "cta_type": cta_type,
        "evidence_segment": evidence_segment,
        "document_type": "INDIVIDUAL_APPRAISAL",
        "display_recommended_price": 19.3 if operation == "ARRIENDO" else 4700.0,
    }


def _action_db(code="16521", *, cta_type="PRICE_AUTHORIZATION", evidence_segment="PRICE_AUTHORIZATION_READY", operation="VENTA"):
    db = FakeDB()
    db.docs[actions.LEDGER_COLLECTION] = [_test_ledger(code=code, cta_type=cta_type, evidence_segment=evidence_segment, operation=operation)]
    operation_key = "precio_arriendo" if operation == "ARRIENDO" else "precio_venta"
    operation_name = "Arriendo" if operation == "ARRIENDO" else "Venta"
    db.docs[actions.PROPERTY_COLLECTION] = [{
        "codigo": code,
        "tipo_operacion": {
            "tipo": operation_name,
            operation_key: {"precio_uf": 21 if operation == "ARRIENDO" else 5000, "precio_clp": 900000 if operation == "ARRIENDO" else 190000000},
        },
        "estado": {"ejecutivo": "Ejecutivo PROCASA"},
    }]
    return db


def _action_token(code="16521", action=actions.ACCEPT_PRICE_ACTION, *, recipient=actions.TEST_RECIPIENT, document_type=None):
    from campanas.test_mode import issue_test_token
    return issue_test_token(
        campaign_id=actions.TEST_CAMPAIGN_ID,
        property_code=code,
        action=action,
        secret=SECRET,
        expires_at=2_000_000_000,
        recipient=recipient,
        document_type=document_type,
    )


def test_test_price_authorization_requires_confirmation_and_keeps_exact_live_price(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    db = _action_db()
    before = dict(db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_venta"])
    token = _action_token()
    click = actions.process_test_action(token, db=db)
    assert click["requires_confirmation"] is True
    ledger_row = db.docs[actions.LEDGER_COLLECTION][0]
    assert [event["event"] for event in ledger_row["response_events"]] == [
        "cta_clicked", "price_confirm_page_opened"
    ]
    assert ledger_row["owner_response"]["status"] == "PENDING"
    result = actions.process_test_action(token, db=db, confirmed=True)
    after = db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_venta"]
    events = db.docs[actions.LEDGER_COLLECTION][0]["response_events"]
    assert result["event"] == "price_authorized"
    assert result["test_mode"] is True
    assert result["test_price_mutation"] is False
    assert result["live_price_before"] == result["live_price_after"] == before
    assert after == before
    assert [event["event"] for event in events] == [
        "cta_clicked", "price_confirm_page_opened", "price_authorized"
    ]
    summary = ledger_row["owner_response"]
    assert summary["status"] == "PRICE_AUTHORIZED"
    assert summary["first_authorized_at"] == summary["last_authorized_at"]
    assert summary["authorized_value"] == 4700.0
    assert summary["current_value_at_campaign"] == 5000.0
    assert all(event["test_mode"] is True and event["property_code"] == "16521" for event in events)
    authorization = next(event for event in events if event["event"] == "price_authorized")
    assert authorization["campaign_id"] == "owner_price_campaign_test_20260923"
    assert authorization["campaign_version"] == "owner_campaign_test_20260923"
    assert authorization["action"] == actions.ACCEPT_PRICE_ACTION
    assert authorization["current_value_at_campaign"] == 5000
    assert authorization["proposed_value"] == 4700
    assert authorization["event_at"] is not None
    assert set(db.requested) <= {actions.LEDGER_COLLECTION, actions.PROPERTY_COLLECTION}


def test_accept_price_http_get_and_post_render_confirmation_and_persist_events(monkeypatch):
    db = _action_db(code="5641")
    token = _action_token("5641")
    price_before = dict(db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_venta"])

    get_response = actions.handle_test_action(token, db=db, confirmed=False)

    page = get_response.body.decode("utf-8")
    assert get_response.status_code == 200
    assert get_response.headers["cache-control"] == "private, no-store"
    assert "<html" in page and "name=\"viewport\"" in page
    assert "Confirma el nuevo valor" in page
    assert "CONFIRMAR AUTORIZACIÓN" in page
    assert "El precio publicado no será modificado automáticamente." in page
    row = db.docs[actions.LEDGER_COLLECTION][0]
    assert [event["event"] for event in row["response_events"]] == [
        "cta_clicked", "price_confirm_page_opened"
    ]
    assert row["owner_response"]["status"] == "PENDING"
    assert all(event["event"] != "price_authorized" for event in row["response_events"])

    post_response = actions.handle_test_action(token, db=db, confirmed=True)

    assert post_response.status_code == 200
    assert "Autorización registrada" in post_response.body.decode("utf-8")
    row = db.docs[actions.LEDGER_COLLECTION][0]
    assert [event["event"] for event in row["response_events"]] == [
        "cta_clicked", "price_confirm_page_opened", "price_authorized"
    ]
    assert row["response_events"][-1]["property_code"] == "5641"
    price_after = db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_venta"]
    assert price_before == price_after


def test_authorized_status_stays_sticky_after_reopening_report_and_contacting_advisor(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    db = _action_db(code="5641")
    accept_token = _action_token("5641")
    actions.process_test_action(accept_token, db=db)
    actions.process_test_action(accept_token, db=db, confirmed=True)
    ledger_row = db.docs[actions.LEDGER_COLLECTION][0]
    first_authorized_at = ledger_row["owner_response"]["first_authorized_at"]
    event_count_after_authorization = len(ledger_row["response_events"])

    # Repeated POST for the same signed token is idempotent.
    actions.process_test_action(accept_token, db=db, confirmed=True)
    assert len(ledger_row["response_events"]) == event_count_after_authorization

    report_token = _action_token(
        "5641", action=actions.REPORT_ACTION, document_type="INDIVIDUAL_APPRAISAL"
    )
    actions.record_test_report_opened("5641", token=report_token, db=db)
    actions.record_test_report_opened("5641", token=report_token, db=db)
    advisor_token = _action_token("5641", action=actions.ADVISOR_ACTION)
    actions.process_test_action(advisor_token, db=db)

    summary = ledger_row["owner_response"]
    assert summary["status"] == "PRICE_AUTHORIZED"
    assert summary["first_authorized_at"] == first_authorized_at
    assert summary["last_authorized_at"] == first_authorized_at
    assert summary["authorized_value"] == 4700.0
    assert summary["current_value_at_campaign"] == 5000.0
    assert summary["last_report_opened_at"] is not None
    assert summary["advisor_requested_at"] is not None
    assert summary["updated_at"] >= summary["advisor_requested_at"]
    events = ledger_row["response_events"]
    assert sum(event["event"] == "report_opened" for event in events) == 2
    assert len({event["event_id"] for event in events}) == len(events)


def test_existing_authorization_event_normalizes_summary_without_losing_authorization(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    db = _action_db(code="5641")
    ledger_row = db.docs[actions.LEDGER_COLLECTION][0]
    authorized_at = "2026-09-25T17:04:00+00:00"
    ledger_row["response_events"] = [{
        "event_id": "old-authorization-id",
        "event": "price_authorized",
        "event_at": authorized_at,
        "authorized_value": 4700.0,
        "current_value_at_campaign": 5000.0,
    }]
    ledger_row["response_event_ids"] = ["old-authorization-id"]

    report_token = _action_token(
        "5641", action=actions.REPORT_ACTION, document_type="INDIVIDUAL_APPRAISAL"
    )
    actions.record_test_report_opened("5641", token=report_token, db=db)

    summary = ledger_row["owner_response"]
    assert summary["status"] == "PRICE_AUTHORIZED"
    assert summary["first_authorized_at"].isoformat() == authorized_at
    assert summary["last_authorized_at"].isoformat() == authorized_at
    assert summary["authorized_value"] == 4700.0
    assert summary["current_value_at_campaign"] == 5000.0
    assert ledger_row["response_events"][0]["event_id"] == "old-authorization-id"
    assert [event["event"] for event in ledger_row["response_events"][-2:]] == [
        "cta_clicked", "report_opened"
    ]


def test_accept_price_invalid_token_returns_404_without_events():
    db = _action_db(code="5641")
    response = actions.handle_test_action("invalid-token", db=db, confirmed=False)
    assert response.status_code == 404
    assert db.docs[actions.LEDGER_COLLECTION][0].get("response_events", []) == []


def test_document_priority_remains_individual_then_communal_then_none(monkeypatch):
    monkeypatch.setattr(runtime, "_appraisal", lambda *_args: {"estimated_mid_uf": 1000})
    monkeypatch.setattr(runtime, "_communal", lambda *_args: {"relevant_metrics": {"n_observations": 10}})
    assert runtime._support(object(), {}, "VENTA", 1000)[0] == "INDIVIDUAL_APPRAISAL"

    monkeypatch.setattr(runtime, "_appraisal", lambda *_args: None)
    assert runtime._support(object(), {}, "VENTA", 1000)[0] == "COMMUNAL_MARKET_REPORT"

    monkeypatch.setattr(runtime, "_communal", lambda *_args: None)
    assert runtime._support(object(), {}, "VENTA", 1000)[0] == "NONE"


def test_test_rent_authorization_requires_confirmation_without_changing_live_rent(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    db = _action_db(code="16527", operation="ARRIENDO")
    before = dict(db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_arriendo"])
    token = _action_token("16527")
    actions.process_test_action(token, db=db)
    result = actions.process_test_action(token, db=db, confirmed=True)
    after = db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_arriendo"]
    assert result["event"] == "price_authorized"
    assert result["test_mode"] is True
    assert result["live_price_before"] == result["live_price_after"] == before
    assert after == before
    assert [event["event"] for event in db.docs[actions.LEDGER_COLLECTION][0]["response_events"]] == [
        "cta_clicked", "price_confirm_page_opened", "price_authorized"
    ]


def test_advisor_cta_writes_only_test_events_to_adjustment_ledger(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    db = _action_db(cta_type="ADVISOR_REVIEW", evidence_segment="MIXED_EVIDENCE")
    result = actions.process_test_action(_action_token(action=actions.ADVISOR_ACTION), db=db)
    assert result["event"] == "advisor_review_requested"
    events = db.docs[actions.LEDGER_COLLECTION][0]["response_events"]
    assert [event["event"] for event in events] == [
        "cta_clicked", "advisor_review_requested"
    ]
    assert all(event["executive"] == "Ejecutivo PROCASA" for event in events)
    assert all(event["campaign_id"] == actions.TEST_CAMPAIGN_ID and event["test_mode"] is True for event in events)
    assert "contactos" not in db.requested
    assert "price_updates" not in db.requested
    assert actions.PROPERTY_COLLECTION not in db.requested


def test_report_open_records_in_adjustment_ledger(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "true")
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_TOKEN_SECRET", SECRET)
    db = _action_db(cta_type="REPORT_ONLY", evidence_segment="MIXED_EVIDENCE")
    token = _action_token("16521", action=actions.REPORT_ACTION, document_type="INDIVIDUAL_APPRAISAL")
    event_id = actions.record_test_report_opened("16521", token=token, db=db)
    events = db.docs[actions.LEDGER_COLLECTION][0]["response_events"]
    assert len(events) == 2
    assert events[0]["event"] == "cta_clicked"
    assert events[1]["event_id"] == event_id
    assert events[1]["event"] == "report_opened"
    assert events[1]["event_at"]
    assert events[1]["test_mode"] is True
    assert events[1]["campaign_id"] == "owner_price_campaign_test_20260923"
    assert events[1]["property_code"] == "16521"
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


def test_invalid_and_cross_property_signed_actions_fail_before_any_event():
    db = _action_db(code="5641")
    token = _action_token("16521", actions.ADVISOR_ACTION)
    with pytest.raises(actions.OwnerCampaignTestError, match="test_campaign_ledger_missing"):
        actions.process_test_action(token, db=db)
    tampered = token[:-1] + ("A" if token[-1:] != "A" else "B")
    with pytest.raises(actions.OwnerCampaignTestError, match="test_action_token_invalid"):
        actions.process_test_action(tampered, db=db)
    expired = actions.issue_test_link_token(
        property_code="5641", action=actions.ADVISOR_ACTION, now_epoch=1,
    )
    with pytest.raises(actions.OwnerCampaignTestError, match="test_action_token_invalid"):
        actions.process_test_action(expired, db=db)
    assert all(not row.get("response_events") for row in db.docs[actions.LEDGER_COLLECTION])


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
    assert "request.method.upper() == 'POST'" in body
    assert "handle_campana_respuesta" not in body
    assert "_require_captacion_report_admin" not in body


def test_http_campaign_runner_is_secret_gated_and_bypasses_crm():
    source = Path(__file__).parents[1] / "webhook.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    route = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_owner_campaign_test_execute"
    )
    body = ast.unparse(route)
    assert "/internal/owner-campaign-test-execute" in body
    assert "X-Owner-Campaign-Test-Secret" in body
    assert "trigger_secret_matches" in body
    assert "parse_trigger_request" in body
    assert "run_test_batch" in body
    assert "_require_captacion_report_admin" not in body
    assert "get_current_user_doc" not in body
    assert "payload.get('recipient')" not in body
    full_source = source.read_text(encoding="utf-8-sig")
    assert "/internal/owner-campaign-test-runner" not in full_source
    assert "/captacion/test-runner" not in body


def test_live_dimensions_use_casa_m2_as_built_area_with_source_lineage():
    dimensions = runtime._dimensions({
        "caracteristicas": {
            "casa_m2": 560,
            "superficie_terreno": 5000,
        },
    })

    assert dimensions["built"] == 560
    assert dimensions["built_source"] == "caracteristicas.casa_m2"
    assert dimensions["land"] == 5000


def test_live_dimensions_preserve_built_area_source_priority():
    dimensions = runtime._dimensions({
        "caracteristicas": {
            "superficie_construida": 100,
            "m2_construidos": 90,
            "casa_m2": 80,
        },
    })

    assert dimensions["built"] == 100
    assert dimensions["built_source"] == "caracteristicas.superficie_construida"


def test_qa_fixture_is_generated_from_approved_evidence_and_contains_only_safe_fields():
    fixture = runtime._load_qa_evidence_fixture()

    assert fixture["source_report_filename"] == "owner_campaign_structural_evidence_dry_run_final_20260923.json"
    assert re.fullmatch(r"[0-9a-f]{64}", fixture["source_report_sha256"])
    assert fixture["record_count"] == len(fixture["segments_by_code"]) == 406
    assert fixture["segment_counts"] == {
        "COMPETITIVE_LOW_RESPONSE": 9,
        "INSUFFICIENT_EVIDENCE": 172,
        "MIXED_EVIDENCE": 180,
        "STRONG_PRICE_ADJUSTMENT": 45,
    }
    assert fixture["cases"]["A"]["appraisal"] == {
        "low_uf": 2357.0, "mid_uf": 2666.0, "high_uf": 2857.0,
    }
    assert fixture["cases"]["A"]["raw_recommended_price_uf"] == 2857.0
    assert fixture["cases"]["A"]["display_recommended_price_uf"] == 2857.0
    assert fixture["cases"]["B"]["total_construccion_m2"] == 64.0
    assert fixture["cases"]["B"]["built_m2_source"].endswith(":SII")
    assert fixture["cases"]["D"]["rent_estimate_uf"] == 16.0
    serialized = json.dumps(fixture).casefold()
    assert "@" not in serialized
    assert "secret" not in serialized
    assert "token" not in serialized


def test_production_runtime_uses_qa_fixture_no(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_CAMPAIGN_TEST_MODE", "false")
    monkeypatch.setattr(runtime, "QA_EVIDENCE_PATH", tmp_path / "must-not-be-read.json")

    with pytest.raises(runtime.LiveTestCaseBuildError, match="qa_fixture_test_mode_only"):
        runtime._load_qa_evidence_fixture()


def test_qa_fixture_conflicting_live_appraisal_fails_closed():
    fixture_case = {
        "appraisal": {"low_uf": 1009.0, "mid_uf": 1442.0, "high_uf": 1809.0},
        "total_construccion_m2": 64.0,
    }
    with pytest.raises(runtime.LiveTestCaseBuildError, match="qa_fixture_conflicts_with_live_appraisal"):
        runtime._assert_live_appraisal_matches_fixture({
            "estimated_low_uf": 1009.0,
            "estimated_mid_uf": 1400.0,
            "estimated_high_uf": 1809.0,
            "property_built_m2": 64.0,
        }, fixture_case, "VENTA")


def test_generate_all_previews_is_preview_only_and_exposes_fixture_provenance(monkeypatch):
    cases = [SimpleNamespace(
        case_id=case_id, property_code=f"test-{case_id}", operation="VENTA",
        evidence_segment="MIXED_EVIDENCE", document_type="NONE", cta_type="ADVISOR_REVIEW",
        portfolio_cases=(),
    ) for case_id in ("A", "B", "C", "D", "E")]
    monkeypatch.setattr(runner, "build_owner_campaign_test_cases_live", lambda _db, case_ids: cases)
    monkeypatch.setattr(
        runner,
        "prepare_test_messages",
        lambda requested: [SimpleNamespace(case=requested[0], html=f"<p>{requested[0].case_id}</p>")],
    )
    monkeypatch.setattr(runner, "qa_evidence_metadata", lambda: {
        "source": "FROZEN_APPROVED_REPORT", "sha256": "a" * 64, "records": 406,
    })

    page, result = runner.generate_all_previews(object())

    assert result["case_ids"] == ["A", "B", "C", "D", "E"]
    assert result["all_preflight_pass"] is True
    assert result["owner_emails_sent"] == 0
    for case_id in ("A", "B", "C", "D", "E"):
        assert f"PREVIEW_{case_id}=PASS" in page
        assert f"{case_id}_ERROR_CODE=NONE" in page
    assert "QA_EVIDENCE_SOURCE: FROZEN_APPROVED_REPORT" in page
    assert "QA_EVIDENCE_RECORDS: 406" in page
    assert "sin envío" in page


def test_preview_failure_reports_safe_per_case_error_codes(monkeypatch):
    monkeypatch.setattr(runner, "build_owner_campaign_test_cases_live", lambda *_a, **_k: (_ for _ in ()).throw(
        runtime.LiveTestCaseBuildError(
            "qa_cases_failed",
            case_errors={
                "A": "NONE", "B": "case_b_live_mixed_evidence_contract_failed",
                "C": "NONE", "D": "NONE", "E": "NONE",
            },
        ),
    ))

    with pytest.raises(runner.TestRunnerError) as exc:
        runner._generate_previews_for_cases(object(), "ALL_NO_SEND", ("A", "B", "C", "D", "E"))

    assert str(exc.value) == (
        "preview_case_build_failed;A_ERROR_CODE=NONE;"
        "B_ERROR_CODE=case_b_live_mixed_evidence_contract_failed;"
        "C_ERROR_CODE=NONE;D_ERROR_CODE=NONE;E_ERROR_CODE=NONE"
    )
    assert "@" not in str(exc.value)
