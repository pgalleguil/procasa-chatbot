from __future__ import annotations

from datetime import datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
import mongomock

from owner_portal.monthly import (
    _gap_explanation,
    _normalize_market_context,
    _position_simulation_data,
    _recommendation_narrative,
    _recommendation_period_label,
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
        "campaign_id": "owner_price_sucre_wave2_20260930",
        "property_code": code,
        "owner_email": owner,
        "send_status": "SENT",
        "campaign_snapshot": {"owner_email": owner, **(snapshot or {})},
    }
    campaign = {
        "source": "EMAIL", "safe_mode": False, "document_available": False,
        "executive_name": "Mariela Arriagada", "top_primary_url": "/accept",
        "top_advisor_url": "https://www.procasa.cl/advisor", "advisor_url": "https://www.procasa.cl/advisor", "sticky_primary_url": "/accept",
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


def test_recommendation_copy_uses_only_available_evidence_and_comparable_wording():
    comparable_position = {
        "reference_value": "91,1 UF/m² útil", "property_value": "132,2 UF/m² útil", "gap_pct": 45.1,
    }
    summary, details = _recommendation_narrative(comparable_position, {}, 10)
    assert "referencia comparable" in summary.casefold()
    assert "90 días" not in summary and all("90 días" not in detail for detail in details)
    assert any("referencia comparable" in detail.casefold() for detail in details)
    assert any("45%" in detail and "10%" in detail for detail in details)
    assert all("mercado" not in detail.casefold() for detail in details)

    activity = {"leads": 0, "conversations": 0, "visits": 0}
    active_summary, active_details = _recommendation_narrative(comparable_position, activity, 10)
    assert "0 leads" in active_summary and "0 conversaciones" in active_summary and "0 visitas" in active_summary
    assert any("últimos 90 días" in detail for detail in active_details)
    no_comparables_summary, _ = _recommendation_narrative({}, {"leads": 2, "conversations": 1}, 7)
    assert "2 leads" in no_comparables_summary and "1 conversación" in no_comparables_summary
    fallback_summary, fallback_details = _recommendation_narrative({}, {}, None)
    assert fallback_summary and fallback_details
    assert "90 días" not in fallback_summary


def test_recommendation_period_uses_current_snapshot_period():
    assert _recommendation_period_label("2026-10") == "Octubre 2026"
    assert _recommendation_period_label("not-a-period") == ""


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
    assert view["top_whatsapp_url"] == view["sticky_whatsapp_url"] == ""
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
    assert view["top_whatsapp_url"] == view["sticky_whatsapp_url"] == ""  # Requires a registered signed portal access.


def test_verified_support_documents_keep_existing_links_and_source_dates():
    import os
    from unittest.mock import patch
    from campanas.owner_campaign_live_events import issue_live_token
    db = mongomock.MongoClient().test
    communal_url = "https://www.procasa.cl/campana/informe?token=communal-signed"
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-support-document-test-secret"}):
        appraisal_token = issue_live_token(
            campaign_id="owner_price_sucre_wave2_20260930", property_code="5438",
            action="ver_informe", recipient="owner@example.test",
            document_type="INDIVIDUAL_APPRAISAL",
            expires_at=int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp()),
            source="EMAIL", interaction_surface="OWNER_PORTAL", cta_placement="ORIGINAL",
        )
        appraisal_url = f"https://www.procasa.cl/campana/informe?token={appraisal_token}"
    monthly = {
        "period": "2026-10",
        "documents": [
            {"type": "COMMUNAL_MARKET_REPORT", "verified": True, "source_date": "2026-04-28"},
            {"document_type": "INDIVIDUAL_APPRAISAL", "verified": True,
             "report_url": appraisal_url, "issued_at": datetime(2026, 9, 15, tzinfo=timezone.utc)},
            {"type": "INDIVIDUAL_APPRAISAL", "verified": True,
             "url": "https://untrusted.example/campana/informe?token=other"},
            {"type": "COMMUNAL_MARKET_REPORT", "verified": False,
             "url": "https://www.procasa.cl/campana/informe?token=unverified"},
        ],
    }
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-support-document-test-secret"}):
        view = _view(db, monthly=monthly, snapshot={"document_type": "COMMUNAL_MARKET_REPORT"}, campaign_view={
            "document_available": True,
            "document_type": "COMMUNAL_MARKET_REPORT",
            "report_url": communal_url,
            "commune": "Talca",
        })
    assert view["support_documents"] == [
        {"type": "COMMUNAL_MARKET_REPORT", "title": "Informe de mercado comunal",
         "metadata": "Talca · PDF · Actualizado 28-04-2026", "url": communal_url},
        {"type": "INDIVIDUAL_APPRAISAL", "title": "Tasación comercial",
         "metadata": "Propiedad 5438 · PDF · Emitida 15-09-2026", "url": appraisal_url},
    ]


