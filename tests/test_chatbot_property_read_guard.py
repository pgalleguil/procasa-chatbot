"""Contract tests: chatbot must never receive raw Prop360 documents."""
import pytest

from chatbot.property_read_guard import (
    SAFE_PROPERTY_PROJECTION,
    chatbot_safe_property_db,
)


class FakeCursor:
    def __init__(self, documents):
        self.docs = list(documents)
    def sort(self, *args, **kwargs):
        return self
    def limit(self, count):
        self.docs = self.docs[:count]
        return self
    def __iter__(self):
        return iter(self.docs)


class FakeCollection:
    def __init__(self, documents):
        self.docs = list(documents)
        self.projections = []
    def find_one(self, query, projection=None, *args, **kwargs):
        self.projections.append(projection)
        return self.docs[0] if self.docs else None
    def find(self, query=None, projection=None, *args, **kwargs):
        self.projections.append(projection)
        return FakeCursor(self.docs)
    def aggregate(self, *args, **kwargs):
        raise AssertionError("aggregate must never be invoked")


def property_with_secrets():
    return {
        "_id": "SENSITIVE_ID", "codigo": "6436",
        "disponible_prop360": True,
        "ubicacion": {
            "comuna": "San Miguel", "region": "Metropolitana",
            "calle": "SECRET CALLE 123", "unidad": "SECRET_UNIT_68",
            "direccion_referencial": "SECRET_REFERENCE",
            "lat": -33.4, "lng": -70.6, "sector": "SECRET_SECTOR",
        },
        "nombre_calle": "SECRET_ROOT_STREET",
        "direccion": "SECRET_ROOT_ADDRESS",
        "observaciones": {
            "descripcion": "SECRET_MESSAGE_AND_ADDRESS",
            "notas_internas": "SECRET_NOTES",
        },
        "descripcion_clean": "SECRET_DESCRIPTION",
        "datos_propietario": {"telefono": "SECRET_PHONE"},
        "propietario": "SECRET_OWNER",
        "metadata": {"lat": -33.4, "internal": "SECRET_METADATA"},
        "publicaciones": {"procasa": {"url_procasa": "SECRET_PUBLICATION_URL"}},
        "tipo_operacion": {
            "tipo": "Departamento", "venta": True,
            "precio_venta": {"precio_uf": 2690, "precio_clp": 110713487},
            "gastos_comunes": 85000,
            "owner_bank_account": "SECRET_ACCOUNT",
        },
        "caracteristicas": {
            "dormitorios": 2, "banos": 2,
            "orientacion": "Sur-Poniente",
            "features": ["piscina"],
            "observacion_privada": "SECRET_FEATURE",
        },
    }


def test_strict_inclusion_projection_excludes_sensitives():
    assert SAFE_PROPERTY_PROJECTION["_id"] == 0
    assert SAFE_PROPERTY_PROJECTION["ubicacion.comuna"] == 1
    for denied in (
        "ubicacion", "ubicacion.calle", "ubicacion.direccion_referencial",
        "ubicacion.unidad", "ubicacion.sector", "nombre_calle", "direccion",
        "observaciones", "descripcion_clean", "datos_propietario",
        "metadata", "publicaciones", "historial_cambios", "propietario",
    ):
        assert denied not in SAFE_PROPERTY_PROJECTION


def test_raw_return_ignoring_projection_is_still_sanitized():
    raw = FakeCollection([property_with_secrets()])
    db = chatbot_safe_property_db({"universo_cartera_prop360": raw})
    result = db["universo_cartera_prop360"].find_one({"codigo": "6436"})
    assert raw.projections == [SAFE_PROPERTY_PROJECTION]
    assert result["codigo"] == "6436"
    assert result["ubicacion"] == {
        "comuna": "San Miguel", "region": "Metropolitana"
    }
    assert result["caracteristicas"]["orientacion"] == "Sur-Poniente"
    assert "SECRET" not in repr(result)
    assert "_id" not in result


def test_find_cursor_sort_limit_never_returns_unsafe_docs():
    raw = FakeCollection([property_with_secrets(), property_with_secrets()])
    db = chatbot_safe_property_db({"universo_cartera_prop360": raw})
    docs = list(
        db["universo_cartera_prop360"].find({"codigo": "6436"}, {"ubicacion": 1})
        .sort([("codigo", 1)]).limit(1)
    )
    assert len(docs) == 1
    assert docs[0]["ubicacion"] == {
        "comuna": "San Miguel", "region": "Metropolitana"
    }
    assert "SECRET" not in str(docs)
    assert raw.projections == [SAFE_PROPERTY_PROJECTION]


def test_property_view_disallows_unprojected_other_methods():
    raw = FakeCollection([property_with_secrets()])
    scoped = chatbot_safe_property_db({"universo_cartera": raw})
    with pytest.raises(PermissionError):
        scoped["universo_cartera"].aggregate([])
    with pytest.raises(PermissionError):
        scoped["universo_cartera"].update_one({}, {})


def test_regular_crm_collection_is_unmodified():
    leads = FakeCollection([{"phone": "TEST_PHONE", "internal_note": "CRM_RETAINED"}])
    db = chatbot_safe_property_db({"universo_cartera_prop360": FakeCollection([]), "leads": leads})
    assert db["leads"] is leads
    assert db["leads"].find_one({"phone": "TEST_PHONE"})["internal_note"] == "CRM_RETAINED"


def test_scope_is_idempotent():
    raw = FakeCollection([property_with_secrets()])
    db = chatbot_safe_property_db({"universo_cartera_prop360": raw})
    assert chatbot_safe_property_db(db) is db


def test_nested_unknown_fields_cannot_be_smuggled_through_allowed_parents():
    raw = property_with_secrets()
    raw["caracteristicas"]["features"] = {
        "mascotas": True, "secreto": "SECRET_NESTED",
    }
    scoped = chatbot_safe_property_db({"universo_cartera_prop360": FakeCollection([raw])})
    doc = scoped["universo_cartera_prop360"].find_one({})
    assert "features" not in doc["caracteristicas"]
    assert "SECRET" not in str(doc)


def test_customer_facing_entrypoints_use_scoped_property_reader():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "chatbot"
    assert "db = chatbot_safe_property_db(db)" in (root / "core.py").read_text(encoding="utf-8")
    link = (root / "link_extractor.py").read_text(encoding="utf-8")
    rag = (root / "rag.py").read_text(encoding="utf-8")
    assert link.count("db = chatbot_safe_property_db(get_db())") >= 3
    assert rag.count("db = chatbot_safe_property_db(get_db())") >= 2

def test_standard_chatbot_property_resolution_never_sees_raw_address():
    from chatbot.core import _buscar_propiedad_en_universo
    raw = FakeCollection([property_with_secrets()])
    db = {
        "universo_cartera_prop360": raw,
        "universo_cartera": FakeCollection([]),
    }
    resolved = _buscar_propiedad_en_universo(db, "6436")
    assert resolved["codigo"] == "6436"
    assert "SECRET" not in str(resolved)
    assert raw.projections and all(
        p == SAFE_PROPERTY_PROJECTION for p in raw.projections
    )
