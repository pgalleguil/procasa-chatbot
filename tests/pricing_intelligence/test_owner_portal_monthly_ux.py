from __future__ import annotations

from datetime import datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
import mongomock

from owner_portal.monthly import (
    _gap_explanation,
    _normalize_market_context,
    build_monthly_portal_view,
    owner_property_portal_id,
)


def _view(db, *, snapshot=None, monthly=None, campaign_view=None, history=None):
    owner = "owner@example.test"
    code = "5438"
    key = owner_property_portal_id(code, owner)
    current = monthly or {"period": "2026-10"}
    db["owner_property_portals"].insert_one({
        "_id": key,
        "owner_key": key,
        "property_code": code,
        "current_portal_state": current,
        "monthly_snapshots": history or [current],
    })
    row = {
        "property_code": code,
        "owner_email": owner,
        "send_status": "SENT",
        "campaign_snapshot": {"owner_email": owner, **(snapshot or {})},
    }
    campaign = {
        "source": "EMAIL", "safe_mode": False, "document_available": False,
        "executive_name": "Mariela Arriagada", "top_primary_url": "/accept",
        "top_advisor_url": "/advisor", "sticky_primary_url": "/accept",
        "sticky_advisor_url": "/advisor", **(campaign_view or {}),
    }
    return build_monthly_portal_view(db, row, campaign)


def test_market_context_has_three_compact_canonical_kpis():
    result = _normalize_market_context({
        "kpis": [
            {"label": "Financiamiento hipotecario", "value": "4,1%", "note": "Promedio"},
            {"label": "TPM", "value": "4,5%"},
            {"label": "Demanda interna", "value": "Selectiva"},
        ],
        "summary": "Una conclusión breve.",
    })
    assert [item["label"] for item in result["kpis"]] == ["Hipotecario", "TPM", "Demanda"]
    assert [item["value"] for item in result["kpis"]] == ["4,1%", "4,5%", "Selectiva"]
    assert len(result["kpis"]) == 3


def test_gap_explanation_only_appears_for_material_verified_gap():
    position = {"reference_value": "56,1 UF/m²", "property_value": "82 UF/m²"}
    explanation = _gap_explanation(position, 10)
    assert "46%" in explanation
    assert "10%" in explanation
    assert _gap_explanation({"reference_value": None, "property_value": "82 UF/m²"}, 10) == ""
    assert _gap_explanation(position, None) == ""


def test_zero_activity_is_preserved_and_unverified_whatsapp_is_hidden():
    db = mongomock.MongoClient().test
    current = {
        "period": "2026-10",
        "activity_90d": {"leads": 0, "conversations": 0, "visits": 0},
    }
    view = _view(db, monthly=current, history=[current], snapshot={"executive_phone": "not-a-phone"})
    assert view["activity_90d"] == {
        "leads": 0, "conversations": 0, "visits": 0, "summary": "", "source_date": "", "source_label": "",
    }
    assert view["executive_whatsapp_url"] == ""
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    assert "Leads registrados" in html
    assert "Conversaciones" in html
    assert "Visitas coordinadas" in html
    assert html.count('class="activity-value">0</div>') == 3


def test_verified_snapshot_phone_is_rendered_without_unsigned_whatsapp_link():
    db = mongomock.MongoClient().test
    current = {"period": "2026-10"}
    view = _view(db, monthly=current, history=[current], snapshot={"executive_phone": "+56 9 1234 5678"})
    assert view["executive_phone"] == "+56 9 1234 5678"
    assert view["executive_whatsapp_url"] == ""  # Requires a registered signed portal access.


def test_month_over_month_requires_real_metrics_in_both_snapshots():
    db = mongomock.MongoClient().test
    previous = {"period": "2026-09", "current_price": 18_507, "operation": "VENTA", "activity_90d": {"leads": 1}}
    current = {"period": "2026-10", "current_price": 16_656, "operation": "VENTA", "activity_90d": {"leads": 4}}
    view = _view(db, monthly=current, history=[previous, current])
    assert {item["label"] for item in view["monthly_changes"]} == {"Precio", "Leads 90 días"}

    db_without_metrics = mongomock.MongoClient().test
    old_media = {"period": "2026-09", "property_media": {"verified_property_code": "5438"}}
    new_media = {"period": "2026-10", "property_media": {"verified_property_code": "5438"}}
    no_changes = _view(db_without_metrics, monthly=new_media, history=[old_media, new_media])
    assert no_changes["monthly_changes"] == []
    assert no_changes["previous_month_available"] is False


