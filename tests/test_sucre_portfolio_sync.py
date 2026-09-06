import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mongomock

import chatbot.sucre_portfolio_sync as sync


def _row(code="100", price="4.500 UF", status="Activa"):
    return {
        "codigo": code,
        "precio": price,
        "estado": status,
        "operacion": "Venta",
        "tipo": "Casa",
        "comuna": "Santiago",
        "captador": "Ejecutivo",
        "direccion": "Dirección",
        "region": "Metropolitana",
    }


class FakeClient:
    def __init__(self, rows, complete=True):
        self.rows = rows
        self.complete = complete
        self.logged_in = False
        self.fetch_office = None

    def login(self):
        self.logged_in = True
        return True

    def fetch_listing(self, office_id):
        self.fetch_office = office_id
        self.last_listing_meta = {
            "office_id": office_id,
            "pages": 1,
            "page_sizes": [len(self.rows)],
            "rows": len(self.rows),
            "reported_total": len(self.rows),
            "response_valid": True,
            "complete": self.complete,
        }
        return self.rows


def _db():
    return mongomock.MongoClient()["URLS"]


def _doc(row, available=True):
    return {
        "codigo": row["codigo"],
        "oficina_id": 7,
        "oficina_nombre": "PROCASA SUCRE",
        "disponible_prop360": available,
        "resumen": {
            "oficina": "PROCASA SUCRE",
            "ejecutivo": row["captador"],
            "snapshot_listado": row,
            "listing_fingerprint": sync.listing_fingerprint(row),
        },
    }


def _patch_detail(monkeypatch, calls):
    def fake_scrape(client, code, listing_row):
        calls.append(("scrape", code))
        return {"codigo": code, "resumen": {"oficina": "PROCASA SUCRE"}}

    def fake_upsert(coll, doc, **kwargs):
        calls.append(("upsert", doc["codigo"]))
        return (True, False)

    monkeypatch.setattr(sync, "scrape_propiedad", fake_scrape)
    monkeypatch.setattr(sync, "upsert_ficha", fake_upsert)


def test_new_property_enters_detail_and_inserts(monkeypatch):
    db = _db()
    calls = []
    _patch_detail(monkeypatch, calls)
    result = sync.run_sucre_portfolio_sync(
        db=db, prop360_client=FakeClient([_row("100")]), apply_bajas=False
    )
    assert result["status"] == "completed"
    assert result["nuevas"] == 1
    assert result["fichas_completas_requeridas"] == 1
    assert result["fichas_completas_consultadas"] == 1
    assert calls == [("scrape", "100"), ("upsert", "100")]


def test_existing_without_change_does_not_enter_detail(monkeypatch):
    db = _db()
    row = _row("100")
    db[sync.COLLECTION_NAME].insert_one(_doc(row))
    calls = []
    _patch_detail(monkeypatch, calls)
    result = sync.run_sucre_portfolio_sync(db=db, prop360_client=FakeClient([row]))
    assert result["sin_cambios_operativos"] == 1
    assert result["modificadas"] == 0
    assert calls == []


def test_price_change_updates_from_listing_without_detail(monkeypatch):
    db = _db()
    db[sync.COLLECTION_NAME].insert_one(_doc(_row("100", "4.500 UF")))
    calls = []
    _patch_detail(monkeypatch, calls)
    result = sync.run_sucre_portfolio_sync(
        db=db, prop360_client=FakeClient([_row("100", "4.700 UF")])
    )
    assert result["modificadas"] == 1
    assert result["fichas_completas_requeridas"] == 0
    assert result["fichas_completas_consultadas"] == 0
    sample = result["operational_change_samples"][0]
    assert sample["changes"]["precio_publicado"] == {
        "moneda_mongo": "UF",
        "monto_mongo": 4500,
        "moneda_prop360": "UF",
        "monto_prop360": 4700,
    }
    assert calls == []


