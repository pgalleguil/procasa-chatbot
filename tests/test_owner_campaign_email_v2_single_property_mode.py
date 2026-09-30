from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lxml import html as lxml_html

from analytics import owner_campaign_email_compat, owner_campaign_email_v2 as renderer
from analytics.owner_campaign_email_compat import make_email_safe_html
from campanas import owner_campaign_live_prepare as live_prepare
from campanas.owner_campaign_live_events import verify_live_token


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
            "positioning_graph": {"cells": [{"reference": index == 9, "marker": index == 19} for index in range(21)], "markers_close": False, "reference_index": 9, "property_index": 19, "reference_label_index": 9, "property_label_index": 16},
            "top3": [
                {"portal": "Toctoc", "surface_label": "53 m² construidos", "rooms_label": "3 dorm.", "baths_label": "1 baño(s)", "price_label": "1.591 UF", "unit_label": "30,0 UF/m² útil", "parking_label": ""},
                {"portal": "Toctoc", "surface_label": "47 m² construidos", "rooms_label": "3 dorm.", "baths_label": "1 baño(s)", "price_label": "2.100 UF", "unit_label": "44,7 UF/m² útil", "parking_label": ""},
                {"portal": "Toctoc", "surface_label": "55 m² construidos", "rooms_label": "3 dorm.", "baths_label": "1 baño(s)", "price_label": "1.714 UF", "unit_label": "31,2 UF/m² útil", "parking_label": ""},
            ], "land_reference_visible": False, "land_top3": [],
        },
        "comparable_summary_text": "Analizamos 20 publicaciones similares. La referencia de mercado se sitúa en 44,0 UF/m² útil y tu propiedad en 62,0 UF/m² útil, un 41% sobre esa referencia.",
        "activity_90d": {"state": "KNOWN_ZERO", "total_leads": 0, "conversations": 0, "visits": 0, "portals": []},
        "single_diagnostic_text": "Señal de mercado para revisar.",
        "single_document_copy": "Tu tasación individual está disponible.",
        "single_recommendation_text": "Revisar el precio sugerido con tu ejecutivo.",
        "diagnostic_text": "Revisar con asesor.",
        "recommendation_text": "Revisar con asesor.",
        "recommendation_title": "Ajuste de precio sugerido",
        "recommendation": "con ajuste de precio sustentado",
        "recommended_price_label": "2.857 UF",
        "cta": {"primary_url": "https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=price", "primary_label": "ACEPTAR NUEVO VALOR", "report_url": "https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report", "advisor_url": "https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=advisor", "secondary_url": "https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report"},
        "document": {"visible": True, "copy": "Tasación individual", "type": "INDIVIDUAL_APPRAISAL"},
    }


def test_canonical_gradual_calculation_uses_only_two_or_three_percent():
    expected = {5: 2, 6: 2, 7: 3, 8: 3, 9: 3, 10: 3}
    for recommended_pct, gradual_pct in expected.items():
        result = renderer.calculate_gradual_price_alternative(
            current_price=3100, recommended_adjustment_pct=recommended_pct,
        )
        assert result["available"] is True
        assert result["adjustment_pct"] == gradual_pct
        assert result["price"] == round(3100 * (100 - gradual_pct) / 100, 4)

    case_5641 = renderer.calculate_gradual_price_alternative(
        current_price=3100, recommended_adjustment_pct=10,
    )
    assert case_5641["adjustment_pct"] == 3
    assert case_5641["price"] == 3007


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
    monkeypatch.setattr(owner_campaign_email_compat, "make_email_safe_html", lambda value: value)
    monkeypatch.setattr(renderer, "_valuation_slots", lambda _model: [])
    monkeypatch.setattr(renderer, "_portfolio_summary", lambda _model: {})
    monkeypatch.setattr(renderer, "_initials", lambda _name: "EG")

    result = renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "operation_raw": "VENTA", "is_rental": False}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )

    assert result == "rendered"
    assert captured["single_property_only"] is True
    assert captured["hero_title"] == "Revisión comercial de tu propiedad"
    assert "Analizamos las condiciones actuales del mercado" in captured["hero_description"]
    assert len(captured["macro_context"]["copy_paragraphs"]) == 2
    assert "una demanda más selectiva" in captured["macro_context"]["copy_paragraphs"][0]
    assert "últimos 90 días" in captured["macro_context"]["copy_paragraphs"][1]
    assert "Banco Central de Chile" in captured["macro_context"]["source_line"]
    assert captured["report_date"]


