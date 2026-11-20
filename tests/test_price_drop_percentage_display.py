"""Regression tests for operator-facing and outbound price-drop percentages."""
from datetime import datetime, timezone

import pytest

from chatbot.price_drop_campaign_discovery import (
    PriceDropEvent,
    event_message,
    format_percentage,
)
from scripts.enviar_piloto_rebajas import _display


def _event(*, old_price=50, new_price=36.5):
    return PriceDropEvent(
        event_id="rebaja_16544_test",
        campaign_id="rebaja_16544_test",
        property_code="16544",
        property_type="Departamento",
        comuna="Santiago",
        operation="Arriendo",
        dropped_at=datetime(2026, 9, 5, 3, 6, 16, tzinfo=timezone.utc),
        old_price=old_price,
        new_price=new_price,
        currency="UF",
        difference=old_price - new_price,
        percent_reduction=(old_price - new_price) / old_price * 100,
        available=True,
        published=True,
        current_price=new_price,
        listing_url="https://www.procasa.cl/16544",
        history_id="test",
    )


def test_whatsapp_message_has_no_percentage_decimals():
    message = event_message(_event())
    assert "UF 50 a UF 36.5" in message
    assert "(27% de rebaja)" in message
    assert "27,00%" not in message
    assert "27.00%" not in message


@pytest.mark.parametrize(("raw", "expected"), [
    (27, "27%"),
    (27.4, "27%"),
    (27.5, "28%"),
    (27.6, "28%"),
    (0.5, "1%"),
])
def test_percentage_round_half_up(raw, expected):
    assert format_percentage(raw) == expected


def test_operator_discovery_display_uses_whole_percent():
    rendered = _display(_event())
    assert "(-27%)" in rendered
    assert "27.00%" not in rendered
