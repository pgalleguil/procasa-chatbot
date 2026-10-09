from __future__ import annotations

import hashlib
import gzip
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import mongomock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from owner_portal import campaign
from owner_portal.email_artifacts import (
    EMAIL_ARTIFACT_COLLECTION,
    masked_html_parity,
    transform_sent_email_to_portal_html,
)
from owner_portal.router import router


CAMPAIGN = "owner_price_sucre_wave2_20260930"
CODE = "17005"
EMAIL = "owner@example.com"


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.links = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_data(self, data):
        self.parts.append(data)


def ledger_row(**overrides):
    row = {
        "_id": f"{CAMPAIGN}:{CODE}",
        "campaign_id": CAMPAIGN,
        "property_code": CODE,
        "owner_email": EMAIL,
        "owner_name": "Private owner",
        "send_status": "SENT",
        "operation": "VENTA",
        "property_type": "Departamento",
        "commune": "Santiago",
        "executive_name": "Ejecutivo PROCASA",
        "executive_email": "exec@example.com",
        "executive_phone": "+56 9 1234 5678",
        "current_price": 3427.5,
        "current_price_clp": 140000000,
        "recommended_adjustment_pct": 9,
        "recommended_price": 3119.025,
        "recommended_price_clp": 127400000,
        "recommendation_reason": "Resumen congelado de la campaña.",
        "document_type": "COMMUNAL_MARKET_REPORT",
        "authorization_status": "PENDING",
        "events": [],
    }
    row.update(overrides)
    return row


def make_test_db(row=None):
    db = mongomock.MongoClient()["test"]
    row = row or ledger_row()
    db[campaign.LEDGER_COLLECTION].insert_one(row)
    operation = str(row.get("operation") or "VENTA").upper()
    current_price = row.get("current_price")
    sale = operation == "VENTA"
    rent = operation == "ARRIENDO"
    db[campaign.MASTER_COLLECTION].insert_one({
        "codigo": str(row.get("property_code") or CODE),
        "metadata": {"tipo_propiedad": row.get("property_type") or "Departamento"},
        "ubicacion": {"comuna": row.get("commune") or "Santiago"},
        "tipo_operacion": {
            "venta": sale, "arriendo": rent,
            "precio_venta": {"precio_uf": current_price} if sale else {},
            "precio_arriendo": {"precio_uf": current_price} if rent else {},
        },
    })
    return db


def client_for(monkeypatch, db):
    monkeypatch.setattr("owner_portal.router.get_db", lambda: db)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def registered_link(monkeypatch, db, *, source="EMAIL"):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-test-secret")
    result = campaign.materialize_portal_accesses(
        db, campaign_id=CAMPAIGN, base_url="https://www.procasa.cl",
        source=source, persist=True,
    )
    assert result["counts"]["eligible"] == 1
    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    expires_at = int(campaign._as_utc(row["portal_access"]["expires_at"]).timestamp())
    token = campaign.issue_portal_token(row, expires_at=expires_at)
    return campaign.build_portal_url("https://www.procasa.cl", CODE, token, source=source)


def registered_short_link(monkeypatch, db, *, source="EMAIL", campaign_id=CAMPAIGN):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-test-secret")
    result = campaign.materialize_portal_accesses(
        db, campaign_id=campaign_id, base_url="https://www.procasa.cl",
        source=source, persist=True,
    )
    assert result["counts"]["eligible"] == 1
    return result["records"][0]["private_url"]


def insert_email_artifact(db, row, html=None):
    html = html or (
        '<!doctype html><html><head><style>.hero{color:#17175f}</style></head><body>'
        '<table class="email-layout"><tr><td>Departamento · Santiago</td></tr></table>'
        '<table class="valuation-strip-single"><tr><td>3.428 UF · 3.119 UF · 9%</td></tr></table>'
        f'<tr><td><a class="report" href="https://old.example/campana/informe?token=old-report">VER INFORME</a></td></tr>'
        f'<tr><td><a class="primary" href="https://old.example/campana/respuesta?campana={row["campaign_id"]}&amp;codigos={row["property_code"]}&amp;accion=aceptar_rebaja&amp;token=old-primary">REVISAR / CONFIRMAR AJUSTE</a></td></tr>'
        f'<tr><td><a class="advisor" href="https://old.example/campana/respuesta?campana={row["campaign_id"]}&amp;codigos={row["property_code"]}&amp;accion=contactar_ejecutivo&amp;token=old-advisor">REVISAR CON MI EJECUTIVO</a></td></tr>'
        '</body></html>'
    )
    row["send_attempts"] = [{"attempt_id": "attempt-test", "message_id": "<sent-message@example.test>", "smtp_status": str(row.get("send_status") or "SENT")}]
    db[campaign.LEDGER_COLLECTION].update_one(
        {"_id": row["_id"]}, {"$set": {"send_attempts": row["send_attempts"]}},
    )
    encoded = html.encode("utf-8")
    db[EMAIL_ARTIFACT_COLLECTION].insert_one({
        "_id": f'{row["campaign_id"]}:{row["property_code"]}',
        "campaign_id": row["campaign_id"],
        "property_code": row["property_code"],
        "owner_email": row["owner_email"],
        "message_id": "<sent-message@example.test>",
        "send_attempt_id": "attempt-test",
        "gmail_message_id": "gmail-id-test",
        "sent_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "html_gzip": gzip.compress(encoded),
        "html_encoding": "utf-8",
        "html_compression": "gzip",
        "original_html_sha256": hashlib.sha256(encoded).hexdigest(),
        "original_html_bytes": len(encoded),
        "compressed_bytes": len(gzip.compress(encoded)),
        "source": "GMAIL_SENT",
        "created_at": datetime(2026, 10, 2, tzinfo=timezone.utc),
    })
    return html


def test_private_portal_requires_registered_signed_token_and_logs_source(monkeypatch):
    db = make_test_db()
    url = registered_link(monkeypatch, db)
    split = urlsplit(url)
    token = parse_qs(split.query)["token"][0]
    client = client_for(monkeypatch, db)

    response = client.get(f"/ajuste/{CODE}?token={token}")
    second_visit = client.get(f"/ajuste/{CODE}?token={token}")

    assert response.status_code == 200
    assert second_visit.status_code == 200
    assert "3.428 UF" in response.text
    assert "3.119 UF" in response.text
    assert "Resumen congelado de la campaña." not in response.text
    assert "Revisión comercial de tu propiedad" in response.text
    assert "Resumen en 30 segundos" in response.text
    assert "Informe comercial · Propietarios" in response.text
    assert "Revisar ajuste" in response.text
    assert "Snapshot de campaña" not in response.text
    assert "Sin métricas históricas en el snapshot" not in response.text
    assert "portal_opened" not in response.text
    assert "owner@example.com" not in response.text
    visible = VisibleTextParser()
    visible.feed(response.text)
    assert CAMPAIGN not in " ".join(visible.parts)
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    stored = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    event = next(item for item in stored["events"] if item["event"] == "portal_opened")
    assert len([item for item in stored["events"] if item["event"] == "portal_opened"]) == 1
    assert not any(item["event"] == "portal_visit" for item in stored["events"])
    assert "owner_portal_visit_count" not in stored
    assert event["source"] == "EMAIL"
    assert event["interaction_surface"] == "OWNER_PORTAL"
    assert event["interaction_channel"] == "EMAIL"
    assert event["report_period"]
    assert event["snapshot_hash"]
    assert "token_hash" in stored["portal_access"]
    assert token not in str(stored["portal_access"])


