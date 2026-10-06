from __future__ import annotations

from datetime import datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
import mongomock
import pytest

from owner_portal.monthly import (
    _activity_funnel,
    _gap_explanation,
    _normalize_market_context,
    _static_position_data,
    _position_simulation_data,
    _recommendation_narrative,
    _recommendation_period_label,
    _market_evidence_model,
    build_monthly_portal_view,
    owner_property_portal_id,
    resolve_owner_activity_90d,
    resolve_owner_commercial_funnel_90d,
)


def _view(db, *, snapshot=None, monthly=None, campaign_view=None, history=None, code="5438",
          campaign_id="owner_price_sucre_wave2_20260930"):
    owner = "owner@example.test"
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
        "campaign_id": campaign_id,
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


def test_unanchored_activity_is_unavailable_and_unverified_whatsapp_is_hidden():
    db = mongomock.MongoClient().test
    current = {
        "period": "2026-10",
        "activity_90d": {"leads": 0, "conversations": 0, "visits": 0},
    }
    view = _view(db, monthly=current, history=[current], snapshot={"executive_phone": "not-a-phone"})
    assert {key: view["activity_90d"][key] for key in ("leads", "conversations", "visits", "summary", "source_date", "source_label")} == {
        "leads": None, "conversations": None, "visits": None, "summary": "", "source_date": "", "source_label": "",
    }
    assert view["activity_90d"]["funnel"]["available"] is True
    assert view["top_whatsapp_url"] == view["sticky_whatsapp_url"] == ""
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    assert "Consultas recibidas" in html
    assert "Conversaciones" not in html
    assert "Visitas" in html and "Ofertas" in html and "Cierre" in html
    assert html.count('class="owner-funnel-stage-row"') == 4
    assert html.count('class="owner-funnel-stage-row"') == 4
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    summary = soup.select_one("#summary-title").find_parent("section")
    leads_card = next(
        card for card in summary.select(":scope > .kpis > .kpi")
        if card.select_one(".kpi-label").get_text(strip=True) == "Consultas recibidas"
    )
    assert leads_card.select_one(".kpi-value").get_text(strip=True) == "—"
    assert leads_card.select_one(".kpi-note").get_text(strip=True) == "Sin dato consolidado"


def test_activity_reconstruction_uses_frozen_cutoff_and_deduplicates_conversation_messages():
    db = mongomock.MongoClient().test
    cutoff = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    db["leads"].insert_one({
        "_id": "lead-1", "prospecto": {"codigo": "5695", "operacion": "VENTA"},
        "created_at": cutoff - timedelta(days=20), "phone": "+56911112222", "source": "portal",
    })
    db["conversation_events"].insert_many([
        {"property_code": "5695", "lead_id": "lead-1", "conversation_id": "conversation-1",
         "event_type": "customer_message_received", "timestamp": cutoff - timedelta(days=2)},
        {"property_code": "5695", "lead_id": "lead-1", "conversation_id": "conversation-1",
         "event_type": "human_message_sent", "timestamp": cutoff - timedelta(days=2)},
    ])
    result = resolve_owner_activity_90d(
        db, "5695", candidates=({},), window_end_candidates=(cutoff,), operation="VENTA",
    )
    assert result["source"] == "CANONICAL_READ_ONLY_RECONSTRUCTION"
    assert result["window_end"] == cutoff
    assert (result["leads"], result["conversations"], result["visits"]) == (1, 1, 0)


def test_activity_successful_empty_query_is_zero_but_unknown_source_is_not_zero():
    db = mongomock.MongoClient().test
    cutoff = datetime(2026, 10, 2, tzinfo=timezone.utc)
    empty = resolve_owner_activity_90d(
        db, "5695", candidates=({},), window_end_candidates=(cutoff,), operation="VENTA",
    )
    assert (empty["leads"], empty["conversations"], empty["visits"]) == (0, 0, 0)
    unknown = resolve_owner_activity_90d(
        db, "5695", candidates=({},), window_end_candidates=(), operation="VENTA",
    )
    assert unknown["state"] == "INTEGRITY_FAILURE"
    assert unknown["leads"] is None


def test_activity_does_not_turn_an_undated_exact_property_lead_into_zero():
    db = mongomock.MongoClient().test
    cutoff = datetime(2026, 10, 2, tzinfo=timezone.utc)
    db["leads"].insert_one({"_id": "lead-undated", "prospecto": {"codigo": "5806", "operacion": "VENTA"}})
    result = resolve_owner_activity_90d(
        db, "5806", candidates=({},), window_end_candidates=(cutoff,), operation="VENTA",
    )
    assert result["state"] == "INTEGRITY_FAILURE"
    assert result["integrity"] == "UNVERIFIED_LEAD_TIMESTAMP"
    assert result["undated_lead_candidates"] == 1
    assert result["leads"] is None


def test_activity_funnel_ratios_never_divide_by_zero():
    zero = _activity_funnel({"leads": 0, "conversations": 0, "visits": 0})
    assert zero["available"] is True
    assert [stage["value"] for stage in zero["stages"]] == [0, 0, 0]
    assert all("Sin base" in stage["ratio"] or "Sin consultas" in stage["ratio"] for stage in zero["stages"])
    one = _activity_funnel({"leads": 1, "conversations": 0, "visits": 0})
    assert one["stages"][1]["ratio"] == "0,0% de los leads"
    assert one["stages"][2]["ratio"] == "0,0% de los leads"


def test_verified_snapshot_phone_is_rendered_without_unsigned_whatsapp_link():
    db = mongomock.MongoClient().test
    current = {"period": "2026-10"}
    view = _view(db, monthly=current, history=[current], snapshot={"executive_phone": "+56 9 1234 5678"})
    assert view["executive_phone"] == "+56 9 1234 5678"
    assert view["top_whatsapp_url"] == view["sticky_whatsapp_url"] == ""  # Requires a registered signed portal access.


def test_activity_90d_falls_back_to_frozen_campaign_render_model_without_zero_filling():
    db = mongomock.MongoClient().test
    view = _view(
        db,
        monthly={"period": "2026-10", "activity_90d": {"state": "UNKNOWN"}},
        campaign_view={"activity_90d": {
            "state": "KNOWN_POSITIVE", "total_leads": 3,
            "conversations": 2, "visits": 1, "source_date": "2026-10-02",
        }},
    )
    assert view["activity_90d"]["leads"] == 3
    assert view["activity_90d"]["conversations"] == 2
    assert view["activity_90d"]["visits"] == 1
    assert view["activity_90d"]["source_date"] == "02-10-2026"
    rendered = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    assert rendered.index('id="summary-title"') < rendered.index('id="activity-title"')


def test_market_position_uses_all_compatible_references_and_classifies_both_states():
    from owner_portal.monthly import _market_position_data

    result = _market_position_data(
        position_simulation={"available": False, "communal_on_same_scale": True},
        position_static={
            "available": True, "unit": "UF/m² útil", "count": 7,
            "reference_value": 35.0, "property_value": 39.9,
        },
        comparables={"surface_ref_m2": 100},
        communal={"offer_uf_m2": 37.0, "unit": "UF/m²", "area_basis": "USEFUL"},
        appraisal_card={"market_position_reference": {
            "verified": True, "property_code": "5438", "appraisal_value_uf": 3750,
            "appraisal_uf_m2": 37.5, "appraisal_uf_m2_unit": "UF/m² útil", "area_basis": "USEFUL",
        }},
        property_state={"surface_ref_m2": 100, "surface_ref_unit": "UF/m² útil", "surface_ref_basis": "USEFUL"},
        property_code="5438", current_price=3990, recommended_price=3591,
        recommendation_is_monthly=True, stale=False,
    )
    assert [ref["kind"] for ref in result["references"]] == ["APPRAISAL", "COMPARABLE", "COMMUNAL"]
    assert [ref["current_gap_label"] for ref in result["references"]] == [
        "+6,4% vs tasación", "+14,0% vs similares", "+7,8% vs oferta comunal",
    ]
    assert [ref["proposed_gap_label"] for ref in result["references"]] == [
        "-4,2% vs tasación", "+2,6% vs similares", "-2,9% vs oferta comunal",
    ]
    assert result["current_classification"]["code"] == "ABOVE_ALL_REFERENCES"
    assert result["current_classification"]["label"] == "Sobre 3 de 3 referencias"
    assert result["proposed_classification"]["code"] == "WITHIN_REFERENCE_RANGE"
    assert result["proposed_classification"]["label"] == "Dentro del rango"
    assert result["appraisal_available"] is True and result["appraisal_on_scale"] is True
    assert result["appraisal_exclusion_reason"] == ""
    assert result["communal_available"] is True and result["communal_on_scale"] is True
    assert result["communal_exclusion_reason"] == ""


def test_market_position_reports_available_references_excluded_from_scale():
    from owner_portal.monthly import _market_position_data

    result = _market_position_data(
        position_simulation={"available": False},
        position_static={"available": True, "unit": "UF/m² útil", "count": 7,
                         "reference_value": 35.0, "property_value": 39.9},
        comparables={"surface_ref_m2": 100},
        communal={"offer_uf_m2": 102.76, "reference_unit": "UF/m²"},
        appraisal_card={"mode": "DOCUMENT_ONLY", "document_url": "/private-report"},
        property_state={"surface_ref_m2": 100, "surface_ref_unit": "UF/m² útil", "surface_ref_basis": "USEFUL"},
        property_code="5695", current_price=3990, recommended_price=None,
        recommendation_is_monthly=False, stale=False,
    )
    assert result["available"] is True
    assert [reference["kind"] for reference in result["references"]] == ["COMPARABLE"]
    assert result["appraisal_available"] is True
    assert result["appraisal_on_scale"] is False
    assert result["appraisal_exclusion_reason"] == "NO_VERIFIED_STRUCTURED_REFERENCE"
    assert result["communal_available"] is True
    assert result["communal_on_scale"] is False
    assert result["communal_exclusion_reason"] == "AREA_BASIS_UNKNOWN"


def test_comparable_surface_ref_preserves_historical_pair_and_requires_explicit_equivalence_for_other_sources():
    from owner_portal.monthly import _market_position_data, _static_position_data

    position = {
        "source": "MONTHLY_COMPARABLES", "unit": "UF/m² útil/construido",
        "count": 7, "reference_value": "35,0 UF/m² útil/construido",
        "property_value": "39,9 UF/m² útil/construido",
    }
    comparables = {
        "count": 7, "evidence_level": "HIGH", "version": "cluster_v2",
        "positioning_mode": "PRICE_M2", "reference_value": 35.0,
        "property_value": 39.9, "unit": "UF/m² útil/construido",
    }
    static = _static_position_data(
        position=position, comparables=comparables,
        historical_email_verified=False, campaign_comparable_mode="PRIMARY_COMPARABLES", stale=False,
    )
    assert static["available"] is True
    assert static["unit"] == "UF/m² útil/construido"
    common = {
        "position_simulation": {"available": False, "communal_on_same_scale": True},
        "position_static": static, "comparables": {},
        "communal": {"offer_uf_m2": 37.0, "reference_unit": "UF/m²", "area_basis": "COMPARABLE_SURFACE_REF"},
        "property_state": {"surface_ref_m2": 100}, "property_code": "5438",
        "current_price": 3990, "recommended_price": 3591,
        "recommendation_is_monthly": True, "stale": False,
    }
    appraisal = {"market_position_reference": {
        "verified": True, "property_code": "5438", "appraisal_value_uf": 3750,
        "appraisal_uf_m2": 37.5, "appraisal_uf_m2_unit": "UF/m² útil/construido",
        "area_basis": "COMPARABLE_SURFACE_REF",
    }}
    compatible = _market_position_data(**common, appraisal_card=appraisal)
    assert [ref["kind"] for ref in compatible["references"]] == ["APPRAISAL", "COMPARABLE", "COMMUNAL"]
    assert all(ref["area_basis"] == "COMPARABLE_SURFACE_REF" for ref in compatible["references"])

    mismatched = _market_position_data(
        **{**common, "communal": {"offer_uf_m2": 37.0, "reference_unit": "UF/m²", "area_basis": "USEFUL"}},
        appraisal_card={"market_position_reference": {
            "verified": True, "property_code": "5438", "appraisal_value_uf": 3750,
            "appraisal_uf_m2": 37.5, "appraisal_uf_m2_unit": "UF/m² útil", "area_basis": "USEFUL",
        }},
    )
    assert [ref["kind"] for ref in mismatched["references"]] == ["COMPARABLE"]


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

    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-support-document-test-secret"}):
        communal_token = issue_live_token(
            campaign_id="owner_price_sucre_wave2_20260930", property_code="5438",
            action="ver_informe", recipient="owner@example.test",
            document_type="COMMUNAL_MARKET_REPORT",
            expires_at=int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp()),
            source="EMAIL", interaction_surface="OWNER_PORTAL", cta_placement="ORIGINAL",
        )
    communal_url = f"https://www.procasa.cl/campana/informe?token={communal_token}"
    db_both = mongomock.MongoClient().test
    db_both["universo_cartera_prop360"].insert_one({
        "codigo": "5438", "ubicacion": {"comuna": "Talca"},
        "metadata": {"tipo_propiedad": "Departamento"},
    })
    db_both["mercado_comunal"].insert_one({
        "match_key": "talca|departamento",
        "comuna": "Talca", "tipo_propiedad": "Departamento",
        "mercado_venta": {"uf_m2_publicacion_actual": 55.2},
        "source": {"fecha_reporte": "28/04/2026"},
    })
    appraisal_primary_monthly = {
        "period": "2026-10", "document_type": "INDIVIDUAL_APPRAISAL",
        "current_price": 3990,
        "individual_appraisal": {
            "verified": True, "property_code": "5438", "estimated_low_uf": 3500,
            "estimated_mid_uf": 3700, "estimated_high_uf": 3900,
        },
        "documents": [
            {"document_type": "COMMUNAL_MARKET_REPORT", "verified": True,
             "url": communal_url, "source_date": "2026-04-28"},
        ],
    }
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-support-document-test-secret"}):
        both = _view(db_both, monthly=appraisal_primary_monthly,
                     snapshot={"document_type": "INDIVIDUAL_APPRAISAL"},
                     campaign_view={"document_available": True,
                                    "document_type": "INDIVIDUAL_APPRAISAL",
                                    "report_url": appraisal_url, "commune": "Talca",
                                    "property_type": "Departamento", "operation": "Venta"})
    assert [item["type"] for item in both["support_documents"]] == [
        "INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT",
    ]
    assert both["appraisal_card"]["mode"] == "STRUCTURED"
    assert both["market_reference_card"]["title"] == "Mercado de departamentos en Talca"


