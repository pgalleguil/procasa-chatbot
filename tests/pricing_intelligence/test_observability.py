from datetime import datetime

from analytics.pricing_intelligence.cohort_reproducibility import (
    cohort_fingerprint,
    cohort_reproducibility_metadata,
)
from analytics.pricing_intelligence.lead_origin import resolve_lead_origin
from analytics.pricing_intelligence.observability import (
    SOURCE_FULL,
    SOURCE_PARTIAL,
    WINDOW_COMPLETE,
    WINDOW_PARTIAL,
    WINDOW_UNKNOWN,
    build_observation_evidence,
    demand_observation,
    derive_lead_source_coverage,
)
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ


AS_OF = datetime(2026, 9, 17, 12, tzinfo=BUSINESS_TZ)


def test_origin_by_primary_prospecto_field():
    result = resolve_lead_origin({"prospecto": {"origen": "Portal Inmobiliario"}})
    assert result == {
        "canonical_origin": "portal_inmobiliario",
        "source_field": "prospecto.origen",
        "confidence": "exact",
        "evidence": ["prospecto.origen=>portal_inmobiliario"],
    }


def test_origin_by_source_event_and_message_portal():
    event_result = resolve_lead_origin(
        {"source_events": [{"portal_source": "TOCTOC"}]}
    )
    message_result = resolve_lead_origin(
        {"messages": [{"portal": "Yapo", "source": "Yapo"}]}
    )
    assert event_result["canonical_origin"] == "toctoc"
    assert event_result["source_field"] == "source_events[].portal_source"
    assert message_result["canonical_origin"] == "yapo"


def test_conflicting_origins_are_not_arbitrarily_selected():
    result = resolve_lead_origin(
        {
            "prospecto": {"origen": "Yapo"},
            "source_events": [{"portal_source": "TOCTOC"}],
        }
    )
    assert result["canonical_origin"] is None
    assert result["confidence"] == "conflict"


def test_unknown_origin_is_explicit():
    result = resolve_lead_origin({"prospecto": {"origen": "CRM interno"}})
    assert result["canonical_origin"] is None
    assert result["confidence"] == "unknown"


def test_trusted_url_domain_is_deterministic_but_generic_url_is_unknown():
    trusted = resolve_lead_origin(
        {"prospecto": {"link": "https://www.yapo.cl/listing/32147802"}}
    )
    generic = resolve_lead_origin(
        {"prospecto": {"link": "https://example.invalid/listing/32147802"}}
    )
    assert trusted["canonical_origin"] == "yapo"
    assert generic["confidence"] == "unknown"


def _property(code="P1", *, yapo="Y1", procasa=None):
    publications = {
        "yapo": {"publicaciones": {"V": {"code": yapo}}},
    }
    if procasa:
        publications["procasa"] = {"publicaciones": {"V": {"code": procasa}}}
    return {"codigo": code, "publicaciones": publications}


def test_exact_procasa_alias_is_supported_and_unsupported_alias_is_not_forced():
    from analytics.pricing_intelligence.property_identity import build_property_identity_resolver

    resolver = build_property_identity_resolver([_property(procasa="PC1")])
    exact = resolver.resolve({"prospecto": {"codigo_procasa": "PC1"}})
    unsupported = resolver.resolve({"prospecto": {"codigo_chilepropiedades": "CP1"}})
    assert exact.status.value == "EXACT_ALIAS"
    assert exact.resolved_property_code == "P1"
    assert unsupported.status.value == "UNMATCHED"


def test_observation_date_and_windows_are_pure_and_explicit():
    evidence = build_observation_evidence(
        property_code="P1",
        operation="venta",
        as_of=AS_OF,
        active_publication_portals=("yapo",),
        publication_dates={
            "yapo": (datetime(2026, 3, 18, tzinfo=BUSINESS_TZ), "captacion.fecha_publicacion")
        },
        crm_lead_capable_portals=("yapo",),
        source_observable_from={"yapo": datetime(2026, 3, 18, tzinfo=BUSINESS_TZ)},
    )
    assert evidence.earliest_verified_publication_at is not None
    assert evidence.window_30d == WINDOW_COMPLETE
    assert evidence.window_90d == WINDOW_COMPLETE


def test_observation_partial_and_unknown_windows():
    partial = build_observation_evidence(
        property_code="P1",
        operation="venta",
        as_of=AS_OF,
        active_publication_portals=("yapo",),
        publication_dates={
            "yapo": (datetime(2026, 9, 1, tzinfo=BUSINESS_TZ), "captacion.fecha_publicacion")
        },
        crm_lead_capable_portals=("yapo",),
        source_observable_from={"yapo": datetime(2026, 9, 1, tzinfo=BUSINESS_TZ)},
    )
    unknown = build_observation_evidence(
        property_code="P2",
        operation="venta",
        as_of=AS_OF,
        active_publication_portals=("yapo",),
        publication_dates={"yapo": None},
        crm_lead_capable_portals=("yapo",),
        source_observable_from={"yapo": None},
    )
    assert partial.window_30d == WINDOW_PARTIAL
    assert partial.window_90d == WINDOW_PARTIAL
    assert unknown.window_30d == WINDOW_UNKNOWN
    assert unknown.window_90d == WINDOW_UNKNOWN


def test_positive_demand_remains_valid_with_partial_coverage():
    result = demand_observation(
        count=3,
        window=WINDOW_COMPLETE,
        source_coverage=SOURCE_PARTIAL,
    )
    assert result["demand_signal"] == "OBSERVED_POSITIVE"
    assert result["demand_count"] == 3
    assert result["demand_confidence"] == "medium"


def test_zero_semantics_distinguish_complete_partial_and_uncertain():
    complete = demand_observation(count=0, window=WINDOW_COMPLETE, source_coverage=SOURCE_FULL)
    partial = demand_observation(count=0, window=WINDOW_COMPLETE, source_coverage=SOURCE_PARTIAL)
    unknown = demand_observation(count=0, window=WINDOW_UNKNOWN, source_coverage=SOURCE_PARTIAL)
    assert complete["demand_signal"] == "OBSERVED_ZERO_COMPLETE"
    assert partial["demand_signal"] == "OBSERVED_ZERO_PARTIAL"
    assert unknown["demand_signal"] == "ZERO_UNCERTAIN"


def test_source_coverage_only_considers_active_crm_capable_portals():
    result = derive_lead_source_coverage(
        property_code="P1",
        window=WINDOW_COMPLETE,
        active_publication_portals=("yapo", "procasa"),
        crm_lead_capable_portals=("yapo",),
        covered_portals=("yapo",),
    )
    assert result.status == SOURCE_FULL


def test_cohort_fingerprint_and_percentiles_are_deterministic():
    rows = [
        {"listing_id": "B", "price_uf": 2000, "surface_m2": 50, "observed_at": "2026-09-01"},
        {"listing_id": "A", "price_uf": 1000, "surface_m2": 40, "observed_at": "2026-09-01"},
    ]
    kwargs = {
        "operation": "venta",
        "cohort_level": "high_similarity",
        "as_of": AS_OF,
        "cutoff": AS_OF,
    }
    assert cohort_fingerprint(rows, **kwargs) == cohort_fingerprint(list(reversed(rows)), **kwargs)
    metadata = cohort_reproducibility_metadata(rows, **kwargs)
    assert metadata["cohort_fingerprint"] == cohort_fingerprint(rows, **kwargs)
    assert metadata["p75"] == 1750