def test_private_portal_load_survives_portal_open_telemetry_failure(monkeypatch):
    import campanas.owner_campaign_live_events as live_events

    db = make_test_db()
    url = registered_link(monkeypatch, db)
    client = client_for(monkeypatch, db)

    def fail_telemetry(*_args, **_kwargs):
        raise RuntimeError("temporary mongo outage")

    monkeypatch.setattr(live_events, "persist_live_event", fail_telemetry)
    response = client.get(urlsplit(url).path + "?" + urlsplit(url).query)
    assert response.status_code == 200
    assert "Revisión comercial de tu propiedad" in response.text


def test_portal_actions_use_campaign_p1_tokens_for_same_property(monkeypatch):
    db = make_test_db()
    url = registered_link(monkeypatch, db, source="WHATSAPP")
    token = parse_qs(urlsplit(url).query)["token"][0]
    verified = campaign.verify_portal_request(db, property_code=CODE, token=token)
    row, claims = verified
    view = campaign.build_private_page_view(
        db, row, claims, base_url="https://www.procasa.cl", source="WHATSAPP",
    )
    client = client_for(monkeypatch, db)
    response = client.get(f"/ajuste/{CODE}?token={token}&source=WHATSAPP")
    assert response.status_code == 200

    from campanas.owner_campaign_live_events import verify_live_token
    for key, action in (("primary_url", "aceptar_rebaja"), ("advisor_url", "contactar_ejecutivo")):
        parsed = urlsplit(view[key])
        query = parse_qs(parsed.query)
        token_claims = verify_live_token(
            query["token"][0], campaign_id=CAMPAIGN, property_code=CODE,
            recipient=EMAIL, action=action,
        )
        assert parsed.path == "/campana/respuesta"
        assert token_claims and token_claims["source"] == "WHATSAPP"
        assert token_claims["interaction_surface"] == "OWNER_PORTAL"
    report = parse_qs(urlsplit(view["report_url"]).query)["token"][0]
    report_claims = verify_live_token(
        report, campaign_id=CAMPAIGN, property_code=CODE,
        recipient=EMAIL, action="ver_informe",
    )
    assert report_claims["document_type"] == "COMMUNAL_MARKET_REPORT"
    assert report_claims["source"] == "WHATSAPP"
    assert report_claims["interaction_surface"] == "OWNER_PORTAL"


def test_portal_email_source_remains_distinct_from_original_email_template(monkeypatch):
    db = make_test_db()
    url = registered_link(monkeypatch, db, source="EMAIL")
    token = parse_qs(urlsplit(url).query)["token"][0]
    row, claims = campaign.verify_portal_request(db, property_code=CODE, token=token)
    view = campaign.build_private_page_view(
        db, row, claims, base_url="https://www.procasa.cl", source="EMAIL",
    )

    from campanas.owner_campaign_live_events import decode_live_token
    for url_key in ("primary_url", "advisor_url", "report_url"):
        action_token = parse_qs(urlsplit(view[url_key]).query)["token"][0]
        action_claims = decode_live_token(action_token)
        assert action_claims["source"] == "EMAIL"
        assert action_claims["interaction_surface"] == "OWNER_PORTAL"


def test_no_code_only_invalid_token_or_cross_property_access(monkeypatch):
    db = make_test_db()
    url = registered_link(monkeypatch, db)
    token = parse_qs(urlsplit(url).query)["token"][0]
    client = client_for(monkeypatch, db)

    assert client.get(f"/ajuste/{CODE}").status_code == 404
    assert client.get(f"/ajuste/{CODE}?token=not-a-token").status_code == 404
    assert client.get(f"/ajuste/99999?token={token}").status_code == 404
    assert client.get(f"/ajuste/{CODE}?token={token}&extra=1").status_code == 404


def test_none_document_hides_report_and_stale_status_hides_authorization(monkeypatch):
    none_db = make_test_db(ledger_row(document_type="NONE"))
    none_url = registered_link(monkeypatch, none_db)
    none_response = client_for(monkeypatch, none_db).get(none_url.replace("https://www.procasa.cl", ""))
    assert none_response.status_code == 200
    assert "VER / DESCARGAR INFORME" not in none_response.text
    assert "/campana/informe?token=" not in none_response.text

    stale_db = make_test_db(ledger_row(send_status="SKIPPED_STALE_OR_MISMATCH"))
    stale_url = registered_link(monkeypatch, stale_db)
    stale_response = client_for(monkeypatch, stale_db).get(stale_url.replace("https://www.procasa.cl", ""))
    assert stale_response.status_code == 200
    assert "REVISAR / CONFIRMAR AJUSTE" not in stale_response.text
    assert "Escribir por WhatsApp" in stale_response.text and "WhatsApp" in stale_response.text
    assert "3.119 UF" not in stale_response.text


def test_code_16544_stale_campaign_keeps_html_report_visible_without_unvalidated_prices(monkeypatch):
    row = ledger_row(
        _id=f"{CAMPAIGN}:16544", property_code="16544", send_status="SKIPPED_STALE_OR_MISMATCH",
        operation="ARRIENDO", property_type="Local Comercial", commune="Maipú",
        current_price=36.5, recommended_adjustment_pct=5, recommended_price=34.675,
        document_type="NONE",
        campaign_snapshot={
            "property_code": "16544", "operation": "ARRIENDO",
            "property_type": "Local Comercial", "commune": "Maipú",
            "leads_90d": 5,
            "prepared_at": datetime(2026, 9, 30, 22, 58, 36, tzinfo=timezone.utc),
        },
    )
    db = make_test_db(row)
    url = registered_short_link(monkeypatch, db)
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))

    assert response.status_code == 200
    assert "Código 16544" in response.text
    assert "Resumen en 30 segundos" in response.text
    assert "Precio actual" in response.text and "En validación" in response.text
    assert "Precio sugerido" in response.text and "Pendiente de validación" in response.text
    assert "Actividad comercial" in response.text
    assert "Consultas recibidas" in response.text
    assert 'data-count-final="5"' in response.text
    assert "Recomendación PROCASA" in response.text
    assert "La recomendación de precio está en validación" in response.text
    assert "Al preparar este ajuste se detectó una diferencia" in response.text
    assert "La autorización estará disponible cuando termine la validación" in response.text
    visible = VisibleTextParser()
    visible.feed(response.text)
    visible_text = " ".join(visible.parts)
    assert "36,5 UF" not in visible_text
    assert "34,675 UF" not in visible_text
    assert "34.675" not in visible_text
    assert "Ajuste: 5%" not in visible_text
    assert "Recomendamos ajustar el precio un 5%" not in visible_text
    assert "REVISAR / CONFIRMAR AJUSTE" not in response.text
    assert "Escribir por WhatsApp" in response.text
    assert "/campana/informe?token=" not in response.text


