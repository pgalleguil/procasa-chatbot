from __future__ import annotations

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


def test_verified_snapshot_phone_builds_click_only_whatsapp_link():
    db = mongomock.MongoClient().test
    current = {"period": "2026-10"}
    view = _view(db, monthly=current, history=[current], snapshot={"executive_phone": "+56 9 1234 5678"})
    assert view["executive_whatsapp_url"].startswith("https://wa.me/56912345678?")
    assert "5438" in view["executive_whatsapp_url"]


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
