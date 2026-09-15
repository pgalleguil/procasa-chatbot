from datetime import datetime, timezone

from analytics.pricing_intelligence.lead_linkage import LeadLinkageService
from analytics.pricing_intelligence.models import LinkageStatus
from analytics.pricing_intelligence.property_identity import build_property_identity_resolver


def property_doc(code, *, yapo=None):
    return {
        "codigo": code,
        "publicaciones": {"yapo": {"publicaciones": {"V": {"code": yapo}}}} if yapo else {},
    }


def test_linkage_service_classifies_all_required_states_and_metrics():
    resolver = build_property_identity_resolver(
        [property_doc("A", yapo="YA"), property_doc("B", yapo="YB"), property_doc("C", yapo="SAME"), property_doc("D", yapo="SAME")]
    )
    leads = [
        {"_id": "1", "created_at": "2026-09-08T10:00:00+00:00", "prospecto": {"codigo": "A"}},
        {"_id": "2", "created_at": "2026-09-08T10:00:00+00:00", "prospecto": {"codigo_yapo": "YB"}},
        {"_id": "3", "created_at": "2026-09-08T10:00:00+00:00", "prospecto": {"codigo": "A", "codigo_yapo": "YB"}},
        {"_id": "4", "created_at": "2026-09-08T10:00:00+00:00", "prospecto": {"codigo_yapo": "SAME"}},
        {"_id": "5", "created_at": "2026-09-08T10:00:00+00:00", "prospecto": {"codigo": "MISSING"}},
    ]
    service = LeadLinkageService(resolver)
    records = service.link_leads(leads)
    assert [record.status for record in records] == [
        LinkageStatus.EXACT_CANONICAL,
        LinkageStatus.EXACT_ALIAS,
        LinkageStatus.CONFLICT,
        LinkageStatus.AMBIGUOUS,
        LinkageStatus.UNMATCHED,
    ]
    metrics = service.metrics(records)
    assert metrics["total"] == 5
    assert metrics["counts_by_status"]["EXACT_CANONICAL"] == 1
    assert metrics["counts_by_status"]["EXACT_ALIAS"] == 1
    assert metrics["distinct_linked_properties"] == 2
    assert metrics["aliases_most_used"]["yapo"] == 3


def test_naive_lead_timestamp_is_not_accepted_for_temporal_features():
    resolver = build_property_identity_resolver([property_doc("A")])
    records = LeadLinkageService(resolver).link_leads(
        [{"_id": "1", "created_at": datetime(2026, 9, 8, 10), "prospecto": {"codigo": "A"}}]
    )
    assert records[0].status is LinkageStatus.EXACT_CANONICAL
    assert records[0].created_at is None
    assert records[0].timestamp_error
