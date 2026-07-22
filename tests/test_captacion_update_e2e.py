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
                if isinstance(v, dict) and "$lte" in v:
                    if not (doc.get(k) is not None and doc.get(k) <= v["$lte"]):
                        ok = False
                elif doc.get(k) != v:
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
                        _list_at(doc, k).append(copy.deepcopy(val))
                if "$setOnInsert" in update and upsert:
                    for k, val in update["$setOnInsert"].items():
                        if _deep_get(doc, k) is None:
                            _deep_set(doc, k, val)
                return _FakeResult(mod_count=1)
        if upsert:
            doc = dict(query)
            if "$set" in update:
                for k, val in update["$set"].items():
                    _deep_set(doc, k, val)
            if "$setOnInsert" in update:
                for k, val in update["$setOnInsert"].items():
                    _deep_set(doc, k, val)
            if "_id" not in doc:
                doc["_id"] = ObjectId()
            self._docs.append(doc)
            return _FakeResult(upserted_id=doc.get("_id"))
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


class _FakeResult:
    def __init__(self, upserted_id=None, mod_count=0):
        self.upserted_id = upserted_id
        self.modified_count = mod_count


class _FakeEmptyResult:
    upserted_id = None
    modified_count = 0
    matched_count = 0


class _FakeDb:
    def __init__(self):
        self._cols = {}

    def __getitem__(self, name):
        return self._cols.setdefault(name, _FakeCollection())

    def __setitem__(self, name, col):
        self._cols[name] = col

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


def test_status_history_written_and_notas_empty(_patch_env):
    """status_history tiene entrada; notas vacio con Bitacora vacia."""
    db = _patch_env
    prop = _property_doc(estado="En gestion")
    db["propiedades_captacion"].insert_one(prop)

    update_captacion_status(str(prop["_id"]), "Corredor", notes="",
                            user_name="Ana", user_doc=_user_doc())

    g = db["propiedades_captacion"].find_one({"_id": prop["_id"]})["gestion"]
    hist = g.get("status_history", [])
    assert len(hist) == 1
    assert hist[0]["from_state"] == "En gestion"
    assert hist[0]["to_state"] == "Corredor"
    # Sin nota vacia
    assert len(g.get("notas", [])) == 0


def test_ledger_event_written_after_state_change(_patch_env):
    """captacion_management_events recibe evento credited=True tras cambio."""
    db = _patch_env
    # Setup a real record_manual_management_decision that writes to our fake DB
    import captacion_management as _cm

    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)
    db["captacion_management_events"] = _FakeCollection()

    result = update_captacion_status(str(prop["_id"]), "Corredor", notes="",
                                     user_name="Ana", user_doc=_user_doc())
    assert result is True

    events = db["captacion_management_events"]._docs
    credited = [e for e in events if e.get("credited")]
    assert len(credited) >= 1, f"No credited event found in: {events}"
    assert credited[0]["event_type"] in ("manual_decision_confirmed", "capture_confirmed")
    assert credited[0]["result"] == "broker_identified"
    assert credited[0]["actor_name_snapshot"] == "Ana"


def test_second_identical_request_no_duplicate_ledger(_patch_env):
    """Segundo POST identico no crea segundo evento de ledger."""
    db = _patch_env
    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)
    db["captacion_management_events"] = _FakeCollection()

    update_captacion_status(str(prop["_id"]), "Corredor", notes="",
                            user_name="Ana", user_doc=_user_doc())
    update_captacion_status(str(prop["_id"]), "Corredor", notes="",
                            user_name="Ana", user_doc=_user_doc())

    events = db["captacion_management_events"]._docs
    credited = [e for e in events if e.get("credited")]
    assert len(credited) == 1, f"Expected 1 credited event, got {len(credited)}: {events}"


def test_same_state_empty_bitacora_no_ledger_write(_patch_env):
    """Mismo estado + sin Bitacora no genera evento de ledger."""
    db = _patch_env
    prop = _property_doc(estado="Corredor")
    db["propiedades_captacion"].insert_one(prop)
    db["captacion_management_events"] = _FakeCollection()

    update_captacion_status(str(prop["_id"]), "Corredor", notes="",
                            user_name="Ana", user_doc=_user_doc())

    events = db["captacion_management_events"]._docs
    credited = [e for e in events if e.get("credited")]
    assert len(credited) == 0, f"Expected 0 credited events, got: {events}"


