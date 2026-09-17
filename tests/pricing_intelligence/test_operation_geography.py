from __future__ import annotations

from datetime import datetime, timezone

import mongomock
import pytest

from owner_portal import service
from owner_portal.semantics import (
    canonical_region,
    operation_price,
    region_to_macrozone,
    resolve_property_operations,
)


AS_OF = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def property_document(
    *,
    sale: bool | None = True,
    rent: bool | None = False,
    sale_price: float | None = 3200,
    rent_price: float | None = None,
    region: str = "RM",
    code: str = "P1",
) -> dict:
    operation = {"tipo": "Departamento"}
    if sale is not None:
        operation["venta"] = sale
    if rent is not None:
        operation["arriendo"] = rent
    if sale_price is not None:
        operation["precio_venta"] = {"precio_uf": sale_price}
    if rent_price is not None:
        operation["precio_arriendo"] = {"precio_uf": rent_price}
    return {
        "codigo": code,
        "estado": {"oficina": "PROCASA SUCRE", "disponible_prop360": True},
        "tipo_operacion": operation,
        "metadata": {"tipo_propiedad": "Departamento"},
        "ubicacion": {"region": region, "comuna": "Santiago", "sector": "Centro"},
        "caracteristicas": {
            "dormitorios": 2,
            "banos": 2,
            "superficie_construida": 65,
        },
    }


def comparable_row(*, operation: str, index: int) -> dict:
    return {
        "comuna": "Santiago",
        "tipo_propiedad": "Departamento",
        "operacion": operation,
        "precio_uf": 2800 + index * 100,
        "superficie": 60 + index,
        "listing_id": f"{operation}-{index}",
    }


def test_resolver_uses_explicit_sale_flag_and_not_property_type():
    resolved = resolve_property_operations(property_document(sale=True, rent=False))

    assert resolved["sale"] is True
    assert resolved["rent"] is False
    assert resolved["operations"] == ["venta"]
    assert resolved["primary_operation"] == "venta"
    assert resolved["conflict"] is False
    assert service._property_type(property_document()) == "Departamento"


def test_resolver_supports_rent_only_and_does_not_fallback_to_sale_price():
    document = property_document(sale=False, rent=True, sale_price=3200, rent_price=22)
    resolved = resolve_property_operations(document)

    assert resolved["operations"] == ["arriendo"]
    assert resolved["primary_operation"] == "arriendo"
    assert operation_price(document, "venta") == {"uf": 3200.0, "clp": None}
    assert operation_price(document, "arriendo") == {"uf": 22.0, "clp": None}
    assert service._price(document) == {"uf": 22.0, "clp": None}


def test_dual_property_requires_explicit_selection_without_collapsing_operations():
    document = property_document(sale=True, rent=True, sale_price=3200, rent_price=25)
    resolved = resolve_property_operations(document)
    safe = service._safe_property(document, [])
    selected_sale = service._safe_property(document, [], operation="venta")

    assert resolved["operations"] == ["venta", "arriendo"]
    assert resolved["primary_operation"] is None
    assert resolved["conflict"] is False
    assert safe["operation"] is None
    assert safe["operations"] == ("venta", "arriendo")
    assert safe["operation_selection_required"] is True
    assert safe["price_uf"] is None
    assert selected_sale["operation"] == "venta"
    assert selected_sale["operation_selection_required"] is False
    assert selected_sale["price_uf"] == 3200.0


@pytest.mark.parametrize(
    "document",
    [
        property_document(sale=False, rent=False),
        property_document(sale=None, rent=None),
        property_document(sale=None, rent=False),
    ],
)
def test_missing_or_false_operation_flags_fail_closed(document):
    resolved = resolve_property_operations(document)

    assert resolved["operations"] == []
    assert resolved["primary_operation"] is None
    assert service._operation(document) is None
    assert service._price(document) == {"uf": None, "clp": None}
    assert service._safe_property(document, [])["operation_selection_required"] is False


def test_comparables_filter_sale_and_rent_as_separate_cohorts():
    client = mongomock.MongoClient()
    db = client["test"]
    db["propiedades_captacion"].insert_many(
        [comparable_row(operation="venta", index=index) for index in range(5)]
        + [comparable_row(operation="arriendo", index=index) for index in range(7)]
    )
    sale_prop = service._safe_property(property_document(sale=True, rent=False), [])
    rent_prop = service._safe_property(
        property_document(sale=False, rent=True, sale_price=None, rent_price=22),
        [],
    )

    sale_market = service._comparables(db, sale_prop, {}, AS_OF, sale_prop["operation"])
    rent_market = service._comparables(db, rent_prop, {}, AS_OF, rent_prop["operation"])

    assert sale_market["comparables_count"] == 5
    assert {row["listing_id"] for row in sale_market["valid_rows"]} == {
        f"venta-{index}" for index in range(5)
    }
    assert rent_market["comparables_count"] == 7
    assert {row["listing_id"] for row in rent_market["valid_rows"]} == {
        f"arriendo-{index}" for index in range(7)
    }
    assert rent_market["aggregate"] == {}
    assert rent_market["source"] is None
    assert rent_market["aggregate_updated_at"] is None


