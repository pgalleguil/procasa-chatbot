from __future__ import annotations

from datetime import datetime, timezone

from campanas.owner_campaign_confirmation_page import render_decision_page, render_success_page
from campanas.owner_campaign_confirmation_page import gradual_option_enabled
from campanas.owner_campaign_live_events import _campaign_whatsapp_text, persist_live_event


def _decision_html(recommended_pct: int = 8) -> str:
    gradual_pct = 3 if recommended_pct >= 7 else 2
    return render_decision_page(
        recommended_pct=recommended_pct,
        recommended_price="2.852 UF",
        current_price="3.100 UF",
        gradual_pct=gradual_pct,
        gradual_price="2.914 UF",
        recommended_url="https://example.test/post?selected_adjustment_type=RECOMMENDED",
        gradual_url="https://example.test/post?selected_adjustment_type=GRADUAL",
        advisor_url="https://example.test/contact",
        gradual_enabled=True,
    )


def test_confirmation_page_has_only_decision_content_and_no_property_identifiers():
    page = _decision_html()
    assert "Confirma tu ajuste de precio" in page
    assert "Selecciona la opción que mejor se ajuste a tu decisión." in page
    assert "RECOMENDACIÓN PROCASA" in page
    assert "PRECIO ACTUAL" in page and "3.100 UF" in page
    assert "AUTORIZAR AJUSTE RECOMENDADO" in page
    assert "AUTORIZAR AJUSTE GRADUAL" in page
    assert "SOLICITAR CONTACTO DE MI EJECUTIVO" in page
    assert "Tu decisión quedará registrada de forma segura." in page
    assert "Código" not in page and "campaign_id" not in page
    assert "Dormitorios" not in page and "comuna" not in page.casefold()
    assert "<meta name=\"viewport\"" in page
    assert "@media(max-width:600px)" in page and "grid-template-columns:1fr" in page


def test_five_percent_recommendation_does_not_offer_duplicate_gradual_adjustment():
    page = render_decision_page(
        recommended_pct=5, recommended_price="2.945 UF", current_price="3.100 UF",
        gradual_pct=2, gradual_price="3.038 UF",
        recommended_url="https://example.test/recommended",
        gradual_url="https://example.test/gradual",
        advisor_url="https://example.test/contact", gradual_enabled=False,
    )
    assert "5%" in page
    assert "AUTORIZAR AJUSTE GRADUAL" not in page
    assert "SOLICITAR CONTACTO DE MI EJECUTIVO" in page


def test_gradual_success_page_shows_only_the_owner_authorized_choice():
    gradual = render_success_page(
        selected_type="GRADUAL", selected_pct=6, selected_price="2.914 UF",
        recommended_pct=8, recommended_price="2.852 UF",
    )
    assert "Ajuste gradual autorizado" in gradual
    assert "RECOMENDACIÓN PROCASA" not in gradual
    assert "8% → 2.852 UF" not in gradual
    assert "AJUSTE AUTORIZADO" in gradual and "6%" in gradual and "2.914 UF" in gradual
    assert "antes de que el cambio se vea reflejado" in gradual

    recommended = render_success_page(
        selected_type="RECOMMENDED", selected_pct=8, selected_price="2.852 UF",
        recommended_pct=8, recommended_price="2.852 UF",
    )
    assert "Ajuste autorizado correctamente" in recommended
    assert "AJUSTE AUTORIZADO" in recommended and "NUEVO VALOR AUTORIZADO" in recommended


def test_advisor_success_never_claims_price_authorization():
    page = render_success_page(selected_type="ADVISOR_REVIEW", selected_pct=None, selected_price="")
    assert "Solicitud enviada" in page
    assert "Tu ejecutivo será informado" in page
    assert "precio autorizado" not in page.casefold()


