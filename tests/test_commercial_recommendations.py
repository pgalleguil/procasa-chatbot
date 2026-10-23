"""Regression suite: pilot must be read-only, deterministic and conservative."""
from chatbot.commercial_recommendations import (
    preview_for_lead, recommend_properties, source_code_from_lead, vector_cosine,
)


def prop(code, *, active=True, office="PROCASA SUCRE", commune="Santiago",
         kind="Departamento", op="venta", uf=2400, dorms=2, area=65,
         vector=None, description="cerca del metro con terraza"):
    return {
        "codigo": str(code), "disponible_prop360": active,
        "estado": {"oficina": office, "disponible_prop360": active},
        "tipo_operacion": {
            "tipo": kind, "venta": op == "venta", "arriendo": op == "arriendo",
            "precio_venta": {"precio_uf": uf} if op == "venta" else {},
            "precio_arriendo": {"precio_uf": uf} if op == "arriendo" else {},
        },
        "ubicacion": {"comuna": commune},
        "caracteristicas": {"dormitorios": dorms, "banos": 2, "superficie_util": area},
        "observaciones": {"descripcion": description},
        "vector_descripcion": vector if vector is not None else [1.0] + [0.0] * 383,
        "publicaciones": {"procasa": {"url_procasa": f"https://www.procasa.cl/{code}"}},
    }


def test_semantic_cosine_orders_matches():
    source = prop("6260")
    near = prop("1001")
    far = prop("1002", vector=[0.0, 1.0] + [0.0] * 382)
    found = recommend_properties(source, {}, [far, near])
    assert [item["codigo"] for item in found["recommendations"]] == ["1001", "1002"]
    assert found["semantic_available"] is True


def test_hard_filters_on_eligibility_and_explicit_budget():
    source = prop("6260", dorms=4)
    docs = [
        prop("1001", uf=2200, dorms=3),
        prop("1002", active=False), prop("1003", op="arriendo"),
        prop("1004", office="PROCASA GRUPO ORIENTE"),
        prop("1005", kind="Casa"), prop("1006", uf=3000),
        prop("1007", uf=2200, dorms=2),
    ]
    lead = {"search_criteria": {"dormitorios": 3, "precio_uf_max": 2500}}
    result = recommend_properties(source, lead, docs)
    assert [item["codigo"] for item in result["recommendations"]] == ["1001"]
    assert result["budget_explicit_uf"] == 2500


def test_unconfirmed_features_of_source_are_not_hard():
    result = recommend_properties(prop("6260", dorms=4), {}, [prop("1001", dorms=2)])
    assert result["recommendations"][0]["codigo"] == "1001"


def test_neighbors_only_when_requested():
    source = prop("6260")
    neighbor = prop("1001", commune="Ñuñoa")
    other_region = prop("1002", commune="Puerto Montt")
    local = recommend_properties(source, {}, [neighbor, other_region])
    extended = recommend_properties(source, {}, [neighbor, other_region], allow_neighbors=True)
    assert not local["recommendations"]
    assert [item["codigo"] for item in extended["recommendations"]] == ["1001"]


def test_seen_listings_and_duplicates_removed():
    source = prop("6260")
    lead = {"propiedades_vistas": ["1001"]}
    x, y = prop("1001"), prop("1002")
    result = recommend_properties(source, lead, [source, x, y, y])
    assert [item["codigo"] for item in result["recommendations"]] == ["1002"]


def test_missing_source_or_finished_opportunity_blocks_preview():
    source = prop("6260")
    inventory = [prop("1001")]
    missing = preview_for_lead({"codigo_propiedad": "9999"}, {"6260": source}, inventory)
    finished = preview_for_lead(
        {"codigo_propiedad": "6260", "pipeline_stage": "CLOSED_WON"},
        {"6260": source}, inventory,
    )
    opted_out = preview_for_lead(
        {"codigo_propiedad": "6260", "do_not_contact": True},
        {"6260": source}, inventory,
    )
    assert missing["status"] == "PROPERTY_NOT_VERIFIED"
    assert finished["status"] == "STAGE_NOT_ELIGIBLE"
    assert opted_out["status"] == "DO_NOT_CONTACT"


def test_missing_embedding_does_not_create_similarity():
    source = prop("6260")
    source["vector_descripcion"] = [1.0] * 10
    valid = prop("1001")
    no_price = prop("1002", uf=None)
    results = recommend_properties(source, {}, [valid, no_price])
    assert results["semantic_available"] is False
    assert [item["codigo"] for item in results["recommendations"]] == ["1001"]
    assert results["recommendations"][0]["similitud_vectorial"] is None
    assert vector_cosine([0.0] * 384, [1.0] * 384) is None


def test_network_requires_opt_in_and_no_links_fabricated():
    source = prop("6260")
    other = prop("1001", office="PROCASA GRUPO ORIENTE")
    other["publicaciones"]["procasa"]["url_procasa"] = "https://malicious.example/1001"
    assert not recommend_properties(source, {}, [other])["recommendations"]
    result = recommend_properties(source, {}, [other], office=None)
    assert result["recommendations"][0]["url_verificada"] is None


def test_source_code_selection_precedence():
    lead = {"codigo_propiedad": "6260", "source_property_code": "1001",
            "cartera_data": {"canonical_code": "1002"}}
    assert source_code_from_lead(lead) == "6260"


def test_dual_sale_rental_operation_fails_closed():
    source = prop("6260")
    source["tipo_operacion"]["arriendo"] = True
    result = recommend_properties(source, {}, [prop("1001")])
    assert result["status"] == "SOURCE_INCOMPLETE"