def test_renderer_keeps_legacy_layout_only_for_multiproperty(monkeypatch):
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
    monkeypatch.setattr(owner_campaign_email_compat, "make_email_safe_html", lambda value: value)
    monkeypatch.setattr(renderer, "_valuation_slots", lambda _model: [])
    monkeypatch.setattr(renderer, "_portfolio_summary", lambda _model: {})
    monkeypatch.setattr(renderer, "_initials", lambda _name: "EG")

    renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "operation_raw": "VENTA", "is_rental": False}, {"code": "9000", "operation_raw": "VENTA", "is_rental": False}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )
    assert captured["single_property_only"] is False
    assert captured["macro_context"]["copy_paragraphs"] == ()

    renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "operation_raw": "ARRIENDO", "is_rental": True}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )
    assert captured["single_property_only"] is True
    assert captured["hero_title"] == "Revisión comercial de tu propiedad"
    assert "quienes buscan arrendar" in captured["hero_description"]
    assert len(captured["macro_context"]["copy_paragraphs"]) == 2
    assert "costo de vida" in captured["macro_context"]["copy_paragraphs"][0]
    assert "arriendos similares" in captured["macro_context"]["copy_paragraphs"][1]
    assert "financiamiento" not in " ".join(captured["macro_context"]["copy_paragraphs"]).casefold()
    assert len(captured["macro_context"]["kpis"]) == 3
    assert captured["macro_context"]["kpis"][0]["value"] == "9,5%"


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
        "duplicate_of_primary": False,
        "source": "COMMUNAL",
        "reference_value": "52,3",
        "reference_unit": "UF/m² de oferta",
        "universe_value": "812",
        "universe_unit": "publicaciones activas",
        "source_date": "18 de mayo de 2026",
    }
    assert renderer._market_reference_model({}, "VENTA") == {"visible": False}


def test_communal_only_context_hides_fake_zero_and_uses_comparables_first():
    support = {"communal_market": {
        "operation": "VENTA",
        "relevant_metrics": {
            "uf_m2_publicacion_actual": 0,
            "uf_m2_venta_efectiva_actual": 0,
            "publicaciones_activas": 0,
        },
        "median_price_uf": 0,
        "n_observations": 0,
        "price_m2_median": 0,
    }}
    appraisal = renderer._appraisal_model(support, 5000, "VENTA")
    reference = renderer._market_reference_model(support, "VENTA")
    assert appraisal == {"visible": False}
    assert reference == {"visible": False}

    comparable = {
        "visible": True,
        "selected_n": 20,
        "positioning_reference_label": "44,0 UF/m² útil",
        "positioning_unit_label": "UF/m² útil",
    }
    populated = renderer._market_reference_model(support, "VENTA", comparable)
    assert populated["visible"] is True
    assert populated["source"] == "COMPARABLES"
    assert populated["reference_value"] == "44,0 UF/m² útil"
    assert populated["universe_value"] == "20"


def test_comparable_market_unit_is_not_duplicated_in_single_property_kpi():
    slots = renderer._single_property_valuation_slots({
        "market_reference": {
            "visible": True,
            "source": "COMPARABLES",
            "reference_value": "44,0 UF/m² útil",
            "reference_unit": "UF/m² útil",
        },
        "comparable": {"visible": False},
    })
    assert slots[1]["value"] == "44,0 UF/m² útil"


def test_comparable_rent_unit_is_not_duplicated_in_single_property_kpi():
    slots = renderer._single_property_valuation_slots({
        "is_rental": True,
        "market_reference": {
            "visible": True,
            "source": "COMPARABLES",
            "reference_value": "$ 7.881/m²/mes",
            "reference_unit": "CLP/m²/mes",
        },
        "comparable": {"visible": False},
    })
    assert slots[1]["value"] == "$ 7.881/m²/mes"


def test_gmail_safe_icons_use_materialized_markup_and_preserve_copy():
    source = '''<!doctype html><html><head><style>
      .icon:before { position:absolute; left:2px; content:""; }
      .icon::after { position:absolute; right:1px; content:""; }
    </style></head><body><span class="icon"></span><p>Detalle visible</p></body></html>'''
    safe_html = make_email_safe_html(source)
    root = lxml_html.fromstring(safe_html)
    icon = root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' icon ')]")[0]
    assert len(icon.xpath("./span[contains(@class,'email-pseudo-before')]") ) == 1
    assert len(icon.xpath("./span[contains(@class,'email-pseudo-after')]") ) == 1
    assert all("position:absolute" in node.get("style", "") for node in icon)
    assert "Detalle visible" in root.text_content()
    assert ":before" not in safe_html and "::before" not in safe_html
    assert ":after" not in safe_html and "::after" not in safe_html


