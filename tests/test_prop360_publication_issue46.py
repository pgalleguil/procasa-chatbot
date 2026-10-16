from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import mongomock
import pytest

from chatbot import publication_sync
from chatbot.link_extractor import resolver_referencia_propiedad
from chatbot.publication_data import merge_publication_snapshot
from chatbot.publication_sync import (
    PROVIDERS,
    _build_incoming,
    _incoming_conflicts,
    _pending_providers,
    _retry_after,
    _existing_claims,
    parse_proppit_destinations,
    run_publication_reconciliation,
    run_publication_sync_batch,
    safe_fetch_html,
    sync_single_property_publications,
)
from chatbot.publication_import import normalize_authorized_publication_rows
from chatbot.publication_audit import run_sucre_publication_audit
from chatbot.property_lookup import build_property_alias, lookup_property_link
from scraping_convecta.scraping_prop360_ficha_completa import parse_publicaciones_json, upsert_ficha


CODE = "16479"
OLD_MLC = "MLC-3842833828"
NEW_MLC = "MLC-4548780920"
OLD_URL = "http://casa.mercadolibre.cl/MLC-3842833828-casa-en-venta-de-8-dorm-en-lo-prado-_JM"
NEW_URL = "https://casa.mercadolibre.cl/MLC-4548780920-casa-en-venta-de-8-dorm-en-lo-prado-_JM"
AGGREGATOR = (
    "https://links.proppit.com/links?crm=real_time_convecta&country=cl&"
    "id=10543041010016479&agency=ProcasaSucre-1-543-7"
)
DESTINATIONS = {
    "icasas": "https://www.icasas.cl/re/a0319b98-40d3-45d2-84ee-478bebefe66b",
    "trovit": "https://casas.trovit.cl/detail/a0319b98-40d3-45d2-84ee-478bebefe66b",
    "mitula": "https://casas.mitula.cl/adform/a0319b98-40d3-45d2-84ee-478bebefe66b",
    "nuroa": "https://www.nuroa.cl/adform/a0319b98-40d3-45d2-84ee-478bebefe66b",
    "nestoria": "https://www.nestoria.cl/adform/a0319b98-40d3-45d2-84ee-478bebefe66b",
}


def _doc(code=CODE, *, office="PROCASA SUCRE", active=True, publications=None):
    return {
        "codigo": code,
        "oficina_id": 7,
        "oficina_nombre": office,
        "disponible_prop360": active,
        "estado": {"estado_prop360": "Activa" if active else "Inactiva", "oficina": office},
        "resumen": {"oficina": office},
        "publicaciones": deepcopy(publications or {}),
    }


def _verified_alias(url, portal, external_id=None, operation="V", status="active"):
    return build_property_alias(
        url, portal, operation, external_id,
        active=status == "active", publication_status=status, resolvable=True,
        source="prop360_portals_api", verification_status="source_verified",
        source_url="https://prop360.example/ficha/16479",
        first_seen_at="2026-10-16T10:00:00+00:00",
        last_seen_at="2026-10-16T10:00:00+00:00",
        verified_at="2026-10-16T10:00:00+00:00",
    )


def _payload(mlc_url=NEW_URL, aggregator=AGGREGATOR):
    pubs = [{
        "Portal": "Portal Inmobiliario",
        "Publications": [{
            "Url": mlc_url, "Code": mlc_url.split("MLC-")[-1].split("-")[0],
            "CodeUnique": "10543041010016479", "PropOperation": "V",
            "Status": 1, "ExpirationDate": "2027-10-07T00:00:00",
        }],
    }]
    if aggregator:
        pubs.append({"Portal": "Proppit", "Publications": [{
            "Url": aggregator, "Code": "10543041010016479",
            "CodeUnique": "10543041010016479", "PropOperation": "V", "Status": 1,
        }]})
    return {"publications": pubs}


class FakeProp360:
    def __init__(self, rows=None, payloads=None):
        self.rows = rows or [{"codigo": CODE, "estado": "Activa"}]
        self.payloads = payloads or {CODE: _payload()}
        self.last_listing_meta = {
            "response_valid": True, "office_id": 7, "complete": True,
            "reported_total": len(self.rows),
        }
        self.portal_calls = []

    def login(self):
        return True

    def fetch_listing(self, office_id):
        assert office_id == 7
        return self.rows

    def get_portales(self, codigo):
        self.portal_calls.append(str(codigo))
        return deepcopy(self.payloads[str(codigo)])


class IntermittentFakeProp360(FakeProp360):
    def __init__(self, *, fail_code="2", **kwargs):
        rows = [{"codigo": code, "estado": "Activa"} for code in ("1", "2", "3")]
        payloads = {code: {"publications": [{"Portal": "Portal Inmobiliario", "Publications": [{
            "Url": f"https://casa.mercadolibre.cl/MLC-{1000000000 + int(code)}-venta-_JM",
            "Code": str(1000000000 + int(code)), "PropOperation": "V", "Status": 1,
        }]}]} for code in ("1", "2", "3")}
        super().__init__(rows=rows, payloads=payloads)
        self.fail_code = fail_code
        self.calls = []

    def get_portales(self, codigo):
        code = str(codigo)
        self.calls.append(code)
        if self.fail_code == code:
            raise TimeoutError("simulated Prop360 timeout")
        return deepcopy(self.payloads[code])


class ReadOnlyCollection:
    def __init__(self, collection):
        self._collection = collection
        self.writes = 0

    def __getattr__(self, name):
        value = getattr(self._collection, name)
        if name in {"update_one", "update_many", "insert_one", "insert_many", "delete_one", "delete_many", "replace_one", "find_one_and_update"}:
            def counted(*args, **kwargs):
                self.writes += 1
                raise AssertionError("dry-run attempted Mongo write")
            return counted
        return value