def test_support_document_dates_are_never_inferred_from_generation_time():
    from owner_portal.monthly import _support_document_date
    assert _support_document_date({"generated_at": "2026-04-28", "source_date": "fecha desconocida"}) == ""
    assert _support_document_date({"source_date": "28/04/2026"}) == "28-04-2026"


def test_communal_report_document_is_full_row_clickable_without_separate_support_section():
    from bs4 import BeautifulSoup
    template = Environment(loader=FileSystemLoader("templates")).get_template("owner_campaign_monthly_portal.html")
    view = premium_fixture("full")
    view["market_reference_card"] = {
        "kind": "COMMUNAL_MARKET_REPORT", "document_url": "/campana/informe?token=communal-signed",
        "document_title": "Informe de mercado comunal", "document_metadata": "Talca · PDF · Corte 28/04/2026",
    }
    soup = BeautifulSoup(template.render(view=view), "html.parser")
    row = soup.select_one(".complementary-market a.complementary-market__document")
    assert row["href"] == "/campana/informe?token=communal-signed"
    assert row["data-document-type"] == "COMMUNAL_MARKET_REPORT"
    assert row.select_one(".complementary-market__document-title").get_text(strip=True) == "Informe de mercado comunal"
    assert "Talca · PDF · Corte 28/04/2026" in row.select_one(".complementary-market__document-meta").get_text(" ", strip=True)
    assert row.select_one(".complementary-market__document-icon svg") and row.select_one(".complementary-market__document-chevron")
    assert not soup.select_one("#support-title")
    assert not soup.select_one(".support-section")
    assert "Abrir informe completo" not in soup.get_text(" ", strip=True)


def test_communal_reference_card_uses_structured_exact_market_data_and_keeps_signed_pdf_link():
    from bs4 import BeautifulSoup

    db = mongomock.MongoClient().test
    db["mercado_comunal"].insert_one({
        "match_key": "santiago|departamento",
        "comuna": "Santiago",
        "tipo_propiedad": "Departamento",
        "mercado_venta": {
            "uf_m2_publicacion_actual": 55.23,
            "uf_m2_venta_efectiva_actual": 56.07,
            "variacion_uf_m2_12m": -9.98,
            "publicaciones_activas": 9215,
            "publicaciones_totales": 68325,
            "tendencia_publicaciones": "estable",
        },
        "mercado_arriendo": {
            "uf_m2_arriendo_actual": 0.24,
            "variacion_arriendo_12m": 4.35,
            "publicaciones_arriendo_activas": 3379,
            "publicaciones_arriendo_totales": 75115,
        },
        "indicadores_mercado": {
            "liquidez": "baja", "presion_baja_precio": "alta",
            "nivel_competencia": "alto", "tendencia_mercado": "desaceleracion",
            "score_presion_comercial": 88,
        },
        "rangos_precio_venta": {"min_uf": 1288, "max_uf": 3799},
        "source": {"filename": "santiago_departamento.pdf", "fecha_reporte": "28/04/2026"},
    })
    signed_report = "/campana/informe?token=existing-signed-report"
    view = _view(
        db,
        snapshot={
            "document_type": "COMMUNAL_MARKET_REPORT", "commune": "Santiago",
            "property_type": "Departamento", "operation": "VENTA",
        },
        monthly={
            "period": "2026-10", "commune": "Santiago",
            "property_type": "Departamento", "operation": "VENTA",
            "document_type": "COMMUNAL_MARKET_REPORT",
        },
        campaign_view={"document_available": True, "report_url": signed_report},
    )
    assert view["market_reference_card"]["title"] == "Mercado de departamentos en Santiago"
    assert view["market_reference_card"]["metrics"] == [
        {"label": "Precio publicado de referencia", "value": "55,2 UF/m²"},
        {"label": "Variación de precios publicados · 12 meses", "value": "-10,0%"},
        {"label": "Propiedades actualmente en oferta", "value": "9.215"},
        {"label": "Competencia", "value": "Alta"},
        {"label": "Liquidez", "value": "Baja"},
    ]
    assert view["market_reference_card"]["source_date"] == "28/04/2026"
    assert view["market_reference_card"]["interpretation"] == (
        "El mercado muestra una alta cantidad de propiedades compitiendo por compradores y una menor velocidad de absorción. "
        "Además, los valores publicados han retrocedido durante los últimos 12 meses. "
        "En este contexto, el posicionamiento de precio adquiere mayor importancia."
    )
    assert view["market_reference_card"]["details"] == [
        {"label": "Tendencia del mercado", "value": "Desaceleración"},
        {"label": "Presión sobre precios", "value": "Alta"},
        {"label": "Rango observado", "value": "1.288–3.799 UF"},
        {"label": "Publicaciones observadas", "value": "68.325"},
    ]
    assert view["market_reference_card"]["document_url"] == signed_report
    assert db["mercado_comunal"].find_one({"comuna": "Santiago"})["indicadores_mercado"]["score_presion_comercial"] == 88

    template = Environment(loader=FileSystemLoader("templates")).get_template("owner_campaign_monthly_portal.html")
    soup = BeautifulSoup(template.render(view=view), "html.parser")
    section = soup.select_one(".complementary-market")
    assert section.select_one("h2").get_text(" ", strip=True) == "Mercado de departamentos en Santiago"
    assert len(section.select(".complementary-market__metric")) == 5
    details = section.select_one("details.complementary-market__details")
    assert details is not None and "open" not in details.attrs
    detail_text = details.get_text(" ", strip=True)
    assert "Referencia efectiva" not in detail_text
    assert "56,1 UF/m²" not in section.get_text(" ", strip=True)
    assert not any(item["value"] == "88" for item in view["market_reference_card"]["details"])
    assert "arriendo" not in detail_text.casefold()
    assert "Presión sobre precios Alta" in detail_text
    assert section.select_one(".complementary-market__source").get_text(" ", strip=True) == (
        "Informe comunal · Santiago · Departamento · Corte 28/04/2026"
    )
    document = section.select_one("a.complementary-market__document")
    assert document["href"] == signed_report
    assert document["data-cta-placement"] == "ORIGINAL"
    assert document["data-document-type"] == "COMMUNAL_MARKET_REPORT"
    assert document.select_one(".complementary-market__document-title").get_text(strip=True) == "Informe de mercado comunal"
    assert "Santiago · PDF" in document.select_one(".complementary-market__document-meta").get_text(" ", strip=True)
    assert document.select_one(".complementary-market__document-icon svg")
    assert document.select_one(".complementary-market__document-chevron")
    assert "Abrir informe completo" not in section.get_text(" ", strip=True)
    assert not soup.select_one("#support-title")
    assert len(soup.select('[data-document-type="COMMUNAL_MARKET_REPORT"]')) == 1
    assert "propiedades comparables" not in section.get_text(" ", strip=True).casefold()


def test_communal_market_card_is_operation_specific_and_fails_closed_on_identity_mismatch():
    from owner_portal.monthly import _communal_market_card

    db = mongomock.MongoClient().test
    db["mercado_comunal"].insert_one({
        "match_key": "santiago|departamento",
        "comuna": "Santiago", "tipo_propiedad": "Departamento",
        "mercado_venta": {"uf_m2_publicacion_actual": 55.23},
        "mercado_arriendo": {
            "uf_m2_arriendo_actual": 0.24,
            "variacion_arriendo_12m": 4.35,
            "publicaciones_arriendo_activas": 3379,
        },
        "source": {"fecha_reporte": "28/04/2026"},
    })
    rental = _communal_market_card(
        db, commune="Santiago", property_type="Departamento",
        operation="ARRIENDO", document_url="/signed",
    )
    assert rental and rental["metrics"][0] == {
        "label": "Precio de arriendo publicado de referencia", "value": "0,24 UF/m²",
    }
    assert _communal_market_card(
        db, commune="Providencia", property_type="Departamento",
        operation="VENTA", document_url="/signed",
    ) is None


def test_historical_communal_card_resolves_missing_identity_from_exact_master_property_5806():
    from bs4 import BeautifulSoup

    db = mongomock.MongoClient().test
    db["universo_cartera_prop360"].insert_one({
        "codigo": "5806",
        "ubicacion": {"comuna": "Concón"},
        "metadata": {"tipo_propiedad": "Departamento"},
    })
    db["mercado_comunal"].insert_one({
        "match_key": "concon|departamento",
        "comuna": "Concón", "tipo_propiedad": "Departamento",
        "mercado_venta": {
            "uf_m2_publicacion_actual": 83.58,
            "variacion_uf_m2_12m": 2.16,
            "publicaciones_activas": 1802,
            "publicaciones_totales": 4200,
        },
        "indicadores_mercado": {
            "nivel_competencia": "HIGH", "liquidez": "LOW",
            "tendencia_mercado": "desaceleracion", "presion_baja_precio": "HIGH",
        },
        "rangos_precio_venta": {"min_uf": 1500, "max_uf": 8500},
        "source": {"fecha_reporte": "29/04/2026"},
    })
    report_url = "/campana/informe?token=5806-communal-qa"
    view = _view(
        db, code="5806",
        snapshot={"document_type": "COMMUNAL_MARKET_REPORT", "operation": "VENTA"},
        monthly={"period": "2026-10", "documents": []},
        campaign_view={
            "document_available": True, "document_type": "COMMUNAL_MARKET_REPORT",
            "report_url": report_url, "operation": "VENTA",
        },
    )

    card = view["market_reference_card"]
    assert view["commune"] == "Concón"
    assert view["property_type"] == "Departamento"
    assert card["title"] == "Mercado de departamentos en Concón"
    assert card["metrics"] == [
        {"label": "Precio publicado de referencia", "value": "83,6 UF/m²"},
        {"label": "Variación de precios publicados · 12 meses", "value": "2,2%"},
        {"label": "Propiedades actualmente en oferta", "value": "1.802"},
        {"label": "Competencia", "value": "Alta"},
        {"label": "Liquidez", "value": "Baja"},
    ]
    assert card["source_date"] == "29/04/2026"
    assert card["document_url"] == report_url
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one(".complementary-market")
    assert section.select_one("h2").get_text(" ", strip=True) == "Mercado de departamentos en Concón"
    assert len(section.select(".complementary-market__metric")) == 5
    assert section.select_one(".complementary-market__interpretation")
    assert section.select_one(".complementary-market__details summary").get_text(" ", strip=True) == "Ver más"
    assert "Corte 29/04/2026" in section.select_one(".complementary-market__source").get_text(" ", strip=True)
    row = section.select_one("a.complementary-market__document")
    assert row["href"] == report_url
    assert row.select_one(".complementary-market__document-title").get_text(strip=True) == "Informe de mercado comunal"
    assert "Corte 29/04/2026" in row.select_one(".complementary-market__document-meta").get_text(" ", strip=True)