def test_support_document_dates_are_never_inferred_from_generation_time():
    from owner_portal.monthly import _support_document_date
    assert _support_document_date({"generated_at": "2026-04-28", "source_date": "fecha desconocida"}) == ""
    assert _support_document_date({"source_date": "28/04/2026"}) == "28-04-2026"


def test_support_document_list_is_full_row_clickable_and_compact():
    from bs4 import BeautifulSoup
    template = Environment(loader=FileSystemLoader("templates")).get_template("owner_campaign_monthly_portal.html")
    view = premium_fixture("full")
    view["support_documents"] = [
            {"type": "COMMUNAL_MARKET_REPORT", "title": "Informe de mercado comunal",
             "metadata": "Talca · PDF · Actualizado 28-04-2026",
             "url": "/campana/informe?token=communal-signed"},
            {"type": "INDIVIDUAL_APPRAISAL", "title": "Tasación comercial",
             "metadata": "Propiedad 5438 · PDF · Emitida 15-09-2026",
             "url": "/campana/informe?token=appraisal-signed"},
        ]
    soup = BeautifulSoup(template.render(view=view), "html.parser")
    section = soup.select_one(".support-section")
    rows = section.select("a.support-row")
    assert section.select_one("#document-title").get_text(" ", strip=True) == "Documentos de respaldo"
    assert len(rows) == 2
    assert [row["data-document-type"] for row in rows] == ["COMMUNAL_MARKET_REPORT", "INDIVIDUAL_APPRAISAL"]
    assert [row["href"] for row in rows] == [item["url"] for item in view["support_documents"]]
    assert all(row.select_one(".support-document-icon svg") and row.select_one(".support-chevron") for row in rows)
    assert all(row.select_one(".support-document-title") and "PDF" in row.select_one(".support-document-meta").get_text() for row in rows)
    assert not section.select("button, .document-action")
    assert "Ver respaldo" not in section.get_text()
    assert "min-height:60px" in template.render(view=view)


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
        "top_advisor_url": "https://www.procasa.cl/advisor", "advisor_url": "https://www.procasa.cl/advisor", "sticky_advisor_url": "/advisor",
        "activity_90d": {"leads": 0, "conversations": 0, "visits": 0},
        "position": {"count": None, "reference_value": None, "property_value": None, "marker_pct": None},
        "market_context": None, "communal_reference": None, "diagnosis": "",
        "recommendation_text": "", "document_available": False, "executive_name": "Ejecutivo PROCASA",
        "monthly_changes": [],
    })
    assert "<svg" in html
    assert 'data-cta-placement="TOP"' in html and "Revisar ajuste" in html
    assert html.count("Las referencias de mercado se basan en publicaciones observadas") == 1