def test_live_price_drift_blocks_recommendation_but_preserves_report(monkeypatch):
    db = make_test_db()
    db[campaign.MASTER_COLLECTION].update_one(
        {"codigo": CODE},
        {"$set": {"tipo_operacion.precio_venta.precio_uf": 3500}},
    )
    url = registered_link(monkeypatch, db, source="WHATSAPP")
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))

    assert response.status_code == 200
    assert "La recomendación de precio está en validación" in response.text
    assert "El precio vigente de la ficha de la propiedad difiere" in response.text
    assert "Resumen en 30 segundos" in response.text
    assert "Actividad comercial" in response.text
    assert "Precio actual" in response.text and "En validación" in response.text
    assert "Precio sugerido" in response.text and "Pendiente de validación" in response.text
    assert "3.427,5 UF" not in response.text and "3.119,025 UF" not in response.text
    assert "REVISAR / CONFIRMAR AJUSTE" not in response.text
    assert "Escribir por WhatsApp" in response.text


def test_validated_source_price_restores_stale_link_and_ignores_daily_clp_uf_drift(monkeypatch):
    row = ledger_row(send_status="SKIPPED_STALE_OR_MISMATCH")
    db = make_test_db(row)
    db[campaign.MASTER_COLLECTION].update_one(
        {"codigo": CODE},
        {"$set": {
            "estado.estado_prop360": "Activa",
            "estado.disponible_prop360": True,
            "tipo_operacion.precio_venta": {
                "precio_uf": 3427.5, "precio_clp": 140000000,
                "precio_publicado": 140000000, "moneda_publicada": "CLP",
            },
        }},
    )
    from owner_portal.price_revalidation import build_price_revalidation

    master = db[campaign.MASTER_COLLECTION].find_one({"codigo": CODE})
    row["price_revalidation"] = build_price_revalidation(
        row, master, validated_at="2026-10-09T12:00:00-03:00",
        uf_rate_clp=41130.94, uf_rate_date="2026-10-09",
    )
    db[campaign.LEDGER_COLLECTION].update_one(
        {"_id": row["_id"]}, {"$set": {"price_revalidation": row["price_revalidation"]}},
    )
    # A later daily UF-derived field can move while the published CLP amount stays fixed.
    db[campaign.MASTER_COLLECTION].update_one(
        {"codigo": CODE}, {"$set": {"tipo_operacion.precio_venta.precio_uf": 3400.0}},
    )

    url = registered_link(monkeypatch, db)
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
    integrity = campaign.validate_campaign_price_integrity(
        db, db[campaign.LEDGER_COLLECTION].find_one({"_id": row["_id"]}),
    )

    assert response.status_code == 200
    assert "Revisar ajuste" in response.text
    assert "La recomendación de precio está en validación" not in response.text
    assert integrity["required"] is False
    assert integrity["price_revalidated"] is True
    assert integrity["source_currency"] == "CLP"
    assert integrity["current_price_clp"] == 140000000
    assert integrity["recommended_price_clp"] == 127400000
    assert integrity["recommended_adjustment_pct"] == 9


def test_validated_source_price_blocks_if_published_amount_changes():
    row = ledger_row(send_status="SKIPPED_STALE_OR_MISMATCH")
    db = make_test_db(row)
    db[campaign.MASTER_COLLECTION].update_one(
        {"codigo": CODE},
        {"$set": {
            "estado.estado_prop360": "Activa",
            "estado.disponible_prop360": True,
            "tipo_operacion.precio_venta": {
                "precio_uf": 3427.5, "precio_clp": 140000000,
                "precio_publicado": 140000000, "moneda_publicada": "CLP",
            },
        }},
    )
    from owner_portal.price_revalidation import build_price_revalidation

    master = db[campaign.MASTER_COLLECTION].find_one({"codigo": CODE})
    row["price_revalidation"] = build_price_revalidation(
        row, master, validated_at="2026-10-09T12:00:00-03:00",
        uf_rate_clp=41130.94, uf_rate_date="2026-10-09",
    )
    db[campaign.LEDGER_COLLECTION].update_one(
        {"_id": row["_id"]}, {"$set": {"price_revalidation": row["price_revalidation"]}},
    )
    db[campaign.MASTER_COLLECTION].update_one(
        {"codigo": CODE}, {"$set": {
            "tipo_operacion.precio_venta.precio_clp": 141000000,
            "tipo_operacion.precio_venta.precio_publicado": 141000000,
        }},
    )

    result = campaign.validate_campaign_price_integrity(
        db, db[campaign.LEDGER_COLLECTION].find_one({"_id": row["_id"]}),
    )

    assert result["required"] is True
    assert result["reason"] == "LIVE_PUBLISHED_PRICE_CHANGED"


def test_price_revalidation_cannot_change_frozen_campaign_percentage():
    row = ledger_row(send_status="SKIPPED_STALE_OR_MISMATCH")
    db = make_test_db(row)
    db[campaign.MASTER_COLLECTION].update_one(
        {"codigo": CODE},
        {"$set": {
            "estado.estado_prop360": "Activa",
            "estado.disponible_prop360": True,
            "tipo_operacion.precio_venta": {
                "precio_uf": 3427.5, "precio_clp": 140000000,
                "precio_publicado": 140000000, "moneda_publicada": "CLP",
            },
        }},
    )
    from owner_portal.price_revalidation import build_price_revalidation

    property_doc = db[campaign.MASTER_COLLECTION].find_one({"codigo": CODE})
    row["price_revalidation"] = build_price_revalidation(
        row, property_doc, validated_at="2026-10-09T12:00:00-03:00",
        uf_rate_clp=41130.94, uf_rate_date="2026-10-09",
    )
    row["price_revalidation"]["recommended_adjustment_pct"] = 10

    result = campaign.validate_campaign_price_integrity(db, row, property_doc=property_doc)

    assert result["required"] is True
    assert result["reason"] == "RECOMMENDATION_NOT_VERIFIABLE"


