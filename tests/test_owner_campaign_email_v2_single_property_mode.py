from __future__ import annotations

from analytics import owner_campaign_email_v2 as renderer
from analytics.owner_campaign_email_compat import make_email_safe_html


def _single_property_model():
    return {
        "code": "5641",
        "property_type": "Departamento",
        "commune": "La Cisterna",
        "property_heading": "DEPARTAMENTO · LA CISTERNA",
        "operation_label": "Venta",
        "operation_raw": "VENTA",
        "is_rental": False,
        "price_label": "3.100 UF",
        "feature_cards": [{"kind": "area", "label": "50 m² construidos"}, {"kind": "bed", "label": "3 dorm."}, {"kind": "bath", "label": "1 baño(s)"}],
        "image": {"available": True, "url": "https://example.test/property.jpg", "source": "UNIVERSO_CARTERA", "count": 1},
        "appraisal": {"visible": True, "kind": "INDIVIDUAL_APPRAISAL", "metrics": [], "mid_label": "2.666 UF", "gap_label": "+16,3%", "gap_amount_label": "+434 UF", "adjustment_label": "-7,8%"},
        "market_reference": {"visible": True, "reference_value": "52,3", "reference_unit": "UF/m² de oferta", "universe_value": "812", "universe_unit": "publicaciones activas", "source_date": "18 de mayo de 2026"},
        "comparable": {
            "visible": True, "effective_type": "APARTMENT", "badge_label": "20 publicaciones similares analizadas", "selected_n": 20,
            "source_date": "18 de mayo de 2026", "display_status": "OK", "positioning_mode": "PRICE_M2",
            "positioning_unit_label": "UF/m² útil", "positioning_reference_label": "44,0 UF/m² útil", "positioning_property_label": "62,0 UF/m² útil",
            "positioning_graph": {"cells": [{"reference": index == 9, "marker": index == 19} for index in range(21)], "markers_close": False},
            "top3": [
                {"portal": "Toctoc", "surface_label": "53 m² construidos", "rooms_label": "3 dorm.", "baths_label": "1 baño(s)", "price_label": "1.591 UF", "unit_label": "30,0 UF/m² útil", "parking_label": ""},
                {"portal": "Toctoc", "surface_label": "47 m² construidos", "rooms_label": "3 dorm.", "baths_label": "1 baño(s)", "price_label": "2.100 UF", "unit_label": "44,7 UF/m² útil", "parking_label": ""},
                {"portal": "Toctoc", "surface_label": "55 m² construidos", "rooms_label": "3 dorm.", "baths_label": "1 baño(s)", "price_label": "1.714 UF", "unit_label": "31,2 UF/m² útil", "parking_label": ""},
            ], "land_reference_visible": False, "land_top3": [],
        },
        "activity_90d": {"state": "KNOWN_ZERO", "total_leads": 0, "conversations": 0, "visits": 0, "portals": []},
        "single_diagnostic_text": "Señal de mercado para revisar.",
        "single_document_copy": "Tu tasación individual está disponible.",
        "single_recommendation_text": "Revisar el precio sugerido con tu ejecutivo.",
        "diagnostic_text": "Revisar con asesor.",
        "recommendation_text": "Revisar con asesor.",
        "recommendation_title": "Ajuste de precio sugerido",
        "recommendation": "con ajuste de precio sustentado",
        "recommended_price_label": "2.857 UF",
        "cta": {"primary_url": "https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=action", "primary_label": "ACEPTAR NUEVO VALOR", "secondary_url": "https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report"},
        "document": {"visible": True, "copy": "Tasación individual", "type": "INDIVIDUAL_APPRAISAL"},
    }


def test_renderer_selects_frozen_single_property_copy_and_macro_context(monkeypatch):
    captured = {}

    class FakeTemplate:
        def render(self, **context):
            captured.update(context)
            return "rendered"

    class FakeEnvironment:
        def __init__(self, **_kwargs):
            pass

        def get_template(self, _name):
            return FakeTemplate()

    monkeypatch.setattr(renderer, "Environment", FakeEnvironment)
    monkeypatch.setattr(renderer, "_valuation_slots", lambda _model: [])
    monkeypatch.setattr(renderer, "_portfolio_summary", lambda _model: {})
    monkeypatch.setattr(renderer, "_initials", lambda _name: "EG")

    result = renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "is_rental": False}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )

    assert result == "rendered"
    assert captured["single_property_only"] is True
    assert captured["hero_title"] == "Revisión comercial de tu propiedad"
    assert "Analizamos el comportamiento reciente" in captured["hero_description"]
    assert len(captured["macro_context"]["copy_paragraphs"]) == 2
    assert "Banco Central de Chile" in captured["macro_context"]["source_line"]
    assert captured["report_date"]


def test_renderer_keeps_legacy_layout_for_multiproperty_and_rental(monkeypatch):
    captured = {}

    class FakeTemplate:
        def render(self, **context):
            captured.update(context)
            return "rendered"

    class FakeEnvironment:
        def __init__(self, **_kwargs):
            pass

        def get_template(self, _name):
            return FakeTemplate()

    monkeypatch.setattr(renderer, "Environment", FakeEnvironment)
    monkeypatch.setattr(renderer, "_valuation_slots", lambda _model: [])
    monkeypatch.setattr(renderer, "_portfolio_summary", lambda _model: {})
    monkeypatch.setattr(renderer, "_initials", lambda _name: "EG")

    renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "is_rental": False}, {"code": "9000", "is_rental": False}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )
    assert captured["single_property_only"] is False
    assert captured["macro_context"]["copy_paragraphs"] == ()

    renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "is_rental": True}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )
    assert captured["single_property_only"] is False
    assert captured["hero_title"].endswith("arriendo")


def test_market_reference_uses_only_communal_source_values():
    result = renderer._market_reference_model(
        {
            "communal_market": {
                "operation": "VENTA",
                "document_date": "2026-05-18T00:00:00",
                "relevant_metrics": {
                    "uf_m2_publicacion_actual": 52.3,
                    "publicaciones_activas": 812,
                },
            }
        },
        "VENTA",
    )
    assert result == {
        "visible": True,
        "reference_value": "52,3",
        "reference_unit": "UF/m² de oferta",
        "universe_value": "812",
        "universe_unit": "publicaciones activas",
        "source_date": "18 de mayo de 2026",
    }
    assert renderer._market_reference_model({}, "VENTA") == {"visible": False}


def test_real_single_property_template_renders_approved_visual_content():
    html = renderer.render_owner_campaign_email_v2(
        [_single_property_model()],
        email="qa@example.test",
        executives=[{"name": "Erika Garrido Varela", "email": "egarrido@procasa.cl", "phone": "+56991951317"}],
        base_url="https://procasa-chatbot-yr8d.onrender.com",
    )
    for expected in (
        "Revisión comercial de tu propiedad",
        "Buenas",
        "CHILE · SEPTIEMBRE 2026",
        "Referencia comunal · contexto secundario",
        "Tu propiedad frente a inmuebles comparables",
        "3 dorm",
        "1 baño",
        "Informe comercial disponible",
        "ACEPTAR NUEVO VALOR",
        "Revisar con mi ejecutivo",
    ):
        assert expected in html
    safe_html = make_email_safe_html(html)
    assert "Revisión comercial de tu propiedad" in safe_html
    assert "Revisar con mi ejecutivo" in safe_html
