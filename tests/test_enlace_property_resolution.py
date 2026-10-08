import asyncio

import mongomock
import pytest

from chatbot import link_extractor
from chatbot.processing_service import LeadProcessingService
from chatbot.property_lookup import (
    PROPERTY_COLLECTION_NAME,
    canonical_portal,
    extract_property_external_id,
    find_property_by_any_identifier,
    find_property_by_international_code,
    get_prop_location,
    get_prop_operation,
)


def _database(*properties):
    db = mongomock.MongoClient().URLS
    if properties:
        db[PROPERTY_COLLECTION_NAME].insert_many(list(properties))
    return db


def _property(code, international_id, *, commune="El Bosque", property_type="Casa"):
    return {
        "codigo": str(code),
        "codigo_internacional": international_id,
        "publicaciones": {
            "codigo_internacional": international_id,
            "codigo_internacional_por_operacion": {"V": international_id},
        },
        "estado": {"estado_prop360": "Activa"},
        "ubicacion": {"comuna": commune},
        "tipo_operacion": {
            "tipo": property_type,
            "venta": True,
            "precio_venta": {"precio_uf": 2500, "precio_clp": 95000000},
        },
    }


PAMELA_MESSAGE = (
    "Hola soy Pamela Bedregal, estoy interesado en que me den más información "
    "de la propiedad 101006508 que encontré en el portal Enlace BancoEstado.\n"
    "https://bancoestado.enlaceinmobiliario.cl/usados/el-bosque/casa/101006508/1349921"
)
ENLACE_URL = "https://bancoestado.enlaceinmobiliario.cl/usados/el-bosque/casa/101006508/1349921"


def _resolve(monkeypatch, db, message):
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)
    return link_extractor.resolver_referencia_propiedad(message)


def test_pamela_message_and_enlace_url_resolve_internal_code(monkeypatch):
    db = _database(_property("6508", "101006508"))

    result = _resolve(monkeypatch, db, PAMELA_MESSAGE)
    url_only = _resolve(monkeypatch, db, ENLACE_URL)

    assert result["status"] == "resolved"
    assert result["property"]["codigo"] == "6508"
    assert result["external_id"] == "101006508"
    assert result["platform"] == "Enlace Inmobiliario"
    assert url_only["property"]["codigo"] == "6508"
    assert extract_property_external_id(ENLACE_URL) == "101006508"
    assert "1349921" not in result["explicit_codes"]


def test_unknown_portal_with_explicit_id_resolves_without_claiming_its_identity(monkeypatch):
    db = _database(_property("6508", "101006508"))
    message = "Quiero información de la propiedad 101006508. https://sitio-de-ejemplo.cl/publicacion/999"

    result = _resolve(monkeypatch, db, message)

    assert result["status"] == "resolved"
    assert result["property"]["codigo"] == "6508"
    assert result["platform"] == "Otro Portal"
    assert result["property"]["_link_match"]["portal"] == ""
    assert result["property"]["_link_match"]["identity_source"] == "message_text"
    assert result["url"].startswith("https://sitio-de-ejemplo.cl/")


def test_explicit_id_without_url_uses_shared_exact_resolver(monkeypatch):
    db = _database(_property("6508", "101006508"))
    result = _resolve(monkeypatch, db, "Quiero información sobre la propiedad 101006508.")

    assert result["status"] == "resolved"
    assert result["has_url"] is False
    assert result["property"]["codigo"] == "6508"
    assert find_property_by_any_identifier(db, "101006508")["codigo"] == "6508"


@pytest.mark.parametrize(
    "identity_fields",
    [
        {"codigo_internacional": "101006508"},
        {"publicaciones": {"codigo_internacional": ["101006508"]}},
        {"publicaciones": {"codigo_internacional_por_operacion": {"V": "101006508"}}},
        {"publicaciones": {"codigo_internacional_por_operacion": {"A": "101006508"}}},
    ],
)
def test_exact_lookup_covers_verified_international_id_fields(identity_fields):
    db = _database({"codigo": "6508", **identity_fields})

    prop, meta = find_property_by_international_code(db, "101006508")

    assert prop["codigo"] == "6508"
    assert meta["match_method"] == "exact_international_code"


def test_conflicting_text_and_enlace_url_id_stay_unresolved(monkeypatch):
    first = _property("6508", "101006508")
    second = _property("6766", "101006766", commune="Colina")
    db = _database(first, second)
    message = (
        "Me interesa la propiedad 101006508. "
        "https://bancoestado.enlaceinmobiliario.cl/usados/colina/casa/101006766/1349921"
    )

    result = _resolve(monkeypatch, db, message)

    assert result["status"] == "ambiguous"
    assert result["property"] is None
    assert result["error_code"] == "IDENTIFIER_CONFLICT"


