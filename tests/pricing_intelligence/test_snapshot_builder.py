from datetime import datetime, timedelta

import pytest

from analytics.pricing_intelligence.models import LinkageStatus
from analytics.pricing_intelligence.lead_linkage import LeadLinkageService
from analytics.pricing_intelligence.property_identity import build_property_identity_resolver
from analytics.pricing_intelligence.snapshot_builder import (
    PropertySnapshotBuilder,
    get_latest_observable_price_change,
)
from analytics.pricing_intelligence.snapshot_cli import main
from analytics.pricing_intelligence.snapshot_repository import PersistenceDisabled, SnapshotRepository
from analytics.pricing_intelligence.time_utils import BUSINESS_TZ, HistoricalSnapshotNotSupported


NOW = datetime(2026, 9, 9, 12, tzinfo=BUSINESS_TZ)


def property_doc(*, price=5000, include_surface=True):
    document = {
        "codigo": "P1",
        "tipo_operacion": {"tipo": "Venta", "precio_venta": {"precio_uf": price, "precio_clp": 190000000}},
        "metadata": {"tipo_propiedad": "Departamento"},
        "ubicacion": {"region": "RM", "comuna": "Santiago", "sector": None, "calle": "NO_COPY"},
        "caracteristicas": {"dormitorios": 2, "banos": 2, "estacionamientos": 1},
        "publicaciones": {"yapo": {"publicaciones": {"V": {"code": "Y1", "publicada": True}}}},
        "datos_propietario": {"nombre": "PII_NOT_INCLUDED", "email": "PII_NOT_INCLUDED"},
        "historial_cambios": [
            {
                "fecha": "2026-09-08T10:00:00-03:00",
                "campo": "tipo_operacion.precio_venta.precio_uf",
                "valor_anterior": 5100,
                "valor_nuevo": 5000,
            },
            {
                "fecha": "2026-09-09T13:00:00-03:00",
                "campo": "tipo_operacion.precio_venta.precio_uf",
                "valor_anterior": 5000,
                "valor_nuevo": 4900,
            },
        ],
    }
    if include_surface:
        document["caracteristicas"]["superficie_construida"] = 70
    return document


def make_builder(leads=()):
    properties = [property_doc()]
    resolver = build_property_identity_resolver(properties)
    linkage = LeadLinkageService(resolver).link_leads(leads)
    return PropertySnapshotBuilder(linkage, now_fn=lambda: NOW)


def test_future_lead_is_excluded_from_previous_7d():
    builder = make_builder(
        [
            {"_id": "past", "created_at": "2026-09-08T10:00:00+00:00", "prospecto": {"codigo": "P1"}},
            {"_id": "future", "created_at": "2026-09-09T16:00:00+00:00", "prospecto": {"codigo": "P1"}},
        ]
    )
    result = builder.build([property_doc()], as_of=NOW)
    assert result.snapshots[0].linked_leads_previous_7d == 1
    assert result.snapshots[0].linked_leads_previous_30d == 1


def test_future_price_change_is_excluded():
    builder = make_builder()
    result = builder.build([property_doc()], as_of=NOW)
    snapshot = result.snapshots[0]
    assert snapshot.current_price_uf == 5000
    assert snapshot.previous_price_uf == 5100
    assert snapshot.last_price_change_at.isoformat().startswith("2026-09-08")


def test_missing_surface_is_null_and_quality_flagged():
    result = make_builder().build([property_doc(include_surface=False)], as_of=NOW)
    snapshot = result.snapshots[0]
    assert snapshot.built_area_m2 is None
    assert snapshot.data_quality.missing_surface is True


def test_missing_listing_date_does_not_invent_days_published():
    snapshot = make_builder().build([property_doc()], as_of=NOW).snapshots[0]
    assert snapshot.listed_at is None
    assert snapshot.days_published is None


def test_snapshot_does_not_include_pii_or_full_address():
    serialized = make_builder().build([property_doc()], as_of=NOW).snapshots[0].to_dict()
    text = repr(serialized)
    assert "PII_NOT_INCLUDED" not in text
    assert "NO_COPY" not in text
    assert serialized["commune"] == "Santiago"
    assert serialized["region"] == "RM"


def test_historical_snapshot_v1_fails_explicitly():
    with pytest.raises(HistoricalSnapshotNotSupported):
        make_builder().build([property_doc()], as_of=NOW - timedelta(days=1))


def test_current_operational_snapshot_works():
    result = make_builder().build([property_doc()], as_of=NOW)
    assert len(result.snapshots) == 1
    assert result.snapshots[0].schema_version == "PropertyDailySnapshotV1"
    assert result.snapshots[0].as_of_local.tzinfo is not None


def test_price_history_deduplicates_two_verified_sources_deterministically():
    embedded = [
        {
            "fecha": "2026-09-08T10:00:00-03:00",
            "campo": "tipo_operacion.precio_venta.precio_uf",
            "valor_anterior": 5100,
            "valor_nuevo": 5000,
        }
    ]
    separate = [
        {
            "codigo": "P1",
            "fecha": "2026-09-08T10:00:00-03:00",
            "campo": "tipo_operacion.precio_venta.precio_uf",
            "valor_anterior": 5100,
            "valor_nuevo": 5000,
        }
    ]
    change = get_latest_observable_price_change("P1", NOW, embedded_events=embedded, separate_events=separate)
    assert change is not None
    assert change.source == "universo_cartera_prop360_historial"


def test_repository_persistence_is_blocked():
    repository = SnapshotRepository(make_builder())
    with pytest.raises(PersistenceDisabled):
        repository.persist([])
    assert repository.proposed_persistence_contract()["status"] == "DOCUMENTED_ONLY_NOT_CREATED"


def test_cli_without_dry_run_aborts(capsys):
    assert main([]) == 2
    assert "solo permite --dry-run" in capsys.readouterr().err
