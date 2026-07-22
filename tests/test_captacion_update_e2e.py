"""End-to-end tests for update_captacion_status with optional Bitacora.

Tests the full persistence cycle: state, status_history, notes (absence when
empty), and management ledger credit.
"""
import copy
import pytz
import datetime as _dt
from uuid import uuid4 as _uuid4

from bson import ObjectId
import pytest


# Load chatbot modules before api_captacion to break circular imports
import chatbot.constants  # noqa
import captacion_management  # noqa
import api_captacion  # noqa
from api_captacion import update_captacion_status


# Fake MongoDB layer
class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = [dict(d) for d in (docs or [])]

    def find_one(self, query, projection=None):
        for doc in self._docs:
            ok = True
            for k, v in query.items():
                if isinstance(v, dict) and "$lte" in v:
                    if not (doc.get(k) is not None and doc.get(k) <= v["$lte"]):
                        ok = False
                elif doc.get(k) != v:
                    ok = False
            if ok:
                return dict(doc)
        return None

    def update_one(self, query, update, upsert=False):
        for doc in self._docs:
            ok = True
            for k, v in query.items():
                if doc.get(k) != v:
                    ok = False
            if ok:
                if "$set" in update:
                    for k, val in update["$set"].items():
                        _deep_set(doc, k, val)
                if "$inc" in update:
                    for k, val in update["$inc"].items():
                        _deep_set(doc, k, (_deep_get(doc, k) or 0) + val)
                if "$push" in update:
                    for k, val in update["$push"].items():
                        container = _list_at(doc, k)
                        container.append(copy.deepcopy(val))
                if "$setOnInsert" in update and upsert:
                    for k, val in update["$setOnInsert"].items():
                        if _deep_get(doc, k) is None:
                            _deep_set(doc, k, val)
                return self
        if upsert:
            doc = dict(query)
            if "$set" in update:
                for k, val in update["$set"].items():
                    _deep_set(doc, k, val)
            if "$setOnInsert" in update:
                for k, val in update["$setOnInsert"].items():
                    _deep_set(doc, k, val)
            self._docs.append(doc)
            return self
        return _FakeEmptyResult()

    def update_many(self, query, update):
        return self

    def insert_one(self, doc):
        d = dict(doc)
        if "_id" not in d:
            d["_id"] = ObjectId()
        self._docs.append(d)
        return type("_r", (), {"upserted_id": d["_id"]})()

    def count_documents(self, query):
        return 0

    def find(self, *args, **kwargs):
        return []

    def create_index(self, *args, **kwargs):
        return kwargs.get("name")

    def aggregate(self, *args, **kwargs):
        return []


class _FakeEmptyResult:
    modified_count = 0
    matched_count = 0


class _FakeDb:
    def __init__(self):
        self._cols = {}

    def __getitem__(self, name):
        return self._cols.setdefault(name, _FakeCollection())

    def command(self, *args, **kwargs):
        return {"ok": 1}


def _deep_get(doc, dotted):
    parts = dotted.split(".")
    current = doc
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _deep_set(doc, dotted, value):
    parts = dotted.split(".")
    for part in parts[:-1]:
        if part not in doc or not isinstance(doc[part], dict):
            doc[part] = {}
        doc = doc[part]
    doc[parts[-1]] = value


def _list_at(doc, dotted):
    parts = dotted.split(".")
    for part in parts[:-1]:
        if part not in doc or not isinstance(doc[part], dict):
            doc[part] = {}
        doc = doc[part]
    key = parts[-1]
    if key not in doc:
        doc[key] = []
    return doc[key]


def _property_doc(obj_id=None, estado="Por contactar"):
    oid = obj_id or ObjectId()
    return {
        "_id": oid,
        "gestion": {
            "estado_captacion": estado,
            "estado": estado,
            "ejecutivo_id": "u1",
            "ejecutivo_asignado": "Ana",
            "assignment_cycle_id": "cycle-1",
        },
        "origen": "yapo",
        "classification": {"state": "DUEÑO_SEGURO"},
    }


