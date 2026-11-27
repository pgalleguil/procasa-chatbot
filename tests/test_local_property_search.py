from datetime import datetime, timezone
import pytest
from chatbot.local_property_search import LocalPropertyIndex, normalized_price_clp

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


def test_external_with_unknown_publication_stays_in_review():
    external = row("external", source_type="EXTERNAL", published=None, operational_state="UNKNOWN")
    result = LocalPropertyIndex([external]).search(PROFILE, now=NOW)
    assert result["compatible_count"] == 0
    assert result["review_count"] == 1
    assert result["excluded"] == {}
    assert "PUBLICATION_STATUS_UNKNOWN" in result["review"][0]["review_reasons"]
    assert "EXTERNAL_HUMAN_VERIFICATION" in result["review"][0]["review_reasons"]


def test_unverified_or_stale_price_cannot_exclude_as_over_budget():
    unverified = row("unverified", price_clp=200_000_000, price_valid=False,
                     last_price_verified_at=None)
    stale = row("stale", price_clp=200_000_000,
                last_price_verified_at=NOW.replace(day=23))
    result = LocalPropertyIndex([unverified, stale]).search(PROFILE, now=NOW)
    assert result["compatible_count"] == 0
    assert result["review_count"] == 2
    assert result["excluded"] == {}
    reasons = {entry["listing_key"]: entry["review_reasons"] for entry in result["review"]}
    assert "PRICE_UNVERIFIED" in reasons["unverified"]
    assert "PRICE_STALE" in reasons["stale"]


def test_structured_features_and_negations_are_hard_requirements():
    profile = {**PROFILE, "required_features": ["patio"], "excluded_features": ["piscina"]}
    rows = [row("match", description="patio amplio y sin piscina"),
            row("pool", description="patio amplio con piscina temperada"),
            row("unknown", description="terraza y vista despejada")]
    result = LocalPropertyIndex(rows).search(profile, now=NOW)
    assert result["compatible_count"] == 1
    assert result["compatible"][0]["listing_key"] == "match"
    assert result["review_count"] == 1
    assert result["review"][0]["listing_key"] == "unknown"
    assert result["excluded"] == {"FORBIDDEN_FEATURE_PISCINA": 1}


def test_text_indexing_and_results_redact_direct_contact_details():
    from chatbot.local_property_search import property_text
    row_with_contact = row("contact", title="Casa +56 9 1234 5678", description="Visitas: ventas@example.cl")
    assert "+56 9 1234 5678" not in property_text(row_with_contact)
    assert "ventas@example.cl" not in property_text(row_with_contact)
    result = LocalPropertyIndex([row_with_contact]).search(PROFILE, now=NOW)
    assert result["compatible"][0]["title"] == "Casa"


def test_nested_result_features_are_sanitized_and_attributes_are_whitelisted():
    item = row("safe-output", features={"detalle": "Contacto +56 9 1234 5678", "mail": "ventas@example.cl"})
    item["attributes"] = {"bedrooms": 3, "owner_email": "ventas@example.cl"}
    result = LocalPropertyIndex([item]).search(PROFILE, now=NOW)["compatible"][0]
    assert result["features"] == {"detalle": "Contacto"}
    assert result["attributes"] == {"bedrooms": 3}


def test_rental_budget():
    p = {**PROFILE, "operation": "ARRIENDO", "max_price_clp": 500_000}
    assert LocalPropertyIndex([row("r", operation="ARRIENDO", price_clp=490_000)]).search(p, now=NOW)["compatible_count"] == 1

def test_uf_price_uses_snapshot_conversion_rate_for_clp_filtering():
    converted = row("uf", price_clp=None, price_currency="UF", price_uf=2_500,
                    price_conversion_uf_clp=40_000, price_valid=True)
    p = {**PROFILE, "max_price_clp": 100_000_000}
    result = LocalPropertyIndex([converted]).search(p, now=NOW)
    assert result["compatible_count"] == 1
    assert result["compatible"][0]["price_clp"] == 100_000_000
    assert normalized_price_clp({**converted, "price_currency":"UNKNOWN"}) is None

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


def test_reranker_cannot_promote_review_or_excluded():
    class Reranker:
        def score(self, query, texts):
            assert all("prohibido" not in t for t in texts)
            return [10 if "preferida" in t else 0 for t in texts]
    rows = [row("a", description="patio amplio"), row("b", description="preferida"),
            row("x", price_clp=200_000_000, description="prohibido"),
            row("y", source_type="EXTERNAL", description="preferida")]
    r = LocalPropertyIndex(rows, reranker=Reranker()).search(PROFILE, now=NOW)
    assert r["compatible"][0]["listing_key"] == "b"
    assert r["review"][0]["listing_key"] == "y"
    assert r["excluded"] == {"OVER_BUDGET": 1}

def test_verified_geography():
    target = {"verified": True, "latitude": -33.5, "longitude": -70.7, "radius_m": 1000}
    p = {**PROFILE, "verified_location": target}
    rows = [row("near", geo=target), row("far", geo={**target, "latitude": -34.0}), row("unknown")]
    r = LocalPropertyIndex(rows).search(p, now=NOW)
    assert r["compatible_count"] == 1
    assert r["review_count"] == 1
    assert r["excluded"] == {"OUTSIDE_RADIUS": 1}

def test_incremental_cache(tmp_path):
    np = pytest.importorskip("numpy")
    from chatbot.local_embedding_cache import EmbeddingCache
    class Encoder:
        def __init__(self):
            self.calls = []
        def encode(self, texts):
            self.calls.extend(texts)
            return np.array([[1., 0.] for _ in texts])
    e = Encoder()
    c = EmbeddingCache(tmp_path / "cache.db", "model-a")
    c.load_or_encode(["a","b"],["uno","dos"],e)
    c.load_or_encode(["b","a","c"],["dos","uno actualizado","dos"],e)
    assert e.calls == ["uno","dos","uno actualizado"]
    EmbeddingCache(tmp_path / "cache.db", "model-b").load_or_encode(["a"],["uno"],e)
    assert e.calls[-1] == "uno"

def test_cache_namespace_needs_immutable_revision():
    from chatbot.local_embedding_cache import model_namespace
    with pytest.raises(ValueError):
        model_namespace("model/name","main",version="1")
    assert model_namespace("model/name","a"*40,version="1") != model_namespace("model/name","b"*40,version="1")


def test_empty_query_uses_price_order_without_dense_or_rerank():
    class Embedder:
        def encode(self, texts):
            assert all(texts)
            return [object() for _ in texts]
    class Reranker:
        def score(self, query, texts):
            raise AssertionError("empty query must not rerank")
    r = LocalPropertyIndex([row("a", price_clp=99_000_000), row("z", price_clp=80_000_000)],
                           Embedder(), reranker=Reranker()).search({**PROFILE,"query":""},now=NOW)
    assert r["compatible"][0]["listing_key"] == "z"
    assert not r["reranked"]

def test_invalid_profile_types():
    index = LocalPropertyIndex([row("r")])
    for p in ([], {**PROFILE,"query":123}, {**PROFILE,"minimum_attributes":[]}):
        with pytest.raises(ValueError):
            index.search(p, now=NOW)