def test_many_unchanged_generate_no_detail_requests(monkeypatch):
    db = _db()
    rows = [_row(str(i)) for i in range(10)]
    db[sync.COLLECTION_NAME].insert_many([_doc(row) for row in rows])
    calls = []
    _patch_detail(monkeypatch, calls)
    result = sync.run_sucre_portfolio_sync(db=db, prop360_client=FakeClient(rows))
    assert result["sin_cambios_operativos"] == 10
    assert result["procesadas"] == 0
    assert result["fichas_completas_requeridas"] == 0
    assert calls == []


def test_possible_baja_and_apply_bajas_false_do_not_write(monkeypatch):
    db = _db()
    db[sync.COLLECTION_NAME].insert_one(_doc(_row("100")))
    result = sync.run_sucre_portfolio_sync(
        db=db, prop360_client=FakeClient([_row("200")]), apply_bajas=False
    )
    assert result["posibles_bajas"] == 1
    assert result["bajas_aplicadas"] == 0
    assert db[sync.COLLECTION_NAME].find_one({"codigo": "100"})["disponible_prop360"] is True


def test_incomplete_listing_fails_and_does_not_apply_bajas(monkeypatch):
    db = _db()
    db[sync.COLLECTION_NAME].insert_one(_doc(_row("100")))
    result = sync.run_sucre_portfolio_sync(
        db=db, prop360_client=FakeClient([_row("200")], complete=False), apply_bajas=True
    )
    assert result["status"] == "failed"
    assert result["bajas_omitidas"] is True
    assert db[sync.COLLECTION_NAME].find_one({"codigo": "100"})["disponible_prop360"] is True


def test_second_execution_is_rejected_while_lock_is_running():
    db = _db()
    from datetime import datetime, timedelta, timezone

    db[sync.LOCK_COLLECTION].insert_one({
        "_id": sync.SYNC_KEY,
        "status": "running",
        "run_id": "other",
        "lease_until": datetime.now(timezone.utc) + timedelta(minutes=10),
    })
    result = sync.run_sucre_portfolio_sync(db=db, prop360_client=FakeClient([_row()]))
    assert result["status"] == "already_running"


def test_incremental_service_does_not_import_embeddings():
    assert "semantic_engine" not in sync.__dict__


def test_admin_authorization_contract():
    assert sync.is_admin_user({"rol": "admin"}) is True
    assert sync.is_admin_user({"rol": "supervisor"}) is False
    assert sync.is_admin_user(None) is False


def test_dry_run_never_writes_portfolio_or_applies_bajas(monkeypatch):
    db = _db()
    row = _row("100")
    db[sync.COLLECTION_NAME].insert_one(_doc(row))
    calls = []
    _patch_detail(monkeypatch, calls)
    result = sync.run_sucre_portfolio_sync(
        db=db, prop360_client=FakeClient([_row("100", "UF 4.700")]),
        dry_run=True, apply_bajas=True,
    )
    assert result["status"] == "completed"
    assert result["bajas_aplicadas"] == 0
    assert result["apply_bajas"] is True
    assert db[sync.COLLECTION_NAME].find_one({"codigo": "100"})["disponible_prop360"] is True
    assert calls == []


def test_endpoint_is_admin_only_and_server_forces_dry_run():
    source = open("webhook.py", encoding="utf-8").read()
    start = source.index('@app.post("/api/crm/portfolio-sync/sucre/dry-run")')
    route = source[start: source.index('\n\n@app.post("/api/session/renew")', start)]
    assert "Depends(get_current_user_doc)" in route
    assert "is_admin_user(user_doc)" in route
    assert "dry_run=True" in route
    assert "apply_bajas=False" in route
    assert "asyncio.to_thread" in route
    assert "mark_bajas" not in route
    assert "semantic_engine" not in route


def test_scheduler_stays_disabled_and_leads_loop_stays_started():
    source = open("webhook.py", encoding="utf-8").read()
    start = source.index("# PROCASA SUCRE ficha sync")
    end = source.index("# UF sync diario", start)
    block = source[start:end]
    assert "asyncio.create_task" not in block
    assert "prop360_task = asyncio.create_task(_ppl())" in source