def test_ambiguous_duplicate_international_id_is_not_resolved(monkeypatch):
    db = _database(_property("6508", "101006508"), _property("6766", "101006508", commune="Colina"))

    prop, meta = find_property_by_international_code(db, "101006508")
    result = _resolve(monkeypatch, db, "La propiedad 101006508")

    assert prop is None
    assert meta["error_code"] == "AMBIGUOUS_PROPERTY_REFERENCE"
    assert result["status"] == "ambiguous"
    assert result["error_code"] == "AMBIGUOUS_PROPERTY_REFERENCE"


def test_unknown_code_and_unknown_url_are_not_guessed(monkeypatch):
    db = _database(_property("6508", "101006508"))

    missing = _resolve(monkeypatch, db, "Quiero la propiedad 109999999.")
    unknown_url = _resolve(monkeypatch, db, "https://sitio-nuevo.example/publicacion/101006508/1349921")

    assert missing["status"] == "unresolved"
    assert missing["property"] is None
    assert unknown_url["status"] == "unresolved"
    assert unknown_url["property"] is None
    assert unknown_url["external_id"] is None


def test_non_property_numbers_are_not_extracted_as_international_ids():
    message = "Mi teléfono es +56 9 87654321 y mi RUT es 12.345.678-9; cuesta 101006508 pesos."

    assert link_extractor.extraer_codigos_internacionales(message) == []
    assert link_extractor.extraer_codigo_internacional(message) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://enlaceinmobiliario.cl/usados/el-bosque/casa/101006508/1349921", "enlaceinmobiliario"),
        (ENLACE_URL, "enlaceinmobiliario"),
        ("https://bancoestado.enlaceinmobiliario.cl.attacker.example/usados/x/casa/101006508/1349921", ""),
        ("https://not-enlaceinmobiliario.cl/usados/x/casa/101006508/1349921", ""),
    ],
)
def test_enlace_hostname_validation(url, expected):
    assert canonical_portal(url) == expected


def test_supported_portal_hostname_regressions():
    cases = {
        "https://www.toctoc.com/propiedad/abc": "toctoc",
        "https://www.yapo.cl/propiedad/12345678": "yapo",
        "https://www.portalinmobiliario.com/MLC-1234567890": "portal_inmobiliario",
        "https://casa.mercadolibre.cl/MLC-1234567890": "mercadolibre",
        "https://www.procasa.cl/propiedad/6508": "procasa",
    }
    assert {url: canonical_portal(url) for url in cases} == cases


@pytest.mark.parametrize(
    ("url", "document"),
    [
        (
            "https://www.toctoc.com/propiedades/casa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            {"publicaciones": {"toctoc": {"url_toctoc": "https://www.toctoc.com/propiedades/casa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}},
        ),
        (
            "https://www.yapo.cl/bienes-raices/32979177",
            {"publicaciones": {"yapo": {"publicaciones": {"A": {"code": "32979177", "url": "https://www.yapo.cl/bienes-raices/32979177"}}}}},
        ),
        (
            "https://www.portalinmobiliario.com/MLC-4514084654-casa-en-venta",
            {"publicaciones": {"portal_inmobiliario": {"publicaciones": {"V": {"code": "MLC4514084654"}}}}},
        ),
        (
            "https://www.mercadolibre.cl/MLC-2268754129-casa-en-venta",
            {"publicaciones": {"portal_inmobiliario": {"publicaciones": {"V": {"code": "MLC-2268754129"}}}}},
        ),
        (
            "https://www.procasa.cl/venta/casa/6508",
            {"publicaciones": {"procasa": {"url_procasa": "https://www.procasa.cl/venta/casa/6508"}}},
        ),
    ],
)
def test_existing_portal_property_lookup_regressions(url, document):
    db = _database({"codigo": "6508", **document})

    prop, _meta = link_extractor.lookup_property_link(db, url, PROPERTY_COLLECTION_NAME)

    assert prop and prop["codigo"] == "6508"


def test_enlace_match_produces_canonical_property_context(monkeypatch):
    db = _database(_property("6508", "101006508"))
    result = _resolve(monkeypatch, db, PAMELA_MESSAGE)
    prop = result["property"]

    assert prop["codigo"] == "6508"
    assert get_prop_location(prop)["comuna"] == "El Bosque"
    assert get_prop_operation(prop)["tipo"] == "Casa"
    assert get_prop_operation(prop)["operacion"] == "Venta"
    assert prop["_link_match"]["external_id"] == "101006508"


def test_processing_service_uses_newest_reference_and_refreshes_old_context(monkeypatch):
    old_property = _property("6766", "101006766", commune="Colina", property_type="Departamento")
    new_property = _property("6508", "101006508", commune="El Bosque", property_type="Casa")
    db = _database(old_property, new_property)
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)
    monkeypatch.setattr(LeadProcessingService, "_db", staticmethod(lambda: db))
    lead = {
        "prospecto": {
            "codigo": "6766",
            "comuna": "Colina",
            "tipo": "Departamento",
            "operacion": "Venta",
        },
        "messages": [
            {"role": "user", "content": "Me interesa la propiedad 101006766."},
            {
                "role": "user",
                "content": "Me interesa la propiedad 101006508. https://sitio-nuevo.example/aviso/42",
            },
        ],
    }

    context = LeadProcessingService.classify(lead)

    assert context["comuna"] == "El Bosque"
    assert context["tipo"] == "CASA"
    assert context["operacion"] == "V"
    assert context["cluster_id"] == "EL BOSQUE-CASA-V"

    lead["messages"].append({
        "role": "user",
        "content": "Ahora me interesa la propiedad 109999999.",
    })
    assert LeadProcessingService.classify(lead) == {}


