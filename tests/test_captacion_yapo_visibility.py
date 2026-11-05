import json
from pathlib import Path

import mongomock

import api_captacion
from api_captacion import get_captacion_capture_datetime
from captacion_kpis import VISIBLE_CLASSIFICATION_STATES, build_kpi_queries
from captacion_visibility import (
    apply_captacion_visibility,
    build_yapo_visibility_diagnostics_query,
    commercial_visibility_exclusion_reasons,
    is_commercially_usable_yapo_document,
    is_new_yapo_cohort,
)


ROOT = Path(__file__).resolve().parents[1]
CONTROLS = json.loads(
    (ROOT / "scrapers" / "scraper_yapo" / "tests" / "fixtures" / "yapo_519_captacion_controls.json")
    .read_text(encoding="utf-8")
)


def _complete_yapo_doc(listing_id, *, state="INCIERTO", batch_id=None, source_batch_id=None, **overrides):
    batch_id = batch_id or "maipu_houses_3000_6000_20261103"
    source_batch_id = source_batch_id or "yapo_maipu_482_20261103_v3"
    url = f"https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa-en-maipu/{listing_id}"
    doc = {
        "listing_id": str(listing_id),
        "origen": "yapo",
        "batch_id": batch_id,
        "yapo_ingestion": {"source_batch_id": source_batch_id},
        "url": url,
        "canonical_url": url,
        "title": "Casa en venta en Maipú",
        "description": "Casa con patio, dormitorios y cocina independiente en Maipú.",
        "descripcion_is_truncated": False,
        "comuna": "Maipú",
        "operacion": "venta",
        "tipo_propiedad": "casa",
        "price": "3500 CLF",
        "precio_uf": 3500,
        "precio_clp": 140000000,
        "availability_status": "NO_VERIFICADO",
        "html_validation_status": "OK",
        "scrape_stage": "processed",
        "classification": {
            "state": state,
            "owner_probability": None,
            "assignment_ready": False,
            "exclude_from_assignment": True,
        },
        "gestion": {"estado": "NUEVO"},
        "created_at": "2026-11-04T12:00:00+00:00",
    }
    doc.update(overrides)
    return doc


def _real_control_documents():
    documents = []
    for record in CONTROLS["records"]:
        batch_id = record.get("batch_id", CONTROLS["batch_id"])
        source_batch_id = record.get("source_batch_id", CONTROLS["source_batch_id"])
        doc = {
            "listing_id": record["listing_id"],
            "origen": "yapo",
            "batch_id": batch_id,
            "yapo_ingestion": {"source_batch_id": source_batch_id},
            "url": record["url"],
            "availability_status": record["availability_status"],
            "html_validation_status": record["html_validation_status"],
            "scrape_stage": record["scrape_stage"],
            "classification": {"state": record["classification_state"]},
            "gestion": {"estado": "NUEVO"},
        }
        if record.get("detail_error"):
            doc["detail_error"] = record["detail_error"]
        if record["classification_state"] == "CORREDOR_SEGURO":
            doc.update(_complete_yapo_doc(
                record["listing_id"], state="CORREDOR_SEGURO",
                batch_id=batch_id, source_batch_id=source_batch_id,
                url=record["url"], canonical_url=record["url"],
            ))
        documents.append(doc)
    return documents


def _eligible_batch_fixture():
    rows = _real_control_documents()
    for index in range(71):
        state = "DUEÑO_PROBABLE" if index < 10 else "INCIERTO"
        rows.append(_complete_yapo_doc(f"9000{index:04d}", state=state))
    return rows


def _reset_list_caches():
    api_captacion.get_captacion_list._property_type_cache = {}
    api_captacion.get_captacion_list._portal_cache = {}
    api_captacion.get_captacion_list._ops_cache = None