class ReadOnlyDB:
    def __init__(self, db):
        self._db = db
        self.wrapped = {}

    def __getitem__(self, name):
        if name not in self.wrapped:
            self.wrapped[name] = ReadOnlyCollection(self._db[name])
        return self.wrapped[name]


def test_all_eight_16479_references_resolve_via_production_resolver(monkeypatch):
    aliases = [
        _verified_alias(OLD_URL, "mercadolibre", OLD_MLC, status="historical"),
        _verified_alias(NEW_URL, "mercadolibre", NEW_MLC),
        _verified_alias(AGGREGATOR, "proppit", "10543041010016479"),
    ]
    aliases.extend(_verified_alias(url, provider) for provider, url in DESTINATIONS.items())
    db = mongomock.MongoClient().db
    db.universo_cartera_prop360.insert_one(_doc(publications={"aliases": aliases}))
    monkeypatch.setattr("chatbot.link_extractor.get_db", lambda: db)

    references = [OLD_MLC, NEW_MLC, AGGREGATOR, *DESTINATIONS.values()]
    for reference in references:
        result = resolver_referencia_propiedad(reference, use_legacy_lookup=False)
        assert result["status"] == "resolved", (reference, result)
        assert result["property"]["codigo"] == CODE
    for provider, url in DESTINATIONS.items():
        prop, meta = lookup_property_link(db, url)
        assert prop["codigo"] == CODE
        assert meta["portal"] == provider


def test_prop360_and_proppit_sync_merge_resolves_all_eight_links(monkeypatch):
    from chatbot.publication_sync import _build_incoming

    db = mongomock.MongoClient().db
    existing = _doc(publications={
        "portal_inmobiliario": {"url_mercado_libre": OLD_URL,
                                 "publicaciones": {"V": {"url": OLD_URL, "code": OLD_MLC, "publicada": True}}},
        "proppit": {"url_proppit": AGGREGATOR, "publicaciones": {"V": {"url": AGGREGATOR}}},
    })
    html = "<html><body>" + "".join(
        f'<a href="{url}">portal</a>' for url in DESTINATIONS.values()
    ) + "</body></html>"
    incoming, _payload, _meta = _build_incoming(
        FakeProp360(), CODE, existing,
        fetcher=lambda _url: {"status": "http_200", "html": html},
        checked_at="2026-10-16T12:00:00+00:00",
    )
    merged = merge_publication_snapshot(existing["publicaciones"], incoming,
                                        observed_at="2026-10-16T12:00:00+00:00")
    db.universo_cartera_prop360.insert_one(_doc(publications=merged))
    monkeypatch.setattr("chatbot.link_extractor.get_db", lambda: db)

    references = [OLD_MLC, NEW_MLC, AGGREGATOR, *DESTINATIONS.values()]
    for reference in references:
        result = resolver_referencia_propiedad(reference, use_legacy_lookup=False)
        assert result["status"] == "resolved", (reference, result)
        assert result["property"]["codigo"] == CODE


def test_unverified_new_mlc_is_not_resolvable_and_expired_alias_is():
    db = mongomock.MongoClient().db
    old = _verified_alias(OLD_URL, "mercadolibre", OLD_MLC, status="historical")
    old.update(activa=False, publication_status="historical", resolvable=True)
    db.universo_cartera_prop360.insert_one(_doc(publications={"aliases": [old]}))
    assert lookup_property_link(db, OLD_MLC)[0]["codigo"] == CODE
    prop, _ = lookup_property_link(db, NEW_MLC)
    assert prop is None


def test_historical_alias_status_precedes_accumulated_legacy_url_array():
    db = mongomock.MongoClient().db
    old = _verified_alias(OLD_URL, "mercadolibre", OLD_MLC, status="historical")
    current = _verified_alias(NEW_URL, "mercadolibre", NEW_MLC, status="active")
    doc = _doc(publications={
        "portal_inmobiliario": {"url_mercado_libre": NEW_URL,
                                 "urls_mercado_libre": [OLD_URL, NEW_URL],
                                 "publicaciones": {"V": {"url": NEW_URL, "code": NEW_MLC}}},
        "aliases": [old, current],
    })
    db.universo_cartera_prop360.insert_one(doc)
    prop, meta = lookup_property_link(db, OLD_URL)
    assert prop["codigo"] == CODE
    assert meta["match_method"] == "historical_alias_url"
    assert meta["publication_status"] == "historical"


def test_reconciliation_diff_reports_current_mlc_separately_from_history():
    from chatbot.publication_sync import _difference

    old_publications = {
        "portal_inmobiliario": {
            "url_mercado_libre": OLD_URL,
            "urls_mercado_libre": [OLD_URL],
            "publicaciones": {"V": {"url": OLD_URL, "code": OLD_MLC, "publicada": True}},
        },
    }
    fresh = parse_publicaciones_json(_payload(), CODE, office_id=7)
    merged = merge_publication_snapshot(old_publications, fresh, observed_at="2026-10-16T12:00:00+00:00")
    ml_rows = [row for row in _difference(old_publications, merged, CODE, "now", "test", "SUCCESS")
               if row["portal"] == "mercadolibre" and row["operacion"] == "V"]
    assert len(ml_rows) == 1
    assert ml_rows[0]["old_mlc"] == [OLD_MLC]
    assert ml_rows[0]["new_mlc"] == [NEW_MLC]
    assert ml_rows[0]["historical_external_ids"] == [OLD_MLC]


