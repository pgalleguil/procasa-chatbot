import csv

import pytest

from campanas import owner_campaign_live_sender as sender
from campanas.owner_campaign_live_config import (
    PRODUCTION_CAMPAIGN_ID,
    WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID,
)


def _manifest(path, campaign_id, owners):
    rows = []
    for index, owner in enumerate(owners, start=1):
        rows.append({
            "batch_id": "wave2_batch_01",
            "campaign_id": campaign_id,
            "property_code": str(6500 + index),
            "owner_email": owner,
            "operation_resolved": "VENTA",
            "executive_name": "Executive",
            "executive_email": "executive@procasa.cl",
            "boss_cc": "boss@procasa.cl",
            "current_price": "1000",
            "recommended_adjustment_pct": "5",
            "recommended_price": "950",
            "document_type": "NONE",
            "send_status": "READY",
        })
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(sender.REQUIRED_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def test_wave2_manifest_allows_repeated_owner_for_independent_property_emails(tmp_path):
    manifest = tmp_path / "wave2.csv"
    _manifest(manifest, WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, ["owner@example.com"] * 2)

    rows, campaign_id, _ = sender._read_manifest(manifest)

    assert campaign_id == WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID
    assert len(rows) == 2


def test_wave1_still_rejects_repeated_owner_email(tmp_path):
    manifest = tmp_path / "wave1.csv"
    _manifest(manifest, PRODUCTION_CAMPAIGN_ID, ["owner@example.com"] * 2)

    with pytest.raises(sender.SenderError, match="manifest_duplicate_or_invalid_owner"):
        sender._read_manifest(manifest)


def test_wave2_multiowner_exception_is_campaign_scoped():
    assert sender._owner_property_count_allowed(WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, 3)
    assert not sender._owner_property_count_allowed(PRODUCTION_CAMPAIGN_ID, 3)
    assert sender._owner_property_count_allowed(PRODUCTION_CAMPAIGN_ID, 1)


def test_wave2_ambiguous_owner_codes_are_excluded_only_from_wave2():
    assert sender._wave2_code_is_ambiguous(WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "5923")
    assert not sender._wave2_code_is_ambiguous(PRODUCTION_CAMPAIGN_ID, "5923")
    assert not sender._wave2_code_is_ambiguous(WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "6581")


def test_wave2_isolates_delivery_unknown_and_never_retries_that_row():
    rows = [{"property_code": "6581"}, {"property_code": "6583"}]
    outcomes = iter([
        {"property_code": "6581", "smtp_status": "DELIVERY_UNKNOWN", "error_type": "SMTPServerDisconnected"},
        {"property_code": "6583", "smtp_status": "SENT"},
    ])
    called = []

    def send_one(_db, row, _campaign_id, _batch_id, *, smtp_factory=None):
        called.append(row["property_code"])
        return next(outcomes)

    results, stop_reason, unsent = sender._send_batch(
        object(), rows, WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "wave2_batch_01", send_one=send_one
    )

    assert called == ["6581", "6583"]
    assert [row["smtp_status"] for row in results] == ["DELIVERY_UNKNOWN", "SENT"]
    assert stop_reason is None
    assert unsent == 0


def test_wave2_skips_individual_smtp_rejection_and_continues():
    rows = [{"property_code": "6581"}, {"property_code": "6583"}]
    outcomes = iter([
        {"property_code": "6581", "smtp_status": "FAILED", "error_type": "SMTPRecipientsRefused"},
        {"property_code": "6583", "smtp_status": "SENT"},
    ])
    called = []

    def send_one(_db, row, _campaign_id, _batch_id, *, smtp_factory=None):
        called.append(row["property_code"])
        return next(outcomes)

    results, stop_reason, unsent = sender._send_batch(
        object(), rows, WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "wave2_batch_01", send_one=send_one
    )

    assert called == ["6581", "6583"]
    assert [row["smtp_status"] for row in results] == ["FAILED", "SENT"]
    assert stop_reason is None
    assert unsent == 0


def test_wave1_keeps_existing_stop_on_delivery_unknown():
    rows = [{"property_code": "6581"}, {"property_code": "6583"}]
    called = []

    def send_one(_db, row, _campaign_id, _batch_id, *, smtp_factory=None):
        called.append(row["property_code"])
        return {"property_code": row["property_code"], "smtp_status": "DELIVERY_UNKNOWN"}

    results, stop_reason, unsent = sender._send_batch(
        object(), rows, PRODUCTION_CAMPAIGN_ID, "wave1_batch", send_one=send_one
    )

    assert called == ["6581"]
    assert stop_reason == "DELIVERY_UNKNOWN"
    assert unsent == 1


def test_wave2_stops_after_repeated_ambiguous_transport_failure():
    rows = [{"property_code": str(code)} for code in range(4)]
    called = []

    def send_one(_db, row, _campaign_id, _batch_id, *, smtp_factory=None):
        called.append(row["property_code"])
        return {
            "property_code": row["property_code"],
            "smtp_status": "DELIVERY_UNKNOWN",
            "error_type": "SMTPServerDisconnected",
        }

    results, stop_reason, unsent = sender._send_batch(
        object(), rows, WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "wave2_batch_01", send_one=send_one
    )

    assert called == ["0", "1", "2"]
    assert len(results) == 3
    assert stop_reason == "repeated_smtp_transport_error:SMTPServerDisconnected"
    assert unsent == 1


def test_wave2_stops_on_systemic_smtp_authentication_error():
    rows = [{"property_code": "6581"}, {"property_code": "6583"}]
    called = []

    def send_one(_db, row, _campaign_id, _batch_id, *, smtp_factory=None):
        called.append(row["property_code"])
        return {
            "property_code": row["property_code"],
            "smtp_status": "FAILED",
            "error_type": "SMTPAuthenticationError",
        }

    results, stop_reason, unsent = sender._send_batch(
        object(), rows, WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "wave2_batch_01", send_one=send_one
    )

    assert called == ["6581"]
    assert len(results) == 1
    assert stop_reason == "systemic_smtp_error:SMTPAuthenticationError"
    assert unsent == 1


def test_wave2_resume_selects_only_ready_and_never_retries_terminal_rows():
    rows = [
        {"property_code": "6581", "send_status": "SENT"},
        {"property_code": "6583", "send_status": "DELIVERY_UNKNOWN"},
        {"property_code": "6585", "send_status": "FAILED"},
        {"property_code": "6587", "send_status": "READY"},
    ]

    assert sender._wave2_ready_rows(rows) == [rows[-1]]


def test_wave2_resume_refuses_a_row_left_sending():
    rows = [{"property_code": "6581", "send_status": "READY"},
            {"property_code": "6583", "send_status": "SENDING"}]

    with pytest.raises(sender.SenderError, match="campaign_batch_contains_sending_rows"):
        sender._wave2_ready_rows(rows)


def test_wave2_master_lookup_is_scoped_to_one_active_property_code():
    class Collection:
        def __init__(self):
            self.count_query = None
            self.find_query = None

        def count_documents(self, query, limit=0):
            self.count_query = (query, limit)
            return 1

        def find_one(self, query):
            self.find_query = query
            return {
                "codigo": "6581",
                "estado": {"oficina": "PROCASA SUCRE", "estado_prop360": "Activa"},
                "disponible_prop360": True,
            }

        def find(self, *_args, **_kwargs):
            raise AssertionError("Wave 2 must not scan the active portfolio")

    class DB:
        def __init__(self, collection):
            self.collection = collection

        def __getitem__(self, _name):
            return self.collection

    from campanas import owner_campaign_test_runtime as runtime

    collection = Collection()
    master = sender._find_wave2_master(DB(collection), runtime, "6581")

    assert master["codigo"] == "6581"
    query, limit = collection.count_query
    assert limit == 2
    assert query["codigo"]["$in"] == runtime._variants("6581")
    assert query["estado.oficina"] == runtime.CAMPAIGN_OFFICE
    assert query["disponible_prop360"] is True
    assert collection.find_query == query


def test_wave2_preflight_skip_only_changes_ready_status_without_touching_snapshot():
    class Collection:
        def __init__(self):
            self.call = None

        def update_one(self, query, update):
            self.call = (query, update)

    class DB:
        def __init__(self, collection):
            self.collection = collection

        def __getitem__(self, _name):
            return self.collection

    collection = Collection()
    status = sender._record_wave2_preflight_skip(
        DB(collection), WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "6581",
        "manifest_recommended_price_stale_or_mismatch",
    )

    assert status == "SKIPPED_STALE_OR_MISMATCH"
    query, update = collection.call
    assert query["send_status"] == "READY"
    assert update["$set"]["send_status"] == "SKIPPED_STALE_OR_MISMATCH"
    assert "campaign_snapshot" not in update["$set"]


def test_preflight_skip_persists_safe_pricing_diagnostics_with_set_only():
    class Collection:
        def __init__(self):
            self.call = None

        def update_one(self, query, update):
            self.call = (query, update)

    class DB:
        def __init__(self, collection):
            self.collection = collection

        def __getitem__(self, _name):
            return self.collection

    collection = Collection()
    diagnostics = {"manifest_recommended_price": 950, "recomputed_recommended_price": 945.25}
    sender._record_wave2_preflight_skip(
        DB(collection), WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "6581",
        "manifest_recommended_price_stale_or_mismatch", details=diagnostics,
    )

    _, update = collection.call
    assert set(update) == {"$set"}
    assert update["$set"]["preflight_diagnostics"] == diagnostics
    assert "campaign_snapshot" not in update["$set"]


def test_frozen_pricing_snapshot_rebuilds_and_reports_tampering():
    from analytics.owner_campaign_email_v2 import (
        OWNER_CAMPAIGN_PRICE_POLICY_VERSION,
        calculate_commercial_price_recommendation,
    )

    inputs = {
        "current_price": 1000, "leads_90d": 0, "comparable_subject": None,
        "comparable_reference": None, "valuation_reference": None,
        "operation": "VENTA", "lead_percentiles": {},
    }
    rebuilt = calculate_commercial_price_recommendation(**inputs)
    manifest = {
        "current_price": 1000, "recommended_adjustment_pct": rebuilt["recommended_adjustment_pct"],
        "recommended_price": rebuilt["recommended_price"],
    }
    snapshot = {
        "policy_version": OWNER_CAMPAIGN_PRICE_POLICY_VERSION,
        "campaign_id": WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "property_code": "6581",
        "inputs": inputs, "recommended_adjustment_pct": rebuilt["recommended_adjustment_pct"],
        "recommended_price": rebuilt["recommended_price"],
    }

    diagnostics = sender._rebuild_pricing_snapshot(
        snapshot, manifest, live_current_price=1000,
        campaign_id=WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, property_code="6581",
    )
    assert diagnostics["recomputed_recommended_price"] == rebuilt["recommended_price"]
    assert diagnostics["pricing_inputs"] == inputs

    tampered = {**snapshot, "recommended_price": 999}
    with pytest.raises(sender.SenderError, match="manifest_pricing_snapshot_recommendation_mismatch") as exc:
        sender._rebuild_pricing_snapshot(
            tampered, manifest, live_current_price=1000,
            campaign_id=WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, property_code="6581",
        )
    assert exc.value.details["snapshot_recommended_price"] == 999
    assert exc.value.details["recomputed_recommended_price"] == rebuilt["recommended_price"]


def test_legacy_frozen_recommendation_16544_survives_newer_model_output():
    manifest = {
        "property_code": "16544",
        "current_price": "36.5",
        "recommended_adjustment_pct": "5",
        "recommended_price": "34.675",
    }

    diagnostics = sender._validate_legacy_manifest_recommendation(
        manifest, live_current_price=36.5,
        recomputed_recommended_price=32.85,
        recomputed_adjustment_pct=10,
    )
    row = {
        "current_price_clp": 1498288,
        "_model": {"pricing_recommendation": {"recommended_adjustment_pct": 10}},
    }
    sender._apply_frozen_manifest_recommendation(row, manifest, operation="ARRIENDO")

    assert diagnostics["validation_basis"] == "LEGACY_FROZEN_RECOMMENDATION_ARITHMETIC"
    assert row["recommended_adjustment_pct"] == 5
    assert row["recommended_price"] == 34.675
    assert row["recommended_price_clp"] == 1423374
    assert row["_model"]["recommended_adjustment_pct"] == 5
    assert row["_model"]["pricing_recommendation"]["recommended_price"] == 34.675


def test_legacy_frozen_recommendation_rejects_bad_arithmetic():
    with pytest.raises(sender.SenderError, match="manifest_recommendation_arithmetic_mismatch"):
        sender._validate_legacy_manifest_recommendation(
            {"current_price": 36.5, "recommended_adjustment_pct": 5, "recommended_price": 35},
            live_current_price=36.5,
        )


def test_campaign_price_policy_keeps_five_percent_minimum_and_ten_percent_ceiling():
    from analytics.owner_campaign_email_v2 import calculate_commercial_price_recommendation

    no_signals = calculate_commercial_price_recommendation(
        current_price=1000, leads_90d=None, operation="VENTA",
    )
    zero_leads = calculate_commercial_price_recommendation(
        current_price=1000, leads_90d=0, operation="VENTA",
    )

    assert no_signals["recommended_adjustment_pct"] == 5
    assert no_signals["recommended_price"] == 950
    assert zero_leads["recommended_adjustment_pct"] == 10
    assert zero_leads["recommended_price"] == 900