def test_inconsistent_campaign_arithmetic_stays_pending_and_hides_prices(monkeypatch):
    db = make_test_db(ledger_row(recommended_price=3000))
    url = registered_link(monkeypatch, db)
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))

    assert response.status_code == 200
    assert "El cálculo de la recomendación no coincide" in response.text
    assert "Resumen en 30 segundos" in response.text
    assert "3000" not in response.text
    assert "3.119,025 UF" not in response.text
    assert "REVISAR / CONFIRMAR AJUSTE" not in response.text


def test_price_integrity_accepts_production_price_block_without_operation_flags():
    row = ledger_row()
    db = make_test_db(row)
    db[campaign.MASTER_COLLECTION].update_one(
        {"codigo": CODE},
        {"$set": {"tipo_operacion": {
            "tipo": "Departamento",
            "precio_venta": {"precio_uf": row["current_price"]},
        }}},
    )
    result = campaign.validate_campaign_price_integrity(
        db, db[campaign.LEDGER_COLLECTION].find_one({"_id": row["_id"]}),
    )
    assert result["required"] is False
    assert result["live_current_price"] == row["current_price"]


def test_missing_live_uf_price_blocks_recommendation():
    row = ledger_row()
    db = make_test_db(row)
    db[campaign.MASTER_COLLECTION].update_one(
        {"codigo": CODE},
        {"$set": {"tipo_operacion.precio_venta": {"precio_clp": 140000000}}},
    )
    result = campaign.validate_campaign_price_integrity(
        db, db[campaign.LEDGER_COLLECTION].find_one({"_id": row["_id"]}),
    )
    assert result["required"] is True
    assert result["reason"] == "LIVE_PRICE_NOT_VERIFIABLE"


def test_authorized_row_does_not_offer_another_authorization(monkeypatch):
    db = make_test_db(ledger_row(authorization_status="PRICE_AUTHORIZED"))
    url = registered_link(monkeypatch, db)
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
    assert response.status_code == 200
    assert "Ajuste autorizado" in response.text


def test_portal_uses_the_same_v2_render_for_the_same_frozen_model():
    from analytics.owner_campaign_email_v2 import render_owner_campaign_email_v2

    frozen_model = {
        "code": CODE,
        "property_type": "Departamento",
        "commune": "Santiago",
        "property_heading": "Departamento · Santiago",
        "operation_raw": "VENTA",
        "operation_label": "Venta",
        "is_rental": False,
        "price_label": "3.428 UF",
        "recommended_price_label": "3.119 UF",
        "display_adjustment_label": "-9%",
        "recommendation_title": "Ajuste de precio sugerido",
        "single_diagnostic_text": "Diagnóstico congelado con sus señales originales.",
        "single_recommendation_text": "Recomendación congelada sin recalcular datos.",
        "single_document_copy": "Informe comercial disponible como contexto.",
        "feature_cards": [],
        "image": {"available": False, "url": "", "source": "NONE", "count": 0},
        "comparable": {"visible": False, "top3": [], "land_top3": [], "land_reference_visible": False},
        "market_reference": {"visible": False},
        "appraisal": {"visible": False, "kind": "NONE", "metrics": [], "adjustment_label": ""},
        "document": {"visible": True, "type": "COMMUNAL_MARKET_REPORT", "copy": ""},
        "activity_90d": {"state": "KNOWN_ZERO", "total_leads": 0, "conversations": 0, "visits": 0, "portals": []},
        "single_valuation_slots": [
            {"label": "PRECIO PUBLICADO", "value": "3.428 UF", "note": "Valor vigente", "emphasis": False, "icon": "price"},
            {"label": "REFERENCIAS", "value": "Análisis comparativo", "note": "Detalle de la muestra más abajo", "emphasis": False, "icon": "market"},
            {"label": "ACTIVIDAD COMERCIAL", "value": "0 leads registrados", "note": "Últimos 90 días", "emphasis": False, "icon": "position"},
            {"label": "NUEVO VALOR", "value": "3.119 UF", "note": "-9%", "emphasis": True, "icon": "new-value"},
        ],
        "cta": {"primary_url": "https://example.test/primary", "advisor_url": "https://example.test/advisor", "report_url": "https://example.test/report"},
    }
    email_html = render_owner_campaign_email_v2(
        [frozen_model], email="owner@example.com", executives=[{"name": "Ejecutivo", "email": "exec@example.com", "phone": ""}],
        base_url="https://www.procasa.cl",
    )
    portal_html = render_owner_campaign_email_v2(
        [frozen_model], email="owner@example.com", executives=[{"name": "Ejecutivo", "email": "exec@example.com", "phone": ""}],
        base_url="https://www.procasa.cl",
    )
    assert portal_html == email_html


def test_missing_historical_data_keeps_shared_template_without_inventing_signals():
    row = ledger_row(campaign_snapshot={
        "operation_resolved": "VENTA",
        "current_price": "3700.0",
        "recommended_adjustment_pct": "6",
        "recommended_price": "3478.0",
        "document_type": "NONE",
        "comparable_mode": "COMMUNAL_FALLBACK",
        "comparable_count": 0,
    })
    view = {
        "primary_url": "https://example.test/primary",
        "advisor_url": "https://example.test/advisor",
        "report_url": "",
        "current_price_label": "3.700 UF",
        "recommended_price_label": "3.478 UF",
        "recommendation_reason": "",
        "sent_at": None,
    }
    html = campaign.build_email_visual_landing_html(
        row, view, base_url="https://www.procasa.cl",
    )
    assert "3.700 UF" in html
    assert "3.478 UF" in html
    assert "Revisión comercial de tu propiedad" in html
    assert "Diagnóstico PROCASA" in html
    assert "Recomendación PROCASA" in html
    assert "Snapshot de campaña" not in html
    assert "Sin métricas históricas" not in html
    assert "Revisión de campaña" not in html
    assert "Tu propiedad frente a inmuebles comparables" not in html
    assert "MERCADO COMPARABLE" in html
    assert "0 leads registrados" not in html
    assert "VER INFORME" not in html


def test_private_portal_access_materialization_is_idempotent(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-test-secret")
    db = make_test_db()
    first = campaign.materialize_portal_accesses(
        db, campaign_id=CAMPAIGN, base_url="https://www.procasa.cl", source="EMAIL",
        now=datetime(2026, 10, 1, tzinfo=timezone.utc), persist=True,
    )
    issued_before = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})["portal_access"]["issued_at"]
    second = campaign.materialize_portal_accesses(
        db, campaign_id=CAMPAIGN, base_url="https://www.procasa.cl", source="EMAIL",
        now=datetime(2026, 10, 2, tzinfo=timezone.utc), persist=True,
    )
    assert first["records"][0]["private_url"] == second["records"][0]["private_url"]
    assert second["counts"]["created"] == 0
    assert second["counts"]["reused"] == 1
    stored = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    assert stored["portal_access"]["issued_at"] == issued_before