def test_explicitly_closed_prop360_publication_is_historical_but_still_resolvable():
    db = mongomock.MongoClient().db
    payload = _payload(mlc_url=OLD_URL, aggregator=None)
    payload["publications"][0]["Publications"][0]["Status"] = 0
    old_doc = _doc(publications={
        "portal_inmobiliario": {"url_mercado_libre": OLD_URL,
                                 "urls_mercado_libre": [OLD_URL],
                                 "publicaciones": {"V": {"url": OLD_URL, "code": OLD_MLC,
                                                             "publicada": True}}},
        "aliases": [_verified_alias(OLD_URL, "mercadolibre", OLD_MLC)],
    })
    incoming = parse_publicaciones_json(payload, CODE, office_id=7)
    merged = merge_publication_snapshot(old_doc["publicaciones"], incoming)
    db.universo_cartera_prop360.insert_one(_doc(publications=merged))
    prop, meta = lookup_property_link(db, OLD_URL)
    assert prop["codigo"] == CODE
    assert meta["publication_status"] == "historical"
    assert meta["match_method"] == "historical_alias_url"
    assert merged["portal_inmobiliario"].get("url_mercado_libre") is None


def test_duplicate_mlc_conflicts_even_if_operation_differs():
    one = _doc(code="16479", publications={"aliases": [_verified_alias(NEW_URL, "mercadolibre", NEW_MLC, "V")]})
    two_url = "https://casa.mercadolibre.cl/MLC-4548780920-otra-publicacion-_JM"
    two = _doc(code="16500", publications={"aliases": [_verified_alias(two_url, "mercadolibre", NEW_MLC, "A")]})
    claims = _existing_claims([one, two])
    incoming = {"aliases": [_verified_alias(NEW_URL, "mercadolibre", NEW_MLC, "V")]}
    conflicts = _incoming_conflicts(incoming, "16479", claims)
    assert conflicts
    assert set(conflicts[0]["candidate_codes"]) == {"16479", "16500"}


def test_duplicate_mlc_conflicts_across_legacy_format_variants():
    one = _doc(code="16479", publications={"aliases": [
        _verified_alias(OLD_URL, "mercadolibre", "MLC3842833828", "V", "historical"),
    ]})
    incoming = {"aliases": [_verified_alias(OLD_URL, "mercadolibre", OLD_MLC, "A")]}
    conflicts = _incoming_conflicts(incoming, "16500", _existing_claims([one]))
    assert conflicts
    assert conflicts[0]["candidate_codes"] == ["16479", "16500"]


def test_chatbot_processing_service_uses_current_resolver_path(monkeypatch):
    db = mongomock.MongoClient().db
    aliases = [
        _verified_alias(OLD_URL, "mercadolibre", OLD_MLC, status="historical"),
        _verified_alias(NEW_URL, "mercadolibre", NEW_MLC),
        _verified_alias(AGGREGATOR, "proppit", "10543041010016479"),
    ]
    aliases.extend(_verified_alias(url, provider) for provider, url in DESTINATIONS.items())
    prop = _doc(publications={"aliases": aliases})
    prop["ubicacion"] = {"comuna": "Lo Prado"}
    prop["tipo_operacion"] = {"tipo": "Casa", "venta": True}
    db.universo_cartera_prop360.insert_one(prop)
    monkeypatch.setattr("chatbot.link_extractor.get_db", lambda: db)

    from chatbot import processing_service
    from chatbot.processing_service import LeadProcessingService
    real_resolver = processing_service.resolver_referencia_propiedad
    resolved = []

    def capture_resolver(*args, **kwargs):
        result = real_resolver(*args, **kwargs)
        resolved.append(result)
        return result

    monkeypatch.setattr(processing_service, "resolver_referencia_propiedad", capture_resolver)

    references = [OLD_MLC, NEW_MLC, OLD_URL, NEW_URL, AGGREGATOR, *DESTINATIONS.values()]
    for reference in references:
        result = LeadProcessingService.classify({
            "phone": "+56900000000",
            "messages": [{"role": "user", "content": reference}],
        })
        assert result["comuna"] == "Lo Prado", (reference, result)
        assert result["tipo"] == "CASA"
        assert result["operacion"] == "V"
        assert resolved[-1]["status"] == "resolved"
        assert resolved[-1]["property"]["codigo"] == CODE
    assert resolved[0]["property"]["_link_match"]["publication_status"] == "historical"


def test_unknown_or_ambiguous_reference_does_not_fall_back_to_old_lead_code(monkeypatch):
    db = mongomock.MongoClient().db
    prop = _doc(publications={"aliases": [_verified_alias(NEW_URL, "mercadolibre", NEW_MLC)]})
    prop["ubicacion"] = {"comuna": "Lo Prado"}
    db.universo_cartera_prop360.insert_one(prop)
    monkeypatch.setattr("chatbot.link_extractor.get_db", lambda: db)
    from chatbot.processing_service import LeadProcessingService

    unknown = LeadProcessingService.classify({
        "phone": "+56900000000", "codigo": "16479",
        "messages": [{"role": "user", "content": "https://casas.trovit.cl/detail/unknown-999"}],
    })
    assert unknown == {}

    duplicate = _doc(code="16500", publications={"aliases": [
        _verified_alias(NEW_URL, "mercadolibre", NEW_MLC),
    ]})
    duplicate["ubicacion"] = {"comuna": "Santiago"}
    db.universo_cartera_prop360.insert_one(duplicate)
    ambiguous = LeadProcessingService.classify({
        "phone": "+56900000000", "codigo": "16479",
        "messages": [{"role": "user", "content": NEW_URL}],
    })
    assert ambiguous == {}


def test_lookup_blocks_duplicate_identifier_claims():
    db = mongomock.MongoClient().db
    for code, suffix, op in (("16479", "casa-en-venta", "V"), ("16500", "otra-publicacion", "A")):
        url = f"https://casa.mercadolibre.cl/MLC-4548780920-{suffix}-_JM"
        db.universo_cartera_prop360.insert_one(
            _doc(code=code, publications={"aliases": [_verified_alias(url, "mercadolibre", NEW_MLC, op)]})
        )
    prop, meta = lookup_property_link(db, NEW_MLC)
    assert prop is None
    assert meta["error_code"] == "AMBIGUOUS_PROPERTY_REFERENCE"