def test_real_control_ids_are_scoped_and_defect_reasons_are_preserved():
    documents = {doc["listing_id"]: doc for doc in _real_control_documents()}

    for listing_id in ("33215751", "33215722"):
        reasons = commercial_visibility_exclusion_reasons(documents[listing_id])
        assert "anuncio_retirado_confirmado" in reasons
        assert is_commercially_usable_yapo_document(documents[listing_id]) is False

    blocked = documents["33213261"]
    reasons = commercial_visibility_exclusion_reasons(blocked)
    assert blocked["availability_status"] == "NO_VERIFICADO"
    assert "anuncio_retirado_confirmado" not in reasons
    assert "extraccion_tecnica_pendiente" in reasons
    assert "error_tecnico_de_extraccion" in reasons
    assert is_commercially_usable_yapo_document(blocked) is False

    for listing_id in ("33216485", "33215229"):
        assert documents[listing_id]["classification"]["state"] == "CORREDOR_SEGURO"
        assert commercial_visibility_exclusion_reasons(documents[listing_id]) == []
        assert documents[listing_id]["classification"]["assignment_ready"] is False


def test_visibility_predicate_is_only_for_exact_new_batch_pairs():
    lot_a = _complete_yapo_doc("90010001")
    lot_b = _complete_yapo_doc(
        "90010002",
        batch_id="issue122_maipu_20261104_141023_35cf",
        source_batch_id="issue122_maipu_20261104_141023_35cf",
    )
    wrong_source = _complete_yapo_doc("90010003", source_batch_id="wrong-batch")
    historic = _complete_yapo_doc("90010004", batch_id="old", source_batch_id="old")
    other_portal = dict(_complete_yapo_doc("90010005"), origen="toctoc")
    noncanonical_origin = dict(_complete_yapo_doc("90010006"), origen="YAPO")

    assert is_new_yapo_cohort(lot_a)
    assert is_new_yapo_cohort(lot_b)
    assert not is_new_yapo_cohort(wrong_source)
    assert not is_new_yapo_cohort(historic)
    assert not is_new_yapo_cohort(other_portal)
    assert not is_new_yapo_cohort(noncanonical_origin)
    assert commercial_visibility_exclusion_reasons(historic) == []


def test_full_unverified_owner_and_uncertain_rows_remain_reviewable():
    uncertain = _complete_yapo_doc("90020001", state="INCIERTO")
    owner = _complete_yapo_doc("90020002", state="DUEÑO_PROBABLE")
    terse_but_complete = _complete_yapo_doc(
        "26959400",
        batch_id="maipu_houses_3000_6000_20261103",
        source_batch_id="yapo_maipu_482_20261103_v3",
        title="Casa",
        description="Casa",
        price="140000000 CLP",
        precio_clp=140000000,
        precio_uf=3700,
        url="https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/26959400",
        canonical_url="https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa/26959400",
    )

    for document in (uncertain, owner, terse_but_complete):
        assert document["availability_status"] == "NO_VERIFICADO"
        assert is_commercially_usable_yapo_document(document)
        assert commercial_visibility_exclusion_reasons(document) == []


def test_new_yapo_capture_date_never_falls_back_to_last_update():
    document = _complete_yapo_doc("90025001", updated_at="2026-11-04T12:30:00+00:00")
    for field in ("first_seen", "first_seen_at", "created_at", "fecha_captura", "processed_at", "scraped_at"):
        document.pop(field, None)

    assert get_captacion_capture_datetime(document) is not None
    assert get_captacion_capture_datetime(document, allow_updated_at_fallback=False) is None