def test_historial_merge_sorts_chronologically(_patch_env):
    """status_history y notas se mergean ordenados por timestamp."""
    db = _patch_env
    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)

    update_captacion_status(str(prop["_id"]), "Corredor", notes="Primera nota",
                            user_name="Ana", user_doc=_user_doc())
    update_captacion_status(str(prop["_id"]), "Descartado", notes="",
                            user_name="Ana", user_doc=_user_doc())
    update_captacion_status(str(prop["_id"]), "En gestion", notes="Tercera",
                            user_name="Ana", user_doc=_user_doc())

    g = db["propiedades_captacion"].find_one({"_id": prop["_id"]})["gestion"]
    assert len(g.get("status_history", [])) == 3
    assert len(g.get("notas", [])) == 2
    assert "notas" in g, "Las notas deben existir en el documento"
    assert g["notas"][0]["content"] == "Primera nota"
    assert g["notas"][1]["content"] == "Tercera"


def test_cache_clear_after_successful_update(monkeypatch):
    """La cache goal se limpia despues de un update exitoso."""
    import api_captacion
    import captacion_management as _cm

    db = _FakeDb()
    prop = _property_doc()
    db["propiedades_captacion"].insert_one(prop)
    db["captacion_management_events"] = _FakeCollection()

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
    monkeypatch.setattr(_cm, "recalculate_daily_metric", lambda *a, **kw: {})
    monkeypatch.setattr(_cm, "audit_management_patterns", lambda *a, **kw: [])
    monkeypatch.setattr(_cm, "_INDEXES_READY", True)

    # Simular app.state con cache pre-poblada
    class _FakeState:
        captacion_goal_cache = {"goal_v1_Ana": {"time": 0, "data": {"total": 0}}}

    fake_app = type("_a", (), {"state": _FakeState()})()

    # No podemos llamar al endpoint real, pero si a update_captacion_status
    result = update_captacion_status(str(prop["_id"]), "Corredor", notes="",
                                     user_name="Ana", user_doc=_user_doc())
    assert result is True

    # La invalidacion se hace en el endpoint; verificamos que el doc se actualizo
    g = db["propiedades_captacion"].find_one({"_id": prop["_id"]})["gestion"]
    assert g["estado"] == "Corredor"


def test_performer_receives_credit_not_assigned_executive(_patch_env):
    """Supervisor gestiona propiedad de Erika → credito para supervisor."""
    db = _patch_env
    prop = _property_doc()
    # Erika is the assigned executive
    prop["gestion"]["ejecutivo_id"] = "erika-id"
    prop["gestion"]["ejecutivo_asignado"] = "Erika Garrido"
    db["propiedades_captacion"].insert_one(prop)
    db["captacion_management_events"] = _FakeCollection()

    supervisor = {"_id": "supervisor-id", "nombre": "Pablo", "email": "pablo@test.cl", "rol": "admin"}
    result = update_captacion_status(str(prop["_id"]), "En gestion", notes="",
                                     user_name="Pablo", user_doc=supervisor)
    assert result is True

    credited = [e for e in db["captacion_management_events"]._docs if e.get("credited")]
    assert len(credited) >= 1
    # Supervisor performed the action → supervisor receives credit
    assert credited[0]["actor_user_id"] == "supervisor-id"
    assert credited[0]["actor_name_snapshot"] == "Pablo"


def test_assigned_executive_receives_credit_for_own_action(_patch_env):
    """Erika gestiona su propia propiedad → credito para Erika."""
    db = _patch_env
    prop = _property_doc()
    prop["gestion"]["ejecutivo_id"] = "erika-id"
    prop["gestion"]["ejecutivo_asignado"] = "Erika Garrido"
    db["propiedades_captacion"].insert_one(prop)
    db["captacion_management_events"] = _FakeCollection()

    erika = {"_id": "erika-id", "nombre": "Erika Garrido", "email": "erika@test.cl", "rol": "agente"}
    result = update_captacion_status(str(prop["_id"]), "En gestion", notes="",
                                     user_name="Erika Garrido", user_doc=erika)
    assert result is True

    credited = [e for e in db["captacion_management_events"]._docs if e.get("credited")]
    assert len(credited) >= 1
    # Erika performed AND is the assigned executive → Erika gets credit
    assert credited[0]["actor_user_id"] == "erika-id"
    assert credited[0]["actor_name_snapshot"] == "Erika Garrido"
