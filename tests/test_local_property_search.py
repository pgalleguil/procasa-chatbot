from datetime import datetime, timezone
import pytest
from chatbot.local_property_search import LocalPropertyIndex

NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)
PROFILE = {"operation": "VENTA", "comunas": ["Maipú"], "property_types": ["CASA"], "max_price_clp": 100_000_000, "query": "patio amplio", "minimum_attributes": {"bedrooms": 3}}

def row(key, **kwargs):
    return dict({"listing_key": key, "source_type": "PROCASA", "operation": "VENTA",
        "commune": "Maipu", "property_type": "CASA", "price_clp": 95_000_000,
        "price_valid": True, "published": True, "operational_state": "ACTIVE",
        "last_price_verified_at": NOW, "last_availability_verified_at": NOW,
        "attributes": {"bedrooms": 3}, "description": "casa"}, **kwargs)

def test_full_catalog_not_cheapest_hundred():
    rows = [row(str(i), price_clp=80_000_000) for i in range(150)]
    rows.append(row("best", description="patio amplio"))
    result = LocalPropertyIndex(rows).search(PROFILE, limit=1, now=NOW)
    assert result["scanned"] == 151
    assert result["compatible"][0]["listing_key"] == "best"

def test_hard_filters_and_unknowns():
    rows = [row("expensive", price_clp=101_000_000, description="patio amplio"),
            row("small", attributes={"bedrooms": 2}), row("unknown", attributes={}),
            row("external", source_type="EXTERNAL"), row("broker", classification_state="CORREDOR_SEGURO"),
            row("good")]
    r = LocalPropertyIndex(rows).search(PROFILE, now=NOW)
    assert r["compatible_count"] == 1
    assert r["review_count"] == 2
    assert r["excluded"] == {"OVER_BUDGET": 1, "MINIMUM_BEDROOMS": 1, "COMMERCIAL_VETO": 1}

def test_rental_budget():
    p = {**PROFILE, "operation": "ARRIENDO", "max_price_clp": 500_000}
    assert LocalPropertyIndex([row("r", operation="ARRIENDO", price_clp=490_000)]).search(p, now=NOW)["compatible_count"] == 1

def test_invalid_budget_and_geography_review():
    index = LocalPropertyIndex([row("r")])
    with pytest.raises(ValueError):
        index.search({**PROFILE, "max_price_clp": 0}, now=NOW)
    assert index.search({**PROFILE, "location_targets": ["colegio"]}, now=NOW)["review_count"] == 1

def test_duplicate_keys_fail():
    with pytest.raises(ValueError):
        LocalPropertyIndex([row("r"), row("r")])

def test_dense_cannot_override_budget():
    class Vector:
        def __matmul__(self, other):
            return 0.9
    class Embedder:
        def encode(self, texts):
            return [Vector() for _ in texts]
    r = LocalPropertyIndex([row("good"), row("bad", price_clp=200_000_000)], Embedder()).search(PROFILE, now=NOW)
    assert r["mode"] == "hybrid"
    assert r["compatible_count"] == 1
    assert r["excluded"] == {"OVER_BUDGET": 1}
