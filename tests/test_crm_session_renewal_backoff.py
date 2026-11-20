"""CRM session renewal must not enter a five-second 401 retry loop.

The browser's session keep-alive may run for hours. A 401 is terminal and a
network failure must be throttled. These guards are static tests for all three
independently rendered CRM pages; authentication remains enforced server-side.
"""
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", [
    "templates/crm_leads_list.html",
    "templates/crm_lead_detail.html",
    "templates/leads_dashboard.html",
])
def test_session_renewal_is_single_flight_and_stops_on_401(filename):
    source = (ROOT / filename).read_text(encoding="utf-8")
    assert "let sessionRenewalInFlight = false;" in source
    assert "if (!sessionRenewalInFlight && idleTime < RENEWAL_INTERVAL" in source
    assert "lastRenewal = now;" in source
    assert "sessionRenewalInFlight = true;" in source
    assert "if (res.status === 401 || res.status === 403)" in source
    assert "window.location.replace('/logout?reason=timeout')" in source
    assert "sessionRenewalInFlight = false;" in source
    assert ".then(res => { if (res.ok) lastRenewal = now; })" not in source