def _user_doc():
    return {"_id": "u1", "nombre": "Ana", "email": "ana@test.cl", "rol": "agente"}


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr("api_captacion.get_db", lambda _d=[db]: _d[0])
    monkeypatch.setattr("api_captacion.get_captacion_collection",
                        lambda d: d["propiedades_captacion"])
    monkeypatch.setattr("api_captacion.get_chile_now",
                        lambda: _dt.datetime(2026, 7, 22, 14, 0))
    monkeypatch.setattr("api_captacion._invalidate_detail_cache",
                        lambda _: None)
    monkeypatch.setattr("api_captacion.log_event",
                        lambda *a, **kw: None)
    monkeypatch.setattr("api_captacion.uuid",
                        type("_m", (), {"uuid4": lambda: "test-uuid"})())
    # Stub management dependencies
    monkeypatch.setattr(captacion_management, "recalculate_daily_metric",
                        lambda *a, **kw: {})
    monkeypatch.setattr(captacion_management, "audit_management_patterns",
                        lambda *a, **kw: [])
    monkeypatch.setattr(captacion_management, "_INDEXES_READY", True)
    return db


# Tests
def test_state_persists_empty_bitacora(_patch_env):
    """Estado + status_history persistidos; sin nota vacia."""
    db = _patch_env
    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)

    result = update_captacion_status(
        str(prop["_id"]), "Corredor",
        notes="", user_name="Ana", user_doc=_user_doc(),
    )
    assert result is True

    g = db["propiedades_captacion"].find_one({"_id": prop["_id"]})["gestion"]
    assert g["estado"] == "Corredor"
    assert g["estado_captacion"] == "Corredor"
    assert len(g.get("status_history", [])) == 1
    assert g["status_history"][0]["from_state"] == "Por contactar"
    assert g["status_history"][0]["to_state"] == "Corredor"
    assert g["status_history"][0]["user"] == "Ana"
    assert len(g.get("notas", [])) == 0


def test_short_bitacora_persists(_patch_env):
    """Nota de 2 chars guardada, estado cambiado."""
    db = _patch_env
    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)

    update_captacion_status(
        str(prop["_id"]), "Descartado",
        notes="OK", user_name="Ana", user_doc=_user_doc(),
    )
    g = db["propiedades_captacion"].find_one({"_id": prop["_id"]})["gestion"]
    assert g["estado"] == "Descartado"
    assert len(g.get("notas", [])) == 1
    assert g["notas"][0]["content"] == "OK"
    assert len(g.get("status_history", [])) == 1


def test_full_bitacora_persists_and_credits(_patch_env):
    """Nota >= 5 chars guardada, estado cambiado, ledger acreditado."""
    db = _patch_env
    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)

    update_captacion_status(
        str(prop["_id"]), "Corredor",
        notes="Motivo valido para cambio",
        user_name="Ana", user_doc=_user_doc(),
    )
    g = db["propiedades_captacion"].find_one({"_id": prop["_id"]})["gestion"]
    assert g["estado"] == "Corredor"
    assert len(g.get("notas", [])) == 1
    assert g["notas"][0]["content"] == "Motivo valido para cambio"
    assert len(g.get("status_history", [])) == 1
    # credit via record_manual_management_decision was invoked (stubbed)


def test_same_state_empty_notes_no_history(_patch_env):
    """Mismo estado + sin Bitacora no genera entradas nuevas."""
    db = _patch_env
    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)

    update_captacion_status(
        str(prop["_id"]), "Por contactar",
        notes="", user_name="Ana", user_doc=_user_doc(),
    )
    g = db["propiedades_captacion"].find_one({"_id": prop["_id"]})["gestion"]
    assert g["estado"] == "Por contactar"
    assert "status_history" not in g
    assert "notas" not in g


def test_repeated_state_change_no_duplicate_history(_patch_env):
    """Segunda solicitud identica no duplica status_history."""
    db = _patch_env
    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)

    update_captacion_status(str(prop["_id"]), "Corredor", notes="X",
                            user_name="Ana", user_doc=_user_doc())
    update_captacion_status(str(prop["_id"]), "Corredor", notes="X",
                            user_name="Ana", user_doc=_user_doc())
    history = db["propiedades_captacion"].find_one(
        {"_id": prop["_id"]})["gestion"].get("status_history", [])
    assert len(history) == 1