def test_master_property_code_mismatch_does_not_supply_communal_identity():
    db = mongomock.MongoClient().test
    db["universo_cartera_prop360"].insert_one({
        "codigo": "5807", "ubicacion": {"comuna": "Concón"},
        "metadata": {"tipo_propiedad": "Departamento"},
    })
    db["mercado_comunal"].insert_one({
        "match_key": "concon|departamento",
        "comuna": "Concón", "tipo_propiedad": "Departamento",
        "mercado_venta": {"uf_m2_publicacion_actual": 83.58},
    })
    view = _view(
        db, code="5806",
        snapshot={"document_type": "COMMUNAL_MARKET_REPORT", "operation": "VENTA"},
        monthly={"period": "2026-10"},
        campaign_view={"document_available": True, "report_url": "/signed", "operation": "VENTA"},
    )
    assert view["market_reference_card"] is None


def test_no_exact_communal_match_hides_card_without_cross_property_communal_data():
    db = mongomock.MongoClient().test
    db["universo_cartera_prop360"].insert_one({
        "codigo": "5806", "ubicacion": {"comuna": "Concón"},
        "metadata": {"tipo_propiedad": "Departamento"},
    })
    db["mercado_comunal"].insert_one({
        "match_key": "santiago|departamento",
        "comuna": "Santiago", "tipo_propiedad": "Departamento",
        "mercado_venta": {"uf_m2_publicacion_actual": 55.23, "publicaciones_activas": 9215},
    })
    view = _view(
        db, code="5806",
        snapshot={"document_type": "COMMUNAL_MARKET_REPORT", "operation": "VENTA"},
        monthly={"period": "2026-10"},
        campaign_view={"document_available": True, "report_url": "/signed", "operation": "VENTA"},
    )
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    assert view["market_reference_card"] is None
    assert "55,2 UF/m²" not in html
    assert "9.215" not in html


def test_communal_match_key_normalizes_viña_del_mar_exactly_without_fuzzy_fallback():
    from owner_portal.monthly import _communal_market_card, _communal_match_key

    expected_key = "vina del mar|departamento"
    assert _communal_match_key("Viña del Mar", "Departamento") == expected_key
    assert _communal_match_key("Viña Del Mar", "Departamento") == expected_key
    assert _communal_match_key("  VIÑA   DEL MAR  ", " departamento ") == expected_key
    assert _communal_match_key("Viña del Mar Norte", "Departamento") != expected_key

    db = mongomock.MongoClient().test
    db["mercado_comunal"].insert_one({
        "match_key": expected_key,
        "comuna": "Viña Del Mar", "tipo_propiedad": "Departamento",
        "mercado_venta": {"uf_m2_publicacion_actual": 83.58},
    })
    matched = _communal_market_card(
        db, commune="Viña del Mar", property_type="Departamento",
        operation="VENTA", document_url="/signed",
    )
    assert matched is not None
    assert matched["metrics"][0]["value"] == "83,6 UF/m²"
    assert _communal_market_card(
        db, commune="Viña del Mar Norte", property_type="Departamento",
        operation="VENTA", document_url="/signed",
    ) is None
    assert _communal_market_card(
        db, commune="Valparaíso", property_type="Departamento",
        operation="VENTA", document_url="/signed",
    ) is None


def test_duplicate_communal_match_keys_fail_closed():
    from owner_portal.monthly import _communal_market_card

    db = mongomock.MongoClient().test
    record = {
        "match_key": "vina del mar|departamento",
        "comuna": "Viña Del Mar", "tipo_propiedad": "Departamento",
        "mercado_venta": {"uf_m2_publicacion_actual": 83.58},
    }
    db["mercado_comunal"].insert_many([record, dict(record)])
    assert _communal_market_card(
        db, commune="Viña del Mar", property_type="Departamento",
        operation="VENTA", document_url="/signed",
    ) is None


def test_appraisal_structured_values_render_with_existing_private_document():
    from bs4 import BeautifulSoup
    import os
    from unittest.mock import patch
    from campanas.owner_campaign_live_events import issue_live_token
    db = mongomock.MongoClient().test
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
        token = issue_live_token(
            campaign_id="owner_price_sucre_wave2_20260930", property_code="5438",
            action="ver_informe", recipient="owner@example.test",
            document_type="INDIVIDUAL_APPRAISAL",
            expires_at=int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp()),
            source="EMAIL", interaction_surface="OWNER_PORTAL", cta_placement="ORIGINAL",
        )
    appraisal_url = f"https://www.procasa.cl/campana/informe?token={token}"
    current = {
        "period": "2026-10", "current_price": 3990, "recommended_price": 3591,
        "recommendation": {"recommended_price": 3591, "recommended_adjustment_pct": 10},
        "individual_appraisal": {
            "verified": True, "property_code": "5438", "estimated_mid_uf": 3750,
            "estimated_low_uf": 3600, "estimated_high_uf": 3900,
            "position_vs_appraisal": "ABOVE_RANGE", "source_date": "2026-09-15",
            "methodology": "Análisis comercial individual", "area_basis": "USEFUL",
        },
        "documents": [{"type": "INDIVIDUAL_APPRAISAL", "verified": True, "url": appraisal_url}],
    }
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
        view = _view(db, snapshot={"document_type": "NONE"}, monthly=current, campaign_view={
            "document_available": True, "document_type": "INDIVIDUAL_APPRAISAL", "report_url": appraisal_url,
        })
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    soup = BeautifulSoup(html, "html.parser")
    assert view["market_reference_card"] is None
    assert view["appraisal_card"]["mode"] == "STRUCTURED"
    assert not ({"copy", "items", "keys", "values", "get", "update", "clear", "pop", "setdefault"} & set(view["appraisal_card"]))
    assert view["appraisal_card"]["document_url"] == appraisal_url
    assert view["appraisal_card"]["issued_label"] == ""
    assert view["appraisal_card"]["markers"] == []
    assert view["appraisal_card"]["range_band"] is None
    assert next(item for item in view["appraisal_card"]["metrics"] if item["label"] == "Diferencia frente a la tasación")["value"] == "+6,4%"
    assert {metric["label"] for metric in view["appraisal_card"]["metrics"]} == {
        "Valor de referencia", "Rango estimado", "Precio publicado", "Diferencia frente a la tasación",
    }
    assert len(view["appraisal_card"]["metrics"]) == 4
    assert any(item["label"] == "Precio recomendado PROCASA" for item in view["appraisal_card"]["details"])
    assert {"Rango inferior", "Valor central", "Rango superior", "Posición del precio publicado frente al rango", "Posición del precio recomendado frente a la tasación", "Fecha de tasación", "Metodología"}.issubset(
        {item["label"] for item in view["appraisal_card"]["details"]}
    )
    assert "por encima del rango" in view["appraisal_card"]["interpretation"]
    section = soup.select_one("[data-appraisal-mode='STRUCTURED']")
    assert section is not None
    assert "3.750 UF" in section.get_text(" ", strip=True)
    assert not any(term in html.casefold() for term in ("built-in method", "dict object", "<bound method"))
    document_row = section.select_one("a.appraisal-card__document")
    assert document_row["href"] == appraisal_url
    assert document_row.select_one(".appraisal-card__document-title").get_text(strip=True) == "Tasación individual"
    assert "Propiedad 5438 · PDF · Emitida 15-09-2026" in document_row.get_text(" ", strip=True)
    assert section.select_one(".appraisal-card__plot") is None
    view["market_reference_card"] = {"title": "Mercado comunal", "metrics": [], "details": []}
    ordered = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    assert ordered.index("id=\"appraisal-title\"") < ordered.index("id=\"complementary-market-title\"")


def _full_data_appraisal_view(*, comparable=True, appraisal=True, communal=True):
    """One coherent, local-only fixture for independent section composition QA."""
    import os
    from unittest.mock import patch
    from campanas.owner_campaign_live_events import issue_live_token

    db = mongomock.MongoClient().test
    code = "QA-FULL-5438"
    owner = "owner@example.test"
    campaign_id = "owner_price_sucre_wave2_20260930"
    secret = "local-full-data-composition-secret"
    expiry = int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp())

    def report_url(document_type):
        token = issue_live_token(
            campaign_id=campaign_id, property_code=code, action="ver_informe",
            recipient=owner, document_type=document_type, expires_at=expiry,
            source="EMAIL", interaction_surface="OWNER_PORTAL", cta_placement="ORIGINAL",
        )
        return f"https://www.procasa.cl/campana/informe?token={token}"

    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": secret}):
        appraisal_url = report_url("INDIVIDUAL_APPRAISAL") if appraisal else ""
        communal_url = report_url("COMMUNAL_MARKET_REPORT") if communal else ""
    current = {
        "period": "2026-10",
        "property": {
            "property_type": "Departamento", "commune": "Talca", "operation": "VENTA",
            "current_price": 3990, "surface_ref_m2": 100,
        },
        "activity_90d": {"leads": 1, "conversations": 1, "visits": 0, "source_date": "2026-10-02"},
        "recommendation": {
            "recommended_price": 3591, "recommended_adjustment_pct": 10,
            "text": "Recomendación de prueba basada en el caso QA coherente.",
            "diagnosis": "Diagnóstico de prueba basado en actividad y referencias verificadas.",
        },
        "documents": [],
    }
    if comparable:
        current["comparables"] = {
            "count": 7, "evidence_level": "HIGH", "version": "cluster_v2",
            "positioning_mode": "PRICE_M2", "reference_value": 35.0,
            "property_value": 39.9, "surface_ref_m2": 100,
            "unit": "UF/m² útil", "source_date": "2026-09-30",
        }
    if communal:
        current["communal_reference"] = {
            "offer_uf_m2": 37.0, "reference_unit": "UF/m² útil de oferta", "area_basis": "USEFUL",
            "universe_value": "1.250", "universe_unit": "publicaciones activas",
            "source_date": "2026-04-28",
        }
        db["mercado_comunal"].insert_one({
            "match_key": "talca|departamento", "comuna": "Talca",
            "tipo_propiedad": "Departamento",
            "mercado_venta": {
                "uf_m2_publicacion_actual": 37.0, "variacion_uf_m2_12m": -2.0,
                "publicaciones_activas": 1250, "publicaciones_totales": 2500,
            },
            "indicadores_mercado": {
                "nivel_competencia": "alto", "liquidez": "baja",
                "tendencia_mercado": "desaceleracion", "presion_baja_precio": "alta",
            },
            "rangos_precio_venta": {"min_uf": 1800, "max_uf": 5000},
            "source": {"fecha_reporte": "28/04/2026"},
        })
        current["documents"].append({
            "document_type": "COMMUNAL_MARKET_REPORT", "verified": True,
            "url": communal_url, "source_date": "2026-04-28",
        })
    if appraisal:
        current["individual_appraisal"] = {
            "verified": True, "property_code": code,
            "estimated_low_uf": 3600, "estimated_mid_uf": 3750,
            "estimated_high_uf": 3900, "source_date": "2026-09-15", "area_basis": "USEFUL",
            "appraisal_uf_m2": 37.5, "appraisal_uf_m2_unit": "UF/m² útil",
        }
        current["documents"].append({
            "document_type": "INDIVIDUAL_APPRAISAL", "verified": True,
            "url": appraisal_url, "source_date": "2026-09-15",
        })

    primary_type = "INDIVIDUAL_APPRAISAL" if appraisal else "COMMUNAL_MARKET_REPORT"
    primary_url = appraisal_url if appraisal else communal_url
    snapshot = {
        "document_type": primary_type if primary_url else "NONE",
        "commune": "Talca", "property_type": "Departamento", "operation": "VENTA",
        "current_price": 3990, "recommended_price": 3591,
        "recommended_adjustment_pct": 10,
    }
    campaign_view = {
        "document_available": bool(primary_url), "document_type": primary_type,
        "report_url": primary_url, "commune": "Talca",
        "property_type": "Departamento", "operation": "Venta",
        "logo_url": "/static/logo.png", "source": "EMAIL",
        "top_primary_url": "/accept", "sticky_primary_url": "/accept",
        "advisor_url": "https://www.procasa.cl/advisor",
    }
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": secret}):
        return _view(db, snapshot=snapshot, monthly=current, campaign_view=campaign_view, code=code)