def test_email_and_whatsapp_share_one_access_identity(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-test-secret")
    db = make_test_db()
    email = campaign.materialize_portal_accesses(
        db, campaign_id=CAMPAIGN, base_url="https://www.procasa.cl",
        source="EMAIL", now=datetime(2026, 10, 1, tzinfo=timezone.utc), persist=True,
    )
    whatsapp = campaign.materialize_portal_accesses(
        db, campaign_id=CAMPAIGN, base_url="https://www.procasa.cl",
        source="WHATSAPP", now=datetime(2026, 10, 2, tzinfo=timezone.utc), persist=True,
    )
    email_url = email["records"][0]["private_url"]
    whatsapp_url = whatsapp["records"][0]["private_url"]
    assert urlsplit(email_url).path == urlsplit(whatsapp_url).path == f"/p/{campaign.short_key_for_token_hash(db[campaign.LEDGER_COLLECTION].find_one({'_id': f'{CAMPAIGN}:{CODE}'})['portal_access']['token_hash'])}"
    assert parse_qs(urlsplit(email_url).query)["source"] == ["EMAIL"]
    assert parse_qs(urlsplit(whatsapp_url).query)["source"] == ["WHATSAPP"]
    stored = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    assert "token_hash" in stored["portal_access"]
    assert "email" not in stored["portal_access"] and "whatsapp" not in stored["portal_access"]
    email_resolved = campaign.get_or_create_portal_url(
        db, stored, base_url="https://www.procasa.cl", source="EMAIL",
        now=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )
    whatsapp_resolved = campaign.get_or_create_portal_url(
        db, stored, base_url="https://www.procasa.cl", source="WHATSAPP",
        now=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )
    assert urlsplit(email_resolved).path == urlsplit(whatsapp_resolved).path


def test_short_landing_serves_monthly_report_without_mutating_exact_sent_email(monkeypatch):
    db = make_test_db()
    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    original_html = insert_email_artifact(db, row)
    url = registered_short_link(monkeypatch, db, source="WHATSAPP")
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))

    assert response.status_code == 200
    assert "Resumen en 30 segundos" in response.text
    assert "Informe comercial · Propietarios" in response.text
    assert "<style>.hero{color:#17175f}</style>" not in response.text
    assert "Departamento en Santiago" in response.text
    assert "Código 17005" in response.text
    assert "3.428 UF" in response.text
    assert "Revisar ajuste" in response.text
    assert "Escribir por WhatsApp" in response.text and "WhatsApp" in response.text
    # This fixture has no verified communal/appraisal source, so no document
    # link should be emitted now that the duplicate global support section is gone.
    assert "/campana/informe?token=" not in response.text
    visible = VisibleTextParser()
    visible.feed(response.text)
    visible_text = " ".join(visible.parts)
    assert "owner@example.com" not in visible_text
    assert CAMPAIGN not in visible_text
    from campanas.owner_campaign_live_events import verify_live_token
    parsed_links = [urlsplit(link) for link in visible.links]
    action_tokens = {
        parse_qs(link.query).get("accion", [""])[0]: parse_qs(link.query).get("token", [""])[0]
        for link in parsed_links if link.path == "/campana/respuesta"
    }
    for action in ("aceptar_rebaja",):
        assert verify_live_token(
            action_tokens[action], campaign_id=CAMPAIGN, property_code=CODE,
            recipient=EMAIL, action=action,
        )
    whatsapp_tokens = [
        parse_qs(link.query).get("token", [""])[0]
        for link in parsed_links if link.path == "/owner-portal/executive-whatsapp"
    ]
    assert len(whatsapp_tokens) == 2
    whatsapp_claims = [campaign.decode_live_token(token) for token in whatsapp_tokens]
    assert {item["action"] for item in whatsapp_claims} == {"executive_whatsapp_clicked"}
    assert {item["cta_placement"] for item in whatsapp_claims} == {"TOP", "STICKY"}
    report_tokens = [parse_qs(link.query)["token"][0] for link in parsed_links if link.path == "/campana/informe"]
    assert report_tokens == []  # No verified document source is present in this fixture.
    for token in (*action_tokens.values(), *report_tokens, *whatsapp_tokens):
        claims = campaign.decode_live_token(token)
        assert claims["source"] == "WHATSAPP"
        assert claims["interaction_surface"] == "OWNER_PORTAL"
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    stored_artifact = db[EMAIL_ARTIFACT_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    assert gzip.decompress(stored_artifact["html_gzip"]).decode("utf-8") == original_html
    stored = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    assert any(event.get("event") == "portal_opened" and event.get("source") == "WHATSAPP" for event in stored["events"])


def test_short_landing_rejects_unknown_expired_and_colliding_keys(monkeypatch):
    db = make_test_db()
    url = registered_short_link(monkeypatch, db)
    key = urlsplit(url).path.rsplit("/", 1)[-1]
    client = client_for(monkeypatch, db)
    assert client.get("/p/00000000000000000000000000000000").status_code == 404
    assert client.get(f"/p/{key}?source=WHATSAPP&extra=1").status_code == 404

    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    expired_access = dict(row["portal_access"], expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    db[campaign.LEDGER_COLLECTION].update_one({"_id": row["_id"]}, {"$set": {"portal_access": expired_access}})
    assert client.get(f"/p/{key}").status_code == 404

    collision_db = make_test_db()
    collision_url = registered_short_link(monkeypatch, collision_db)
    collision_key = urlsplit(collision_url).path.rsplit("/", 1)[-1]
    second_code = "17006"
    second = ledger_row(property_code=second_code, _id=f"{CAMPAIGN}:{second_code}", owner_email="second@example.com")
    # Deliberately collide on the 128-bit lookup prefix. The router must fail closed before token verification.
    second["portal_access"] = dict(collision_db[campaign.LEDGER_COLLECTION].find_one({"_id": row["_id"]})["portal_access"], token_hash=collision_key + "0" * 32)
    collision_db[campaign.LEDGER_COLLECTION].insert_one(second)
    collision_client = client_for(monkeypatch, collision_db)
    assert collision_client.get(f"/p/{collision_key}").status_code == 404


def test_short_landing_none_and_stale_status_rules(monkeypatch):
    none_db = make_test_db(ledger_row(document_type="NONE"))
    insert_email_artifact(none_db, none_db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"}), html=(
        '<html><head></head><body><table class="valuation-strip-single"><tr><td>3.428 UF</td></tr></table>'
        '<a href="https://old.example/campana/respuesta?accion=aceptar_rebaja&amp;token=x">REVISAR / CONFIRMAR AJUSTE</a>'
        '<a href="https://old.example/campana/respuesta?accion=contactar_ejecutivo&amp;token=y">REVISAR CON MI EJECUTIVO</a></body></html>'
    ))
    none_url = registered_short_link(monkeypatch, none_db)
    none_response = client_for(monkeypatch, none_db).get(none_url.replace("https://www.procasa.cl", ""))
    assert none_response.status_code == 200
    assert "/campana/informe?token=" not in none_response.text
    assert "VER INFORME" not in none_response.text

    stale_db = make_test_db(ledger_row(send_status="SKIPPED_STALE_OR_MISMATCH"))
    stale_url = registered_short_link(monkeypatch, stale_db)
    stale_response = client_for(monkeypatch, stale_db).get(stale_url.replace("https://www.procasa.cl", ""))
    assert stale_response.status_code == 200
    assert "3.119 UF" not in stale_response.text
    assert "REVISAR / CONFIRMAR AJUSTE" not in stale_response.text
    assert "Escribir por WhatsApp" in stale_response.text and "WhatsApp" in stale_response.text
    assert "/campana/informe?token=" not in stale_response.text


def test_short_landing_wave1_sent_communal_and_delivery_unknown(monkeypatch):
    for status in ("SENT", "DELIVERY_UNKNOWN"):
        wave1 = "owner_price_sucre_wave1_20260928"
        row = ledger_row(
            _id=f"{wave1}:{CODE}", campaign_id=wave1, send_status=status,
            document_type="COMMUNAL_MARKET_REPORT",
        )
        db = make_test_db(row)
        source_html = insert_email_artifact(db, db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{wave1}:{CODE}"}))
        url = registered_short_link(monkeypatch, db, campaign_id=wave1)
        response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
        assert response.status_code == 200
        assert "Resumen en 30 segundos" in response.text
        assert "Informe comercial · Propietarios" in response.text
        assert "3.428 UF" in response.text
        assert "Revisar ajuste" in response.text
        # No matching communal dataset is present in this fixture.
        assert "/campana/informe?token=" not in response.text
        stored = db[EMAIL_ARTIFACT_COLLECTION].find_one({"_id": f"{wave1}:{CODE}"})
        assert gzip.decompress(stored["html_gzip"]).decode("utf-8") == source_html


def test_sent_short_portal_falls_back_to_verified_campaign_snapshot(monkeypatch):
    missing_db = make_test_db()
    missing_url = registered_short_link(monkeypatch, missing_db)
    missing_response = client_for(monkeypatch, missing_db).get(missing_url.replace("https://www.procasa.cl", ""))
    assert missing_response.status_code == 200
    assert "3.428 UF" in missing_response.text

    corrupt_db = make_test_db()
    corrupt_row = corrupt_db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    insert_email_artifact(corrupt_db, corrupt_row)
    corrupt_db[EMAIL_ARTIFACT_COLLECTION].update_one(
        {"_id": f"{CAMPAIGN}:{CODE}"}, {"$set": {"original_html_sha256": "0" * 64}},
    )
    corrupt_url = registered_short_link(monkeypatch, corrupt_db)
    corrupt_response = client_for(monkeypatch, corrupt_db).get(corrupt_url.replace("https://www.procasa.cl", ""))
    assert corrupt_response.status_code == 200
    assert "3.428 UF" in corrupt_response.text


def test_sent_portal_ignores_artifacts_with_identity_or_message_id_mismatch(monkeypatch):
    db = make_test_db()
    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    insert_email_artifact(db, row)
    db[EMAIL_ARTIFACT_COLLECTION].update_one(
        {"_id": f"{CAMPAIGN}:{CODE}"}, {"$set": {"owner_email": "other@example.test"}},
    )
    url = registered_short_link(monkeypatch, db)
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
    assert response.status_code == 200
    assert "Resumen en 30 segundos" in response.text

    mid_db = make_test_db()
    mid_row = mid_db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    insert_email_artifact(mid_db, mid_row)
    mid_db[EMAIL_ARTIFACT_COLLECTION].update_one(
        {"_id": f"{CAMPAIGN}:{CODE}"}, {"$set": {"message_id": "<different-message@example.test>"}},
    )
    mid_url = registered_short_link(monkeypatch, mid_db)
    mid_response = client_for(monkeypatch, mid_db).get(mid_url.replace("https://www.procasa.cl", ""))
    assert mid_response.status_code == 200
    assert "Resumen en 30 segundos" in mid_response.text


def test_short_landing_preserves_email_source_in_rewritten_tokens(monkeypatch):
    db = make_test_db()
    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    original = insert_email_artifact(db, row)
    url = registered_short_link(monkeypatch, db, source="EMAIL")
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
    assert response.status_code == 200
    assert "Resumen en 30 segundos" in response.text
    parser = VisibleTextParser()
    parser.feed(response.text)
    from campanas.owner_campaign_live_events import decode_live_token
    placements_by_action = {"aceptar_rebaja": [], "executive_whatsapp_clicked": [], "ver_informe": []}
    for link in parser.links:
        parsed = urlsplit(link)
        query = parse_qs(parsed.query)
        token = query.get("token", [""])[0]
        if token:
            claims = decode_live_token(token)
            assert claims["source"] == "EMAIL"
            assert claims["interaction_surface"] == "OWNER_PORTAL"
            placements_by_action[claims["action"]].append(claims["cta_placement"])
    assert {action: sorted(values) for action, values in placements_by_action.items()} == {
        "aceptar_rebaja": ["STICKY", "TOP"],
        "executive_whatsapp_clicked": ["STICKY", "TOP"],
        "ver_informe": [],
    }


def test_portal_cta_placements_keep_whatsapp_source_and_event_attribution(monkeypatch):
    from campanas.owner_campaign_live_events import decode_live_token, persist_live_event

    for source in ("EMAIL", "WHATSAPP"):
        db = make_test_db()
        row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
        insert_email_artifact(db, row)
        url = registered_short_link(monkeypatch, db, source=source)
        response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
        assert response.status_code == 200
        parser = VisibleTextParser()
        parser.feed(response.text)
        claims_by_placement = {}
        for link in parser.links:
            if urlsplit(link).scheme in {"mailto", "tel"}:
                continue
            token = parse_qs(urlsplit(link).query).get("token", [""])[0]
            claims = decode_live_token(token) if token else None
            assert claims and claims["source"] == source
            assert claims["interaction_surface"] == "OWNER_PORTAL"
            claims_by_placement.setdefault(claims.get("cta_placement"), []).append(claims)
        # No verified document source exists in this fixture, so ORIGINAL is absent.
        assert {"TOP", "STICKY"}.issubset(claims_by_placement)
        assert {claims["action"] for claims in claims_by_placement["STICKY"]} == {
            "aceptar_rebaja", "executive_whatsapp_clicked",
        }
        top = next(claims for claims in claims_by_placement["TOP"] if claims["action"] == "aceptar_rebaja")
        assert top["action"] == "aceptar_rebaja"
        event_claims = {**top, "cta_placement": "TOP"}
        persist_live_event(db, event_claims, event="cta_clicked", action="aceptar_rebaja")
        stored = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
        event = next(item for item in stored["events"] if item["event"] == "cta_clicked")
        assert event["cta_placement"] == "TOP"
        assert event["source"] == source


def test_href_rewriter_preserves_all_non_href_markup_and_supports_email_source():
    original = ('<html><head></head><body><table style="x" class="valuation-strip-single"><tr><td>value</td></tr></table>'
                '<a class="x" href="https://old/campana/respuesta?accion=aceptar_rebaja&amp;token=old" title="keep">'
                'text</a><a href="https://old/campana/respuesta?accion=contactar_ejecutivo&amp;token=old">advisor</a></body></html>')
    view = {"primary_url": "/campana/respuesta?accion=aceptar_rebaja&token=new1",
            "advisor_url": "/campana/respuesta?accion=contactar_ejecutivo&token=new2",
            "top_primary_url": "/campana/respuesta?accion=aceptar_rebaja&token=top",
            "sticky_primary_url": "/campana/respuesta?accion=aceptar_rebaja&token=sticky",
            "sticky_advisor_url": "/campana/respuesta?accion=contactar_ejecutivo&token=advisor-sticky",
            "report_url": "", "document_available": False}
    rewritten, count = transform_sent_email_to_portal_html(original, view, "EMAIL")
    assert count == 2
    assert masked_html_parity(original, rewritten)
    assert "title=" in rewritten and ">text</a>" in rewritten
    assert rewritten.index("valuation-strip-single") < rewritten.index("OWNER_PORTAL_UI:CTA_TOP:START") < rewritten.index("</body>")
    assert rewritten.count('data-owner-portal-ui="CTA_TOP"') == 1
    assert rewritten.count('data-owner-portal-ui="STICKY_ACTION_BAR"') == 1


def test_all_stale_sample_rows_keep_safe_renderer_without_price_authorization(monkeypatch):
    for i in range(33):
        code = str(18000 + i)
        stale = ledger_row(
            _id=f"{CAMPAIGN}:{code}", property_code=code,
            send_status="SKIPPED_STALE_OR_MISMATCH", document_type="NONE",
        )
        db = make_test_db(stale)
        url = registered_short_link(monkeypatch, db)
        response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
        assert response.status_code == 200
        assert "REVISAR / CONFIRMAR AJUSTE" not in response.text
        assert "Escribir por WhatsApp" in response.text and "WhatsApp" in response.text
        assert "/campana/informe?token=" not in response.text
        parser = VisibleTextParser()
        parser.feed(response.text)
        sticky = next(attrs for tag, attrs in parser.tags if attrs.get("id") == "owner-portal-sticky-actions")
        assert sticky.get("aria-label") == "Acciones rápidas"
        visible_text = " ".join(parser.parts)
        assert "La recomendación de precio está en validación" in visible_text
        assert "no es posible autorizar el ajuste mientras termina la revisión" in visible_text
        from campanas.owner_campaign_live_events import decode_live_token
        sticky_claims = [
            decode_live_token(parse_qs(urlsplit(link).query)["token"][0])
            for link in parser.links if "token=" in link
        ]
        assert any(item and item["action"] == "executive_whatsapp_clicked" and item["cta_placement"] == "STICKY" for item in sticky_claims)
        assert not any(item and item["action"] == "aceptar_rebaja" for item in sticky_claims)


def test_landing_does_not_invent_missing_clp_snapshot_values(monkeypatch):
    db = make_test_db(ledger_row(current_price_clp=None, recommended_price_clp=None))
    original = insert_email_artifact(db, db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"}))
    url = registered_short_link(monkeypatch, db)
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
    assert response.status_code == 200
    assert "Resumen en 30 segundos" in response.text
    assert "3.428 UF" in response.text
    assert "$ 140.000.000" not in response.text


def test_materialization_accepts_noncanonical_email_case_without_rewriting_send_data(monkeypatch):
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-test-secret")
    db = make_test_db(ledger_row(owner_email="Owner@Example.com"))
    result = campaign.materialize_portal_accesses(
        db, campaign_id=CAMPAIGN, base_url="https://www.procasa.cl",
        source="EMAIL", persist=True,
    )
    assert result["counts"]["created"] == 1
    stored = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    assert stored["owner_email"] == "Owner@Example.com"
    assert stored["send_status"] == "SENT"
    assert stored["portal_access"]["status"] == "ACTIVE"


def test_expired_or_wrong_recipient_access_rejected(monkeypatch):
    db = make_test_db()
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-test-secret")
    now = datetime.now(timezone.utc)
    expired = now - timedelta(seconds=5)
    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    token = campaign.issue_portal_token(row, expires_at=int(expired.timestamp()))
    claims = campaign.decode_live_token(token) if int(expired.timestamp()) > int(now.timestamp()) else None
    assert claims is None
    access = {
        "token_id": "expired-test", "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "issued_at": now - timedelta(days=91), "expires_at": expired,
        "revoked_at": None, "status": "ACTIVE", "purpose": "owner_portal_view",
    }
    db[campaign.LEDGER_COLLECTION].update_one({"_id": row["_id"]}, {"$set": {"portal_access": access}})
    assert campaign.verify_portal_request(db, property_code=CODE, token=token) is None


def test_ambiguous_identity_never_gets_a_private_portal(monkeypatch):
    db = make_test_db(ledger_row(send_status="AMBIGUOUS_OWNER_IDENTITY"))
    monkeypatch.setenv("OWNER_CAMPAIGN_PRODUCTION_TOKEN_SECRET", "local-test-secret")
    result = campaign.materialize_portal_accesses(
        db, campaign_id=CAMPAIGN, base_url="https://www.procasa.cl",
        source="EMAIL", persist=True,
    )
    assert result["counts"]["eligible"] == 0
    assert result["counts"]["blocked"] == 1
    stored = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    assert "portal_access" not in stored



def test_executive_whatsapp_is_signed_click_only_and_preserves_attribution(monkeypatch):
    from bs4 import BeautifulSoup
    from campanas.owner_campaign_live_events import decode_live_token
    for source in ("EMAIL", "WHATSAPP"):
        db = make_test_db(ledger_row(
            executive_phone="+56 9 1234 5678", executive_name="Mariela Arriagada",
            owner_name="Camila Pérez",
        ))
        url = registered_short_link(monkeypatch, db, source=source)
        client = client_for(monkeypatch, db)
        response = client.get(url.replace("https://www.procasa.cl", ""))
        soup = BeautifulSoup(response.text, "html.parser")
        top = soup.select_one('a[data-cta-placement="TOP"][data-cta-type="WHATSAPP"]')
        sticky = soup.select_one('a[data-cta-placement="STICKY"][data-cta-type="WHATSAPP"]')
        assert top and sticky and "wa.me" not in top["href"] and "wa.me" not in sticky["href"]
        assert "Escribir por WhatsApp" in top.get_text(" ", strip=True)
        assert sticky.get_text(" ", strip=True) == "WhatsApp"
        top_claims = decode_live_token(parse_qs(urlsplit(top["href"]).query)["token"][0])
        sticky_claims = decode_live_token(parse_qs(urlsplit(sticky["href"]).query)["token"][0])
        assert top_claims["source"] == source and top_claims["cta_placement"] == "TOP"
        assert sticky_claims["source"] == source and sticky_claims["cta_placement"] == "STICKY"
        assert top_claims["action"] == sticky_claims["action"] == "executive_whatsapp_clicked"
        assert not soup.select_one('a[data-cta-placement="EXECUTIVE"]')
        assert "Teléfono" in soup.get_text() and "+56 9 1234 5678" in soup.get_text()
        assert "Correo" in soup.get_text() and "exec@example.com" in soup.get_text()
        assert not soup.select_one(".executive-phone[href]") and not soup.select_one(".executive-email[href]")
        assert "Hablar con ejecutivo" not in soup.get_text()

        clicks = [
            (top, "TOP", "00000000-0000-4000-8000-000000000001"),
            (top, "TOP", "00000000-0000-4000-8000-000000000001"),
            (top, "TOP", "00000000-0000-4000-8000-000000000002"),
            (sticky, "STICKY", "00000000-0000-4000-8000-000000000003"),
        ]
        for link, placement, client_event_id in clicks:
            url = link["href"].replace("https://www.procasa.cl", "")
            url += f"&owner_portal_client_event_id={client_event_id}&owner_portal_session_id=00000000-0000-4000-8000-000000000099"
            click = client.get(url, follow_redirects=False)
            assert click.status_code == 302
            assert click.headers["location"].startswith("https://wa.me/56912345678?")
            message = parse_qs(urlsplit(click.headers["location"]).query)["text"][0]
            assert message == (
                "Hola Mariela, te contacto por el informe comercial de la propiedad código 17005. "
                "Quisiera conversar contigo sobre la recomendación de precio y las alternativas disponibles."
            )
            assert "Camila" not in message and "soy Mariela" not in message
        row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
        events = [event for event in row["events"] if event["event"] == "executive_whatsapp_clicked"]
        assert len(events) == 3
        assert [event["placement"] for event in events] == ["TOP", "TOP", "STICKY"]
        assert [event["click_sequence_number"] for event in events] == [1, 2, 3]
        assert [event["is_first_whatsapp_click"] for event in events] == [True, False, False]
        assert all(event["source"] == source for event in events)
        assert all(event["interaction_surface"] == "OWNER_PORTAL" for event in events)
        assert all(event["interaction_channel"] == source and event["channel"] == source for event in events)
        assert all(event["report_period"] and event["snapshot_hash"] for event in events)
        assert all(event["session_id"] for event in events)
        assert [event["control_id"] for event in events] == ["whatsapp_top", "whatsapp_top", "whatsapp_sticky"]
        assert all(event["event_type"] == "OWNER_WHATSAPP_CLICK" for event in events)
        assert all(event["cta_type"] == "WHATSAPP" and event["intent"] == "INTENT_TO_CONTACT" for event in events)
        assert all(event["property_code"] == CODE and event["campaign_id"] == CAMPAIGN for event in events)
        assert all(event["executive_name"] == "Mariela Arriagada" and event["executive_email"] == "exec@example.com" for event in events)
        assert all(event["owner_identity_hash"] and event["qa_mode"] is False for event in events)
        assert row["authorization_status"] == "PENDING"
        assert not any(event["event"] in {"price_authorized", "advisor_review_requested"} for event in row["events"])
        db[campaign.LEDGER_COLLECTION].update_one({"_id": row["_id"]}, {"$set": {"portal_access.revoked_at": datetime.now(timezone.utc)}})
        denied = client.get(top["href"].replace("https://www.procasa.cl", ""), follow_redirects=False)
        assert denied.status_code == 404


def test_executive_whatsapp_rejects_tampered_token(monkeypatch):
    db = make_test_db(ledger_row(executive_phone="+56 9 1234 5678"))
    registered_short_link(monkeypatch, db)
    client = client_for(monkeypatch, db)
    assert client.get("/owner-portal/executive-whatsapp?token=invalid").status_code == 404
    assert db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})["events"] == []


def test_executive_whatsapp_redirect_survives_telemetry_failure(monkeypatch):
    from bs4 import BeautifulSoup

    db = make_test_db(ledger_row(executive_phone="+56 9 1234 5678"))
    url = registered_short_link(monkeypatch, db)
    client = client_for(monkeypatch, db)
    landing = client.get(url.replace("https://www.procasa.cl", ""))
    soup = BeautifulSoup(landing.text, "html.parser")
    link = soup.select_one('a[data-cta-placement="TOP"][data-cta-type="WHATSAPP"]')
    assert link

    def fail_telemetry(*_args, **_kwargs):
        raise RuntimeError("temporary mongo outage")

    monkeypatch.setattr("campanas.owner_campaign_live_events.persist_owner_whatsapp_click", fail_telemetry)
    response = client.get(link["href"].replace("https://www.procasa.cl", ""), follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://wa.me/56912345678?")


def test_executive_whatsapp_rejects_client_supplied_placement(monkeypatch):
    db = make_test_db(ledger_row(executive_phone="+56 9 1234 5678"))
    registered_short_link(monkeypatch, db)
    client = client_for(monkeypatch, db)
    response = client.get("/owner-portal/executive-whatsapp?token=invalid&placement=TOP")
    assert response.status_code == 404
    assert db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})["events"] == []


def test_missing_assigned_phone_hides_whatsapp_without_changing_adjustment_cta(monkeypatch):
    from bs4 import BeautifulSoup
    db = make_test_db(ledger_row(executive_phone=""))
    url = registered_short_link(monkeypatch, db)
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert not soup.select('[data-cta-type="WHATSAPP"]')
    assert "Escribir por WhatsApp" not in soup.get_text()
    assert "WhatsApp" not in soup.get_text()
    primary = soup.select_one('#owner-portal-top-actions .button-primary[href]')
    assert primary and "Revisar ajuste" in primary.get_text(" ", strip=True)
    claims = campaign.decode_live_token(parse_qs(urlsplit(primary["href"]).query)["token"][0])
    assert claims["action"] == "aceptar_rebaja" and claims["cta_placement"] == "TOP"