def test_operation_badge_is_inside_photo_and_feature_icons_are_gmail_safe_text():
    model = _single_property_model()
    model["feature_cards"] = [
        {"kind": "area", "label": "130 m² construidos"},
        {"kind": "bed", "label": "2 dorm."},
        {"kind": "bath", "label": "1 baño(s)"},
        {"kind": "parking", "label": "1 estacionamiento"},
    ]
    html = renderer.render_owner_campaign_email_v2(
        [model], email="qa@example.test",
        executives=[{"name": "Jorge Pablo Caro", "email": "jpcaro@procasa.cl", "phone": "+56940904971"}],
        base_url="https://example.test",
    )
    root = lxml_html.fromstring(html)
    photo = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' single-photo-wrap ')]")
    assert len(photo) == 1
    assert photo[0].get("background") == "https://example.test/property.jpg"
    badge = photo[0].xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' single-operation-badge ')]")
    assert len(badge) == 1 and badge[0].text == "VENTA"
    detail_text = " ".join(root.itertext())
    for detail in ("Venta", "130 m² construidos", "2 dorm.", "1 baño", "1 estacionamiento"):
        assert detail in detail_text
    visible_icons = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' property-feature-icon ')]")
    assert [icon.text for icon in visible_icons] == ["m²", "D", "B", "E"]
    assert "aria-label=\"Dormitorios\"" in html
    assert "class=\"comp-icon-area\"" not in html
    assert "max-width:620px" in html


def test_none_document_hides_document_module_and_report_navigation():
    model = _single_property_model()
    model["document"] = {"visible": False, "copy": "", "type": "NONE"}
    model["single_document_copy"] = ""
    html = renderer.render_owner_campaign_email_v2(
        [model], email="qa@example.test",
        executives=[{"name": "Jorge Pablo Caro", "email": "jpcaro@procasa.cl", "phone": "+56940904971"}],
        base_url="https://example.test",
    )
    root = lxml_html.fromstring(html)
    text = " ".join(root.itertext())
    assert "RESPALDO COMERCIAL" not in text
    assert "En este envío no hay un documento adicional disponible para descarga." not in text
    assert "VER INFORME" not in text
    assert "document-support-single" not in html


def test_zero_non_applicable_room_and_bath_features_are_hidden():
    assert renderer._rooms_label({"dormitorios": 0}) == ""
    assert renderer._baths_label({"banos": 0}) == ""
    assert renderer._parking_label({"estacionamientos": 0}) == ""


def test_price_position_markers_have_only_straight_lines_and_labels_under_each_marker():
    model = _single_property_model()
    graph = renderer._positioning_graph({"p10": 0, "p90": 100, "median": 55}, 50)
    assert graph["markers_close"] is True
    model["comparable"].update({
        "visible": True,
        "positioning_mode": "PRICE_M2",
        "positioning_graph": graph,
        "positioning_reference_label": "55 UF/m² útil",
        "positioning_property_label": "50 UF/m² útil",
        "positioning_unit_label": "UF/m² útil",
        "selected_n": 20,
        "top3": [],
    })
    html = renderer.render_owner_campaign_email_v2(
        [model], email="qa@example.test",
        executives=[{"name": "Jorge Pablo Caro", "email": "jpcaro@procasa.cl", "phone": "+56940904971"}],
    )
    root = lxml_html.fromstring(html)
    bar = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' position-track ')]")
    assert len(bar) == 1
    band_cells = bar[0].xpath(".//td[contains(concat(' ', normalize-space(@class), ' '), ' position-band-cell ')]")
    assert len(band_cells) == 21
    reference_labels = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' position-ref-box ')]")
    property_labels = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' position-prop-box ')]")
    reference_stems = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' position-ref-stem ')]")
    property_stems = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' position-property-stem ')]")
    assert len(reference_labels) == len(property_labels) == 1
    assert len(reference_stems) == len(property_stems) == 21
    assert sum(bool(cell.xpath("./span")) for cell in reference_stems) == 1
    assert sum(bool(cell.xpath("./span")) for cell in property_stems) == 1
    assert root.xpath("//*[@data-component-id='single-property-comparison-bar-v1']")
    assert root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' email-pseudo-before ')]")
    assert "max-width:620px" in html