def test_full_data_fixture_renders_comparables_appraisal_communal_and_following_sections():
    from bs4 import BeautifulSoup

    view = _full_data_appraisal_view()
    template = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    )
    soup = BeautifulSoup(template.render(view=view), "html.parser")

    assert view["position_simulation"]["available"] is True
    assert view["appraisal_card"]["mode"] == "STRUCTURED"
    assert view["market_position"]["market_evidence"] is view["market_evidence"]
    assert view["market_reference_card"]["title"] == "Mercado de departamentos en Talca"
    assert soup.select_one("#position-title")
    assert soup.select_one("#appraisal-title")
    assert soup.select_one("#complementary-market-title")
    assert soup.select_one("#diagnosis-title")
    assert soup.select_one("#recommendation-title")
    diagnosis = soup.select_one("#diagnosis-title").find_parent("section")
    assert diagnosis.select_one(".diagnosis-copy").get_text(" ", strip=True) == view["diagnosis"]
    assert diagnosis.select_one("details") is None
    assert diagnosis.select_one("summary") is None
    section_ids = [
        "class=\"hero", "class=\"actions-top", "id=\"summary-title\"",
        "id=\"activity-title\"", "id=\"appraisal-title\"",
        "id=\"complementary-market-title\"", "id=\"position-title\"", "id=\"diagnosis-title\"",
        "id=\"recommendation-title\"", "class=\"card executive-card\"",
    ]
    html = template.render(view=view)
    positions = [html.index(marker) for marker in section_ids]
    assert positions == sorted(positions)
    support_types = {row["type"] for row in view["support_documents"]}
    assert support_types == {"INDIVIDUAL_APPRAISAL", "COMMUNAL_MARKET_REPORT"}
    assert soup.select_one(".appraisal-card [data-document-type='INDIVIDUAL_APPRAISAL']")
    assert soup.select_one(".complementary-market [data-document-type='COMMUNAL_MARKET_REPORT']")
    assert not soup.select_one("#support-title")
    assert len(soup.select('[data-document-type="INDIVIDUAL_APPRAISAL"]')) == 1
    assert len(soup.select('[data-document-type="COMMUNAL_MARKET_REPORT"]')) == 1
    position = soup.select_one("[data-position-simulation]")
    assert [marker.get("data-point") for marker in position.select(".position-simulation__track [data-point]")] == [
        "appraisal", "comparable", "communal", "property_recommended", "property_current",
    ]
    assert position.select_one('[data-legend-kind="property_recommended"]')
    assert position.select_one('[data-legend-kind="property_current"]')
    assert [gap.get_text(strip=True) for gap in position.select('[data-legend-kind="property_current"] .position-simulation__property-gap')] == [
        "+6,4% vs tasación", "+14,0% vs similares", "+7,8% vs oferta comunal",
    ]
    assert [gap.get_text(strip=True) for gap in position.select('[data-legend-kind="property_recommended"] .position-simulation__property-gap')] == [
        "-4,2% vs tasación", "+2,6% vs similares", "-2,9% vs oferta comunal",
    ]
    assert position.select_one(".position-simulation__legend-marker--appraisal")
    assert position.select_one(".position-simulation__legend-marker--comparable")
    assert position.select_one(".position-simulation__legend-marker--communal")
    assert position.select_one(".position-simulation__legend-marker--property-current")
    assert position.select_one(".position-simulation__legend-marker--property-recommended")
    assert all(point.name == "span" for point in position.select(".position-simulation__point"))
    assert position.select_one('[data-point="property_current"]')
    assert position.select_one('[data-point="property_recommended"]')
    style = soup.find("style").get_text()
    assert ".position-simulation__point {" in style and "width:10px; height:10px" in style
    assert "border-radius:50%" in style
    assert "border:2px solid #fff" not in style
    assert ".position-simulation__point--appraisal { --marker-color:#c86c24; }" in style
    assert ".position-simulation__point--comparable { --marker-color:#6958cc; }" in style
    assert ".position-simulation__point--communal { --marker-color:#68758c; }" in style
    assert ".position-simulation__point--property-recommended { --marker-color:#21834d; z-index:2; }" in style
    assert ".position-simulation__point--property-current::before" in style and "inset:-3px" in style
    assert "border:2px solid #fff" not in style
    assert "@media (prefers-reduced-motion:reduce)" in style
    assert "7 propiedades similares" in soup.get_text(" ", strip=True)
    assert soup.select_one(".actions-top .button-primary").get_text(" ", strip=True) == "Revisar ajuste"
    assert soup.select_one(".sticky .button-primary").get_text(" ", strip=True) == "Revisar ajuste"
    assert not soup.select(".actions-top .button-primary svg, .sticky .button-primary svg")
    assert position.select_one(".position-simulation__sources")
    assert view["position_simulation"]["appraisal_x"] == view["market_position"]["references"][0]["x"]
    assert view["position_simulation"]["median_x"] is not None
    assert view["position_simulation"]["communal_x"] is not None


def test_appraisal_and_comparable_and_communal_sections_are_independent_in_all_combinations():
    from bs4 import BeautifulSoup

    template = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    )
    cases = [
        (True, True, True, (True, True, True)),
        (True, False, True, (True, False, True)),
        (True, True, False, (True, True, False)),
        (False, True, True, (False, True, True)),
        (False, False, True, (False, False, True)),
    ]
    for comparable, appraisal, communal, expected in cases:
        view = _full_data_appraisal_view(comparable=comparable, appraisal=appraisal, communal=communal)
        soup = BeautifulSoup(template.render(view=view), "html.parser")
        actual = (
            soup.select_one("#position-title") is not None,
            soup.select_one("#appraisal-title") is not None,
            soup.select_one("#complementary-market-title") is not None,
        )
        assert actual == expected
        assert actual[0] == comparable
        assert actual[1] == appraisal
        assert actual[2] == communal


def test_market_position_omits_appraisal_with_explicit_incompatible_area_basis():
    from owner_portal.monthly import _market_position_data

    common = {
        "position_simulation": {"available": False},
        "position_static": {
            "available": True, "unit": "UF/m² útil", "count": 7,
            "reference_value": 35.0, "property_value": 39.9,
        },
        "comparables": {"surface_ref_m2": 100, "source_date": "2026-09-30"},
        "communal": {"offer_uf_m2": 37.0, "reference_unit": "UF/m² de oferta", "area_basis": "USEFUL", "source_date": "2026-04-28"},
        "property_state": {"surface_ref_m2": 100}, "property_code": "5438",
        "current_price": 3990, "recommended_price": 3591,
        "recommendation_is_monthly": True, "stale": False,
    }
    appraisal = {"market_position_reference": {
        "verified": True, "property_code": "5438", "appraisal_value_uf": 3750,
        "appraisal_uf_m2": 32.0, "appraisal_uf_m2_unit": "UF/m² construido", "area_basis": "BUILT",
    }}
    result = _market_position_data(**common, appraisal_card=appraisal)
    assert result["available"] is True
    assert [reference["kind"] for reference in result["references"]] == ["COMPARABLE", "COMMUNAL"]

    # A value without its own verified appraisal surface is not derived from
    # the property's/comparable group's denominator.
    appraisal["market_position_reference"]["appraisal_uf_m2"] = None
    appraisal["market_position_reference"]["appraisal_uf_m2_unit"] = "UF/m²"
    appraisal["market_position_reference"]["area_basis"] = "USEFUL"
    derived = _market_position_data(**common, appraisal_card=appraisal)
    assert [reference["kind"] for reference in derived["references"]] == ["COMPARABLE", "COMMUNAL"]
    assert derived["appraisal_exclusion_reason"] == "NO_STRUCTURED_VALUE"

    # A same-currency communal marker with unknown area basis is not plotted.
    unknown_area = {**common, "communal": {
        "offer_uf_m2": 37, "reference_unit": "UF/m² de oferta",
    }}
    excluded = _market_position_data(**unknown_area, appraisal_card={})
    assert all(reference["kind"] != "COMMUNAL" for reference in excluded["references"])

    # Communal market remains a separate card, but its marker is omitted when
    # its scale differs or the established compatibility flag rejects it.
    incompatible_communal = {**common, "communal": {
        "offer_uf_m2": 37, "reference_unit": "CLP/m² de oferta",
    }}
    no_commun = _market_position_data(**incompatible_communal, appraisal_card={})
    assert all(reference["kind"] != "COMMUNAL" for reference in no_commun["references"])

    explicit_reject = {**common, "position_simulation": {"available": False, "communal_on_same_scale": False}}
    rejected = _market_position_data(**explicit_reject, appraisal_card={})
    assert all(reference["kind"] != "COMMUNAL" for reference in rejected["references"])


def test_market_position_bar_always_keeps_current_and_recommended_markers():
    from owner_portal.monthly import _market_position_data

    result = _market_position_data(
        position_simulation={"available": False},
        position_static={
            "available": True, "unit": "UF/m² útil", "count": 13,
            "reference_value": 91.1, "property_value": 132.2,
        },
        comparables={"surface_ref_m2": 140}, communal={}, appraisal_card=None,
        property_state={"operation": "VENTA", "surface_ref_m2": 140, "surface_ref_basis": "USEFUL"},
        property_code="5695", current_price=18507, recommended_price=16656.3,
        recommendation_is_monthly=True, stale=False,
    )

    assert [marker["kind"] for marker in result["bar_markers"]] == [
        "COMPARABLE", "PROPERTY_RECOMMENDED", "PROPERTY_CURRENT",
    ]
    assert result["property"]["current_uf_m2"] == 18507 / 140
    assert result["property"]["recommended_uf_m2"] == 16656.3 / 140
    assert result["bar_markers"][1]["value_label"] == "119,0"
    assert result["bar_markers"][2]["value_label"] == "132,2"


def test_market_position_full_data_has_exact_five_markers_in_fixed_order():
    from owner_portal.monthly import _market_position_data

    result = _market_position_data(
        position_simulation={"available": False},
        position_static={
            "available": True, "unit": "UF/m² útil", "count": 13,
            "reference_value": 91.1, "property_value": 132.2,
        },
        comparables={"surface_ref_m2": 140},
        communal={
            "offer_uf_m2": 102.8, "reference_unit": "UF/m² útil",
            "area_basis": "USEFUL", "measurement_definition": "UF_PER_M2",
        },
        appraisal_card={"mode": "STRUCTURED", "market_position_reference": {
            "verified": True, "property_code": "5695", "appraisal_value_uf": 13300,
            "appraisal_uf_m2": 95.0, "appraisal_uf_m2_unit": "UF/m² útil",
            "area_basis": "USEFUL", "measurement_definition": "UF_PER_M2",
        }},
        property_state={"operation": "VENTA", "surface_ref_m2": 140, "surface_ref_basis": "USEFUL"},
        property_code="5695", current_price=18507, recommended_price=16656.3,
        recommendation_is_monthly=True, stale=False,
    )

    assert [marker["kind"] for marker in result["bar_markers"]] == [
        "APPRAISAL", "COMPARABLE", "COMMUNAL", "PROPERTY_RECOMMENDED", "PROPERTY_CURRENT",
    ]
    assert [marker["value_label"] for marker in result["bar_markers"]] == [
        "95,0", "91,1", "102,8", "119,0", "132,2",
    ]
    assert result["appraisal_on_scale"] is True
    assert result["communal_on_scale"] is True


def test_market_position_final_copy_and_full_scale_evidence_are_not_repeated():
    from bs4 import BeautifulSoup

    view = _full_data_appraisal_view()
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one(".position-simulation")

    assert section.select_one("[data-position-summary]").get_text(" ", strip=True) == (
        "Actualmente, tu propiedad se publica en 39,9 UF/m² útil: 6,4% sobre la tasación, "
        "14,0% sobre propiedades similares y 7,8% sobre la oferta comunal."
    )
    assert "Referencias de mercado" not in section.get_text(" ", strip=True)
    assert [item.get("data-legend-kind") for item in section.select(".position-simulation__legend-item")] == [
        "appraisal", "comparable", "communal", "property_recommended", "property_current",
    ]
    assert "repeat(5,minmax(0,1fr))" in html
    assert "grid-column:2 / span 2" in html and "grid-column:4 / span 2" in html