def test_portal_template_has_svg_icons_one_disclaimer_and_short_top_cta():
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("owner_campaign_monthly_portal.html")
    html = template.render(view={
        "property_type": "Departamento", "commune": "Talca", "operation": "VENTA",
        "property_code": "5438", "updated_label": "02-10-2026", "property_image_url": "",
        "can_authorize": True, "already_authorized": False, "safe_mode": False,
        "top_primary_url": "/accept", "sticky_primary_url": "/accept",
        "top_advisor_url": "/advisor", "sticky_advisor_url": "/advisor",
        "activity_90d": {"leads": 0, "conversations": 0, "visits": 0},
        "position": {"count": None, "reference_value": None, "property_value": None, "marker_pct": None},
        "market_context": None, "communal_reference": None, "diagnosis": "",
        "recommendation_text": "", "document_available": False, "executive_name": "Ejecutivo PROCASA",
        "monthly_changes": [],
    })
    assert "<svg" in html
    assert 'data-cta-placement="TOP"' in html and "Revisar ajuste" in html
    assert html.count("Las referencias de mercado se basan en publicaciones observadas") == 1


def test_missing_market_metrics_never_create_empty_cards():
    context = _normalize_market_context({"kpis": [{"label": "TPM", "value": "4,5%"}], "summary": "Resumen verificado."})
    assert len(context["kpis"]) == 1 and context["kpis"][0]["label"] == "TPM"
    assert _normalize_market_context({"summary": "Resumen verificado."})["kpis"] == []
    assert _normalize_market_context({"kpis": [{"label": "TPM", "value": None}]}) is None


def test_market_position_requires_compatible_units_and_preserves_zero():
    from owner_portal.monthly import _position_data, _position_gap
    assert round(_position_gap("100 UF/m²", "145 UF/m²"), 2) == 45
    assert round(_position_gap("100 UF/m² útil", "92 UF/m² útil"), 2) == -8
    assert _position_gap("100 UF/m² útil", "145 UF/m² total") is None
    assert _position_gap("100", "145") is None
    assert _position_gap(100, 0, "UF/m²") == -100
    assert _position_gap(0, 145, "UF/m²") is None
    position = _position_data({}, {"comparables": {"reference_value": 56.1, "property_value": 57.0, "unit": "UF/m² útil"}})
    assert round(position["gap_pct"], 2) == 1.60


def test_previous_month_zero_and_unchanged_metrics_are_not_missing():
    from owner_portal.monthly import _previous_snapshot_changes
    old = {"period": "2026-09", "activity_90d": {"leads": 0, "visits": 0}}
    current = {"period": "2026-10", "activity_90d": {"leads": 0, "visits": 2}}
    pairs = _previous_snapshot_changes([old, current], current)
    assert pairs[0]["before"] == "0" and pairs[0]["after"] == "0"
    assert pairs[1]["before"] == "0" and pairs[1]["after"] == "2"


def test_executive_directory_matches_exact_identity_and_never_guesses_photo():
    from owner_portal.executive import resolve_executive_contact
    db = mongomock.MongoClient().test
    row = {"executive_email": "exec@example.test", "campaign_snapshot": {}}
    db.usuarios.insert_one({"nombre": "Ana Pérez", "email": "exec@example.test", "is_active": True,
        "phone": "+56 9 1234 5678", "photo_verified": True, "photo_url": "https://crm.example.test/ana.jpg"})
    contact = resolve_executive_contact(db, row, {}, "Ana Pérez")
    assert contact["phone"] == "+56 9 1234 5678"
    assert contact["photo_url"].endswith("/ana.jpg") and contact["initials"] == "AP"
    db.usuarios.insert_one({"nombre": "Ana Pérez", "email": "exec@example.test", "is_active": True, "phone": "56999999999"})
    ambiguous = resolve_executive_contact(db, row, {}, "Ana Pérez")
    assert ambiguous["phone"] == "" and ambiguous["photo_url"] == ""
    assert resolve_executive_contact(db, {"campaign_snapshot": {}}, {}, "Ana")["phone"] == ""


PREMIUM_CASES = ("full", "zero", "no_previous", "previous", "no_phone", "no_photo", "photo_phone", "missing_macro", "material_gap", "no_gap", "stale", "authorized")