def test_comparison_bar_keeps_labels_tied_to_actual_marker_positions_at_edges():
    low = renderer._positioning_graph({"p10": 10, "p90": 90, "median": 10}, 10)
    high = renderer._positioning_graph({"p10": 10, "p90": 90, "median": 90}, 90)
    assert low["reference_index"] == low["property_index"]
    assert high["reference_index"] == high["property_index"]
    assert 0 < low["reference_index"] < 20
    assert 0 < high["reference_index"] < 20
    assert "reference_label_index" not in low and "property_label_index" not in low
    assert len(low["cells"]) == len(high["cells"]) == 21


def test_legacy_secondary_url_is_never_reused_as_report_destination():
    model = _single_property_model()
    model["cta"] = {
        "primary_url": "https://example.test/price?token=p",
        "primary_label": "ACEPTAR NUEVO VALOR",
        "secondary_url": "https://example.test/test-accion?token=advisor",
        "advisor_url": "https://example.test/test-accion?token=advisor",
    }
    html = renderer.render_owner_campaign_email_v2(
        [model], email="qa@example.test",
        executives=[{"name": "Jorge Pablo Caro", "email": "jpcaro@procasa.cl", "phone": "+56940904971"}],
    )
    root = lxml_html.fromstring(html)
    report_buttons = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' document-action-single ')]")
    advisor_links = root.xpath("//a[contains(@href, 'token=advisor')]")
    assert not report_buttons
    assert advisor_links


def test_productive_email_uses_signed_campaign_links_to_confirmation_and_advisor(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "unit-test-secret")
    captured = {}

    def fake_render(models, **kwargs):
        captured["model"] = models[0]
        return "rendered"

    monkeypatch.setattr(renderer, "render_owner_campaign_email_v2", fake_render)
    row = {
        "campaign_id": "owner_price_sucre_wave1_20260928",
        "property_code": "5641",
        "owner_email": "qa@example.test",
        "document_type": "COMMUNAL_MARKET_REPORT",
        "_model": {"executive": {}, "document": {"visible": True}},
    }

    assert live_prepare._render_one(row) == "rendered"
    cta = captured["model"]["cta"]
    confirmation = parse_qs(urlparse(cta["primary_url"]).query)
    advisor = parse_qs(urlparse(cta["advisor_url"]).query)
    report = parse_qs(urlparse(cta["report_url"]).query)
    identity = {
        "campaign_id": row["campaign_id"],
        "property_code": row["property_code"],
        "recipient": row["owner_email"],
    }

    assert cta["primary_label"] == "REVISAR / CONFIRMAR AJUSTE"
    assert confirmation["campana"] == [row["campaign_id"]]
    assert confirmation["codigos"] == [row["property_code"]]
    assert confirmation["mode"] == ["owner_campaign"]
    assert verify_live_token(confirmation["token"][0], **identity, action="aceptar_rebaja")
    assert verify_live_token(advisor["token"][0], **identity, action="contactar_ejecutivo")
    assert verify_live_token(
        report["token"][0], **identity, action="ver_informe",
    )["document_type"] == "COMMUNAL_MARKET_REPORT"


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
        "REFERENCIA COMPLEMENTARIA",
        "Tu propiedad frente a inmuebles comparables",
        "3 dorm",
        "1 baño",
        "Informe comercial disponible",
        "REVISAR / CONFIRMAR AJUSTE",
        "REVISAR CON MI EJECUTIVO",
        "Comparamos tu propiedad con publicaciones disponibles que comparten características relevantes.",
        "Se muestran 3 referencias representativas; el cálculo utiliza la muestra completa.",
    ):
        assert expected in html
    assert "Referencia comparable · contexto secundario" not in html
    assert "Muy bajo" not in html
    rendered_root = lxml_html.fromstring(html)
    graph_text = " ".join(rendered_root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' single-comparison-bar ')]//text()"))
    assert not any(tick in graph_text for tick in ("26,1", "34,3", "42,4", "50,6", "58,8", "66,9"))
    assert len(rendered_root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' position-metric ')]")) == 0
    assert "Analizamos 20 publicaciones similares. La referencia de mercado se sitúa en 44,0 UF/m² útil" in html
    assert "Las referencias corresponden a publicaciones observadas y no garantizan un precio final de venta." in html
    assert len(rendered_root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' comp-card-single ')]")) == 3
    assert 'class="comp-card-single" style="width:33.333%;' in html
    assert 'height:116px;' not in html.split('class="comp-card-single"', 1)[-1].split('>', 1)[0]
    safe_html = make_email_safe_html(html)
    assert "Revisión comercial de tu propiedad" in safe_html
    assert "REVISAR CON MI EJECUTIVO" in safe_html
    valuation_cells = [
        " ".join(cell.itertext()).strip()
        for cell in lxml_html.fromstring(html).xpath("//*[contains(concat(' ',normalize-space(@class),' '),' valuation-strip-single ')]//td")
    ]
    assert "REFERENCIAS Análisis comparativo Detalle de la muestra más abajo" in valuation_cells[1]
    assert "POSICIONAMIENTO Sobre la muestra comparable Frente a publicaciones similares" in valuation_cells[2]