def test_market_position_adjusted_copy_uses_owner_friendly_communal_label():
    view = _full_data_appraisal_view()
    assert view["market_position"]["adjusted_copy"] == (
        "Con el precio recomendado de 35,9 UF/m² útil, la propiedad quedaría "
        "4,2% bajo la tasación, 2,6% sobre propiedades similares y 2,9% bajo la oferta comunal."
    )


def test_owner_appraisal_analysis_log_is_compact_and_contains_no_document_or_owner_data(caplog):
    import logging
    from owner_portal.monthly import _log_owner_appraisal_analysis

    caplog.set_level(logging.INFO, logger="owner_portal.monthly")
    _log_owner_appraisal_analysis("5695", "FOUND", {
        "extraction_status": "STRUCTURED", "text_extracted": True, "page_count": 4,
        "appraisal_value": 3750, "surface_m2": 100, "area_basis": "USEFUL",
        "appraisal_uf_m2_source": "DERIVED_FROM_VERIFIED_VALUE_AND_SURFACE",
        "owner_email": "private@example.com", "token": "secret-token",
        "pdf_text": "private PDF contents", "source_file_id": "drive-file-id",
    })
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert messages[0].startswith("[OWNER_APPRAISAL_ANALYSIS] property_code=5695 ")
    assert "appraisal_resolver_status=FOUND" in messages[0]
    assert "appraisal_extraction_status=STRUCTURED" in messages[0]
    assert "page_count=4" in messages[0]
    assert "has_appraisal_value=True" in messages[0]
    assert "area_basis=USEFUL" in messages[0]
    assert "has_derived_uf_m2=True" in messages[0]
    assert all(secret not in messages[0] for secret in (
        "private@example.com", "secret-token", "private PDF contents", "drive-file-id",
    ))

    caplog.clear()
    _log_owner_appraisal_analysis("5806", "FOUND", {})
    assert not caplog.records


def test_market_position_compatibility_requires_currency_area_and_measurement_match():
    from owner_portal.monthly import are_market_position_scales_compatible

    comparable = {
        "unit": "UF/m² útil", "currency_basis": "UF", "area_basis": "USEFUL",
        "measurement_definition": "UF_PER_M2",
    }
    assert are_market_position_scales_compatible(comparable, dict(comparable)) is True
    assert are_market_position_scales_compatible(comparable, {**comparable, "area_basis": "BUILT"}) is False
    assert are_market_position_scales_compatible(comparable, {**comparable, "currency_basis": "CLP"}) is False
    assert are_market_position_scales_compatible(comparable, {**comparable, "measurement_definition": "UF_PER_M2_SALE_EFFECTIVE"}) is False
    assert are_market_position_scales_compatible(
        comparable, {**comparable, "unit": "UF/m²", "area_basis": ""}
    ) is False


def test_market_evidence_summary_counts_document_only_without_treating_it_as_quantitative():
    from owner_portal.monthly import _market_evidence_model, _market_position_data

    position = _market_position_data(
        position_simulation={"available": False},
        position_static={
            "available": True, "unit": "UF/m² útil", "count": 13,
            "reference_value": 91.1, "property_value": 132.2,
        },
        comparables={"surface_ref_m2": 140},
        communal={"offer_uf_m2": 102.76, "reference_unit": "UF/m²"},
        appraisal_card={"mode": "DOCUMENT_ONLY", "market_position_reference": {}},
        property_state={"surface_ref_m2": 140, "operation": "VENTA"},
        property_code="5695", current_price=18507, recommended_price=16656.3,
        recommendation_is_monthly=True, stale=False,
    )
    evidence = _market_evidence_model(
        market_position=position,
        position_static={"available": True, "unit": "UF/m² útil", "count": 13, "reference_value": 91.1},
        appraisal_card={"mode": "DOCUMENT_ONLY", "market_position_reference": {}},
        communal={"offer_uf_m2": 102.76, "reference_unit": "UF/m²"}, stale=False,
    )

    assert evidence["evidence_source_count"] == 3
    assert evidence["quantitative_reference_count"] == 2
    assert evidence["scale_compatible_count"] == 1
    assert evidence["summary_position_label"] == "3 fuentes disponibles"
    appraisal_source = next(item for item in evidence["sources"] if item["kind"] == "APPRAISAL")
    communal_source = next(item for item in evidence["sources"] if item["kind"] == "COMMUNAL")
    assert appraisal_source["quantitative_value"] is None
    assert communal_source["value"] == 102.76
    assert communal_source["scale_compatible"] is False
    assert [item["kind"] for item in position["bar_markers"]] == [
        "COMPARABLE", "PROPERTY_RECOMMENDED", "PROPERTY_CURRENT",
    ]


def test_appraisal_only_scale_requires_exact_typed_property_surface():
    from owner_portal.monthly import _market_position_data

    result = _market_position_data(
        position_simulation={"available": False}, position_static={"available": False},
        comparables={}, communal={},
        appraisal_card={"market_position_reference": {
            "verified": True, "property_code": "5438", "appraisal_value_uf": 3750,
            "appraisal_uf_m2": 37.5, "appraisal_uf_m2_unit": "UF/m² útil", "area_basis": "USEFUL",
        }},
        property_state={"surface_ref_m2": 100, "surface_ref_unit": "UF/m² útil", "surface_ref_basis": "useful"},
        property_code="5438", current_price=3990, recommended_price=3591,
        recommendation_is_monthly=True, stale=False,
    )
    assert result["available"] is True
    assert [reference["kind"] for reference in result["references"]] == ["APPRAISAL"]
    assert result["simulation_available"] is True
    assert result["current_x"] != result["proposed_x"]


def _verified_appraisal_fixture(db, *, appraisal=None, docs=True, snapshot=None, campaign_view=None,
                                property_code="5438", campaign_id="owner_price_sucre_wave2_20260930"):
    import os
    from unittest.mock import patch
    from campanas.owner_campaign_live_events import issue_live_token
    document_url = ""
    monthly_docs = []
    if docs:
        with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
            token = issue_live_token(
                campaign_id=campaign_id, property_code=property_code,
                action="ver_informe", recipient="owner@example.test",
                document_type="INDIVIDUAL_APPRAISAL",
                expires_at=int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp()),
                source="EMAIL", interaction_surface="OWNER_PORTAL", cta_placement="ORIGINAL",
            )
        document_url = f"https://www.procasa.cl/campana/informe?token={token}"
        monthly_docs = [{"type": "INDIVIDUAL_APPRAISAL", "verified": True, "url": document_url, "source_date": "2026-09-15"}]
    current = {"period": "2026-10", "current_price": 3990, "documents": monthly_docs, **(snapshot or {})}
    if appraisal is not None:
        current["individual_appraisal"] = appraisal
    campaign = {"document_available": bool(docs), "document_type": "INDIVIDUAL_APPRAISAL", "report_url": document_url, **(campaign_view or {})}
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
        view = _view(
            db, code=property_code, campaign_id=campaign_id,
            snapshot={"document_type": "NONE"}, monthly=current, campaign_view=campaign,
        )
    return view, document_url


def test_appraisal_document_only_and_no_appraisal_states_render_correctly():
    from bs4 import BeautifulSoup
    db = mongomock.MongoClient().test
    document_only, document_url = _verified_appraisal_fixture(db)
    html = Environment(loader=FileSystemLoader("templates")).get_template("owner_campaign_monthly_portal.html").render(view=document_only)
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one("[data-appraisal-mode='DOCUMENT_ONLY']")
    assert section is not None and section.select_one("a.appraisal-card__document")["href"] == document_url
    assert "PDF" in section.select_one(".appraisal-card__document-meta").get_text(" ", strip=True)
    assert "Propiedad 5438" in section.select_one(".appraisal-card__document-meta").get_text(" ", strip=True)
    assert "Emitida 15-09-2026" in section.select_one(".appraisal-card__document-meta").get_text(" ", strip=True)
    assert not section.select_one(".appraisal-card__metrics")
    assert "No hay tasación" not in section.get_text(" ", strip=True)
    assert section.select_one(".appraisal-card__copy").get_text(" ", strip=True) == document_only["appraisal_card"]["body_copy"]
    assert "Esta propiedad cuenta con una tasación individual." in section.get_text(" ", strip=True)
    assert not any(term in html.casefold() for term in ("built-in method", "dict object", "<bound method"))

    no_appraisal, _ = _verified_appraisal_fixture(mongomock.MongoClient().test, docs=False)
    no_html = Environment(loader=FileSystemLoader("templates")).get_template("owner_campaign_monthly_portal.html").render(view=no_appraisal)
    assert no_appraisal["appraisal_card"] is None
    assert "Tasación individual de tu propiedad" not in no_html


def test_property_5695_document_only_appraisal_renders_safely_without_portal_request():
    from bs4 import BeautifulSoup

    view, _ = _verified_appraisal_fixture(
        mongomock.MongoClient().test,
        property_code="5695",
        campaign_id="owner_price_sucre_wave1_20260928",
        snapshot={"individual_appraisal": None},
    )
    card = view["appraisal_card"]
    assert view["property_code"] == "5695"
    assert card["mode"] == "DOCUMENT_ONLY"
    assert card["body_copy"] == (
        "Esta propiedad cuenta con una tasación individual. Revisa el documento completo "
        "para consultar su valor de referencia y los antecedentes utilizados en su elaboración."
    )
    assert not ({"copy", "items", "keys", "values", "get", "update", "clear", "pop", "setdefault"} & set(card))

    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one("[data-appraisal-mode='DOCUMENT_ONLY']")
    assert section is not None
    assert "Tasación individual disponible" in section.get_text(" ", strip=True)
    assert card["body_copy"] in section.get_text(" ", strip=True)
    assert section.select_one("a.appraisal-card__document") is not None
    assert not any(term in html.casefold() for term in ("built-in method", "dict object", "<bound method"))


def test_exact_resolver_result_is_reused_for_one_private_appraisal_link(monkeypatch):
    import os
    from unittest.mock import patch
    from campanas.owner_campaign_live_events import decode_live_token

    db = mongomock.MongoClient().test
    owner = "owner@example.test"
    code = "5438"
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    key = owner_property_portal_id(code, owner)
    monthly = {"period": "2026-10", "current_price": 3990, "documents": []}
    db["owner_property_portals"].insert_one({
        "_id": key, "owner_key": key, "property_code": code,
        "current_portal_state": monthly, "monthly_snapshots": [monthly],
    })
    row = {
        "campaign_id": "owner_price_sucre_wave2_20260930", "property_code": code,
        "owner_email": owner, "send_status": "SENT",
        "portal_access": {"expires_at": expiry},
        "campaign_snapshot": {"owner_email": owner, "document_type": "COMMUNAL_MARKET_REPORT"},
    }
    campaign = {
        "source": "WHATSAPP", "safe_mode": False, "document_available": False,
        "advisor_url": "https://procasa-chatbot-yr8d.onrender.com/advisor",
        "top_advisor_url": "https://procasa-chatbot-yr8d.onrender.com/advisor",
    }
    resolver = {"status": "FOUND", "document": {
        "id": "private-drive-id", "name": f"{code}.pdf", "modifiedTime": "2026-09-15T18:30:00Z",
    }}
    def resolve_exact(requested_code):
        assert requested_code == code
        return resolver

    monkeypatch.setattr("campanas.private_report.resolve_appraisal_document_cached", resolve_exact)
    monkeypatch.setattr("campanas.private_report.resolve_appraisal_analysis_cached", lambda requested_code, record: {
        "property_code": requested_code, "source_file_id": record["id"],
        "source_file_modified_at": record["modifiedTime"],
        "verified": True, "estimated_mid_uf": 3750, "appraisal_value": 3750,
        "extraction_status": "STRUCTURED", "extraction_confidence": "HIGH",
        "text_extracted": True, "field_sources": {"estimated_mid_uf": "Valor de tasación"},
    })
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
        view = build_monthly_portal_view(db, row, campaign)

    assert view["appraisal_card"]["mode"] == "STRUCTURED"
    rendered = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(rendered, "html.parser")
    appraisal_section = soup.select_one("[data-appraisal-mode='STRUCTURED']")
    assert appraisal_section is not None
    assert appraisal_section.select_one(".appraisal-card__heading").get_text(strip=True) == "Tasación individual de tu propiedad"
    appraisal_doc = next(item for item in view["support_documents"] if item["type"] == "INDIVIDUAL_APPRAISAL")
    assert view["appraisal_card"]["document_url"] == appraisal_doc["url"]
    assert appraisal_doc["url"].startswith("https://procasa-chatbot-yr8d.onrender.com/campana/informe?")
    assert "private-drive-id" not in appraisal_doc["url"]
    assert appraisal_doc["metadata"] == "Propiedad 5438 · PDF · Archivo actualizado 15/09/2026"
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
        claims = decode_live_token(appraisal_doc["url"].split("token=", 1)[1])
    assert claims["document_type"] == "INDIVIDUAL_APPRAISAL"
    assert claims["property_code"] == code and claims["recipient"] == owner
    assert claims["action"] == "ver_informe"
    assert appraisal_section.select_one("a.appraisal-card__document")["href"] == appraisal_doc["url"]
    assert not soup.select_one("#support-title")
    assert len(soup.select('[data-document-type="INDIVIDUAL_APPRAISAL"]')) == 1