def test_comparables_fail_closed_without_explicit_operation():
    client = mongomock.MongoClient()
    db = client["test"]
    db["propiedades_captacion"].insert_many(
        [comparable_row(operation="venta", index=index) for index in range(5)]
    )
    dual_prop = service._safe_property(property_document(sale=True, rent=True), [])

    market = service._comparables(db, dual_prop, {"mercado_venta": {"median": 1}}, AS_OF, dual_prop["operation"])

    assert market["comparables_count"] == 0
    assert market["aggregate"] == {}
    assert "seleccionada explícitamente" in market["explanation"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("RM", "Región Metropolitana de Santiago"),
        ("Metropolitana", "Región Metropolitana de Santiago"),
        ("Región Metropolitana", "Región Metropolitana de Santiago"),
        ("Valparaiso", "Valparaíso"),
        ("Bío-Bío", "Biobío"),
        ("Bernardo O'Higgins", "O'Higgins"),
        ("Araucanía", "La Araucanía"),
    ],
)
def test_canonical_region_accepts_strict_known_aliases(raw, expected):
    assert canonical_region(raw) == expected


def test_canonical_region_and_macrozone_fail_closed_for_unknown_values():
    assert canonical_region("Santiago Centro") is None
    assert region_to_macrozone("Santiago Centro") is None
    assert region_to_macrozone("RM") == "METROPOLITANA"
    assert region_to_macrozone("Biobío") == "SUR"


def test_current_sucre_region_aliases_are_all_resolved():
    current_aliases = {
        "Metropolitana": "Región Metropolitana de Santiago",
        "Valparaiso": "Valparaíso",
        "Maule": "Maule",
        "Bío-Bío": "Biobío",
        "Araucanía": "La Araucanía",
        "Coquimbo": "Coquimbo",
        "Bernardo OHiggins": "O'Higgins",
        "Los Ríos": "Los Ríos",
        "Los Lagos": "Los Lagos",
        "Ñuble": "Ñuble",
    }

    assert {raw: canonical_region(raw) for raw in current_aliases} == current_aliases


def test_auditor_counts_scoped_operations_prices_regions_and_conflicts():
    from scripts.audit_owner_portal_operation_geography import audit_documents

    documents = [
        property_document(code="S1", sale=True, rent=False, region="RM"),
        property_document(code="R1", sale=False, rent=True, sale_price=None, rent_price=22, region="Valparaiso"),
        property_document(code="D1", sale=True, rent=True, sale_price=3100, rent_price=24, region="Biobío"),
        property_document(code="N1", sale=False, rent=False, sale_price=None, rent_price=None, region="Desconocida"),
        property_document(code="O1", sale=True, rent=False, region="RM"),
    ]
    documents[-1]["estado"]["oficina"] = "PROCASA VILLARRICA"
    documents[-1]["resumen"] = {"snapshot_listado": {"operacion": "Arriendo"}}

    report = audit_documents(documents)

    assert report["active_total"] == 4
    assert report["sale_only"] == 1
    assert report["rent_only"] == 1
    assert report["both"] == 1
    assert report["no_operation"] == 1
    assert report["sale_price_missing"] == 0
    assert report["rent_price_missing"] == 0
    assert report["regions_unresolved"] == {"Desconocida": 1}
    assert report["property_types_by_operation"]["venta"]["Departamento"] == 2
    assert report["property_types_by_operation"]["arriendo"]["Departamento"] == 2


def test_dual_view_dto_exposes_selection_contract_and_canonical_geography():
    client = mongomock.MongoClient()
    db = client["test"]
    db["universo_cartera_prop360"].insert_one(property_document(sale=True, rent=True, code="D1"))

    view = service.get_owner_portal_property_view(db, "D1", AS_OF)

    assert view is not None
    payload = view.to_dict()
    assert payload["operation"] is None
    assert payload["operations"] == ["venta", "arriendo"]
    assert payload["operation_selection_required"] is True
    assert payload["canonical_region"] == "Región Metropolitana de Santiago"
    assert payload["macrozone"] == "METROPOLITANA"
    assert payload["current_price_uf"] is None
    assert payload["market_data_available"] is False