def test_single_rent_none_uses_approved_single_visual_system_without_document_or_sale_copy():
    model = _single_property_model()
    model.update({
        "code": "6132",
        "operation_label": "Arriendo",
        "operation_raw": "ARRIENDO",
        "is_rental": True,
        "price_label": "21 UF / mes",
        "document": {"visible": False, "copy": "", "type": "NONE"},
        "appraisal": {"visible": False, "kind": "NONE", "metrics": []},
        "market_reference": {"visible": False},
        "single_document_copy": "",
        "single_diagnostic_text": "Revisemos el posicionamiento del canon mensual.",
        "single_recommendation_text": "Revisa el canon mensual con tu ejecutivo.",
            "cta": {"primary_url": "https://example.test/advisor", "primary_label": "REVISAR CON MI EJECUTIVO", "advisor_url": "https://example.test/advisor"},
    })
    model["comparable"].update({
        "positioning_mode": "TOTAL_PRICE",
        "positioning_unit_label": "UF/mes",
        "positioning_reference_label": "20 UF/mes",
        "positioning_property_label": "21 UF/mes",
        "top3": [{**item, "unit_label": "UF/m²/mes"} for item in model["comparable"]["top3"]],
    })
    html = renderer.render_owner_campaign_email_v2(
        [model], email="qa@example.test",
        executives=[{"name": "Jorge Pablo Caro", "email": "jpcaro@procasa.cl", "phone": "+56940904971"}],
        base_url="https://example.test",
    )
    root = lxml_html.fromstring(html)
    text = " ".join(root.text_content().split()).casefold()
    assert "hero-single" in html
    assert "single-property-review" in html
    assert "quienes buscan arrendar" in html
    assert "compradores" not in text
    assert "financiamiento hipotecario" not in text
    assert "concretar una venta" not in text
    assert "21 uf / mes" in text
    assert "data-component-id=\"single-property-comparison-bar-v1\"" in html
    assert "respaldo comercial" not in text
    assert "ver informe" not in text
    assert "document-support-single" not in html
    assert "ver informe" not in text
    assert html.count("https://example.test/property.jpg") == 1
    safe_html = make_email_safe_html(html)
    assert "@media" in safe_html and "viewport" in safe_html
    assert 'alt="PROCASA"' in safe_html


def test_rent_comparable_metrics_are_clp_per_square_metre_per_month():
    evidence = {
        "client_validation_v3": {
            "comparable_display_status": "FULL",
            "effective_type": "OFFICE",
            "primary_surface": "built_m2",
            "integral_comparables": [
                {"listing_id": "r1", "price_uf": 135, "price_clp": 5518848,
                 "built_m2": 965, "price_m2_built": 5719, "price_m2": 5719,
                 "property_type": "Oficina", "commune": "Santiago"},
                {"listing_id": "r2", "price_uf": 235, "price_clp": 9588780,
                 "built_m2": 1218, "price_m2_built": 7880.6, "price_m2": 7880.6,
                 "property_type": "Oficina", "commune": "Santiago"},
                {"listing_id": "r3", "price_uf": 129, "price_clp": 5266452,
                 "built_m2": 720, "price_m2_built": 7318, "price_m2": 7318,
                 "property_type": "Oficina", "commune": "Santiago"},
            ],
        }
    }
    prop = {
        "operacion": "ARRIENDO", "superficie_construida": 1300,
        "precio_publicado_uf": 185, "precio_publicado_clp": 7556900,
    }
    result = renderer._comparable_model(evidence, "HIGH", 185, prop)

    assert result["positioning_unit_label"] == "CLP/m²/mes"
    assert result["positioning_property_label"] == "$ 5.813/m²/mes"
    assert result["positioning_reference_label"] == "$ 7.318/m²/mes"
    assert result["top3"][0]["price_label"] == "$5.518.848"
    assert result["top3"][0]["unit_label"] == "$ 5.719/m²/mes"
    assert result["top3"][0]["property_type_label"] == "Oficina"
    assert result["top3"][0]["commune_label"] == "Santiago"
    slots = renderer._single_property_valuation_slots({
        "is_rental": True,
        "price_label": "185 UF / mes",
        "comparable": result,
        "appraisal": {"visible": False},
        "activity_90d": {"state": "UNKNOWN"},
    })
    assert slots[2]["value"] == "Bajo la muestra comparable"