def test_core_processes_unknown_portal_code_through_canonical_property_flow(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "DEEPSEEK_API_KEY", "test-key")
    from chatbot import core, storage

    db = _database(_property("6508", "101006508"))
    monkeypatch.setattr(link_extractor, "get_db", lambda: db)
    phone = "56911112222"
    message = "Me interesa la propiedad 101006508. https://sitio-nuevo.example/aviso/42"
    prospect = {
        "codigo": "6766",
        "comuna": "Colina",
        "tipo": "Departamento",
        "operacion": "Venta",
    }
    lead = {"phone": phone, "prospecto": prospect}
    updates = []
    saved_messages = []
    viewed_properties = []

    class _LeadCollection:
        @staticmethod
        def find_one(_query):
            return lead

    class _CoreDB:
        def __getitem__(self, name):
            return _LeadCollection() if name == "leads" else db[name]

    def update_prospect(_phone, fields, *_args, **_kwargs):
        updates.append(dict(fields))
        prospect.update(fields)

    async def no_live_alert(**_kwargs):
        return {"status": "enqueued"}

    monkeypatch.setattr(core, "get_db", lambda: _CoreDB())
    monkeypatch.setattr(core, "guardar_mensaje", lambda *args, **_kwargs: saved_messages.append(args))
    monkeypatch.setattr(storage, "obtener_bot_pausado", lambda _phone: False)
    monkeypatch.setattr(core, "obtener_conversacion", lambda _phone: [{"role": "user", "content": message}])
    monkeypatch.setattr(core, "obtener_prospecto", lambda _phone: prospect)
    monkeypatch.setattr(core, "actualizar_prospecto", update_prospect)
    monkeypatch.setattr(core, "ensure_conversation_id", lambda _phone: "conversation-test")
    monkeypatch.setattr(core, "get_visit_data_state", lambda _phone: {"status": "not_offered"})
    monkeypatch.setattr(core, "record_observability_event", lambda *args, **_kwargs: None)
    monkeypatch.setattr(core, "registrar_propiedades_vistas", lambda _phone, codes: viewed_properties.extend(codes))
    monkeypatch.setattr(core, "obtener_propiedades_vistas", lambda _phone: [])
    monkeypatch.setattr(core, "es_propietario", lambda _phone: (False, None))
    monkeypatch.setattr(core, "clasificar_corredor_externo", lambda _message: {"is_external_broker": False})
    monkeypatch.setattr(core, "buscar_semanticamente", lambda *args, **_kwargs: [])
    monkeypatch.setattr(core, "extraer_filtros_estructurados", lambda _message: ({}, None))
    monkeypatch.setattr(core, "formatear_ficha_tecnica", lambda *_args, **_kwargs: "Ficha 6508")
    monkeypatch.setattr(core, "build_specific_property_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "send_alert_once", no_live_alert)
    monkeypatch.setattr(
        core, "generar_respuesta_estructurada",
        lambda *_args, **_kwargs: {
            "intencion": "consulta_general",
            "respuesta_bot": "Encontré la propiedad 6508 en El Bosque.",
            "datos_extraidos": {},
        },
    )
    monkeypatch.setattr(core.CrmService, "update_intent", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(core.CrmService, "get_lead", staticmethod(lambda _phone: lead))
    monkeypatch.setattr(core.CrmService, "calculate_score", staticmethod(lambda _lead: {"total": 0}))
    monkeypatch.setattr(storage, "get_pending_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(storage, "log_event", lambda *_args, **_kwargs: None)

    response = asyncio.run(core.process_user_message(phone, message))

    assert "6508" in response
    assert prospect["codigo"] == "6508"
    assert prospect["codigo_propiedad"] == "6508"
    assert prospect["comuna"] == "El Bosque"
    assert prospect["tipo"] == "Casa"
    assert prospect["operacion"] == "Venta"
    assert prospect["portal_origen"] == "Otro Portal"
    assert prospect["external_id_origen"] == "101006508"
    assert viewed_properties == ["6508"]
    assert saved_messages[0][1:3] == ("user", message)
    assert sum(1 for item in updates if item.get("codigo") == "6508") == 1

    message = "Quiero información sobre la propiedad 109999999."
    unresolved_response = asyncio.run(core.process_user_message(phone, message))

    assert "No pude confirmarlo" in unresolved_response
    assert prospect["link_pendiente"] is True
    assert viewed_properties == ["6508"]
