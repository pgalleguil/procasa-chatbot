from __future__ import annotations

from datetime import datetime, timezone

import mongomock

from owner_portal.monthly import (
    OWNER_PROPERTY_PORTAL_COLLECTION,
    build_monthly_portal_view,
    owner_property_portal_id,
    persist_monthly_snapshot,
)


def _db_with_monthly_record(property_code="5765", owner_email="owner@example.com", **record_fields):
    db = mongomock.MongoClient()["portal-test"]
    key = owner_property_portal_id(property_code, owner_email)
    record = {
        "_id": key,
        "owner_key": key,
        "property_code": property_code,
        **record_fields,
    }
    db[OWNER_PROPERTY_PORTAL_COLLECTION].insert_one(record)
    return db


def _row(status="SENT", snapshot=None):
    return {
        "campaign_id": "campaign-test",
        "property_code": "5765",
        "owner_email": "owner@example.com",
        "send_status": status,
        "campaign_snapshot": snapshot or {
            "owner_email": "owner@example.com",
            "operation_resolved": "VENTA",
            "operation": "VENTA",
            "property_type": "Casa",
            "commune": "Talca",
            "current_price": 2852,
            "recommended_adjustment_pct": 9,
            "recommended_price": 2595,
            "comparable_count": 17,
            "document_type": "NONE",
            "prepared_at": datetime(2026, 9, 30, tzinfo=timezone.utc),
            "leads_90d": 0,
        },
    }


def _campaign_view(status="SENT"):
    stale = status == "SKIPPED_STALE_OR_MISMATCH"
    return {
        "safe_mode": stale,
        "already_authorized": False,
        "source": "EMAIL",
        "sent_at": datetime(2026, 9, 30, tzinfo=timezone.utc),
        "current_price_label": "2.852 UF",
        "recommended_price_label": "2.595 UF",
        "recommendation_reason": "Razón congelada",
        "comparable_count": 17,
        "document_available": not stale,
        "report_url": "/campana/informe?token=signed",
        "executive_name": "Ejecutivo",
        "top_primary_url": "" if stale else "/campana/respuesta?token=primary",
        "top_advisor_url": "/campana/respuesta?token=advisor-top",
        "advisor_url": "/campana/respuesta?token=advisor",
        "sticky_primary_url": "" if stale else "/campana/respuesta?token=primary-sticky",
        "sticky_advisor_url": "/campana/respuesta?token=advisor-sticky",
    }


def test_monthly_snapshot_drives_dashboard_without_recalculating_price():
    db = _db_with_monthly_record(
        current_portal_state={
            "period": "2026-10",
            "generated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "property": {
                "operation": "VENTA", "property_type": "Casa", "commune": "Talca",
                "current_price": 2852.4, "document_type": "NONE",
            },
            "activity_90d": {"leads": 0, "conversations": 0, "visits": 0},
            "comparables": {
                "count": 17, "reference_value": "56,1 UF/m² útil",
                "property_value": "57,0 UF/m² útil", "positioning": "Sobre la referencia",
            },
            "communal_reference": {"summary": "45,8 UF/m² de oferta · 215 publicaciones activas"},
            "market_context": {
                "reference_month": "Chile · octubre 2026", "mortgage_rate": "4,1%",
                "mortgage_note": "Snapshot verificado", "tpm": "4,5%", "tpm_note": "Vigente",
                "demand_status": "Selectiva", "context_summary": "Contexto congelado",
                "source_names": ["Banco Central", "MINVU"],
            },
            "recommendation": {
                "recommended_adjustment_pct": 9,
                "recommended_price": 2595.4,
                "diagnosis": "Diagnóstico congelado", "recommendation_text": "Recomendación congelada",
            },
        },
    )
    row = _row(snapshot={"owner_email": "owner@example.com", "current_price": 2852, "document_type": "NONE"})
    before = db[OWNER_PROPERTY_PORTAL_COLLECTION].count_documents({})
    view = build_monthly_portal_view(db, row, _campaign_view())

    assert view["historic_source"] == "MONTHLY_SNAPSHOT"
    assert view["current_price_label"] == "2.852 UF"
    assert view["recommended_price_label"] == "2.595 UF"
    assert view["comparable_count"] == 17
    assert view["position"]["reference_value"] == "56,1 UF/m² útil"
    assert view["position"]["property_value"] == "57,0 UF/m² útil"
    assert view["activity_90d"] == {"leads": 0, "conversations": 0, "visits": 0, "summary": ""}
    assert view["market_context"]["kpis"][0]["value"] == "4,1%"
    assert view["market_context"]["kpis"][1]["value"] == "4,5%"
    assert view["market_context"]["kpis"][2]["value"] == "Selectiva"
    assert view["market_context"]["sources"] == "Banco Central, MINVU"
    assert view["diagnosis"] == "Diagnóstico congelado"
    assert view["recommendation_text"] == "Recomendación congelada"
    assert view["document_available"] is False
    assert db[OWNER_PROPERTY_PORTAL_COLLECTION].count_documents({}) == before