@pytest.mark.parametrize(
    ("pdf_text", "expected_mid", "expected_range", "expected_date", "expected_method"),
    [
        (
            "Valor de tasación: 2.050 UF\nUF/m²: 82,5 UF\nFecha de tasación: 15/09/2026\nMetodología: Comparación directa",
            2050, False, "15/09/2026", "Comparación directa",
        ),
        ("Valor comercial\n3.900 UF", 3900, False, "", ""),
        ("Rango de tasación: 3.600 UF - 4.100 UF", None, True, "", ""),
    ],
)
def test_pdf_appraisal_parser_uses_only_explicit_labels(pdf_text, expected_mid, expected_range, expected_date, expected_method):
    from campanas.private_report import _extract_appraisal_fields
    parsed = _extract_appraisal_fields(
        pdf_text, property_code="5994", file_record={"id": "qa-pdf-id", "modifiedTime": "2026-09-20T00:00:00Z"},
    )
    assert parsed["property_code"] == "5994"
    assert parsed.get("estimated_mid_uf") == expected_mid
    assert ("estimated_low_uf" in parsed and "estimated_high_uf" in parsed) is expected_range
    assert parsed.get("appraisal_date", "") == expected_date
    assert parsed.get("methodology", "") == expected_method
    assert parsed["extraction_status"] == ("STRUCTURED" if expected_mid else "DOCUMENT_ONLY")
    assert parsed["extraction_confidence"] == ("HIGH" if expected_mid else "LOW")


def test_pdf_appraisal_parser_does_not_invent_midpoint_or_range():
    from campanas.private_report import _extract_appraisal_fields
    parsed = _extract_appraisal_fields(
        "Precio publicado: 3.990 UF\nSuperficie útil: 88 m2",
        property_code="5994", file_record={"id": "qa-pdf-id"},
    )
    assert "estimated_mid_uf" not in parsed
    assert "estimated_low_uf" not in parsed
    assert "estimated_high_uf" not in parsed
    assert parsed["extraction_status"] == "DOCUMENT_ONLY"


@pytest.mark.parametrize(
    ("pdf_text", "basis", "surface", "explicit_m2"),
    [
        ("Valor de tasación: 3.900 UF\nSuperficie útil: 78 m²", "USEFUL", 78.0, None),
        ("Valor comercial 4.000 UF\n80 m² construidos", "BUILT", 80.0, None),
        ("Valor comercial 8.000 UF\nTerreno: 200 m2", "LAND", 200.0, None),
        ("UF/m²: 75,4", None, None, 75.4),
        ("UF/m2 75.4", None, None, 75.4),
        ("Valor por m²: 75,4 UF", None, None, 75.4),
        ("75.4 UF/m2", None, None, 75.4),
    ],
)
def test_pdf_appraisal_parser_extracts_explicit_surface_basis_and_uf_m2(pdf_text, basis, surface, explicit_m2):
    from campanas.private_report import _extract_appraisal_fields
    parsed = _extract_appraisal_fields(pdf_text, property_code="5695", file_record={"id": "verified-pdf"})
    assert parsed.get("area_basis") == basis
    assert parsed.get("surface_m2") == surface
    expected_value = explicit_m2
    if expected_value is None and basis:
        expected_value = parsed["estimated_mid_uf"] / surface
    assert parsed.get("appraisal_uf_m2") == expected_value
    if explicit_m2 is not None:
        assert parsed["appraisal_uf_m2_source"] == "EXPLICIT"
    elif basis:
        assert parsed["appraisal_uf_m2_source"] == "DERIVED_FROM_VERIFIED_VALUE_AND_SURFACE"


def test_pdf_appraisal_parser_derives_uf_m2_only_from_same_document_value_and_typed_surface():
    from campanas.private_report import _extract_appraisal_fields
    parsed = _extract_appraisal_fields(
        "Valor de tasación: 3.900 UF\nSuperficie útil: 78 m²",
        property_code="5695", file_record={"id": "verified-pdf"},
    )
    assert parsed["appraisal_uf_m2"] == 50
    assert parsed["appraisal_uf_m2_source"] == "DERIVED_FROM_VERIFIED_VALUE_AND_SURFACE"
    no_basis = _extract_appraisal_fields(
        "Valor de tasación: 3.900 UF\nSuperficie total: 78 m²",
        property_code="5695", file_record={"id": "verified-pdf"},
    )
    assert "appraisal_uf_m2" not in no_basis


@pytest.mark.parametrize("status", ["NOT_FOUND", "AMBIGUOUS", "ERROR"])
def test_real_resolver_fail_closed_keeps_monthly_portal_available(monkeypatch, status):
    from unittest.mock import patch
    db = mongomock.MongoClient().test
    monkeypatch.setattr(
        "campanas.private_report.resolve_appraisal_document_cached",
        lambda _code: {"status": status, "document": None},
    )
    row = {
        "campaign_id": "owner_price_sucre_wave2_20260930", "property_code": "5438",
        "owner_email": "owner@example.test", "send_status": "SENT",
        "portal_access": {"expires_at": datetime.now(timezone.utc) + timedelta(days=30)},
        "campaign_snapshot": {"owner_email": "owner@example.test"},
    }
    with patch.dict("os.environ", {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
        view = build_monthly_portal_view(db, row, {
            "safe_mode": False, "document_available": False,
            "advisor_url": "https://procasa-chatbot-yr8d.onrender.com/advisor",
        })
    assert view["appraisal_card"] is None
    assert view["support_documents"] == []


def test_appraisal_mid_only_has_no_artificial_range_and_ignores_generated_date():
    view, _ = _verified_appraisal_fixture(
        mongomock.MongoClient().test,
        appraisal={"verified": True, "property_code": "5438", "estimated_mid_uf": 3750},
        snapshot={"generated_at": datetime(2026, 10, 5, tzinfo=timezone.utc)},
    )
    card = view["appraisal_card"]
    assert card["mode"] == "STRUCTURED"
    assert not any(item["label"] == "Rango estimado" for item in card["metrics"])
    assert not card["issued_label"]


def test_wrong_property_appraisal_data_is_rejected_and_duplicate_pdf_fails_closed():
    from unittest.mock import patch
    import os
    from campanas.owner_campaign_live_events import issue_live_token
    db = mongomock.MongoClient().test
    wrong, _ = _verified_appraisal_fixture(db, appraisal={
        "verified": True, "property_code": "9999", "estimated_mid_uf": 2500,
        "estimated_low_uf": 2400, "estimated_high_uf": 2600,
    })
    assert wrong["appraisal_card"]["mode"] == "DOCUMENT_ONLY"
    assert "2.500 UF" not in str(wrong["appraisal_card"])

    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
        second = issue_live_token(
            campaign_id="owner_price_sucre_wave2_20260930", property_code="5438",
            action="ver_informe", recipient="owner@example.test", document_type="INDIVIDUAL_APPRAISAL",
            expires_at=int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp()),
            source="EMAIL", interaction_surface="OWNER_PORTAL", cta_placement="ORIGINAL",
        )
    second_url = f"https://www.procasa.cl/campana/informe?token={second}"
    ambiguous = {"period": "2026-10", "current_price": 3990, "individual_appraisal": {
        "verified": True, "property_code": "5438", "estimated_mid_uf": 2500,
    }, "documents": [
        {"type": "INDIVIDUAL_APPRAISAL", "verified": True, "url": wrong["appraisal_card"]["document_url"]},
        {"type": "INDIVIDUAL_APPRAISAL", "verified": True, "url": second_url},
    ]}
    with patch.dict(os.environ, {"OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET": "local-appraisal-test-secret"}):
        ambiguous_view = _view(mongomock.MongoClient().test, monthly=ambiguous, snapshot={"document_type": "NONE"}, campaign_view={
            "document_available": False, "report_url": "",
        })
    assert ambiguous_view["appraisal_card"] is None


def test_appraisal_visibility_is_independent_of_authorization_and_range_position_is_recomputed():
    authorized, _ = _verified_appraisal_fixture(
        mongomock.MongoClient().test,
        appraisal={"verified": True, "property_code": "5438", "estimated_mid_uf": 3750,
                   "estimated_low_uf": 3600, "estimated_high_uf": 3900, "position_vs_appraisal": "BELOW_RANGE"},
        campaign_view={"already_authorized": True, "top_primary_url": ""},
    )
    assert authorized["appraisal_card"]["mode"] == "STRUCTURED"
    assert authorized["can_authorize"] is False
    assert authorized["appraisal_card"]["conflict"] is False
    assert any(
        item["label"] == "Posición del precio publicado frente al rango"
        and item["value"] == "Por encima del rango"
        for item in authorized["appraisal_card"]["details"]
    )
    assert "por encima del rango" in authorized["appraisal_card"]["interpretation"]


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


def test_missing_verified_update_date_is_omitted_instead_of_fabricated():
    view = premium_fixture("full")
    view["updated_label"] = ""
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    assert "Seguimiento comercial actualizado al" not in html
    assert "Fecha no disponible" not in html


def test_summary_has_four_cards_and_reuses_canonical_activity_and_position():
    from bs4 import BeautifulSoup
    db = mongomock.MongoClient().test
    current = {
        "period": "2026-10",
        "property": {
            "property_type": "Departamento", "commune": "Santiago", "operation": "VENTA",
            "current_price": 18_507, "surface_ref_m2": 140,
        },
        "activity_90d": {"leads": 1, "conversations": 0, "visits": 0, "source_date": "2026-10-02"},
        "comparables": {
            "count": 13, "evidence_level": "HIGH", "version": "cluster_v2",
            "positioning_mode": "PRICE_M2", "reference_value": 91.1,
            "property_value": 132.2, "surface_ref_m2": 140, "unit": "UF/m² útil",
            "source_date": "2026-09-30", "analysis_generated_at": "2026-09-30",
        },
        "communal_reference": {
            "offer_uf_m2": 55.2, "reference_unit": "UF/m² de oferta",
            "universe_value": "9.215", "universe_unit": "propiedades actualmente en oferta",
            "source_date": "2026-04-28",
        },
        "recommendation": {
            "recommended_price": 16_656, "recommended_adjustment_pct": 10,
            "diagnosis": "Las consultas no se han convertido en visitas coordinadas.",
            "text": "Proponemos evaluar el posicionamiento de precio.",
        },
        "documents": [{"type": "COMMUNAL_MARKET_REPORT", "verified": True}],
    }
    db["mercado_comunal"].insert_one({
        "match_key": "santiago|departamento",
        "comuna": "Santiago", "tipo_propiedad": "Departamento",
        "mercado_venta": {
            "uf_m2_publicacion_actual": 55.23, "variacion_uf_m2_12m": -9.98,
            "publicaciones_activas": 9_215, "publicaciones_totales": 68_325,
        },
        "indicadores_mercado": {
            "liquidez": "baja", "nivel_competencia": "alto",
            "tendencia_mercado": "desaceleracion", "presion_baja_precio": "alta",
        },
        "rangos_precio_venta": {"min_uf": 1_288, "max_uf": 3_799},
        "source": {"fecha_reporte": "28/04/2026"},
    })
    report_url = "/campana/informe?token=coherent-qa-report"
    view = _view(
        db,
        snapshot={
            "document_type": "COMMUNAL_MARKET_REPORT", "commune": "Santiago",
            "property_type": "Departamento", "operation": "VENTA",
            "current_price": 18_507, "recommended_price": 16_656,
            "recommended_adjustment_pct": 10,
        },
        monthly=current,
        campaign_view={
            "document_available": True, "document_type": "COMMUNAL_MARKET_REPORT",
            "report_url": report_url, "commune": "Santiago",
            "property_type": "Departamento", "operation": "Venta",
        },
    )

    position = view["position_simulation"]
    market = view["market_reference_card"]
    assert view["commune"] == market["source_label"].split(" · ")[1] == "Santiago"
    assert view["property_type"] == "Departamento" and "departamentos" in market["title"]
    assert view["current_price_label"] == "18.507 UF"
    assert view["recommended_price_label"] == "16.656 UF"
    assert position["surface_ref_m2"] == 140
    assert abs(position["current_m2"] - 18_507 / 140) < 0.02
    assert abs(position["proposed_m2"] - 16_656 / 140) < 0.02
    assert position["median"] == 91.1 and position["communal_value"] == 55.23
    assert view["market_position"]["communal_available"] is True
    assert view["market_position"]["communal_on_scale"] is False
    assert view["market_position"]["communal_exclusion_reason"] == "AREA_BASIS_UNKNOWN"
    assert market["metrics"][0]["value"] == "55,2 UF/m²"
    assert market["metrics"][1]["value"] == "-10,0%"
    assert market["metrics"][2]["value"] == "9.215"
    assert view["activity_90d"]["leads"] == 1

    template = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    )
    soup = BeautifulSoup(template.render(view=view), "html.parser")
    summary = soup.select_one("#summary-title").find_parent("section")
    cards = summary.select(":scope > .kpis > .kpi")
    assert len(cards) == 4
    assert [card.select_one(".kpi-label").get_text(" ", strip=True) for card in cards] == [
        "Precio actual", "Precio sugerido", "Consultas recibidas", "Posición de mercado",
    ]
    activity_section = soup.select_one("#activity-title").find_parent("section")
    summary_leads = cards[2].select_one(".kpi-value").get_text(strip=True)
    activity_leads = activity_section.select_one(".owner-funnel-stage-label strong").get_text(strip=True)
    assert summary_leads == activity_leads == str(view["activity_90d"]["leads"])
    assert cards[1].select_one(".kpi-note").get_text(" ", strip=True) == "-10% recomendado"
    assert cards[3].select_one(".kpi-label").get_text(strip=True) == "Posición de mercado"
    assert cards[3].select_one(".kpi-value").get_text(strip=True) == "2 fuentes disponibles"
    assert cards[3].select_one(".market-evidence-summary").get_text(" ", strip=True) == (
        "2 referencias con valor · 1 referencia directamente en la misma escala · Similares · Oferta comunal"
    )
    assert view["market_reference_card"]["source_label"].startswith("Informe comunal · Santiago · Departamento")

    missing_db = mongomock.MongoClient().test
    missing_current = {**current, "property": {**current["property"], "commune": "Talca"}}
    missing_current.pop("communal_reference")
    missing = _view(
        missing_db,
        snapshot={"document_type": "COMMUNAL_MARKET_REPORT", "commune": "Talca", "property_type": "Departamento", "operation": "VENTA"},
        monthly=missing_current,
        campaign_view={"document_available": True, "report_url": report_url, "commune": "Talca", "property_type": "Departamento", "operation": "Venta"},
    )
    assert missing["market_reference_card"] is None
    missing_html = template.render(view=missing)
    assert "Mercado de departamentos en Talca" not in missing_html
    assert "55,2 UF/m²" not in missing_html