def test_rent_heading_says_arriendo_and_visual_structure_is_shared():
    sale_model = _single_property_model()
    rent_model = _single_property_model()
    rent_model.update({
        "code": "6132", "operation_label": "Arriendo", "operation_raw": "ARRIENDO",
        "is_rental": True, "price_label": "185 UF / mes",
        "document": {"visible": False, "type": "NONE", "copy": ""},
        "appraisal": {"visible": False, "kind": "NONE", "metrics": []},
        "recommended_price_label": None,
    })
    executive = {"name": "Jorge Pablo Caro", "email": "jpcaro@procasa.cl", "phone": "+56940904971"}
    sale_html = renderer.render_owner_campaign_email_v2([sale_model], email="qa@example.test", executives=[executive], base_url="https://example.test")
    rent_html = renderer.render_owner_campaign_email_v2([rent_model], email="qa@example.test", executives=[executive], base_url="https://example.test")
    sale_root, rent_root = lxml_html.fromstring(sale_html), lxml_html.fromstring(rent_html)

    def single_structure(root):
        return [" ".join(node.get("class", "").split()) for node in root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' single-property-review ') or contains(concat(' ',normalize-space(@class),' '),' hero-single ') or contains(concat(' ',normalize-space(@class),' '),' property-card-single ') or contains(concat(' ',normalize-space(@class),' '),' valuation-strip-single ') or contains(concat(' ',normalize-space(@class),' '),' activity-90-single ') or contains(concat(' ',normalize-space(@class),' '),' evidence-single ') or contains(concat(' ',normalize-space(@class),' '),' insight-single ') or contains(concat(' ',normalize-space(@class),' '),' executive-single ') or contains(concat(' ',normalize-space(@class),' '),' footer-one ')]")]

    rent_text = " ".join(rent_root.text_content().split()).casefold()
    assert "Tu propiedad frente a inmuebles comparables" in rent_html
    assert "comprador" not in rent_text and "venta" not in rent_text
    assert "las referencias corresponden a publicaciones observadas y no garantizan un valor final de arriendo." in rent_text
    assert single_structure(sale_root) == single_structure(rent_root)


def test_single_property_comparable_copy_uses_live_values_and_position():
    comparable = {
        "visible": True,
        "selected_n": 20,
        "positioning_reference_label": "100,3 UF/m² útil",
        "positioning_property_label": "110,7 UF/m² útil",
        "positioning_delta_pct": 10.37,
    }
    summary = renderer._single_comparable_summary(comparable)
    assert summary == (
        "Analizamos 20 publicaciones similares. La referencia de mercado se sitúa en "
        "100,3 UF/m² útil y tu propiedad en 110,7 UF/m² útil, un 10% sobre esa referencia."
    )


def test_single_property_fallback_copy_does_not_claim_primary_comparables():
    diagnostic = renderer._single_diagnostic_copy(
        {"visible": False},
        {
            "visible": True,
            "reference_value": "91,6",
            "reference_unit": "UF/m² de oferta",
            "universe_value": "3.046",
            "universe_unit": "publicaciones activas",
        },
        {"state": "KNOWN_ZERO", "total_leads": 0},
    )
    assert "muestra suficiente de inmuebles estructuralmente comparables" in diagnostic
    assert "referencia comunal disponible es 91,6 UF/m² de oferta" in diagnostic
    assert "3.046 publicaciones activas" in diagnostic
    assert "no registró leads" in diagnostic
    assert "referencias comparables sitúan" not in diagnostic


