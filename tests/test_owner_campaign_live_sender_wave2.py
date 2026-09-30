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