def test_mongo_predicate_excludes_only_bad_new_yapo_and_not_other_portals():
    collection = mongomock.MongoClient().URLS.propiedades_captacion
    valid = _complete_yapo_doc("90030001")
    invalid = _complete_yapo_doc(
        "90030002", title="Sin información", html_validation_status="NO_VERIFICADO",
    )
    removed = _complete_yapo_doc(
        "90030003", availability_status="DESACTIVADO", html_validation_status="LISTING_REMOVED",
        scrape_stage="ad_removed",
    )
    historic = _complete_yapo_doc(
        "90030004", batch_id="legacy", source_batch_id="legacy",
        html_validation_status="LISTING_REMOVED", scrape_stage="ad_removed",
    )
    toctoc = _complete_yapo_doc(
        "90030005", batch_id="legacy", source_batch_id="legacy",
        html_validation_status="LISTING_REMOVED", scrape_stage="ad_removed",
    )
    toctoc["origen"] = "toctoc"
    for document in (valid, invalid, removed, historic, toctoc):
        collection.insert_one(document)

    query = apply_captacion_visibility({"classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)}})
    returned = {doc["listing_id"] for doc in collection.find(query)}
    assert returned == {"90030001", "90030004", "90030005"}
    assert collection.count_documents(build_yapo_visibility_diagnostics_query(VISIBLE_CLASSIFICATION_STATES)) == 2


def test_list_count_sort_and_pagination_share_the_same_visibility_filter(monkeypatch):
    database = mongomock.MongoClient().URLS
    collection = database.propiedades_captacion
    for document in _eligible_batch_fixture():
        collection.insert_one(document)
    monkeypatch.setattr(api_captacion, "get_db", lambda: database)
    _reset_list_caches()

    pages = []
    for page in (1, 2, 8):
        items, total, _, _, _ = api_captacion.get_captacion_list(
            user_role="admin", page=page, limit=10, order_filter="recientes", return_portals=True,
        )
        pages.append(items)
        assert total == 71
        assert all(item["listing_id"] not in {"33215751", "33215722", "33213261", "33216485", "33215229"} for item in items)
    assert [len(items) for items in pages] == [10, 10, 1]

    filtered, filtered_total, _, _, _ = api_captacion.get_captacion_list(
        user_role="admin", page=2, limit=10, comuna_filter="Maipú",
        status_filter="NUEVO", operacion_filter="venta", property_type_filter="casa",
        order_filter=None, sort_by="precio", sort_dir="asc", return_portals=True,
    )
    assert filtered_total == 71
    assert len(filtered) == 10
    assert all(item["listing_id"] not in {"33215751", "33215722", "33213261"} for item in filtered)

    agent_items, agent_total, _ = api_captacion.get_captacion_list(
        user_role="agente", user_id="agent-with-no-assignment", page=1, limit=10,
    )
    assert agent_total == 0
    assert agent_items == []

    supervisor_items, supervisor_total, _ = api_captacion.get_captacion_list(
        user_role="supervisor", user_id="supervisor-with-no-assignment", page=1, limit=10,
    )
    assert supervisor_total == 71
    assert len(supervisor_items) == 10


def test_kpi_query_builder_uses_visibility_predicate():
    collection = mongomock.MongoClient().db.properties
    for document in _eligible_batch_fixture():
        collection.insert_one(document)
    queries = build_kpi_queries({
        "origen": "yapo",
        "classification.state": {"$in": list(VISIBLE_CLASSIFICATION_STATES)},
    })
    assert collection.count_documents(queries["available"]) == 71
    assert collection.count_documents(queries["management"]) == 0


def test_admin_diagnostics_http_endpoint_is_scoped_and_role_protected(monkeypatch):
    from fastapi.testclient import TestClient
    import chatbot.storage
    import webhook

    database = mongomock.MongoClient().URLS
    for document in _eligible_batch_fixture():
        database.propiedades_captacion.insert_one(document)
    monkeypatch.setattr(chatbot.storage, "get_db", lambda: database)

    async def admin_user(_request):
        return {"rol": "admin", "nombre": "Administrador"}

    monkeypatch.setattr(webhook, "get_current_user_doc", admin_user)
    client = TestClient(webhook.app)
    response = client.get("/api/captacion/visibility-diagnostics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["cohort_total"] == 76
    assert payload["classification_visible_total"] == 74
    assert payload["commercially_eligible_total"] == 71
    assert payload["excluded_total"] == 3
    by_id = {row["listing_id"]: row for row in payload["excluded"]}
    assert set(by_id) == {"33215751", "33215722", "33213261"}
    assert "anuncio_retirado_confirmado" in by_id["33215751"]["reasons"]
    assert "error_tecnico_de_extraccion" in by_id["33213261"]["reasons"]
    assert "anuncio_retirado_confirmado" not in by_id["33213261"]["reasons"]

    async def supervisor_user(_request):
        return {"rol": "supervisor", "nombre": "Supervisor"}

    monkeypatch.setattr(webhook, "get_current_user_doc", supervisor_user)
    supervisor_response = client.get("/api/captacion/visibility-diagnostics")
    assert supervisor_response.status_code == 200

    async def agent_user(_request):
        return {"rol": "agente", "nombre": "Ejecutivo"}

    monkeypatch.setattr(webhook, "get_current_user_doc", agent_user)
    denied = client.get("/api/captacion/visibility-diagnostics")
    assert denied.status_code == 403


def test_real_captacion_route_renders_only_eligible_rows_on_first_page(monkeypatch):
    import time

    from fastapi.testclient import TestClient
    import chatbot.storage
    import webhook

    database = mongomock.MongoClient().URLS
    for document in _eligible_batch_fixture():
        database.propiedades_captacion.insert_one(document)
    monkeypatch.setattr(api_captacion, "get_db", lambda: database)

    async def admin_user(_request):
        return {"_id": "admin-route-test", "rol": "admin", "nombre": "Administrador"}

    class AsyncCollection:
        async def find_one(self, *_args, **_kwargs):
            return {"revision": 0}

    class AsyncDatabase:
        def __getitem__(self, _name):
            return AsyncCollection()

    monkeypatch.setattr(webhook, "get_current_user_doc", admin_user)
    monkeypatch.setattr(chatbot.storage, "get_async_db", lambda: AsyncDatabase())
    monkeypatch.setattr(webhook, "_captacion_diag_is_first_module_visit", lambda: False)

    goal_key = "route-test-goal"
    monkeypatch.setattr(webhook, "_captacion_goal_cache_key", lambda *_args, **_kwargs: goal_key)
    now = time.time()
    monkeypatch.setattr(webhook.app.state, "captacion_goal_cache", {
        goal_key: {"time": now, "data": {
            "mode": "individual", "name": "Administrador", "week_count": 0,
            "week_goal": 0, "week_percent": 0, "expected_to_date": 0,
            "contact_attempts": 0, "effective_contacts": 0, "captures": 0,
            "daily": [], "weekend_activity": 0, "is_workday": False,
            "today_count": 0, "today_goal": 0, "today_percent": 0,
            "today_remaining": 0, "today_status": "SIN_META", "today_reason": "",
        }},
    }, raising=False)
    monkeypatch.setattr(webhook.app.state, "captacion_executives_cache", {
        "time": now, "data": [],
    }, raising=False)

    stats_key = f"stats_{webhook.CAPTACION_KPI_CACHE_VERSION}_global_admin"
    stats_record = {
        "time": now, "schema_version": webhook.CAPTACION_KPI_CACHE_SCHEMA,
        "kpi_revision": 0, "in_gestion_count": 0, "captados_count": 0,
        "descartados_count": 0, "available_count": 71, "ready_to_contact_count": 0,
        "pending_count": 71, "comunas_clean": [], "source_counts": [],
        "contact_type_counts": {"corredor": 0, "otros": 0},
        "worked_portal_counts": [], "contact_attempts": 0,
        "effective_contacts": 0, "contactability_pct": None,
        "contactability_result_buckets": [], "contactability_insight": "",
    }
    monkeypatch.setattr(webhook.app.state, "captacion_stats_cache", {
        stats_key: stats_record,
    }, raising=False)

    # The route's async path is cached above; all property reads run against
    # this in-memory Mongo fixture through api_captacion.get_db.
    client = TestClient(webhook.app)
    response = client.get("/captacion?orden=recientes&page=1")

    assert response.status_code == 200
    html = response.text
    assert "71 resultados" in html
    assert "Anunciante / indicador" in html
    assert "Dueño probable" in html or "Anunciante incierto" in html
    assert "Probabilidad no calculada" in html
    assert "Disponibilidad pendiente" in html
    assert "Fecha de captura" in html
    assert "33215751" not in html and "33215722" not in html and "33213261" not in html
    assert "33216485" not in html and "33215229" not in html