def premium_fixture(case):
    import os
    db = mongomock.MongoClient().test
    code, owner = "5438", "owner@example.test"
    key = owner_property_portal_id(code, owner)
    current = {
        "period": "2026-10", "property": {"property_type": "Departamento", "commune": "Talca", "operation": "VENTA"},
        "property_media": {"public_page_url": "https://www.procasa.cl/5438", "hero_image_url": "https://demoazimg.prop360.cl/procasa/img/propiedades/5438_main.JPEG", "verified_property_code": code, "public_page_active": True, "image_source": "PROCASA_PUBLIC_PROPERTY"},
        "activity_90d": {"leads": 0 if case == "zero" else 1, "conversations": 0, "visits": 0, "source_date": "2026-10-02"},
        "market_context": {"mortgage_rate": "4,1%", "tpm": "4,5%", "demand_status": "Selectiva", "reference_month": "Octubre 2026", "source_date": "2026-10-01", "summary": "El financiamiento y una demanda más selectiva hacen que el posicionamiento de precio sea especialmente relevante. " * 3},
        "comparables": {"count": 17, "reference_value": "56,1 UF/m² útil", "property_value": "82 UF/m² útil", "source_date": "2026-09-30"},
        "diagnosis": "Las consultas todavía no se han traducido en conversaciones ni visitas coordinadas. Conviene observar la respuesta comercial y revisar el posicionamiento frente a alternativas disponibles. " * 3,
        "recommendation": {"text": "Proponemos revisar el posicionamiento comercial y evaluar las opciones de ajuste de precio disponibles para esta propiedad. " * 3},
        "executive": {"name": "Ejecutivo QA", "email": "executive@example.test", "phone": "+56 9 1234 5678", "role": "Ejecutivo PROCASA", "photo_url": "/static/qa_executive.png", "photo_verified": True},
    }
    current["recommendation"]["diagnosis"] = current.pop("diagnosis")
    if case == "missing_macro": current["market_context"].pop("tpm")
    if case == "no_gap": current["comparables"].pop("property_value")
    if case in {"no_phone", "no_photo", "zero", "no_previous"}: current["executive"].pop("photo_url")
    if case in {"no_phone", "no_previous"}: current["executive"].pop("phone")
    previous = {"period": "2026-09", "activity_90d": {"leads": 0, "visits": 0}}
    history = [previous, current] if case == "previous" else [current]
    db.owner_property_portals.insert_one({"_id": key, "owner_key": key, "property_code": code, "current_portal_state": current, "monthly_snapshots": history})
    row = {"campaign_id": "owner_price_sucre_wave2_20260930", "property_code": code, "owner_email": owner,
        "send_status": "SKIPPED_STALE_OR_MISMATCH" if case == "stale" else "SENT", "executive_name": "Ejecutivo QA", "executive_email": "executive@example.test",
        "portal_access": {"expires_at": datetime.now(timezone.utc) + timedelta(days=90)},
        "campaign_snapshot": {"owner_email": owner, "current_price": 18507, "recommended_price": 16656, "recommended_adjustment_pct": 10, "document_type": "COMMUNAL_MARKET_REPORT"}}
    campaign = {"logo_url": "/static/logo.png", "source": "EMAIL", "safe_mode": case == "stale", "already_authorized": case == "authorized", "can_authorize": case not in {"stale", "authorized"},
        "executive_name": "Ejecutivo QA", "operation": "Venta", "property_type": "Departamento", "commune": "Talca", "updated_label": "02-10-2026",
        "current_price_label": "18.507 UF", "recommended_price_label": "16.656 UF", "recommended_adjustment_pct": 10,
        "document_available": True, "document_type": "COMMUNAL_MARKET_REPORT", "report_url": "/report",
        "top_primary_url": "/accept", "top_advisor_url": "/advisor", "sticky_primary_url": "/accept", "sticky_advisor_url": "/advisor"}
    from unittest.mock import patch
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-fixture-secret"}):
        return build_monthly_portal_view(db, row, campaign)


def test_premium_fixture_matrix_safe_fields_and_layout_order():
    from bs4 import BeautifulSoup
    template = Environment(loader=FileSystemLoader("templates")).get_template("owner_campaign_monthly_portal.html")
    for case in PREMIUM_CASES:
        view = premium_fixture(case)
        html = template.render(view=view)
        soup = BeautifulSoup(html, "html.parser")
        assert soup.select_one(".hero-photo") and view["property_code"] == "5438"
        assert soup.select_one(".executive-avatar span")
        assert "-webkit-line-clamp" not in html
        assert len(soup.select("details.expandable")) >= (0 if case == "stale" else 2)
        if case == "previous":
            assert html.index('id="summary-title"') < html.index('id="monthly-changes-title"') < html.index('id="market-title"')
            assert view["monthly_changes"][0]["before"] == "0"
        else:
            assert not soup.select_one("#monthly-changes-title")
        if case == "no_phone":
            assert not soup.select_one(".executive-phone") and not soup.select_one('[data-cta-placement="EXECUTIVE"]')
        if case == "no_photo":
            assert not soup.select_one(".executive-avatar img")
        if case == "photo_phone":
            assert soup.select_one(".executive-avatar img") and soup.select_one(".executive-phone")
        if case == "missing_macro": assert len(soup.select(".market-kpi")) == 2
        if case == "no_gap": assert not soup.select_one(".position-kpi") and not view["gap_explanation"]
        if case in {"stale", "authorized"}: assert not soup.select_one('.button-primary[href]')
