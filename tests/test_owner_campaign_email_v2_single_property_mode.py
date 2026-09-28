from __future__ import annotations

from lxml import html as lxml_html

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
            "positioning_graph": {"cells": [{"reference": index == 9, "marker": index == 19} for index in range(21)], "markers_close": False, "reference_index": 9, "property_index": 19, "reference_label_index": 9, "property_label_index": 16},
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
        "cta": {"primary_url": "https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=price", "primary_label": "ACEPTAR NUEVO VALOR", "report_url": "https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report", "advisor_url": "https://procasa-chatbot-yr8d.onrender.com/campana/test-accion?token=advisor", "secondary_url": "https://procasa-chatbot-yr8d.onrender.com/campana/informe?token=report"},
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
    assert captured["single_property_only"] is True
    assert captured["hero_title"].endswith("arriendo")
    assert len(captured["macro_context"]["copy_paragraphs"]) == 2
    assert "financiamiento" not in " ".join(captured["macro_context"]["copy_paragraphs"]).casefold()
    assert len(captured["macro_context"]["kpis"]) == 3
    assert captured["macro_context"]["kpis"][0]["value"] == "Arriendo"


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
    assert len(bar[0].xpath(".//td")) == 21
    labels = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' position-anchor-label ')]")
    lines = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' position-anchor-line ')]")
    assert len(labels) == 2
    assert len(lines) == 2
    assert root.xpath("//*[@data-component-id='single-property-comparison-bar-v1']")
    assert all("height:" in line.get("style", "") for line in lines)
    assert not root.xpath("//*[contains(@style, 'transparent') and contains(@style, 'background')]")
    for label in labels:
        assert label.getparent().getparent().getparent().xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' position-anchor-line ')]")
    assert "max-width:620px" in html


def test_comparison_bar_keeps_labels_tied_to_actual_marker_positions_at_edges():
    low = renderer._positioning_graph({"p10": 10, "p90": 90, "median": 10}, 10)
    high = renderer._positioning_graph({"p10": 10, "p90": 90, "median": 90}, 90)
    assert low["reference_index"] == low["property_index"] == 2
    assert high["reference_index"] == high["property_index"] == 18
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
    valuation_cells = [
        " ".join(cell.itertext()).strip()
        for cell in lxml_html.fromstring(html).xpath("//*[contains(concat(' ',normalize-space(@class),' '),' valuation-strip-single ')]//td")
    ]
    assert "REFERENCIA DEL SEGMENTO 52,3 UF/m² Corte 18 de mayo de 2026" in valuation_cells[1]
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
    assert "Estamos preparando tu propiedad para un nuevo escenario de arriendo" in html
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
    assert result["top3"][0]["price_label"] == "$ 5.518.848 / mes"
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
    assert "Publicaciones similares en arriendo" in rent_html
    assert "comprador" not in rent_text and "venta" not in rent_text
    assert single_structure(sale_root) == single_structure(rent_root)


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
    assert "La muestra es distinta del universo comunal." in visible