def test_upsert_merge_keeps_all_old_urls_and_distribution_idempotently():
    old = {
        "portal_inmobiliario": {
            "url_mercado_libre": OLD_URL,
            "urls_mercado_libre": [OLD_URL],
            "publicaciones": {"V": {"url": OLD_URL, "code": OLD_MLC, "publicada": False}},
        },
        "proppit": {
            "url_proppit": AGGREGATOR,
            "portales_distribucion": {"icasas": {"V": {
                "url_actual": DESTINATIONS["icasas"], "external_id": "a0319b98-40d3-45d2-84ee-478bebefe66b",
                "urls_observadas": [DESTINATIONS["icasas"]], "last_checked_at": "2026-10-16T10:00:00+00:00",
            }}},
        },
        "aliases": [_verified_alias(OLD_URL, "mercadolibre", OLD_MLC, status="historical")],
    }
    current = parse_publicaciones_json(_payload(), CODE, office_id=7)
    first = merge_publication_snapshot(old, current, observed_at="2026-10-16T11:00:00+00:00")
    second = merge_publication_snapshot(first, current, observed_at="2026-10-16T12:00:00+00:00")
    old_urls = set(first["portal_inmobiliario"]["urls_mercado_libre"])
    assert OLD_URL in old_urls and NEW_URL in old_urls
    assert any(alias.get("external_id") == OLD_MLC and alias.get("resolvable") for alias in first["aliases"])
    assert first["proppit"]["portales_distribucion"]["icasas"]["V"]["url_actual"] == DESTINATIONS["icasas"]
    assert len({(a.get("portal"), a.get("external_id"), a.get("url_normalized")) for a in second["aliases"]}) == len(second["aliases"])
    assert set(second["portal_inmobiliario"]["urls_mercado_libre"]) == old_urls
    assert second == first


def test_upsert_ficha_preserves_historical_urls_and_nested_provider_data(monkeypatch):
    mongo = mongomock.MongoClient()
    db = mongo.db
    coll = db.universo_cartera_prop360
    existing = _doc(publications={
        "portal_inmobiliario": {"url_mercado_libre": OLD_URL,
                                 "urls_mercado_libre": [OLD_URL],
                                 "publicaciones": {"V": {"url": OLD_URL, "code": OLD_MLC}}},
        "proppit": {"url_proppit": AGGREGATOR,
                    "portales_distribucion": {"trovit": {"V": {
                        "url_actual": DESTINATIONS["trovit"], "urls_observadas": [DESTINATIONS["trovit"]],
                    }}},
                    "publicaciones": {"V": {"url": AGGREGATOR}}},
        "aliases": [_verified_alias(OLD_URL, "mercadolibre", OLD_MLC, status="historical")],
    })
    coll.insert_one(existing)
    incoming = parse_publicaciones_json(_payload(), CODE, office_id=7)
    new_doc = _doc(publications=incoming)
    new_doc["metadata"] = {"status": "ok"}
    monkeypatch.setattr("scraping_convecta.scraping_prop360_ficha_completa.canonicalize_prices",
                        lambda doc, *args, **kwargs: doc)
    _new, updated = upsert_ficha(coll, new_doc, history_coll=db.property_history, uf_info={})
    assert updated
    saved = coll.find_one({"codigo": CODE})
    assert OLD_URL in saved["publicaciones"]["portal_inmobiliario"]["urls_mercado_libre"]
    assert any(a.get("external_id") == OLD_MLC and a.get("resolvable") for a in saved["publicaciones"]["aliases"])
    assert saved["publicaciones"]["proppit"]["portales_distribucion"]["trovit"]["V"]["url_actual"] == DESTINATIONS["trovit"]


def test_upsert_publication_history_merge_is_not_applied_to_other_offices(monkeypatch):
    mongo = mongomock.MongoClient()
    coll = mongo.db.universo_cartera_prop360
    existing = _doc(code="20001", office="PROCASA VITACURA", publications={"legacy": {"old": True}})
    existing["oficina_id"] = 3
    coll.insert_one(existing)
    incoming = parse_publicaciones_json(_payload(), "20001", office_id=3)
    new_doc = _doc(code="20001", office="PROCASA VITACURA", publications=incoming)
    new_doc["oficina_id"] = 3
    monkeypatch.setattr("scraping_convecta.scraping_prop360_ficha_completa.canonicalize_prices",
                        lambda doc, *args, **kwargs: doc)
    _new, updated = upsert_ficha(coll, new_doc, history_coll=mongo.db.property_history, uf_info={})
    assert updated
    saved = coll.find_one({"codigo": "20001"})["publicaciones"]
    assert saved["legacy"] == {"old": True}
    assert "aliases" not in saved


def test_verified_proppit_absence_retires_but_keeps_old_destination_resolvable():
    from chatbot.publication_sync import _build_incoming

    old_url = DESTINATIONS["icasas"]
    old = _doc(publications={
        "proppit": {"url_proppit": AGGREGATOR,
                    "portales_distribucion": {"icasas": {"V": {
                        "url_actual": old_url, "external_id": "a0319b98-40d3-45d2-84ee-478bebefe66b",
                        "urls_observadas": [old_url],
                    }}}},
        "aliases": [_verified_alias(old_url, "icasas")],
    })
    incoming, _payload_value, source_meta = _build_incoming(
        FakeProp360(), CODE, old, payload=_payload(aggregator=AGGREGATOR),
        fetcher=lambda _url: {"status": "http_200", "html": "<html><body>No destinations yet</body></html>"},
        checked_at="2026-10-16T12:00:00+00:00",
    )
    merged = merge_publication_snapshot(old["publicaciones"], incoming, observed_at="2026-10-16T12:00:00+00:00")
    node = merged["proppit"]["portales_distribucion"]["icasas"]["V"]
    assert node.get("url_actual") is None
    assert old_url in node["urls_historicas"]
    alias = next(a for a in merged["aliases"] if a.get("url") == old_url)
    assert alias["publication_status"] == "historical" and alias["resolvable"] is True
    assert set(_pending_providers(source_meta)) == set(PROVIDERS)


