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

    def find(self, query=None, projection=None):
        query = query or {}
        return [dict(doc) for doc in self.docs if all(doc.get(key) == value for key, value in query.items())]

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
    assert actions.TEST_RECIPIENT == "pgalleguillos@procasa.cl"
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
    for relative in (
        "campanas/owner_campaign_test_actions.py",
        "campanas/owner_campaign_test_sender.py",
        "campanas/owner_campaign_test_runner.py",
        "campanas/owner_campaign_test_runtime.py",
        "campanas/private_report.py",
        "tests/test_owner_campaign_test_safety.py",
    ):
        assert old_test_address not in (root / relative).read_text(encoding="utf-8")


def test_live_case_builder_can_limit_first_phase_to_a_b_d(monkeypatch):
    loaded = {}
    monkeypatch.setattr(runtime, "_load_code_docs", lambda _db, codes: loaded.update(codes=codes) or {code: {"code": code} for code in codes})
    monkeypatch.setattr(runtime, "_build_case", lambda _db, doc, case_id, **_kwargs: SimpleNamespace(case_id=case_id, doc=doc))
    monkeypatch.setattr(runtime, "_build_portfolio", lambda *_args: pytest.fail("E must not block the first phase"))
    cases = runtime.build_owner_campaign_test_cases_live(object(), case_ids=("A", "B", "D"))
    assert [case.case_id for case in cases] == ["A", "B", "D"]
    assert loaded["codes"] == {"5641", "16521", "16527"}


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
        assert "class=\"shell shell-single\"" in item.html
        assert "class=\"hero-single\"" in item.html
        if item.report_token:
            assert "https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=" in item.html
        assert "https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=" in item.html
        assert "localhost" not in item.html
        style = re.search(r"<style>(.*?)</style>", item.html, re.DOTALL)
        assert style is not None
        assert hashlib.sha256(style.group(1).encode("utf-8")).hexdigest() == "5da1d474e021f7ffc983167ba33956e22a8351dc480f6a32a23a47086f54f54b"
        assert "Actividad comercial" in item.text
        assert "Yapo 2" in item.text
    assert "tasación" not in rendered[3].text.casefold()


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
    assert result[0]["provider_message_id"] == "NOT_EXPOSED_BY_SMTP"
    assert result[0]["message_id"].startswith("<")
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
        "property_code": code,
        "test_mode": True,
        "actual_recipient_email": actions.TEST_RECIPIENT,
        "intended_owner_email": OWNER_EMAIL,
        "executive": "Ejecutivo PROCASA",
        "operation": operation,
        "cta_type": cta_type,
        "evidence_segment": evidence_segment,
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
    authorization = events[1]
    assert authorization["campaign_id"] == "owner_price_campaign_test_20260924"
    assert authorization["campaign_version"] == "owner_campaign_test_20260924"
    assert authorization["event_name"] == "price_authorized_test"
    assert authorization["operation"] == "VENTA"
    assert authorization["current_price"] == 5000
    assert authorization["proposed_price"] == 4700
    assert authorization["event_at"] is not None
    assert set(db.requested) <= {actions.LEDGER_COLLECTION, actions.PROPERTY_COLLECTION, actions.EVENT_COLLECTION}


def test_test_rent_authorization_appends_events_without_changing_live_rent():
    db = _action_db(code="16527", operation="ARRIENDO")
    before = dict(db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_arriendo"])
    result = actions.process_test_action(_action_token("16527"), db=db)
    after = db.docs[actions.PROPERTY_COLLECTION][0]["tipo_operacion"]["precio_arriendo"]
    assert result["event"] == "price_authorized"
    assert result["test_mode"] is True
    assert result["live_price_before"] == result["live_price_after"] == before
    assert after == before
    assert [event["event_type"] for event in db.docs[actions.EVENT_COLLECTION]] == ["cta_clicked", "price_authorized"]


def test_advisor_cta_writes_only_test_events_and_no_legacy_response_collections():
    db = _action_db(cta_type="ADVISOR_REVIEW", evidence_segment="MIXED_EVIDENCE")
    result = actions.process_test_action(_action_token(action=actions.ADVISOR_ACTION), db=db)
    assert result["event"] == "advisor_review_requested"
    assert [event["event_type"] for event in db.docs[actions.EVENT_COLLECTION]] == [
        "advisor_review_requested"
    ]
    assert all(event["executive"] == "Ejecutivo PROCASA" for event in db.docs[actions.EVENT_COLLECTION])
    assert all(event["campaign_id"] == actions.TEST_CAMPAIGN_ID and event["test_mode"] is True for event in db.docs[actions.EVENT_COLLECTION])
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
    assert events[0]["campaign_id"] == "owner_price_campaign_test_20260924"
    assert events[0]["event_name"] == "report_opened_test"
    assert events[0]["property_code"] == "16521"
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
    assert db.docs.get(actions.EVENT_COLLECTION, []) == []


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


def test_internal_runner_route_uses_existing_admin_guard_only():
    source = Path(__file__).parents[1] / "webhook.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    route = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_owner_campaign_test_runner"
    )
    body = ast.unparse(route)
    assert "/internal/owner-campaign-test-runner" in body
    assert "handle_admin_runner_request" in body
    assert "_require_captacion_report_admin" in body
