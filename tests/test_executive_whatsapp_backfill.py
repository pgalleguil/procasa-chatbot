"""Historical export importer: synthetic data, no Atlas writes or live sessions."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from chatbot.executive_whatsapp_backfill import import_reviewed_export
from chatbot.executive_whatsapp_export import (
    ExportBinding, parse_authorized_whatsapp_export,
)
from tests.test_executive_whatsapp_audit import FakeDB

LOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
HIGH = datetime(2026, 11, 12, tzinfo=timezone.utc)


def fixture():
    binding = ExportBinding(
        lead_id="L1",
        executive_user_id="user-hernan",
        executive_label="Ejecutivo Prueba",
        client_label="Cliente Prueba",
        client_phone="+56912345678",
        session_id="executive-session-1",
    )
    text = (
        "[11/11/26, 09:15] Cliente Prueba: Hola, sigo buscando\n"
        "[11/11/26, 09:16] Ejecutivo Prueba: Tengo un departamento disponible\n"
    )
    parsed = parse_authorized_whatsapp_export(
        text, binding=binding, start_utc=LOW, end_utc=HIGH,
    )
    manifest = {
        "source_sha256": parsed["source_sha256"],
        "lead_id": "L1",
        "executive_user_id": "user-hernan",
        "reviewer_user_ids": ["auditor-1", "auditor-2"],
        "business_content_only_verified": True,
    }
    return parsed, manifest


def test_import_dryrun_preserves_lead():
    db = FakeDB()
    parsed, manifest = fixture()
    before = deepcopy(db.leads)
    result = import_reviewed_export(db, parsed=parsed, manifest=manifest)
    assert result["mode"] == "dry_run"
    assert result["results"] == {"planned": 2}
    assert db.leads == before


def test_two_reviewers_required_and_export_checksum_locked():
    db = FakeDB()
    parsed, manifest = fixture()
    with pytest.raises(ValueError, match="two_independent_reviewers"):
        import_reviewed_export(db, parsed=parsed, manifest={
            **manifest, "reviewer_user_ids": ["auditor-1", "auditor-1"],
        }, commit=True)
    with pytest.raises(ValueError, match="export_checksum"):
        import_reviewed_export(db, parsed=parsed, manifest={
            **manifest, "source_sha256": "0" * 64,
        }, commit=True)
    with pytest.raises(ValueError, match="business_only"):
        import_reviewed_export(db, parsed=parsed, manifest={
            **manifest, "business_content_only_verified": False,
        }, commit=True)
    assert "executive_whatsapp_messages" not in db.leads[0]


def test_explicit_import_is_idempotent_and_preserves_chatbot():
    db = FakeDB()
    parsed, manifest = fixture()
    old = deepcopy(db.leads[0]["messages"])
    first = import_reviewed_export(db, parsed=parsed, manifest=manifest, commit=True)
    second = import_reviewed_export(db, parsed=parsed, manifest=manifest, commit=True)
    assert first["written"] == 2
    assert second["written"] == 0
    assert second["results"] == {"duplicate_confirmed": 2}
    saved = db.leads[0]["executive_whatsapp_messages"]
    assert len(saved) == 2
    assert saved[0]["ingestion_source"] == "whatsapp_export_txt"
    assert saved[0]["time_usable_for_sla"] is False
    assert saved[0]["timestamp_verified"] is False
    assert db.leads[0]["messages"] == old


def test_wrong_executive_or_lead_never_imported():
    db = FakeDB()
    parsed, manifest = fixture()
    wrong = {**manifest, "executive_user_id": "other-executive"}
    with pytest.raises(ValueError, match="export_record_binding_mismatch"):
        import_reviewed_export(db, parsed=parsed, manifest=wrong, commit=True)
    assert "executive_whatsapp_messages" not in db.leads[0]


def test_reassigned_cycle_messages_excluded():
    db = FakeDB()
    db.cycles[0]["unassigned_at"] = "2026-11-02T00:00:00Z"
    parsed, manifest = fixture()
    result = import_reviewed_export(db, parsed=parsed, manifest=manifest, commit=True)
    assert result["written"] == 0
    assert result["results"] == {"no_confirmed_assignment_at_message_time": 2}
    assert "executive_whatsapp_messages" not in db.leads[0]