def test_top_and_sticky_whatsapp_render_only_when_signed_urls_exist():
    from bs4 import BeautifulSoup
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("owner_campaign_monthly_portal.html")
    html = template.render(view={
        "property_type": "Departamento", "commune": "Talca", "operation": "VENTA",
        "property_code": "5438", "updated_label": "02-10-2026", "property_image_url": "",
        "can_authorize": True, "already_authorized": False, "safe_mode": False,
        "top_primary_url": "/accept", "sticky_primary_url": "/accept",
        "top_whatsapp_url": "/owner-portal/executive-whatsapp?token=top-signed",
        "sticky_whatsapp_url": "/owner-portal/executive-whatsapp?token=sticky-signed",
        "activity_90d": {}, "position": {}, "market_context": None, "communal_reference": None,
        "diagnosis": "", "recommendation_text": "", "document_available": False,
        "executive_name": "Ana Pérez", "executive_role": "Ejecutiva responsable · PROCASA SUCRE",
        "executive_initials": "AP", "executive_phone": "+56 9 1234 5678",
        "executive_email": "ana@example.test", "executive_photo_url": "", "monthly_changes": [],
    })
    soup = BeautifulSoup(html, "html.parser")
    top = soup.select_one('a[data-cta-placement="TOP"][data-cta-type="WHATSAPP"]')
    sticky = soup.select_one('a[data-cta-placement="STICKY"][data-cta-type="WHATSAPP"]')
    assert top and "Escribir por WhatsApp" in top.get_text(" ", strip=True)
    assert sticky and sticky.get_text(" ", strip=True) == "WhatsApp"
    assert top["href"].endswith("top-signed") and sticky["href"].endswith("sticky-signed")
    assert not soup.select_one('a[data-cta-placement="EXECUTIVE"]')
    assert "Hablar con mi ejecutivo" not in soup.get_text() and "Hablar con ejecutivo" not in soup.get_text()
    assert "Revisar ajuste" in soup.select_one('a[data-cta-placement="TOP"]').get_text()
    assert "Teléfono" in soup.get_text() and "+56 9 1234 5678" in soup.get_text()
    assert "Correo" in soup.get_text() and "ana@example.test" in soup.get_text()
    assert not soup.select_one(".executive-phone[href]") and not soup.select_one(".executive-email[href]")
    assert len(soup.select('.button-whatsapp svg[aria-hidden="true"]')) == 2


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


def test_comparable_position_simulation_uses_frozen_cluster_and_same_surface():
    comparable = {
        "count": 13, "evidence_level": "HIGH", "version": "cluster_v2",
        "positioning_mode": "PRICE_M2", "reference_value": 91.1,
        "property_value": 132.2, "surface_ref_m2": 140,
        "unit": "UF/m² útil", "source_date": "2026-09-30",
        "analysis_generated_at": "2026-09-30",
    }
    communal = {
        "offer_uf_m2": 102.8, "reference_unit": "UF/m² de oferta",
        "universe_value": "2.867", "universe_unit": "publicaciones activas",
        "source_date": "2026-04-28",
    }
    result = _position_simulation_data(
        position={"reference_value": "91,1 UF/m² útil", "property_value": "132,2 UF/m² útil"},
        comparables=comparable, communal=communal,
        property_state={"operation": "VENTA", "current_price": 18507, "surface_ref_m2": 140},
        current_price=18507, recommended_price=16656,
        current_price_label="18.507 UF", recommended_price_label="16.656 UF",
        adjustment=10, recommendation_is_monthly=True,
        stale=False, already_authorized=False,
    )
    assert result["available"] is True
    assert result["surface_ref_m2"] == 140
    assert round(18507 / result["surface_ref_m2"], 1) == round(result["current_m2"], 1) == 132.2
    assert round(16656 / result["surface_ref_m2"], 1) == round(result["proposed_m2"], 1) == 119.0
    assert round(result["current_gap_pct"]) == 45
    assert round(result["proposed_gap_pct"]) == 31
    assert result["current_gap_label"] == "45% sobre propiedades similares"
    assert result["proposed_gap_label"] == "31% sobre propiedades similares"
    assert result["communal_value"] == 102.8
    assert result["communal_on_same_scale"] is True
    assert result["communal_source_date"] == "28-04-2026"
    assert result["comparables_source_date"] == "30-09-2026"
    assert "por sobre el valor observado en propiedades similares" in result["current_copy"]
    assert "la diferencia frente a propiedades similares bajaría aproximadamente a 31%" in result["adjusted_copy"]