def test_new_provider_alias_expansion_is_limited_to_sucre():
    data = parse_publicaciones_json(_payload(), CODE, office_id=3)
    assert "aliases" not in data
    assert data["portal_inmobiliario"]["url_mercado_libre"] == NEW_URL


def test_missing_office_id_does_not_expand_aliases_by_assumption():
    data = parse_publicaciones_json(_payload(), CODE)
    assert "aliases" not in data


@pytest.mark.parametrize("count", [0, 1, 3, 5])
def test_proppit_parser_only_returns_destinations_present_in_source(count):
    selected = list(DESTINATIONS.values())[:count]
    html = "<html><body>" + "".join(f'<a href="{url}">portal</a>' for url in selected) + "</body></html>"
    parsed = parse_proppit_destinations(html)
    assert sum(len(urls) for urls in parsed.values()) == count
    for provider, urls in parsed.items():
        if urls:
            assert urls == [DESTINATIONS[provider]]


def test_missing_syndication_destinations_are_pending_for_retry():
    assert _pending_providers({"aggregator_results": [{
        "source_status": "source_verified_no_destinations",
        "providers": {provider: "not_observed" for provider in PROVIDERS},
    }]}) == sorted(PROVIDERS)


def test_robots_block_is_not_reported_as_success_or_retried_quickly():
    source = {"aggregator_results": [{
        "source_status": "robots_blocked", "reason": "robots_disallowed",
        "providers": {provider: "not_observed" for provider in PROVIDERS},
    }]}
    assert _pending_providers(source) == []


def test_proppit_retry_schedule_is_one_six_twenty_four_hours():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    assert (_retry_after(1, now) - now).total_seconds() == 3600
    assert (_retry_after(2, now) - now).total_seconds() == 21600
    assert (_retry_after(3, now) - now).total_seconds() == 86400


def test_publication_worker_is_disabled_by_default(monkeypatch):
    from chatbot import publication_sync_worker

    monkeypatch.delenv("PUBLICATION_SYNC_ENABLED", raising=False)
    assert publication_sync_worker.main([]) == 0


def test_publication_worker_execute_requires_backup_and_reuses_bounded_batch():
    from chatbot.publication_sync_worker import run_worker

    calls = []
    run_worker(
        execute=True, interval_seconds=60, backup_dir="/private/backups",
        output_dir="/private/reports", max_cycles=1,
        batch_runner=lambda **kwargs: calls.append(kwargs) or {"status": "ok"},
        sleep=lambda _duration: pytest.fail("one-cycle worker must not sleep"),
    )
    assert calls == [{
        "dry_run": False, "execute": True, "office_id": 7,
        "batch_size": 25, "backup_dir": "/private/backups",
        "output_dir": "/private/reports",
    }]
    with pytest.raises(ValueError, match="durable_private_backup"):
        run_worker(execute=True, interval_seconds=60, max_cycles=1,
                   batch_runner=lambda **_kwargs: {})


def test_safe_fetch_blocks_non_allowlisted_host_before_request():
    result = safe_fetch_html("https://evil.example/MLC-4548780920")
    assert result["status"] == "blocked"
    assert result["reason"] == "host_not_allowlisted"


def test_safe_fetch_rejects_private_dns_and_revalidates_redirect(monkeypatch):
    monkeypatch.setattr(publication_sync, "_resolve_public_addresses", lambda *_args: ["127.0.0.1"])
    result = safe_fetch_html("https://links.proppit.com/links")
    assert result["status"] == "blocked"
    assert result["reason"] == "non_public_ip"


