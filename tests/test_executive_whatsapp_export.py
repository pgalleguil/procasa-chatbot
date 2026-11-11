"""Historical WhatsApp TXT parser tests; synthetic chats only, never persisted."""
from datetime import datetime, timezone

import pytest

from chatbot.executive_whatsapp_export import (
    ExportBinding, parse_authorized_whatsapp_export,
)


def binding(**overrides):
    raw = {
        "lead_id": "L1",
        "executive_user_id": "U1",
        "executive_label": "Ejecutivo Prueba",
        "client_label": "Cliente Prueba",
        "client_phone": "+56912345678",
        "session_id": "test-executive-session",
    }
    raw.update(overrides)
    return ExportBinding(**raw)


LOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
HIGH = datetime(2026, 11, 12, tzinfo=timezone.utc)


def parse(text, **kwargs):
    return parse_authorized_whatsapp_export(
        text, binding=binding(**kwargs), start_utc=LOW, end_utc=HIGH,
    )


def test_parses_two_export_formats_with_client_executive_and_multiline_text():
    result = parse(
        "[10/11/26, 09:15:20] Cliente Prueba: Hola, sigo buscando\n"
        "¿Hay departamentos en San Miguel?\n"
        "10-11-2026 09:16 - Ejecutivo Prueba: Sí, te envío opciones\n"
    )
    assert len(result["records"]) == 2
    assert [x["actor_type"] for x in result["records"]] == ["client", "executive"]
    assert "¿Hay departamentos en San Miguel?" in result["records"][0]["content"]
    assert result["records"][0]["timestamp"].tzinfo == timezone.utc
    assert result["coverage_complete"] is False
    assert result["requires_phone_and_assignment_verification"] is True
    assert result["persisted"] is False
    assert result["outbound_messages_sent"] == 0
    assert all(x["assignment_cycle_id"] is None for x in result["records"])


def test_idempotent_evidence_keys_for_repeat_export():
    text = "[11/11/26, 10:30] Cliente Prueba: Hola\n"
    x = parse(text)
    y = parse(text)
    assert len(x["records"]) == 1
    assert x["records"][0]["evidence_key"] == y["records"][0]["evidence_key"]


def test_identical_messages_at_same_second_get_unique_deterministic_ids():
    text = (
        "[11/11/26, 10:30] Cliente Prueba: Hola\n"
        "[11/11/26, 10:30] Cliente Prueba: Hola\n"
    )
    x = parse(text)
    assert len(x["records"]) == 2
    assert len({record["evidence_key"] for record in x["records"]}) == 2


def test_excludes_unlisted_participant_and_system_header():
    x = parse(
        "[11/11/26, 10:30] Persona Ajena: Conversación privada\n"
        "[11/11/26, 10:31] Cliente Prueba: Mensaje del lead\n"
    )
    assert len(x["records"]) == 1
    assert x["skipped_counts"] == {"unrecognized_sender": 1}
    assert "Persona Ajena" not in repr(x["records"])


def test_rejects_ambiguous_identity_and_invalid_phone():
    with pytest.raises(ValueError, match="ambiguous_historical_participant"):
        binding(client_label="ejecutivo prueba")
    with pytest.raises(ValueError, match="invalid_historical_client_phone"):
        binding(client_phone="no-phone-test")


def test_does_not_guess_date_outside_window_or_invalid_date():
    x = parse(
        "[11/10/26, 10:00] Cliente Prueba: Antiguo\n"
        "[31/11/26, 10:00] Cliente Prueba: Fecha imposible\n"
    )
    assert x["records"] == []
    assert x["skipped_counts"]["outside_requested_window"] == 1
    assert x["skipped_counts"]["invalid_or_ambiguous_timestamp"] == 1


def test_window_validation_fails_closed():
    with pytest.raises(ValueError, match="historical_export_window_requires_timezone"):
        parse_authorized_whatsapp_export(
            "", binding=binding(),
            start_utc=datetime(2026, 11, 1), end_utc=HIGH,
        )


def test_chilean_time_zone_and_am_pm_are_explicit():
    x = parse("[11/11/26, 10:30 AM] Cliente Prueba: Hola\n")
    assert len(x["records"]) == 1
    assert x["records"][0]["timestamp"].hour == 13  # Chile DST = UTC-3


def test_export_text_too_large_rejected():
    with pytest.raises(ValueError, match="oversized_historical_export"):
        parse("A" * 2_000_001)