def test_comparable_position_simulation_fails_closed_on_weak_or_mismatched_data():
    base = {
        "count": 13, "evidence_level": "HIGH", "version": "cluster_v2",
        "positioning_mode": "PRICE_M2", "reference_value": 91.1,
        "property_value": 132.2, "surface_ref_m2": 140, "unit": "UF/m² útil",
    }
    args = {
        "position": {}, "comparables": base, "communal": {},
        "property_state": {"operation": "VENTA", "current_price": 18507, "surface_ref_m2": 140},
        "current_price": 18507, "recommended_price": 16656,
        "current_price_label": "18.507 UF", "recommended_price_label": "16.656 UF",
        "adjustment": 10, "recommendation_is_monthly": True,
        "stale": False, "already_authorized": False,
    }
    assert _position_simulation_data(**args)["available"] is True
    assert _position_simulation_data(**{**args, "comparables": {**base, "evidence_level": "LIMITED"}})["available"] is False
    assert _position_simulation_data(**{**args, "property_state": {**args["property_state"], "surface_ref_m2": 150}})["reason"] == "POSITIONING_SURFACE_DENOMINATOR_MISMATCH"
    assert _position_simulation_data(**{**args, "recommendation_is_monthly": False})["available"] is False
    assert _position_simulation_data(**{**args, "stale": True})["available"] is False
    assert _position_simulation_data(**{**args, "already_authorized": True})["available"] is False


def test_sent_email_comparable_fallback_restores_position_bar_without_monthly_comparables():
    from bs4 import BeautifulSoup

    db = mongomock.MongoClient().test
    owner, code = "owner@example.test", "5695"
    key = owner_property_portal_id(code, owner)
    monthly = {
        "period": "2026-10",
        # Even if a later monthly price exists, the email comparison must stay
        # bound to the price actually sent with this campaign.
        "current_price": 19000,
        "property_media": {"verified_property_code": code},
    }
    db["owner_property_portals"].insert_one({
        "_id": key, "owner_key": key, "property_code": code,
        "current_portal_state": monthly, "monthly_snapshots": [monthly],
    })
    row = {
        "campaign_id": "owner_price_sucre_wave1_20260928",
        "property_code": code, "owner_email": owner, "send_status": "SENT",
        "campaign_snapshot": {
            "owner_email": owner, "operation": "VENTA", "current_price": 18507,
            "recommended_price": 16656.3, "recommended_adjustment_pct": 10,
            "comparable_mode": "PRIMARY_COMPARABLES", "comparable_count": 13,
        },
    }
    campaign = {
        "source": "WHATSAPP", "safe_mode": False, "already_authorized": False,
        "operation": "Venta", "current_price_label": "18.507 UF",
        "recommended_price_label": "16.656 UF", "recommended_adjustment_pct": 10,
        "document_available": False,
    }
    # Minimal text extracted from the immutable sent-email HTML. The production
    # route passes HTML only after artifact/ledger identity verification.
    sent_email_html = """
      <div class="single-heading">Departamento &#183; Talca</div>
      <div class="single-operation">Venta &#183; Codigo 5695 &#183; 2D &#183; 2B &#183; m&#178; 140 m&#178; &#250;tiles</div>
      <div class="evidence-badge-single">13 publicaciones</div>
      <div class="position-ref-box">Referencia de mercado <b>91,1 UF/m&#178; &#250;til</b></div>
      <div class="position-prop-box">Tu propiedad <b>132,2 UF/m&#178; &#250;til</b></div>
    """
    from owner_portal.monthly import extract_verified_email_evidence, _position_data
    extracted = extract_verified_email_evidence(sent_email_html)
    assert extracted.get("verified_property_surfaces_m2") == {"useful": 140.0}, extracted
    assert _position_data(extracted, {}).get("count") == 13, extracted

    view = build_monthly_portal_view(db, row, campaign, email_html=sent_email_html)
    simulation = view["position_simulation"]
    assert simulation["available"] is True, simulation
    assert round(simulation["current_m2"], 1) == round(18507 / 140, 1)  # frozen campaign price, not monthly 19,000 UF
    assert simulation["count"] == 13
    assert simulation["surface_ref_m2"] == 140
    assert round(simulation["current_m2"], 1) == 132.2
    assert round(simulation["proposed_m2"], 1) == 119.0
    assert round(simulation["current_gap_pct"]) == 45
    assert round(simulation["proposed_gap_pct"]) == 31
    assert simulation["communal_value"] is None  # No unverified communal value is inferred.

    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    section = BeautifulSoup(html, "html.parser").select_one("[data-position-simulation]")
    assert section is not None
    assert section.select_one('[data-position-state="adjusted"]') is not None
    assert section.select_one('[data-position-gap]').get_text(strip=True) == "45% sobre propiedades similares"
    assert section.select_one('[data-point="property"]') is not None