def test_summary_keeps_unknown_leads_unavailable_instead_of_zero():
    from bs4 import BeautifulSoup
    view = _view(mongomock.MongoClient().test, monthly={"period": "2026-10"})
    soup = BeautifulSoup(Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view), "html.parser")
    summary = soup.select_one("#summary-title").find_parent("section")
    cards = summary.select(":scope > .kpis > .kpi")
    assert len(cards) == 4
    leads = next(card for card in cards if card.select_one(".kpi-label").get_text(strip=True) == "Consultas recibidas")
    assert leads.select_one(".kpi-value").get_text(strip=True) == "—"
    assert leads.select_one(".kpi-note").get_text(strip=True) == "Sin dato consolidado"
    assert "Consultas recibidas —" in leads.get_text(" ", strip=True)


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
        stale=False,
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
        "stale": False,
    }
    assert _position_simulation_data(**args)["available"] is True
    assert _position_simulation_data(**{**args, "comparables": {**base, "evidence_level": "LIMITED"}})["available"] is False
    assert _position_simulation_data(**{**args, "property_state": {**args["property_state"], "surface_ref_m2": 150}})["reason"] == "POSITIONING_SURFACE_DENOMINATOR_MISMATCH"
    assert _position_simulation_data(**{**args, "recommendation_is_monthly": False})["available"] is False
    assert _position_simulation_data(**{**args, "stale": True})["available"] is False
    # Authorization status is deliberately outside comparable analytics.


def test_static_comparables_render_without_simulation_and_keep_quality_gates():
    position = {
        "source": "VERIFIED_SENT_EMAIL", "count": 7,
        "reference_value": "91,1 UF/m² útil", "property_value": "132,2 UF/m² útil",
    }
    comparable_mode = {"source": "VERIFIED_SENT_EMAIL", "campaign_comparable_mode": "PRIMARY_COMPARABLES"}
    args = {
        "position": position, "comparables": comparable_mode,
        "historical_email_verified": True,
        "campaign_comparable_mode": "PRIMARY_COMPARABLES", "stale": False,
    }
    result = _static_position_data(**args)  # No surface or proposed price needed for current position.
    assert result["available"] is True
    assert result["count"] == 7
    assert result["reference_label"] == "91,1 UF/m² útil"
    assert result["property_label"] == "132,2 UF/m² útil"
    assert result["gap_label"] == "+45,1% sobre propiedades similares"
    assert _static_position_data(**{**args, "position": {**position, "count": 4}})["reason"] == "COMPARABLE_SAMPLE_TOO_SMALL"
    assert _static_position_data(**{**args, "position": {**position, "property_value": None}})["reason"] == "COMPARABLE_VALUES_MISSING"
    assert _static_position_data(**{**args, "comparables": {**comparable_mode, "evidence_conflict": True}})["reason"] == "COMPARABLE_EVIDENCE_CONFLICT"
    assert _static_position_data(**{**args, "campaign_comparable_mode": "COMMUNAL_FALLBACK"})["reason"] == "NON_PRIMARY_COMPARABLE_MODE"
    communal_fallback = {**comparable_mode, "campaign_comparable_mode": "COMMUNAL_FALLBACK"}
    assert _static_position_data(**{**args, "comparables": communal_fallback, "campaign_comparable_mode": "COMMUNAL_FALLBACK"})["available"] is False


def test_authorization_does_not_hide_comparables_or_restore_price_cta():
    from bs4 import BeautifulSoup
    template = Environment(loader=FileSystemLoader("templates")).get_template("owner_campaign_monthly_portal.html")
    for case, count in (("full", 7), ("authorized", 7), ("authorized", 20)):
        view = premium_fixture(case, comparable_count=count)
        soup = BeautifulSoup(template.render(view=view), "html.parser")
        assert view["position_simulation"]["available"] is True
        assert view["position_simulation"]["count"] == count
        assert soup.select_one(".position-simulation") is not None
        if case == "authorized":
            assert view["already_authorized"] is True
            assert soup.select_one('.button-primary[aria-disabled="true"]')
            assert not soup.select_one('.button-primary[href]')


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
    assert section.select_one('[data-legend-kind="property_current"] [data-position-gap]').get_text(strip=True) == "+45,1% vs similares"
    assert section.select_one('[data-point="property_current"]') is not None
    assert section.select_one('[data-point="property_recommended"]') is not None

    # A current comparison remains useful without the surface needed for the
    # adjusted-price simulation.
    email_without_surface = sent_email_html.replace(
        '<div class="single-operation">Venta &#183; Codigo 5695 &#183; 2D &#183; 2B &#183; m&#178; 140 m&#178; &#250;tiles</div>',
        "",
    )
    static_row = {**row, "campaign_snapshot": {**row["campaign_snapshot"]}}
    static_row["campaign_snapshot"].pop("recommended_price")
    static_view = build_monthly_portal_view(db, static_row, campaign, email_html=email_without_surface)
    assert static_view["position_simulation"]["available"] is False
    assert static_view["position_static"]["available"] is True
    static_html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=static_view)
    static_soup = BeautifulSoup(static_html, "html.parser")
    static_section = static_soup.select_one("[data-position-simulation]")
    assert static_section is not None
    assert "91,1 UF/m² útil" in static_section.get_text(" ", strip=True)
    assert "132,2 UF/m² útil" in static_section.get_text(" ", strip=True)
    assert "+45,1% vs similares" in static_section.get_text(" ", strip=True)
    assert not static_section.select_one(".position-simulation__selector")

    communal_row = {**row, "campaign_snapshot": {**row["campaign_snapshot"], "comparable_mode": "COMMUNAL_FALLBACK"}}
    communal_view = build_monthly_portal_view(db, communal_row, campaign, email_html=email_without_surface)
    assert communal_view["position_static"]["available"] is False
    communal_html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=communal_view)
    assert BeautifulSoup(communal_html, "html.parser").select_one("#position-title") is None


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


def premium_fixture(case, comparable_count=13):
    import os
    db = mongomock.MongoClient().test
    code, owner = "5438", "owner@example.test"
    key = owner_property_portal_id(code, owner)
    current = {
        "period": "2026-10", "property": {"property_type": "Departamento", "commune": "Talca", "operation": "VENTA", "current_price": 18507, "surface_ref_m2": 140},
        "property_media": {"public_page_url": "https://www.procasa.cl/5438", "hero_image_url": "https://demoazimg.prop360.cl/procasa/img/propiedades/5438_main.JPEG", "verified_property_code": code, "public_page_active": True, "image_source": "PROCASA_PUBLIC_PROPERTY"},
        "activity_90d": {"leads": 0 if case == "zero" else 1, "conversations": 0, "visits": 0, "source_date": "2026-10-02"},
        "market_context": {"mortgage_rate": "4,1%", "tpm": "4,5%", "demand_status": "Selectiva", "reference_month": "Octubre 2026", "source_date": "2026-10-01", "summary": "El financiamiento y una demanda más selectiva hacen que el posicionamiento de precio sea especialmente relevante. " * 3},
        "comparables": {"count": comparable_count, "evidence_level": "HIGH", "version": "cluster_v2", "positioning_mode": "PRICE_M2", "reference_value": 91.1, "property_value": 132.2, "surface_ref_m2": 140, "unit": "UF/m² útil", "source_date": "2026-09-30", "analysis_generated_at": "2026-09-30"},
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
        assert len(soup.select("details.expandable")) >= (0 if case == "stale" else 1)
        diagnosis = soup.select_one("#diagnosis-title")
        if diagnosis:
            diagnosis_section = diagnosis.find_parent("section")
            assert diagnosis_section.select_one(".diagnosis-copy")
            assert diagnosis_section.select_one("details") is None
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
        if case == "no_gap":
            assert soup.select_one(".position-kpi .kpi-value").get_text(strip=True) == "1 fuente disponible"
            assert not soup.select_one(".position-kpi.above, .position-kpi.below")
            assert not view["gap_explanation"]
        if case in {"stale", "no_gap"}:
            assert view["position_simulation"]["available"] is False
        if case == "no_gap":
            assert not soup.select_one("#position-title")
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
            assert simulation["communal_on_same_scale"] is False  # Fixture does not declare a communal area basis.
            assert round(simulation["current_gap_pct"]) == 45
            assert round(simulation["proposed_gap_pct"]) == 31
            assert simulation["current_gap_pct"] == (132.2 / 91.1 - 1) * 100
            assert simulation["current_gap_pct"] != (132.2 / 102.8 - 1) * 100
            assert simulation["comparables_source_date"] == "30-09-2026"
            assert simulation["communal_source_date"] == "28-04-2026"
            position_section = soup.select_one(".position-simulation")
            assert position_section.select_one('[data-position-state="current"][aria-pressed="true"]')
            assert position_section.select_one('[data-position-state="adjusted"][disabled]')
            assert position_section.select_one('[data-legend-kind="property_current"] [data-position-gap]').get_text(strip=True) == "+45,1% vs similares"
            assert [item.get("data-point") for item in position_section.select(".position-simulation__track [data-point]:not([hidden])")] == ["comparable", "property_recommended", "property_current"]
            assert not position_section.select_one('[data-reference-gap][data-adjusted-gap]')
            assert len(position_section.select('[data-legend-kind="property_recommended"] .position-simulation__property-gap')) >= 1
            assert 'data-position-gap' in html and 'data-primary-x=' not in html
            assert "corte 30-09-2026" in position_section.get_text(" ", strip=True)
            assert "Oferta comunal · corte 28-04-2026" in position_section.get_text(" ", strip=True)  # Provenance stays visible; unknown area basis stays off-scale.
            assert "Propiedades similares" in position_section.get_text(" ", strip=True)
            assert "Referencia basada en 13 propiedades similares seleccionadas." in position_section.get_text(" ", strip=True)
            assert "mediana comparable" not in position_section.get_text(" ", strip=True).lower()
            assert position_section.select_one('[data-position-summary]')
            assert position_section.select_one('[data-position-classification]')
            assert position_section.select_one('[data-position-classification]').get("aria-live") == "polite"
            assert "Simulación visual. No modifica el precio" in position_section.get_text(" ", strip=True)
            assert "Oferta comunal observada" not in position_section.get_text(" ", strip=True)
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
        if case == "authorized":
            assert view["position_simulation"]["available"] is True
            assert soup.select_one(".position-simulation")
            assert soup.select_one('.button-primary[aria-disabled="true"]')
            assert not soup.select_one('.button-primary[href]')
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
    assert not recommendation.select_one(".recommendation-logo")
    assert soup.select_one(".masthead img.logo[alt='PROCASA']")
    assert ".logo { width:136px; height:50px; object-fit:cover; object-position:center 43%; }" in html
    assert ".logo { width:116px; height:44px; }" in html
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


def test_executive_signature_uses_requested_role_and_omits_removed_pitch():
    from bs4 import BeautifulSoup
    from owner_portal.executive import resolve_executive_contact

    db = mongomock.MongoClient().test
    template = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    )
    expected = "Agente inmobiliario · PROCASA SUCRE"
    for source in ({"role": "Ejecutiva PROCASA"}, {"role": "Ejecutivo PROCASA"}, {}):
        contact = resolve_executive_contact(db, {"campaign_snapshot": {"executive": source}}, {}, "Alex")
        assert contact["role"] == expected
        view = premium_fixture("full")
        view["executive_role"] = contact["role"]
        soup = BeautifulSoup(template.render(view=view), "html.parser")
        assert soup.select_one(".executive-role").get_text(" ", strip=True) == expected
        assert not soup.select_one(".executive-description")
        assert "Te explico cómo llegamos a este precio" not in soup.get_text(" ", strip=True)


