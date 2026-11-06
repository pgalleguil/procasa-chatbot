"""Public facts, confidentiality and combined fact/visit regressions."""
from chatbot.core import formatear_ficha_tecnica
from chatbot.customer_response_semantics import build_specific_property_response
from chatbot.price_drop_v2_customer_response import stabilize_v2_reply
from chatbot.public_property_facts import (
    public_property_summary, answer_public_property_question,
)
from chatbot.property_read_guard import _filter_property, SAFE_PROPERTY_PROJECTION


def sample():
    return {
        "codigo": "6436", "disponible_prop360": True,
        "ubicacion": {"comuna": "San Miguel", "region": "Metropolitana",
                      "calle": "CALLE_PRIVADA_123", "numero": "999",
                      "unidad": "DEPTO_PRIVADO_800", "sector": "SECTOR_PRIVADO"},
        "datos_propietario": {"nombre": "NOMBRE_PROPIETARIO_SECRETO",
                              "telefono": "+56999999999", "email": "secret@local.cl"},
        "propietario": "NOMBRE_PROPIETARIO_SECRETO",
        "descripcion": "DIRECCION_SECRETA_NO_LEER",
        "observaciones": {"descripcion": "TELEFONO_SECRETO_NO_LEER",
                          "notas_internas": "NOTAS_SECRETAS"},
        "tipo_operacion": {"tipo": "Departamento", "venta": True,
                           "precio_venta": {"precio_uf": 2690},
                           "gastos_comunes": 85000,
                           "owner_email": "otro@local.cl"},
        "caracteristicas": {
            "dormitorios": 2, "banos": 2, "superficie_util": 60,
            "superficie_total": 65, "superficie_terraza": 5,
            "piso": 6, "numero_pisos": 12, "ascensores": 2,
            "bodegas": 1, "orientacion": "Sur-Poniente",
            "ano_construccion": 2002,
            "features": ["piscina", "has_grill",
                         "dueño=SECRETO +56999999999", "has_terrace"],
        },
    }


def test_full_public_ficha_restored_for_both_customer_flows():
    data = sample()
    raw = public_property_summary(data)
    normal = formatear_ficha_tecnica(data, lead_executive="NO_PUBLICAR")
    rebaja = build_specific_property_response("Quiero toda la información y características", data)
    assert normal == raw == rebaja
    for fact in ("6436", "San Miguel", "UF 2.690", "$85.000",
                 "Dormitorios: 2", "Baños: 2", "60 m²", "65 m²",
                 "Piso: 6", "Pisos del edificio: 12", "Ascensores: 2",
                 "Sur-Poniente", "piscina", "quincho/parrilla"):
        assert fact in raw
    for secret in ("CALLE_PRIVADA", "DEPTO_PRIVADO", "NOMBRE_PROPIETARIO",
                   "+56999999999", "secret@local", "DIRECCION_SECRETA",
                   "TELEFONO_SECRETO", "NOTAS_SECRETAS",
                   "NO_PUBLICAR", "SECTOR_PRIVADO"):
        assert secret not in raw


def test_exact_address_and_owner_information_not_available():
    for prompt in ("¿Cuál es la dirección exacta y nombre del dueño?",
                   "¿Cuál es el teléfono del propietario?",
                   "Dame el correo del dueño y los gastos comunes"):
        result = answer_public_property_question(prompt, sample())
        assert "Por privacidad" in result
        assert "+56999999999" not in result
        assert "NOMBRE_PROPIETARIO_SECRETO" not in result
        assert "CALLE_PRIVADA" not in result
    result = answer_public_property_question("¿Dónde queda?", sample())
    assert "San Miguel" in result and "CALLE_PRIVADA" not in result


def test_multi_fact_questions_answer_all_not_just_two():
    answer = build_specific_property_response(
        "¿Cuánto cuesta, qué piso es, cuántos dormitorios, baños, metros, "
        "estacionamientos, gastos comunes y orientación tiene?", sample())
    for part in ("Precio: UF 2.690", "Piso: 6", "Dormitorios: 2",
                 "Baños: 2", "Gastos comunes: $85.000",
                 "Orientación: Sur-Poniente"):
        assert part in answer
    assert "Estacionamientos: la ficha no informa" in answer


def test_concurrent_visit_request_preserves_all_verified_facts():
    text = ("Me interesa el 6436, ¿cuántos gastos comunes, qué piso, "
            "orientación y superficie tiene? Quisiera visitarlo mañana a las 10.")
    facts = build_specific_property_response(text, sample())
    result = stabilize_v2_reply(
        text, facts,
        context={"version":"price_drop_leads_v2",
                 "property_code":"6436", "new_price_uf":2690},
        visit_preference="mañana a las 10",
    )
    for fact in ("$85.000", "Piso: 6", "Orientación: Sur-Poniente"):
        assert fact in result
    assert "mañana a las 10" in result
    assert "todavía no está confirmada" in result
    assert "administración" in result
    assert "un ejecutivo debe" not in result


def test_defense_in_depth_rejects_private_free_text_in_allowed_fields():
    raw = sample()
    raw["ubicacion"]["comuna"] = "nombre: secreto@local.cl"
    raw["caracteristicas"]["orientacion"] = "Sur +56999999999"
    view = _filter_property(raw)
    assert "comuna" not in view.get("ubicacion", {})
    assert "orientacion" not in view.get("caracteristicas", {})
    assert view["caracteristicas"]["features"] == [
        "piscina", "has_grill", "has_terrace"]
    assert "propietario" not in view
    assert "observaciones" not in view
    assert "ubicacion.calle" not in SAFE_PROPERTY_PROJECTION
    assert "datos_propietario" not in SAFE_PROPERTY_PROJECTION


def test_no_invented_property_characteristics():
    result = build_specific_property_response(
        "¿Cuánto son sus gastos comunes, orientación y estacionamientos?",
        {"codigo": "6436"},
    )
    assert result.count("la ficha no informa") == 3
    assert "$85.000" not in result