def test_sent_email_comparable_fallback_fails_closed_without_verified_surface():
    from owner_portal.monthly import extract_verified_email_evidence

    evidence = extract_verified_email_evidence("""
      <div class="position-ref-box">Propiedades similares <b>91,1 UF/m² útil</b></div>
      <div class="position-prop-box">Tu propiedad <b>132,2 UF/m² útil</b></div>
    """)
    assert evidence.get("verified_property_surfaces_m2") is None


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


PREMIUM_CASES = ("full", "zero", "no_previous", "previous", "no_phone", "no_photo", "photo_phone", "missing_macro", "material_gap", "no_gap", "historical_email", "stale", "authorized")


def premium_fixture(case):
    import os
    db = mongomock.MongoClient().test
    code, owner = "5438", "owner@example.test"
    key = owner_property_portal_id(code, owner)
    current = {
        "period": "2026-10", "property": {"property_type": "Departamento", "commune": "Talca", "operation": "VENTA", "current_price": 18507, "surface_ref_m2": 140},
        "property_media": {"public_page_url": "https://www.procasa.cl/5438", "hero_image_url": "https://demoazimg.prop360.cl/procasa/img/propiedades/5438_main.JPEG", "verified_property_code": code, "public_page_active": True, "image_source": "PROCASA_PUBLIC_PROPERTY"},
        "activity_90d": {"leads": 0 if case == "zero" else 1, "conversations": 0, "visits": 0, "source_date": "2026-10-02"},
        "market_context": {"mortgage_rate": "4,1%", "tpm": "4,5%", "demand_status": "Selectiva", "reference_month": "Octubre 2026", "source_date": "2026-10-01", "summary": "El financiamiento y una demanda más selectiva hacen que el posicionamiento de precio sea especialmente relevante. " * 3},
        "comparables": {"count": 13, "evidence_level": "HIGH", "version": "cluster_v2", "positioning_mode": "PRICE_M2", "reference_value": 91.1, "property_value": 132.2, "surface_ref_m2": 140, "unit": "UF/m² útil", "source_date": "2026-09-30", "analysis_generated_at": "2026-09-30"},
        "communal_reference": {"offer_uf_m2": 102.8, "reference_unit": "UF/m² de oferta", "summary": "102,8 UF/m² de oferta · 2.867 publicaciones activas", "universe_value": "2.867", "universe_unit": "publicaciones activas", "source_date": "2026-04-28"},
        "diagnosis": "Las consultas todavía no se han traducido en conversaciones ni visitas coordinadas. Conviene observar la respuesta comercial y revisar el posicionamiento frente a alternativas disponibles. " * 3,
        "recommendation": {"text": "Proponemos revisar el posicionamiento comercial y evaluar las opciones de ajuste de precio disponibles para esta propiedad. " * 3, "recommended_price": 16656, "recommended_adjustment_pct": 10},
        "executive": {"name": "Ejecutivo QA", "email": "executive@example.test", "phone": "+56 9 1234 5678", "role": "Ejecutivo PROCASA", "photo_url": "/static/qa_executive.png", "photo_verified": True},
    }
    current["recommendation"]["diagnosis"] = current.pop("diagnosis")
    if case == "missing_macro": current["market_context"].pop("tpm")
    if case == "no_gap": current["comparables"].pop("property_value")
    if case == "historical_email":
        current.pop("comparables")
        current.pop("communal_reference")
    if case in {"no_phone", "no_photo", "zero", "no_previous"}: current["executive"].pop("photo_url")
    if case in {"no_phone", "no_previous"}: current["executive"].pop("phone")
    previous = {"period": "2026-09", "activity_90d": {"leads": 0, "visits": 0}}
    history = [previous, current] if case == "previous" else [current]
    db.owner_property_portals.insert_one({"_id": key, "owner_key": key, "property_code": code, "current_portal_state": current, "monthly_snapshots": history})
    row = {"campaign_id": "owner_price_sucre_wave2_20260930", "property_code": code, "owner_email": owner,
        "send_status": "SKIPPED_STALE_OR_MISMATCH" if case == "stale" else "SENT", "executive_name": "Ejecutivo QA", "executive_email": "executive@example.test",
        "portal_access": {"expires_at": datetime.now(timezone.utc) + timedelta(days=90)},
        "campaign_snapshot": {"owner_email": owner, "current_price": 18507, "recommended_price": 16656, "recommended_adjustment_pct": 10, "document_type": "COMMUNAL_MARKET_REPORT",
            **({"comparable_mode": "PRIMARY_COMPARABLES", "comparable_count": 13} if case == "historical_email" else {})}}
    campaign = {"logo_url": "/static/logo.png", "source": "EMAIL", "safe_mode": case == "stale", "already_authorized": case == "authorized", "can_authorize": case not in {"stale", "authorized"},
        "executive_name": "Ejecutivo QA", "operation": "Venta", "property_type": "Departamento", "commune": "Talca", "updated_label": "02-10-2026",
        "current_price_label": "18.507 UF", "recommended_price_label": "16.656 UF", "recommended_adjustment_pct": 10,
        "document_available": True, "document_type": "COMMUNAL_MARKET_REPORT", "report_url": "/report",
        "top_primary_url": "/accept", "top_advisor_url": "https://www.procasa.cl/advisor", "advisor_url": "https://www.procasa.cl/advisor", "sticky_primary_url": "/accept", "sticky_advisor_url": "/advisor"}
    from unittest.mock import patch
    sent_email_html = None
    if case == "historical_email":
        sent_email_html = """
          <div class="single-heading">Departamento &#183; Talca</div>
          <div class="single-operation">Venta &#183; Codigo 5438 &#183; 2D &#183; 2B &#183; m&#178; 140 m&#178; &#250;tiles</div>
          <div class="evidence-badge-single">13 publicaciones</div>
          <div class="position-ref-box">Referencia de mercado <b>91,1 UF/m&#178; &#250;til</b></div>
          <div class="position-prop-box">Tu propiedad <b>132,2 UF/m&#178; &#250;til</b></div>
        """
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-fixture-secret"}):
        return build_monthly_portal_view(db, row, campaign, email_html=sent_email_html)


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
        if case in {"stale", "authorized", "no_gap"}:
            assert view["position_simulation"]["available"] is False
        if case in {"authorized", "no_gap"}:
            assert soup.select_one(".position-neutral")
        if case in {"stale", "authorized"}: assert not soup.select_one('.button-primary[href]')
        if case in {"stale", "authorized"}:
            assert soup.select_one('[data-cta-placement="TOP"][data-cta-type="WHATSAPP"]')
            assert soup.select_one('[data-cta-placement="STICKY"][data-cta-type="WHATSAPP"]')
        if case == "full":
            simulation = view["position_simulation"]
            assert simulation["available"] is True
            assert simulation["count"] == 13
            assert simulation["current_m2_label"] == "132,2"
            assert simulation["proposed_m2_label"] == "119,0"
            assert simulation["median_label"] == "91,1"
            assert simulation["communal_label"] == "102,8"
            assert simulation["communal_on_same_scale"] is True
            assert round(simulation["current_gap_pct"]) == 45
            assert round(simulation["proposed_gap_pct"]) == 31
            assert simulation["current_gap_pct"] == (132.2 / 91.1 - 1) * 100
            assert simulation["current_gap_pct"] != (132.2 / 102.8 - 1) * 100
            assert simulation["comparables_source_date"] == "30-09-2026"
            assert simulation["communal_source_date"] == "28-04-2026"
            position_section = soup.select_one(".position-simulation")
            assert position_section.select_one('[data-position-state="current"][aria-pressed="true"]')
            assert position_section.select_one('[data-position-state="adjusted"][disabled]')
            assert position_section.select_one('[data-position-gap]').get_text(strip=True) == "45% sobre propiedades similares"
            assert "Propiedades similares" in position_section.get_text(" ", strip=True)
            assert "Referencia basada en 13 propiedades similares seleccionadas." in position_section.get_text(" ", strip=True)
            assert "mediana comparable" not in position_section.get_text(" ", strip=True).lower()
            assert position_section.select_one('[data-position-summary]')
            assert "Simulación visual. No modifica el precio" in position_section.get_text(" ", strip=True)
            assert "Oferta comunal observada" in position_section.get_text(" ", strip=True)
            assert "Referencia de mercado" not in position_section.get_text(" ", strip=True)
            assert not soup.select_one(".complementary"), "Communal snapshot already shown in the interactive positioning section"
            recommendation = soup.select_one(".recommendation")
            assert recommendation.select_one(".recommendation-summary")
            assert recommendation.select_one(".recommendation-details summary .view-more").get_text(strip=True) == "Ver más ↓"
            assert recommendation.select_one(".recommendation-details summary .view-less").get_text(strip=True) == "Ver menos ↑"
            assert view["recommendation_period_label"] == "Octubre 2026"
            assert view["recommended_price_label"] == "16.656 UF"  # exact source value remains intact
            assert view["recommended_price_display_label"] == "≈ 16.650 UF"
            assert view["recommendation_difference_label"] == "≈ 1.857 UF"
            assert view["adjustment_headline_label"] == "10%"
            assert "1 lead" in view["recommendation_summary"] and "90 días" in view["recommendation_summary"]
            assert len(view["recommendation_details"]) == 3
        if case == "historical_email":
            simulation = view["position_simulation"]
            assert simulation["available"] is True
            assert simulation["current_m2_label"] == "132,2"
            assert simulation["proposed_m2_label"] == "119,0"
            assert round(simulation["current_gap_pct"]) == 45
            assert round(simulation["proposed_gap_pct"]) == 31
            assert simulation["communal_value"] is None
        if case == "zero":
            assert "0 leads" in view["recommendation_summary"]
            assert "0 conversaciones" in view["recommendation_summary"]
            assert "0 visitas" in view["recommendation_summary"]


def test_recommendation_prices_centered_procasa_logo_and_equal_simulation_buttons():
    from bs4 import BeautifulSoup

    template = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    )
    html = template.render(view=premium_fixture("full"))
    soup = BeautifulSoup(html, "html.parser")

    recommendation = soup.select_one(".recommendation")
    assert recommendation.select_one(".recommendation-logo[alt='PROCASA']")
    price_boxes = recommendation.select(".recommendation-step")
    assert len(price_boxes) == 2
    assert [box.get_text(" ", strip=True) for box in price_boxes] == [
        "Precio actual 18.507 UF", "Precio propuesto ≈ 16.650 UF",
    ]

    selector = soup.select_one(".position-simulation__selector")
    options = selector.select(".position-simulation__option")
    assert [option.get_text(" ", strip=True) for option in options] == [
        "Precio actual", "Con ajuste (-10%)",
    ]
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in html
    assert ".recommendation-step { min-width:0; padding:10px; border:1px solid #e4e3f0; border-radius:11px; background:#fff; text-align:center; }" in html
