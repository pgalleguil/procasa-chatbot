from __future__ import annotations

from datetime import datetime, timezone

from campanas.owner_campaign_confirmation_page import render_decision_page, render_success_page
from campanas.owner_campaign_live_events import _campaign_whatsapp_text, persist_live_event


def _decision_html(recommended_pct: int = 8) -> str:
    gradual_pct = {10: 8, 9: 7, 8: 6, 7: 5, 6: 5}.get(recommended_pct)
    return render_decision_page(
        recommended_pct=recommended_pct,
        recommended_price="2.852 UF",
        current_price="3.100 UF",
        gradual_pct=gradual_pct,
        gradual_price="2.914 UF",
        recommended_url="https://example.test/post?selected_adjustment_type=RECOMMENDED",
        gradual_url="https://example.test/post?selected_adjustment_type=GRADUAL",
        advisor_url="https://example.test/contact",
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
    page = _decision_html(5)
    assert "5%" in page
    assert "AUTORIZAR AJUSTE GRADUAL" not in page
    assert "QUIERO PROPONER OTRO AJUSTE" in page


def test_success_pages_keep_recommendation_and_owner_choice_distinct():
    gradual = render_success_page(
        selected_type="GRADUAL", selected_pct=6, selected_price="2.914 UF",
        recommended_pct=8, recommended_price="2.852 UF",
    )
    assert "Ajuste gradual autorizado" in gradual
    assert "RECOMENDACIÓN PROCASA" in gradual and "8% → 2.852 UF" in gradual
    assert "TU AJUSTE AUTORIZADO" in gradual and "6% → 2.914 UF" in gradual
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
    assert "8% · RECOMENDADO" in recommended
    assert "✅ AJUSTE GRADUAL CONFIRMADO" in gradual
    assert "6% · GRADUAL" in gradual
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

    conflicting_claims = {**claims, "event_id": "signed-click-2"}
    _stored, inserted_again = persist_live_event(
        db, conflicting_claims, event="price_authorized", action="aceptar_rebaja",
        details={**details, "selected_adjustment_type": "RECOMMENDED", "selected_adjustment_pct": 8, "selected_price": 2852},
    )
    assert inserted_again is False
    assert len([event for event in db.ledger.row["events"] if event["event"] == "price_authorized"]) == 1
    assert db.ledger.row["selected_adjustment_type"] == "GRADUAL"