def test_diagnostic_interprets_leads_and_comparable_position_without_repeating_recommendation():
    comparable = {
        "visible": True,
        "positioning_property_value": 110.7,
        "positioning_reference_value": 100.3,
        "positioning_property_label": "110,7 UF/m² útil",
        "positioning_delta_pct": 10.37,
    }
    diagnostic = renderer._single_diagnostic_copy(
        comparable,
        {"visible": False},
        {"state": "KNOWN_POSITIVE", "total_leads": 2, "conversations": 0, "visits": 0},
    )
    assert "2 leads" in diagnostic
    assert "0 conversaciones" in diagnostic and "0 visitas coordinadas" in diagnostic
    assert "110,7 UF/m² útil" in diagnostic and "10% sobre" in diagnostic
    assert "reduciendo su competitividad" in diagnostic
    assert "Recomendamos reducir" not in diagnostic


def test_diagnostic_does_not_describe_engaged_leads_as_limited_response():
    diagnostic = renderer._single_diagnostic_copy(
        {"visible": False},
        {"visible": True, "source": "COMMUNAL", "reference_value": "91,6", "reference_unit": "UF/m²/mes de oferta"},
        {"state": "KNOWN_POSITIVE", "total_leads": 8, "conversations": 3, "visits": 2},
    )
    assert "8 leads" in diagnostic and "3 conversaciones" in diagnostic and "2 visitas coordinadas" in diagnostic
    assert "muestra suficiente de inmuebles estructuralmente comparables" in diagnostic
    assert "muestra interés comercial" in diagnostic
    assert "respuesta comercial limitada" not in diagnostic


def test_diagnostic_and_recommendation_containers_share_border_system():
    template = (Path(__file__).parents[1] / "templates" / "owner_campaign_email_v2.html").read_text(encoding="utf-8")
    assert ".insight-cell-single { box-sizing:border-box; width:49%; padding:0; border:0; background:transparent; vertical-align:top; }" in template
    assert ".insight-single { box-sizing:border-box; width:100%; min-height:215px; padding:14px 13px; border:1px solid #E5E6EF; border-radius:9px; overflow:hidden;" in template
    assert ".insight-recommendation { border-color:#EAE3D7; background:#FFFCF6; }" in template
    assert ".document-line-single { box-sizing:border-box; width:100%; margin:12px 0 0; border:1px solid #E5E6EF; border-collapse:separate; border-spacing:0; border-radius:9px; overflow:hidden;" in template
    assert ".document-line-single td { padding:14px 13px; vertical-align:middle; }" in template
    assert ".insight-single { display:block; width:100% !important; min-height:0; }" in template


def test_single_context_and_comparable_disclaimer_align_to_property_content():
    template = (Path(__file__).parents[1] / "templates" / "owner_campaign_email_v2.html").read_text(encoding="utf-8")
    assert ".shell-single .context-strip-single-shell { padding:0 14px 0 30px; border:0; background:#ffffff; }" in template
    assert ".macro-context-panel-single { box-sizing:border-box; padding:8px 24px; border-radius:0 0 9px 9px; background:#ECEBFF;" in template
    assert ".disclaimer-single { margin:7px 0 0 9px;" in template
    assert ".disclaimer-single { margin-left:9px; }" in template


def test_complementary_reference_has_requested_gap_and_compact_auto_height():
    template = (Path(__file__).parents[1] / "templates" / "owner_campaign_email_v2.html").read_text(encoding="utf-8")
    style = next(line.strip() for line in template.splitlines() if ".market-reference-single {" in line)
    assert "margin:17px 9px 0" in style
    assert "box-sizing:border-box" in style and "padding:8px 10px" in style
    assert "height:" not in style and "min-height:" not in style
    assert ".market-reference-single { margin:7px 9px 0; }" in template