def test_no_monthly_or_verified_history_does_not_invent_secondary_data():
    db = mongomock.MongoClient()["portal-empty"]
    view = build_monthly_portal_view(db, _row(), _campaign_view(), email_html=None)

    assert view["historic_source"] == "CAMPAIGN_SNAPSHOT"
    assert view["current_price_label"] == "2.852 UF"
    assert view["recommended_price_label"] == "2.595 UF"
    assert view["activity_90d"]["leads"] == 0
    assert view["activity_90d"]["conversations"] is None
    assert view["position"]["reference_value"] is None
    assert view["market_context"] is None
    assert view["communal_reference"] == {}
    assert view["diagnosis"] is None
    assert view["recommendation_text"] == "Razón congelada"


def test_verified_email_evidence_fills_only_secondary_historical_blocks():
    db = mongomock.MongoClient()["portal-email-evidence"]
    email_html = """
      <div class="single-heading">Casa · Talca</div>
      <div class="macro-heading-single">CHILE · SEPTIEMBRE 2026</div>
      <div class="macro-copy-single">Contexto de prueba.</div>
      <div class="macro-source-single">Fuentes: Banco Central y MINVU · septiembre 2026</div>
      <div class="activity-stat-value-single">0 leads registrados</div>
      <p class="activity-copy-single">La propiedad no registró interacciones.</p>
      <span class="evidence-badge-single">17 publicaciones similares analizadas</span>
      <span class="position-ref-box">Referencia de mercado<br><strong>56,1 UF/m² útil</strong></span>
      <span class="position-prop-box">Tu propiedad<br><strong>57,0 UF/m² útil</strong></span>
      <p class="comparable-summary-single">La propiedad está un 2% sobre la referencia.</p>
      <p class="diagnostic-copy">Diagnóstico histórico.</p>
      <p class="recommendation-copy">Recomendación histórica.</p>
      <span class="market-reference-compact-single">45,8 UF/m² · 215 publicaciones activas</span>
    """
    view = build_monthly_portal_view(db, _row(), _campaign_view(), email_html=email_html)

    assert view["activity_90d"] == {"leads": 0, "conversations": None, "visits": None, "summary": "La propiedad no registró interacciones."}
    assert view["property_type"] == "Casa"
    assert view["commune"] == "Talca"
    assert view["market_context"]["sources"] == "Banco Central y MINVU · septiembre 2026"
    assert view["comparable_count"] == 17
    assert view["position"]["reference_value"] == "56,1 UF/m² útil"
    assert view["position"]["property_value"] == "57,0 UF/m² útil"
    assert view["position"]["interpretation"] == "La propiedad está un 2% sobre la referencia."
    assert view["position"]["marker_pct"] is not None
    assert view["diagnosis"] == "Diagnóstico histórico."
    assert view["recommendation_text"] == "Recomendación histórica."
    assert view["communal_reference"]["summary"] == "45,8 UF/m² · 215 publicaciones activas"


def test_stale_monthly_portal_never_exposes_price_authorization():
    db = mongomock.MongoClient()["portal-stale"]
    row = _row(status="SKIPPED_STALE_OR_MISMATCH")
    view = build_monthly_portal_view(db, row, _campaign_view(status="SKIPPED_STALE_OR_MISMATCH"))

    assert view["safe_mode"] is True
    assert view["can_authorize"] is False
    assert view["top_primary_url"] == ""
    assert view["sticky_primary_url"] == ""
    assert view["top_advisor_url"]
    assert view["sticky_advisor_url"]
    assert view["current_price_label"] == ""
    assert view["diagnosis"] == ""


def test_monthly_snapshot_writer_is_idempotent_and_preserves_history():
    db = mongomock.MongoClient()["portal-write-test"]
    october = {"period": "2026-10", "generated_at": datetime(2026, 10, 1, tzinfo=timezone.utc), "property": {"current_price": 2800}}
    september = {"period": "2026-09", "generated_at": datetime(2026, 9, 1, tzinfo=timezone.utc), "property": {"current_price": 2852}}

    assert persist_monthly_snapshot(db, property_code="5765", owner_email="owner@example.com", snapshot=october)["status"] == "CREATED"
    assert persist_monthly_snapshot(db, property_code="5765", owner_email="owner@example.com", snapshot=october)["status"] == "UNCHANGED"
    assert persist_monthly_snapshot(db, property_code="5765", owner_email="owner@example.com", snapshot=september)["status"] == "CREATED"

    record = db[OWNER_PROPERTY_PORTAL_COLLECTION].find_one({"_id": owner_property_portal_id("5765", "owner@example.com")})
    assert [item["period"] for item in record["monthly_snapshots"]] == ["2026-09", "2026-10"]
    assert record["current_portal_state"]["period"] == "2026-10"
    assert "owner_email" not in record
    assert len(record["monthly_snapshots"]) == 2

    conflicting = {**october, "property": {"current_price": 2500}}
    try:
        persist_monthly_snapshot(db, property_code="5765", owner_email="owner@example.com", snapshot=conflicting)
    except ValueError as exc:
        assert str(exc) == "monthly_snapshot_period_is_immutable"
    else:
        raise AssertionError("conflicting period was accepted")

    try:
        persist_monthly_snapshot(
            db, property_code="5766", owner_email="owner@example.com",
            snapshot={"period": "2026-10", "property": {"owner_email": "private@example.com"}},
        )
    except ValueError as exc:
        assert str(exc) == "monthly_snapshot_contains_private_access_data"
    else:
        raise AssertionError("nested private data was accepted")