def test_campaign_gradual_feature_flag_defaults_disabled_and_can_be_enabled(monkeypatch):
    monkeypatch.delenv("OWNER_CAMPAIGN_GRADUAL_OPTION_ENABLED", raising=False)
    assert gradual_option_enabled() is False
    monkeypatch.setenv("OWNER_CAMPAIGN_GRADUAL_OPTION_ENABLED", "true")
    assert gradual_option_enabled() is True
    assert gradual_option_enabled("owner_price_sucre_wave1_20260929") is False


def test_current_campaign_selection_page_hides_gradual_and_keeps_recommended_and_advisor():
    page = render_decision_page(
        recommended_pct=10, recommended_price="2.790 UF", current_price="3.100 UF",
        gradual_pct=3, gradual_price="3.007 UF",
        recommended_url="https://example.test/recommended",
        gradual_url="https://example.test/gradual",
        advisor_url="https://example.test/advisor", gradual_enabled=False,
    )
    assert "RECOMENDACIÓN PROCASA" in page
    assert "AUTORIZAR AJUSTE RECOMENDADO" in page
    assert "¿Prefieres conversarlo antes de decidir?" in page
    assert "SOLICITAR CONTACTO DE MI EJECUTIVO" in page
    assert "OPCIÓN GRADUAL" not in page
    assert "AUTORIZAR AJUSTE GRADUAL" not in page
    assert "3.007 UF" not in page
    assert ".options.one-option .card{min-height:0" in page
    assert "AUTORIZAR AJUSTE RECOMENDADO" in page
    assert "Tu decisión quedará registrada" in page
    assert "min-height:48px" in page
    assert "grid-template-columns:1fr" in page
    assert "font-size:24px" in page
    assert "max-width:170px" in page


def test_future_gradual_path_keeps_5641_recommendation_and_selected_price_separate():
    page = render_decision_page(
        recommended_pct=10, recommended_price="2.790 UF", current_price="3.100 UF",
        gradual_pct=3, gradual_price="3.007 UF",
        recommended_url="https://example.test/recommended",
        gradual_url="https://example.test/gradual",
        advisor_url="https://example.test/advisor", gradual_enabled=True,
    )
    assert "10%" in page and "2.790 UF" in page
    assert "OPCIÓN GRADUAL" in page and "3%" in page and "3.007 UF" in page
    assert "8%" not in page

    success = render_success_page(
        selected_type="GRADUAL", selected_pct=3, selected_price="3.007 UF",
        recommended_pct=10, recommended_price="2.790 UF",
    )
    assert "3%" in success and "3.007 UF" in success
    assert "10%" not in success and "2.790 UF" not in success

    at = datetime(2026, 9, 29, tzinfo=timezone.utc)
    whatsapp = _campaign_whatsapp_text({
        "operation": "VENTA", "property_code": "5641", "property_type": "Departamento",
        "commune": "La Cisterna", "owner_name": "QA", "executive_name": "Ejecutivo QA",
        "campaign_id": "qa-run-fresh", "previous_price": 3100,
        "recommended_adjustment_pct": 10, "recommended_price": 2790,
        "gradual_adjustment_pct": 3, "gradual_price": 3007,
        "selected_adjustment_type": "GRADUAL", "selected_adjustment_pct": 3,
        "selected_price": 3007,
    }, "price_authorized", at)
    assert "Recomendación PROCASA: 10% → 2.790 UF" in whatsapp
    assert "Ajuste gradual autorizado: 3% → 3.007 UF" in whatsapp


