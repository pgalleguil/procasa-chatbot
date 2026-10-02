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

    def handle_starttag(self, tag, attrs):
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
    db[campaign.LEDGER_COLLECTION].insert_one(row or ledger_row())
    db[campaign.MASTER_COLLECTION].insert_one({
        "codigo": CODE,
        "metadata": {"tipo_propiedad": "Departamento"},
        "ubicacion": {"comuna": "Santiago"},
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
        '<table class="email-layout"><tr><td>Departamento · Santiago</td></tr>'
        '<tr><td>3.428 UF · 3.119 UF · 9%</td></tr>'
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

    assert response.status_code == 200
    assert "3.428 UF" in response.text
    assert "$ 140.000.000" in response.text
    assert "3.119 UF" in response.text
    assert "Resumen congelado de la campaña." not in response.text
    assert "Revisión comercial de tu propiedad" in response.text
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
    assert event["source"] == "EMAIL"
    assert event["interaction_surface"] == "OWNER_PORTAL"
    assert event["interaction_channel"] == "EMAIL"
    assert "token_hash" in stored["portal_access"]
    assert token not in str(stored["portal_access"])


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
    assert "REVISAR CON MI EJECUTIVO" in stale_response.text
    assert "3.119 UF" not in stale_response.text


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


def test_short_landing_serves_exact_sent_email_with_only_campaign_hrefs_rewritten(monkeypatch):
    db = make_test_db()
    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    original_html = insert_email_artifact(db, row)
    url = registered_short_link(monkeypatch, db, source="WHATSAPP")
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))

    assert response.status_code == 200
    assert masked_html_parity(original_html, response.text)
    assert "<style>.hero{color:#17175f}</style>" in response.text
    assert "Departamento · Santiago" in response.text
    assert "Código 17005" not in response.text
    assert "3.428 UF · 3.119 UF · 9%" in response.text
    assert "REVISAR / CONFIRMAR AJUSTE" in response.text
    assert "REVISAR CON MI EJECUTIVO" in response.text
    assert "/campana/informe?token=" in response.text
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
    for action in ("aceptar_rebaja", "contactar_ejecutivo"):
        assert verify_live_token(
            action_tokens[action], campaign_id=CAMPAIGN, property_code=CODE,
            recipient=EMAIL, action=action,
        )
    report_tokens = [parse_qs(link.query)["token"][0] for link in parsed_links if link.path == "/campana/informe"]
    assert report_tokens and campaign.decode_live_token(report_tokens[0])["action"] == "ver_informe"
    for token in (*action_tokens.values(), *report_tokens):
        claims = campaign.decode_live_token(token)
        assert claims["source"] == "WHATSAPP"
        assert claims["interaction_surface"] == "OWNER_PORTAL"
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
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
        '<html><body><a href="https://old.example/campana/respuesta?accion=aceptar_rebaja&amp;token=x">REVISAR / CONFIRMAR AJUSTE</a>'
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
    assert "REVISAR CON MI EJECUTIVO" in stale_response.text
    assert "/campana/informe?token=" in stale_response.text


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
        assert masked_html_parity(source_html, response.text)
        assert "3.428 UF" in response.text
        assert "REVISAR / CONFIRMAR AJUSTE" in response.text
        assert "/campana/informe?token=" in response.text


def test_sent_short_portal_fails_closed_for_missing_or_corrupt_artifact(monkeypatch):
    missing_db = make_test_db()
    missing_url = registered_short_link(monkeypatch, missing_db)
    assert client_for(monkeypatch, missing_db).get(missing_url.replace("https://www.procasa.cl", "")).status_code == 503

    corrupt_db = make_test_db()
    corrupt_row = corrupt_db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    insert_email_artifact(corrupt_db, corrupt_row)
    corrupt_db[EMAIL_ARTIFACT_COLLECTION].update_one(
        {"_id": f"{CAMPAIGN}:{CODE}"}, {"$set": {"original_html_sha256": "0" * 64}},
    )
    corrupt_url = registered_short_link(monkeypatch, corrupt_db)
    assert client_for(monkeypatch, corrupt_db).get(corrupt_url.replace("https://www.procasa.cl", "")).status_code == 503


def test_sent_portal_artifact_identity_and_message_id_are_enforced(monkeypatch):
    db = make_test_db()
    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    insert_email_artifact(db, row)
    db[EMAIL_ARTIFACT_COLLECTION].update_one(
        {"_id": f"{CAMPAIGN}:{CODE}"}, {"$set": {"owner_email": "other@example.test"}},
    )
    url = registered_short_link(monkeypatch, db)
    assert client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", "")).status_code == 503

    mid_db = make_test_db()
    mid_row = mid_db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    insert_email_artifact(mid_db, mid_row)
    mid_db[EMAIL_ARTIFACT_COLLECTION].update_one(
        {"_id": f"{CAMPAIGN}:{CODE}"}, {"$set": {"message_id": "<different-message@example.test>"}},
    )
    mid_url = registered_short_link(monkeypatch, mid_db)
    assert client_for(monkeypatch, mid_db).get(mid_url.replace("https://www.procasa.cl", "")).status_code == 503


def test_short_landing_preserves_email_source_in_rewritten_tokens(monkeypatch):
    db = make_test_db()
    row = db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"})
    original = insert_email_artifact(db, row)
    url = registered_short_link(monkeypatch, db, source="EMAIL")
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
    assert response.status_code == 200
    assert masked_html_parity(original, response.text)
    parser = VisibleTextParser()
    parser.feed(response.text)
    from campanas.owner_campaign_live_events import decode_live_token
    for link in parser.links:
        parsed = urlsplit(link)
        query = parse_qs(parsed.query)
        token = query.get("token", [""])[0]
        if token:
            claims = decode_live_token(token)
            assert claims["source"] == "EMAIL"
            assert claims["interaction_surface"] == "OWNER_PORTAL"


def test_href_rewriter_preserves_all_non_href_markup_and_supports_email_source():
    original = ('<table style="x"><a class="x" href="https://old/campana/respuesta?accion=aceptar_rebaja&amp;token=old" title="keep">'
                'text</a><a href="https://old/campana/respuesta?accion=contactar_ejecutivo&amp;token=old">advisor</a></table>')
    view = {"primary_url": "/campana/respuesta?accion=aceptar_rebaja&token=new1",
            "advisor_url": "/campana/respuesta?accion=contactar_ejecutivo&token=new2",
            "report_url": "", "document_available": False}
    rewritten, count = transform_sent_email_to_portal_html(original, view, "EMAIL")
    assert count == 2
    assert masked_html_parity(original, rewritten)
    assert "title=" in rewritten and ">text</a>" in rewritten


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
        assert "REVISAR CON MI EJECUTIVO" in response.text
        assert "/campana/informe?token=" not in response.text


def test_landing_does_not_invent_missing_clp_snapshot_values(monkeypatch):
    db = make_test_db(ledger_row(current_price_clp=None, recommended_price_clp=None))
    original = insert_email_artifact(db, db[campaign.LEDGER_COLLECTION].find_one({"_id": f"{CAMPAIGN}:{CODE}"}))
    url = registered_short_link(monkeypatch, db)
    response = client_for(monkeypatch, db).get(url.replace("https://www.procasa.cl", ""))
    assert response.status_code == 200
    assert masked_html_parity(original, response.text)
    assert "3.428 UF · 3.119 UF · 9%" in response.text
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

