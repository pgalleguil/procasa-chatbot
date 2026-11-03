from __future__ import annotations

from datetime import datetime, timezone

from scrapers.scraper_yapo.simulate_captacion_compatibility import (
    _canonical_document,
    _normalize_saved_attributes,
)


def test_canonical_adapter_normalizes_real_fields_without_inventing_missing_values():
    attributes = {
        "Publicado": "02/11/2026",
        "M² totales": "150 m²",
        "Área construida (m²)": "90 m2",
        "Baños": "2",
    }
    normalized = _normalize_saved_attributes(attributes)
    assert normalized == {
        "fecha_publicacion_raw": "02/11/2026",
        "fecha_publicacion": "2026-11-02",
        "m2_totales": 150.0,
        "m2_construidos": 90.0,
    }

    item = {
        "listing_id": "12345678", "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678",
        "title": "Casa en venta", "description": "Descripción completa.", "comuna": "Maipú",
        "operacion": "venta", "tipo_propiedad": "casa", "precio_raw": "UF 4.000",
        "precio_moneda_original": "UF", "precio_original_num": 4000, "precio_uf": 4000,
        "precio_clp": 165054280, "precio_uf_normalizado": 4000, "precio_clp_normalizado": 165054280,
        "processed_at": "2026-11-02T18:21:00+00:00", "updated_at": "2026-11-02T18:21:00+00:00",
        "fecha_scraping": "2026-11-02T18:21:00+00:00", "fecha_publicacion": normalized["fecha_publicacion"],
        "fecha_publicacion_raw": normalized["fecha_publicacion_raw"], "publicador": "Anunciante",
        "publicador_visible": "Anunciante", "seller_name": "Anunciante", "contact_name": "Anunciante",
        "dormitorios": 4, "banos": 2, "estacionamientos": None, "m2_totales": normalized["m2_totales"],
        "m2_construidos": normalized["m2_construidos"], "superficie_total": normalized["m2_totales"],
        "superficie_util": normalized["m2_construidos"], "raw_attributes": attributes, "attributes": attributes,
        "image_urls": ["https://photos.encuentra24.com/32200413_img1"],
        "enlaces_fotos": ["https://photos.encuentra24.com/32200413_img1"],
        "main_image_url": "https://photos.encuentra24.com/32200413_img1", "image_urls_count": 1,
        "html_validation_status": "OK", "html_validation_reason": "valid_html", "html_source_path": "saved.html",
    }
    classification = {"state": "DUEÑO_PROBABLE", "assignment_ready": False, "evidence": ["review"]}
    doc = _canonical_document(item, classification, "run", "batch")
    assert doc["classification"] == classification
    assert doc["fecha_publicacion"] == "2026-11-02"
    assert doc["m2_totales"] == doc["superficie_total"] == 150.0
    assert doc["m2_construidos"] == doc["superficie_util"] == 90.0
    assert doc["details"]["m2_total"] == 150.0
    assert doc["raw_attributes"] == attributes
    assert doc["enlaces_fotos"] == doc["images"] == doc["image_urls"]
    assert doc["gestion"]["estado"] == "NUEVO"
    assert doc["gestion"]["ejecutivo_id"] is None
    assert doc["classification"]["assignment_ready"] is False
    assert "contact_phone" not in doc
    assert doc["processed_at"] == "2026-11-02T18:21:00+00:00"


def test_crm_detail_read_and_filter_adapters_accept_canonical_doc(monkeypatch):
    import api_captacion as crm

    doc = {
        "_id": "preimport-sim", "origen": "yapo", "source_portal": "yapo", "listing_id": "12345678",
        "url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/12345678",
        "title": "Casa en Maipú", "description": "Descripción completa", "comuna": "Maipú",
        "comuna_slug": "maipu", "operacion": "venta", "tipo_propiedad": "casa", "precio_raw": "UF 4.000",
        "precio_uf": 4000, "precio_uf_normalizado": 4000, "precio_clp": 165054280,
        "dormitorios": 4, "banos": 2, "m2_totales": 150, "m2_construidos": 90,
        "enlaces_fotos": ["https://photos.encuentra24.com/12345678_img1"],
        "images": ["https://photos.encuentra24.com/12345678_img1"],
        "main_image_url": "https://photos.encuentra24.com/12345678_img1",
        "publicador": "Anunciante", "seller_name": "Anunciante",
        "details": {"comuna_norm": "maipu", "tipo_propiedad": "casa", "operacion": "venta",
                    "precio_uf": 4000, "precio_clp": 165054280, "m2_total": 150,
                    "m2_construidos": 90, "dormitorios": 4, "banos": 2, "enlaces_fotos": ["https://photos.encuentra24.com/12345678_img1"]},
        "classification": {"state": "DUEÑO_PROBABLE", "assignment_ready": False, "evidence": ["review"]},
        "processed_at": "2026-11-02T18:21:00+00:00",
        "gestion": {"estado": "NUEVO", "estado_captacion": "NUEVO", "ejecutivo_id": None,
                    "ejecutivo_asignado": None, "intent_count": 0, "notas": [], "actividades": []},
    }
    monkeypatch.setattr(crm, "_l1_get", lambda _key: None)
    monkeypatch.setattr(crm, "_l1_set", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(crm, "get_captacion_raw", lambda _id: doc)
    monkeypatch.setattr(crm, "get_market_insights", lambda comuna, tipo: {"sales_count": 0, "normalized_commune": comuna, "avg_uf_m2": 0})
    monkeypatch.setattr(crm, "get_captacion_capture_datetime", lambda _doc: datetime(2026, 11, 2, tzinfo=timezone.utc))

    detail = crm.get_captacion_detail("preimport-sim")
    assert detail["titulo"] == "Casa en Maipú"
    assert detail["enlaces_fotos"] == doc["enlaces_fotos"]
    assert detail["details"]["dormitorios"] == 4
    assert detail["details"]["m2_total"] == 150
    assert detail["classification_state"] == "DUEÑO_PROBABLE"
    assert detail["classification_evidence"] == ["review"]
    assert detail["estado_captacion"] == "Por contactar"
    assert detail["gestion"]["ejecutivo_id"] is None

    condition = crm._captacion_price_range_condition("UF", 3000, 6000)
    assert condition["$or"][0]["precio_uf_normalizado"] == {"$gte": 3000.0, "$lte": 6000.0}
    assert crm.normalize_commune_canonical(doc["comuna"]) == doc["comuna_slug"]
    assert crm._captacion_property_type_pattern("casa").search(doc["tipo_propiedad"])