def test_safe_fetch_allows_verified_proppit_redirect_and_revalidates_each_hop(monkeypatch):
    from chatbot import publication_sync

    class Response:
        def __init__(self, status, headers=None, chunks=()):
            self.status_code = status
            self.headers = headers or {}
            self.encoding = "utf-8"
            self._chunks = chunks

        def close(self):
            pass

        def iter_content(self, _size):
            yield from self._chunks

    class Session:
        trust_env = True

        def __init__(self):
            self.responses = [
                Response(302, {"Location": "https://proppit.com/property-links/uuid"}),
                Response(200, chunks=[b"<a href='https://www.icasas.cl/re/uuid'>iCasas</a>"]),
            ]
            self.urls = []

        def get(self, url, **_kwargs):
            self.urls.append(url)
            if url.endswith("robots.txt"):
                return Response(404)
            return self.responses.pop(0)

    validations = []
    session = Session()
    monkeypatch.setattr(publication_sync, "_validate_public_url",
                        lambda url, **_kwargs: (validations.append(url) or (True, "")))
    monkeypatch.setattr(publication_sync, "_robots_allows", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(publication_sync, "_rate_limit", lambda _host: None)
    result = safe_fetch_html(AGGREGATOR, session=session)
    assert result["status"] == "http_200"
    assert result["final_url"] == "https://proppit.com/property-links/uuid"
    assert validations == [AGGREGATOR, "https://proppit.com/property-links/uuid"]
    assert session.trust_env is False


def test_public_dns_resolution_uses_remaining_budget(monkeypatch):
    import dns.resolver

    lifetimes = []

    class Record:
        address = "8.8.8.8"

    class Resolver:
        timeout = None

        def resolve(self, _host, _record_type, *, lifetime, **_kwargs):
            lifetimes.append(lifetime)
            return [Record()]

    monkeypatch.setattr(dns.resolver, "Resolver", lambda configure=True: Resolver())
    addresses = publication_sync._resolve_public_addresses("links.proppit.com", 0.5)
    assert addresses == ["8.8.8.8", "8.8.8.8"]
    assert len(lifetimes) == 2
    assert all(0 < value <= 0.5 for value in lifetimes)


def test_safe_fetch_stops_slow_stream_at_property_budget(monkeypatch):
    clock = [0.0]
    dns_timeouts = []
    monkeypatch.setattr(publication_sync.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(publication_sync, "_validate_public_url",
                        lambda _url, *, dns_timeout: (dns_timeouts.append(dns_timeout) or (True, "")))
    monkeypatch.setattr(publication_sync, "_robots_allows", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(publication_sync, "_rate_limit", lambda _host: None)

    class Response:
        status_code = 200
        headers = {}
        encoding = "utf-8"

        def close(self):
            pass

        def iter_content(self, _chunk_size):
            yield b"first"
            clock[0] = 1.1
            yield b"second"

    class Session:
        trust_env = True

        def get(self, *_args, **_kwargs):
            return Response()

    result = safe_fetch_html("https://links.proppit.com/links", session=Session(),
                             budget_seconds=1, connect_timeout=1, read_timeout=1, retries=1)
    assert result["status"] == "timeout"
    assert result["reason"] == "property_audit_budget_exhausted"
    assert dns_timeouts and all(value <= 0.5 for value in dns_timeouts)


def test_prop360_client_can_bound_post_attempts_for_audit():
    from scraping_convecta.scraping_prop360_ficha_completa import Prop360Client

    class FailingSession:
        def __init__(self):
            self.calls = 0

        def post(self, *_args, **_kwargs):
            self.calls += 1
            raise TimeoutError("bounded test timeout")

    client = Prop360Client("", "", delay=0, request_timeout=7, max_attempts=1)
    session = FailingSession()
    client.client = session
    with pytest.raises(TimeoutError, match="bounded test timeout"):
        client._post("https://prop360.invalid/portals", {})
    assert session.calls == 1
    assert client.request_timeout == 7
    assert client.max_attempts == 1


def test_full_reconciliation_dry_run_writes_no_mongo_and_reports_all_providers(tmp_path):
    mongo = mongomock.MongoClient()
    raw_db = mongo.db
    doc = _doc(publications={"portal_inmobiliario": {"url_mercado_libre": OLD_URL}})
    raw_db.universo_cartera_prop360.insert_one(doc)
    readonly_db = ReadOnlyDB(raw_db)
    aggregator_html = "<html><body>" + "".join(
        f'<a href="{url}">portal</a>' for url in DESTINATIONS.values()
    ) + "</body></html>"
    report = run_publication_reconciliation(
        dry_run=True, office_id=7, db=readonly_db,
        prop360_client=FakeProp360(), fetcher=lambda _url: {"status": "http_200", "html": aggregator_html},
        output_dir=tmp_path,
    )
    assert report["total_active"] == 1
    assert report["checked"] == 1
    assert report["mongo_writes"] == 0
    assert readonly_db["universo_cartera_prop360"].writes == 0
    assert {provider for provider in ("proppit", *PROVIDERS)}.issubset(
        {row["portal"] for row in report["records"]}
    )
    assert {row["portal"] for row in report["records"] if row["new_urls"]} >= set(DESTINATIONS)


def test_office_mismatch_and_inactive_property_never_enter_scope(tmp_path):
    mongo = mongomock.MongoClient()
    db = mongo.db
    db.universo_cartera_prop360.insert_many([
        _doc(code="16479", office="OTRA OFICINA"),
        _doc(code="16500", active=False),
    ])
    fake = FakeProp360(rows=[{"codigo": "16479", "estado": "Activa"}, {"codigo": "16500", "estado": "Activa"}],
                       payloads={"16479": _payload(), "16500": _payload()})
    result = run_publication_reconciliation(
        dry_run=True, office_id=7, db=db, prop360_client=fake,
        fetcher=lambda _url: {"status": "http_200", "html": ""}, output_dir=tmp_path,
    )
    assert result["checked"] == 0
    assert result["errors"] == 2
    assert result["mongo_writes"] == 0


def test_inventory_dry_run_reports_prop360_only_and_mongo_only_codes(tmp_path):
    mongo = mongomock.MongoClient()
    db = mongo.db
    db.universo_cartera_prop360.insert_many([
        _doc(code="16479"),
        _doc(code="6579"),
    ])
    fake = FakeProp360(
        rows=[{"codigo": "16479", "estado": "Activa"}, {"codigo": "17000", "estado": "Activa"}],
        payloads={"16479": _payload(), "17000": _payload()},
    )
    report = run_publication_reconciliation(
        dry_run=True, office_id=7, db=db, prop360_client=fake,
        fetcher=lambda _url: {"status": "robots_blocked", "reason": "robots_disallowed", "html": ""},
        output_dir=tmp_path,
    )
    assert report["total_active"] == 2
    assert report["mongo_active"] == 2
    assert report["prop360_only"] == ["17000"]
    assert report["mongo_only"] == ["6579"]
    assert report["inventory_mismatches"] == 2
    assert report["mongo_writes"] == 0


def test_batch_failure_middle_is_queued_and_later_codes_are_processed(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("PUBLICATION_SYNC_EXECUTE", "true")
    mongo = mongomock.MongoClient()
    db = mongo.db
    db.universo_cartera_prop360.insert_many([_doc(code=code) for code in ("1", "2", "3")])
    fake = IntermittentFakeProp360()
    result = run_publication_sync_batch(
        dry_run=False, execute=True, office_id=7, batch_size=3, db=db,
        prop360_client=fake, backup_dir=tmp_path / "durable-backup",
        output_dir=tmp_path / "reports",
    )
    assert result["status"] == "partial"
    assert result["retry_codes"] == ["2"]
    assert set(result["terminal_codes"]) == {"1", "3"}
    assert fake.calls == ["1", "2", "3"]
    state = db.prop360_publication_sync_state.find_one({"_id": "2"})
    assert state["status"] == "pending"
    assert state["attempt_count"] == 1
    assert state["next_retry_at"] is not None
    assert "TimeoutError" in state["error_code"]
    assert db.prop360_publication_sync_state.find_one({"_id": "__cursor__"})["last_code"] == "3"

    db.prop360_publication_sync_state.update_one(
        {"_id": "2"}, {"$set": {"next_retry_at": datetime.now(timezone.utc) - timedelta(seconds=1)}}
    )
    fake.fail_code = None
    retried = run_publication_sync_batch(
        dry_run=False, execute=True, office_id=7, batch_size=1, db=db,
        prop360_client=fake, backup_dir=tmp_path / "durable-backup",
        output_dir=tmp_path / "reports",
    )
    assert retried["status"] == "completed"
    assert fake.calls[-1] == "2"
    assert db.prop360_publication_sync_state.find_one({"_id": "2"})["status"] == "success"


def test_sync_single_property_is_read_only_without_both_explicit_flags(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLICATION_SYNC_EXECUTE", raising=False)
    monkeypatch.delenv("PUBLICATION_SYNC_NEW_PROPERTIES_ENABLED", raising=False)
    mongo = mongomock.MongoClient()
    db = mongo.db
    doc = _doc(publications={"portal_inmobiliario": {"url_mercado_libre": OLD_URL}})
    db.universo_cartera_prop360.insert_one(doc)
    before = db.universo_cartera_prop360.find_one({"codigo": CODE})
    result = sync_single_property_publications(
        FakeProp360(), db.universo_cartera_prop360, db, before,
        payload=_payload(aggregator=None),
    )
    after = db.universo_cartera_prop360.find_one({"codigo": CODE})
    assert result["proposed_change"] is True
    assert result["mongo_writes"] == 0
    assert after["publicaciones"] == before["publicaciones"]
    assert db.prop360_publication_sync_state.count_documents({}) == 0


def test_new_property_write_rejects_missing_enable_flags_and_backup(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLICATION_SYNC_EXECUTE", raising=False)
    monkeypatch.setenv("PUBLICATION_SYNC_NEW_PROPERTIES_ENABLED", "true")
    mongo = mongomock.MongoClient()
    db = mongo.db
    doc = _doc(publications={})
    db.universo_cartera_prop360.insert_one(doc)
    with pytest.raises(RuntimeError, match="both_explicit_enable_flags"):
        sync_single_property_publications(
            FakeProp360(), db.universo_cartera_prop360, db, doc,
            payload=_payload(aggregator=None), backup_dir=tmp_path, execute=True,
        )
    assert db.universo_cartera_prop360.find_one({"codigo": CODE})["publicaciones"] == {}


def test_render_backup_requires_mounted_durable_root(tmp_path, monkeypatch):
    from chatbot import publication_sync

    monkeypatch.setenv("RENDER", "true")
    root = tmp_path / "render-disk"
    root.mkdir()
    monkeypatch.setenv("PUBLICATION_SYNC_BACKUP_DURABLE_ROOT", str(root))
    monkeypatch.setattr(publication_sync.os.path, "ismount", lambda _path: False)
    with pytest.raises(RuntimeError, match="not_a_mounted_disk"):
        publication_sync._ensure_backups([_doc()], "run", root / "backups")
    monkeypatch.setattr(publication_sync.os.path, "ismount", lambda _path: True)
    backup = publication_sync._ensure_backups([_doc()], "run2", root / "backups")
    assert backup["documents"] == 1
    assert Path(backup["path"]).exists()


def test_reconcile_batch_cli_propagates_backup_and_output_dirs(monkeypatch, tmp_path):
    import scripts.reconcile_sucre_publications as cli

    monkeypatch.setenv("PUBLICATION_SYNC_EXECUTE", "true")
    captured = {}

    def fake_batch(**kwargs):
        captured.update(kwargs)
        return {"status": "completed", "mongo_writes": 0}

    monkeypatch.setattr(cli, "run_publication_sync_batch", fake_batch)
    exit_code = cli.main([
        "--office-id", "7", "--batch", "--execute", "--limit", "3",
        "--backup-dir", str(tmp_path / "backup"),
        "--output-dir", str(tmp_path / "reports"),
    ])
    assert exit_code == 0
    assert captured["backup_dir"] == str(tmp_path / "backup")
    assert captured["output_dir"] == str(tmp_path / "reports")


def test_authorized_import_distinguishes_declared_from_confirmed_links(monkeypatch):
    declared_rows = [{
        "codigo_prop360": CODE, "portal": provider, "operacion": "V", "url": url,
        "publication_role": "current", "attribution_status": "declared",
        "availability_status": "unknown", "source_record_id": f"feed-{provider}",
    } for provider, url in DESTINATIONS.items()]
    declared = normalize_authorized_publication_rows(
        declared_rows, source_type="admin_evidence_import", imported_at="2026-10-16T12:00:00+00:00",
    )
    assert declared["accepted_rows"] == 5
    assert all(alias["resolvable"] is False for alias in declared["publications"]["aliases"])
    assert all(alias["verification_status"] == "unverified" for alias in declared["publications"]["aliases"])

    confirmed_rows = [{**row, "attribution_status": "confirmed",
                       "evidence_reference": f"portal-export-row-{n}"}
                      for n, row in enumerate(declared_rows, start=1)]
    confirmed = normalize_authorized_publication_rows(
        confirmed_rows, source_type="convecta_feed", imported_at="2026-10-16T12:00:00+00:00",
    )
    merged = merge_publication_snapshot({}, confirmed["publications"],
                                        observed_at="2026-10-16T12:00:00+00:00")
    mongo = mongomock.MongoClient()
    db = mongo.db
    imported_doc = _doc(publications=merged)
    imported_doc["ubicacion"] = {"comuna": "Lo Prado"}
    imported_doc["tipo_operacion"] = {"tipo": "Casa", "venta": True}
    db.universo_cartera_prop360.insert_one(imported_doc)
    from chatbot import link_extractor
    from chatbot import processing_service
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)
    real_resolver = processing_service.resolver_referencia_propiedad
    classified_codes = []

    def capture(*args, **kwargs):
        result = real_resolver(*args, **kwargs)
        if result.get("status") == "resolved":
            classified_codes.append(result["property"].get("codigo"))
        return result

    monkeypatch.setattr(processing_service, "resolver_referencia_propiedad", capture)
    from chatbot.processing_service import LeadProcessingService
    for url in DESTINATIONS.values():
        prop, meta = lookup_property_link(db, url)
        assert prop["codigo"] == CODE
        assert meta["verification_status"] == "source_verified"
        result = LeadProcessingService.classify({
            "phone": "+56900000001", "messages": [{"role": "user", "content": url}],
        })
        assert result["comuna"] == "Lo Prado"
    assert classified_codes == [CODE] * 5


def test_resumable_local_audit_keeps_incremental_checkpoint_and_skips_success(tmp_path):
    mongo = mongomock.MongoClient()
    db = mongo.db
    db.universo_cartera_prop360.insert_many([_doc(code="16479"), _doc(code="16500")])
    fake = FakeProp360(rows=[{"codigo": "16479", "estado": "Activa"},
                             {"codigo": "16500", "estado": "Activa"}],
                       payloads={"16479": _payload(aggregator=None),
                                 "16500": _payload(aggregator=None)})
    fetcher = lambda _url, **_kwargs: {"status": "robots_blocked", "reason": "robots_disallowed", "html": ""}
    first = run_sucre_publication_audit(
        output_dir=tmp_path, batch_size=1, db=db, prop360_client=fake, fetcher=fetcher,
    )
    run_dir = Path(first["run_dir"])
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["last_code_evaluated"] == "16479"
    assert first["traceable_codes"] == 1
    assert first["coverage_complete"] is False
    assert first["mongo_writes"] == 0
    second = run_sucre_publication_audit(
        output_dir=tmp_path, resume_dir=run_dir, batch_size=1, db=db,
        prop360_client=fake, fetcher=fetcher,
    )
    assert second["coverage_complete"] is True
    assert second["traceable_codes"] == 2
    assert second["mongo_writes"] == 0
    assert fake.portal_calls == ["16479", "16500"]
    assert (run_dir / "events.jsonl").read_text(encoding="utf-8").count("\n") == 2
    assert Path(second["report_json"]).exists()
    assert Path(second["report_csv"]).exists()


def test_resumable_local_audit_retries_timeout_but_preserves_attempt_event(tmp_path):
    class TimeoutOnce(FakeProp360):
        def __init__(self):
            super().__init__(rows=[{"codigo": CODE, "estado": "Activa"}],
                             payloads={CODE: _payload(aggregator=None)})
            self.calls = 0

        def get_portales(self, codigo):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary source timeout")
            return super().get_portales(codigo)

    mongo = mongomock.MongoClient()
    db = mongo.db
    db.universo_cartera_prop360.insert_one(_doc())
    fake = TimeoutOnce()
    first = run_sucre_publication_audit(
        output_dir=tmp_path, batch_size=1, db=db, prop360_client=fake,
    )
    assert first["traceable_codes"] == 1
    assert first["retryable_codes"] == [CODE]
    run_dir = Path(first["run_dir"])
    second = run_sucre_publication_audit(
        output_dir=tmp_path, resume_dir=run_dir, batch_size=1, db=db,
        prop360_client=fake, fetcher=lambda _url, **_kwargs: {
            "status": "robots_blocked", "reason": "robots_disallowed", "html": "",
        },
    )
    assert second["coverage_complete"] is True
    assert second["retryable_codes"] == []
    assert fake.calls == 2
    assert (run_dir / "events.jsonl").read_text(encoding="utf-8").count("\n") == 2
    assert second["mongo_writes"] == 0


def test_resumable_audit_processes_later_codes_before_retrying_slow_code(tmp_path):
    class FirstCodeTimeoutOnce(FakeProp360):
        def __init__(self):
            super().__init__(rows=[{"codigo": "16479", "estado": "Activa"},
                                  {"codigo": "16500", "estado": "Activa"}],
                             payloads={"16479": _payload(aggregator=None),
                                       "16500": _payload(aggregator=None)})
            self.first_code_calls = 0

        def get_portales(self, codigo):
            if codigo == "16479":
                self.first_code_calls += 1
                if self.first_code_calls == 1:
                    raise TimeoutError("first property is slow")
            return super().get_portales(codigo)

    mongo = mongomock.MongoClient()
    db = mongo.db
    db.universo_cartera_prop360.insert_many([_doc(code="16479"), _doc(code="16500")])
    fake = FirstCodeTimeoutOnce()
    first = run_sucre_publication_audit(output_dir=tmp_path, batch_size=1, db=db,
                                        prop360_client=fake)
    run_dir = Path(first["run_dir"])
    assert first["last_code_evaluated"] == "16479"
    assert first["retryable_codes"] == ["16479"]

    second = run_sucre_publication_audit(output_dir=tmp_path, resume_dir=run_dir,
                                         batch_size=1, db=db, prop360_client=fake)
    assert second["last_code_evaluated"] == "16500"
    assert second["traceable_codes"] == 2
    assert second["retryable_codes"] == ["16479"]
    assert fake.portal_calls == ["16500"]

    third = run_sucre_publication_audit(output_dir=tmp_path, resume_dir=run_dir,
                                         batch_size=1, db=db, prop360_client=fake)
    assert third["last_code_evaluated"] == "16479"
    assert third["coverage_complete"] is True
    assert third["retryable_codes"] == []
    assert fake.portal_calls == ["16500", "16479"]
    assert third["mongo_writes"] == 0