def test_local_render_sale_16469_and_rent_16527_share_visual_template_and_copy_context():
    sale = _single_property_model()
    sale.update({"code": "16469", "operation_raw": "VENTA", "operation_label": "Venta", "is_rental": False, "price_label": "7.750 UF"})
    sale["market_reference"] = {"visible": False}
    sale["activity_90d"] = {"state": "UNKNOWN", "total_leads": None, "portals": []}
    sale["comparable"].update({
        "selected_n": 20,
        "positioning_reference_label": "100,3 UF/m² útil",
        "positioning_property_label": "110,7 UF/m² útil",
        "positioning_delta_pct": 10.37,
        "positioning_delta_label": "+10%",
        "positioning_delta_context": "sobre la referencia",
    })
    sale["comparable_summary_text"] = renderer._single_comparable_summary(sale["comparable"])
    sale["single_diagnostic_text"] = renderer._single_diagnostic_copy(sale["comparable"], sale["market_reference"], sale["activity_90d"])
    assert "100,3 UF/m² útil" not in sale["single_diagnostic_text"]
    assert "actividad comercial de los últimos 90 días no está disponible" in sale["single_diagnostic_text"]

    rent = _single_property_model()
    rent.update({"code": "16527", "operation_raw": "ARRIENDO", "operation_label": "Arriendo", "is_rental": True, "price_label": "21 UF"})
    rent["comparable"] = {"visible": False, "top3": [], "land_top3": [], "land_reference_visible": False}
    rent["comparable_summary_text"] = ""
    rent["market_reference"] = {
        "visible": True,
        "source": "COMMUNAL",
        "reference_value": "0,40",
        "reference_unit": "UF/m²/mes de oferta",
        "universe_value": "325",
        "universe_unit": "arriendos activos",
    }
    rent["activity_90d"] = {"state": "UNKNOWN", "total_leads": None, "portals": []}
    rent["single_diagnostic_text"] = renderer._single_diagnostic_copy(rent["comparable"], rent["market_reference"], rent["activity_90d"])
    rent["recommendation_text"] = "La respuesta comercial del arriendo y los antecedentes disponibles orientan el reposicionamiento."
    rent["single_recommendation_text"] = rent["recommendation_text"]
    rent["recommended_price_label"] = "19,5 UF"
    rent["document"] = {"visible": True, "type": "COMMUNAL_MARKET_REPORT", "copy": "Informe de mercado comunal disponible."}
    rent["cta"]["report_url"] = "https://example.test/report/qa"

    executive = {"name": "Ejecutivo QA", "email": "qa@example.test", "phone": "+56900000000"}
    sale_html = renderer.render_owner_campaign_email_v2([sale], email="qa@example.test", executives=[executive], base_url="https://example.test")
    rent_html = renderer.render_owner_campaign_email_v2([rent], email="qa@example.test", executives=[executive], base_url="https://example.test")
    sale_root, rent_root = lxml_html.fromstring(sale_html), lxml_html.fromstring(rent_html)
    shared_classes = ("property-card-single", "valuation-strip-single", "insight-single", "executive-single", "footer-one")
    for class_name in shared_classes:
        xpath = f"//*[contains(concat(' ',normalize-space(@class),' '),' {class_name} ')]"
        assert len(sale_root.xpath(xpath)) == len(rent_root.xpath(xpath))
    assert "Analizamos 20 publicaciones similares" in sale_html
    assert "100,3 UF/m² útil" in sale_html and "110,7 UF/m² útil" in sale_html
    assert len(sale_root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' evidence-single ')]")) == 1
    assert "Tu propiedad frente a inmuebles comparables" not in rent_html
    rent_text = " ".join(rent_root.text_content().split()).casefold()
    assert "venta" not in rent_text and "sobreprecio" not in rent_text
    assert "las referencias corresponden a publicaciones observadas y no garantizan un valor final de arriendo." in rent_text
    assert "muestra suficiente de inmuebles estructuralmente comparables" in rent_text
    assert "0,40 uf/m²/mes de oferta" in rent_text


def test_compact_complementary_reference_and_single_mobile_rules_are_present():
    html = renderer.render_owner_campaign_email_v2(
        [_single_property_model()],
        email="qa@example.test",
        executives=[{"name": "Erika Garrido Varela", "email": "egarrido@procasa.cl", "phone": "+56991951317"}],
        base_url="https://example.test",
    )
    assert "Mercado comunal" in html
    assert "Como contexto adicional, el segmento comunal resume" not in html
    template_source = (Path(renderer.__file__).resolve().parents[1] / "templates" / renderer.TEMPLATE_FILE).read_text(encoding="utf-8")
    assert ".market-reference-compact-single { font-size:9px" in template_source
    assert ".comp-card-single { display:block; width:auto !important" in template_source
    assert ".actions-single { width:100%; }" in template_source
    assert "min-height:82px" not in template_source


def test_missing_comparable_source_date_does_not_leave_empty_sample_label():
    model = _single_property_model()
    model["comparable"]["source_date"] = ""
    html = renderer.render_owner_campaign_email_v2(
        [model],
        email="qa@example.test",
        executives=[{"name": "Erika Garrido Varela", "email": "egarrido@procasa.cl", "phone": "+56991951317"}],
        base_url="https://example.test",
    )
    visible = " ".join(lxml_html.fromstring(html).text_content().split())
    assert "Muestra del ;" not in visible
    assert "Se muestran 3 referencias representativas; el cálculo utiliza la muestra completa." in visible