def test_whatsapp_copy_identifies_recommended_and_gradual_decisions():
    common = {
        "operation": "VENTA", "property_code": "123", "property_type": "Departamento",
        "commune": "Ñuñoa", "owner_name": "Ana", "executive_name": "Luis",
        "campaign_id": "campaign-1", "current_price": 3100, "recommended_price": 2852,
        "recommended_adjustment_pct": 8,
        "selected_price": 2914,
    }
    at = datetime(2026, 9, 29, tzinfo=timezone.utc)
    recommended = _campaign_whatsapp_text({**common, "selected_adjustment_type": "RECOMMENDED", "selected_adjustment_pct": 8, "selected_price": 2852}, "price_authorized", at)
    gradual = _campaign_whatsapp_text({**common, "selected_adjustment_type": "GRADUAL", "selected_adjustment_pct": 6}, "price_authorized", at)
    assert "✅ AJUSTE DE PRECIO CONFIRMADO" in recommended
    assert "Ajuste recomendado autorizado: 8% → 2.852 UF" in recommended
    assert "✅ AJUSTE GRADUAL CONFIRMADO" in gradual
    assert "Ajuste gradual autorizado: 6% → 2.914 UF" in gradual
    for message in (recommended, gradual):
        assert "Código: 123" in message
        assert "Departamento · Ñuñoa" in message
        assert "Precio actual: 3.100 UF" in message
        assert "Recomendación PROCASA: 8% → 2.852 UF" in message
        assert "Ejecutivo: Luis" in message
    assert "Nuevo precio autorizado: 2.852 UF" in recommended
    assert "Nuevo precio autorizado: 2.914 UF" in gradual


class _FakeUpdateResult:
    def __init__(self, modified_count: int):
        self.modified_count = modified_count


class _FakeLedger:
    def __init__(self):
        self.row = {
            "_id": "campaign-1:123", "campaign_id": "campaign-1", "property_code": "123",
            "authorization_status": "PENDING", "recommended_adjustment_pct": 8,
            "recommended_price": 2852, "events": [],
        }

    def update_one(self, query, update):
        if self.row["authorization_status"] == "PRICE_AUTHORIZED" and "$ne" in query.get("authorization_status", {}):
            return _FakeUpdateResult(0)
        pushed = update["$push"]["events"]
        self.row["events"].extend(pushed.get("$each", [pushed]))
        for key, value in update.get("$set", {}).items():
            self.row[key] = value
        return _FakeUpdateResult(1)

    def find_one(self, _query):
        return self.row


class _FakeDB:
    def __init__(self):
        self.ledger = _FakeLedger()

    def __getitem__(self, _name):
        return self.ledger


def test_authorization_event_atomically_keeps_recommendation_offer_and_selection_separate():
    db = _FakeDB()
    claims = {"campaign_id": "campaign-1", "property_code": "123", "event_id": "signed-click-1"}
    details = {
        "previous_price": 3100,
        "recommended_adjustment_pct": 8, "recommended_price": 2852,
        "gradual_adjustment_pct": 6, "gradual_price": 2914,
        "selected_adjustment_type": "GRADUAL", "selected_adjustment_pct": 6,
        "selected_price": 2914, "authorization_status": "PRICE_AUTHORIZED",
    }
    stored, inserted = persist_live_event(db, claims, event="price_authorized", action="aceptar_rebaja", details=details)
    assert inserted is True
    assert stored["recommended_adjustment_pct"] == 8
    assert stored["recommended_price"] == 2852
    assert stored["selected_adjustment_type"] == "GRADUAL"
    assert stored["selected_adjustment_pct"] == 6
    assert stored["selected_price"] == 2914
    auth = [event for event in stored["events"] if event["event"] == "price_authorized"]
    gradual = [event for event in stored["events"] if event["event"] == "gradual_selected"]
    assert len(auth) == len(gradual) == 1
    assert auth[0]["previous_price"] == 3100
    assert auth[0]["recommended_price"] == 2852
    assert auth[0]["gradual_adjustment_pct"] == 6 and auth[0]["gradual_price"] == 2914
    assert gradual[0]["selected_price"] == 2914
    assert stored["gradual_authorized_at"] == auth[0]["event_at"]

    conflicting_claims = {**claims, "event_id": "signed-click-2"}
    _stored, inserted_again = persist_live_event(
        db, conflicting_claims, event="price_authorized", action="aceptar_rebaja",
        details={**details, "selected_adjustment_type": "RECOMMENDED", "selected_adjustment_pct": 8, "selected_price": 2852},
    )
    assert inserted_again is False
    assert len([event for event in db.ledger.row["events"] if event["event"] == "price_authorized"]) == 1
    assert db.ledger.row["selected_adjustment_type"] == "GRADUAL"