def test_owner_commercial_funnel_uses_dashboard_stages_and_deduplicates_visit_orders():
    db = mongomock.MongoClient().test
    cutoff = datetime(2026, 10, 1, tzinfo=timezone.utc)
    created = cutoff - timedelta(days=20)
    for index in range(10):
        stage_history = [{"to": "NEW", "timestamp": created}]
        if index in {0, 1}:
            stage_history.extend([
                {"to": "OFFER", "timestamp": created + timedelta(days=3)},
            ])
        if index == 0:
            stage_history.append({"to": "CLOSED_WON", "timestamp": created + timedelta(days=8)})
        db["leads"].insert_one({
            "prospecto": {"codigo": "5695", "operacion": "Venta"},
            "created_at": created + timedelta(minutes=index),
            "phone": f"569123456{index:02d}", "stage_history": stage_history,
        })
    accepted = cutoff - timedelta(days=5)
    for index in range(4):
        db["visitas"].insert_one({
            "property_code": "5695", "status": "signed", "visita_code": f"V-{index}",
            "timeline": [{"action": "accepted", "server_timestamp": accepted}],
        })
    db["ordenes_visitas"].insert_one({
        "property_code": "5695", "status": "signed", "visita_code": "V-0",
        "timeline": [{"action": "accepted", "server_timestamp": accepted}],
    })
    funnel = resolve_owner_commercial_funnel_90d(
        db, "5695", candidates=(), window_end_candidates=(cutoff,), operation="VENTA",
    )
    stages = funnel["stages"]
    assert [stage["key"] for stage in stages] == ["LEADS", "VISITS", "OFFERS", "CLOSINGS"]
    assert [stage["count"] for stage in stages] == [10, 4, 2, 1]
    assert [stage["width"] for stage in stages] == sorted([stage["width"] for stage in stages], reverse=True)
    assert len({item for item in db["visitas"].distinct("visita_code")}) == 4
    assert stages[1]["source"] == "visitas.status+timeline.accepted"
    assert stages[2]["source"] == "leads.stage_history:OFFER|NEGOTIATION|CLOSED_WON"
    assert stages[3]["source"] == "leads.stage_history:CLOSED_WON"
    assert funnel["cutoff"] == cutoff


def test_property_5695_owner_funnel_keeps_verified_lead_and_zero_visits():
    db = mongomock.MongoClient().test
    cutoff = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
    db["leads"].insert_one({
        "_id": "5695-lead-1", "prospecto": {"codigo": "5695", "operacion": "VENTA"},
        "created_at": datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        "phone": "+56912345678", "stage_history": [{"to": "NEW", "timestamp": datetime(2026, 8, 7, 12, tzinfo=timezone.utc)}],
    })
    funnel = resolve_owner_commercial_funnel_90d(
        db, "5695", candidates=(), window_end_candidates=(cutoff,), operation="VENTA",
    )
    assert [stage["count"] for stage in funnel["stages"]] == [1, 0, 0, 0]
    assert funnel["stages"][0]["source"] == "leads.created_at"
    assert funnel["stages"][1]["source"] == "visitas.status+timeline.accepted"
    assert funnel["stages"][2]["status"] == funnel["stages"][3]["status"] == "VERIFIED"
    assert funnel["cutoff"] == cutoff


def test_market_evidence_summary_keeps_unscaled_sources_visible():
    result = _market_evidence_model(
        market_position={
            "available": True,
            "current_value": 132.2,
            "references": [{"kind": "COMPARABLE", "value": 91.1, "unit": "UF/m² útil", "area_basis": "USEFUL"}],
        },
        position_static={"available": True, "reference_value": 91.1, "unit": "UF/m² útil", "count": 13, "source": "PRIMARY"},
        appraisal_card={"mode": "DOCUMENT_ONLY"},
        communal={"offer_uf_m2": 102.76, "reference_unit": "UF/m² de oferta", "currency_basis": "UF", "source_date": "28/04/2026"},
        stale=False,
    )
    assert [source["kind"] for source in result["sources"]] == ["APPRAISAL", "COMPARABLE", "COMMUNAL"]
    assert [source["kind"] for source in result["scale_compatible_sources"]] == ["COMPARABLE"]
    assert result["summary_position_label"] == "3 fuentes disponibles"
    assert result["evidence_source_count"] == 3
    assert result["quantitative_reference_count"] == 2
    assert result["scale_compatible_count"] == 1
    assert "1 referencia directamente en la misma escala" in result["summary_position_note"]
    assert "2 referencias con valor" in result["summary_position_note"]
    assert "Oferta comunal" in result["summary_position_note"]
    assert result["sources"][0]["value_label"] == ""
    assert result["sources"][2]["value_label"] == "102,8 UF/m²"


def test_market_position_displays_available_non_scale_values_with_source_labels():
    from bs4 import BeautifulSoup

    view = premium_fixture("full")
    evidence = _market_evidence_model(
        market_position={
            "available": True, "current_value": 39.9,
            "references": [{"kind": "COMPARABLE", "value": 35.0, "unit": "UF/m² útil", "area_basis": "USEFUL"}],
        },
        position_static={"available": True, "reference_value": 35.0, "unit": "UF/m² útil", "count": 7, "source": "PRIMARY"},
        appraisal_card={"mode": "STRUCTURED", "market_position_reference": {
            "appraisal_uf_m2": 37.5, "appraisal_uf_m2_unit": "UF/m² útil", "area_basis": "USEFUL",
        }},
        communal={"offer_uf_m2": 37.0, "reference_unit": "UF/m²", "currency_basis": "UF"},
        stale=False,
    )
    view["market_position"]["market_evidence"] = evidence
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    soup = BeautifulSoup(html, "html.parser")
    extra_refs = soup.select(".market-position-extra-ref")
    assert [item.get("data-kind") for item in extra_refs] == ["APPRAISAL", "COMMUNAL"]
    extra_text = " ".join(item.get_text(" ", strip=True) for item in extra_refs)
    assert "Tasación · 37,5 UF/m² útil" in extra_text
    assert "Oferta comunal · 37,0 UF/m² · base de superficie pendiente de validar" in extra_text
    assert soup.select_one(".market-position-evidence").get_text(" ", strip=True).startswith("Referencias de mercado")
    evidence_text = soup.select_one(".market-position-evidence").get_text(" ", strip=True)
    assert "Comparadas en esta escala: Similares" in evidence_text
    assert "35,0 UF/m² útil" not in evidence_text
    assert "37,5 UF/m² útil" in evidence_text
    assert [item["kind"] for item in evidence["scale_compatible_sources"]] == ["COMPARABLE"]


def test_owner_funnel_reuses_portals_and_costs_bar_silhouette_visual_language():
    from bs4 import BeautifulSoup

    view = premium_fixture("full")
    view["activity_90d"]["funnel"]["stages"] = [
        {"key": "LEADS", "label": "Leads", "count": 10, "value": "10", "ratio": "100%", "width": 100, "shape_width": 100, "fill_width": 100, "status": "VERIFIED"},
        {"key": "VISITS", "label": "Visitas", "count": 4, "value": "4", "ratio": "40,0% de leads", "width": 76, "shape_width": 76, "fill_width": 40, "status": "VERIFIED"},
        {"key": "OFFERS", "label": "Ofertas", "count": 2, "value": "2", "ratio": "50,0% de visitas", "width": 54, "shape_width": 54, "fill_width": 20, "status": "VERIFIED"},
        {"key": "CLOSINGS", "label": "Cierre", "count": 1, "value": "1", "ratio": "50,0% de ofertas", "width": 34, "shape_width": 34, "fill_width": 10, "status": "VERIFIED"},
    ]
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    soup = BeautifulSoup(html, "html.parser")
    funnel = soup.select_one(".activity-funnel")
    stages = funnel.select(".owner-funnel-stage-row")
    assert [stage.select_one(".owner-funnel-stage-label span").get_text(strip=True) for stage in stages] == ["Leads", "Visitas", "Ofertas", "Cierre"]
    widths = [float(stage.select_one(".owner-funnel-stage-bar")["style"].split("--stage-width:", 1)[1].split("%", 1)[0]) for stage in stages]
    assert all(left > right for left, right in zip(widths, widths[1:]))
    assert all(stage.select_one(".owner-funnel-stage-label strong") for stage in stages)
    assert all(stage.select_one(".owner-funnel-stage-label small") for stage in stages)
    assert "owner-funnel-silhouette" in html and "renderFunnelSilhouette" in html
    assert "ownerFunnelBarGrow .72s cubic-bezier(.16,1,.3,1) .52s" in html
    assert "ownerFunnelLineDraw .72s cubic-bezier(.16,1,.3,1) .46s" in html
    assert "animateFunnelCounts" not in html and "toLocaleString('es-CL')" not in html
    assert "clip-path:polygon" not in html
    assert "prefers-reduced-motion:reduce" in html
    assert not any("Conversaciones" in stage.get_text() for stage in stages)


def test_owner_funnel_keeps_shape_when_zero_or_not_instrumented():
    from bs4 import BeautifulSoup
    view = premium_fixture("full")
    rows = [
        ("LEADS", "Leads", 1, "1", "VERIFIED", 100),
        ("VISITS", "Visitas", 0, "0", "VERIFIED", 76),
        ("OFFERS", "Ofertas", None, "—", "NOT_INSTRUMENTED", 54),
        ("CLOSINGS", "Cierre", None, "—", "NOT_INSTRUMENTED", 34),
    ]
    view["activity_90d"]["funnel"]["stages"] = [
        {"key": key, "label": label, "count": count, "value": value, "ratio": "QA", "shape_width": width,
         "fill_width": 100 if count == 1 else 0, "status": status}
        for key, label, count, value, status, width in rows
    ]
    html = Environment(loader=FileSystemLoader("templates")).get_template(
        "owner_campaign_monthly_portal.html"
    ).render(view=view)
    soup = BeautifulSoup(html, "html.parser")
    bars = soup.select(".activity-funnel .owner-funnel-stage-bar")
    assert len(bars) == 4
    widths = [float(bar["style"].split("--stage-width:", 1)[1].split("%", 1)[0]) for bar in bars]
    fills = [float(bar["style"].split("--fill-width:", 1)[1].split("%", 1)[0]) for bar in bars]
    assert widths == [100, 76, 54, 34]
    assert fills == [100, 0, 0, 0]
    assert bars[2]["data-status"] == bars[3]["data-status"] == "NOT_INSTRUMENTED"
    assert "owner-funnel-zero-marker" not in html
