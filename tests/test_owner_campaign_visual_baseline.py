from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from analytics.owner_campaign_email_v2 import (
    OWNER_CAMPAIGN_VISUAL_BASELINE_SHA256,
    TEMPLATE_FILE,
)


def test_owner_campaign_v2_template_matches_approved_visual_baseline():
    template_path = Path(__file__).resolve().parents[1] / "templates" / TEMPLATE_FILE
    source = template_path.read_bytes().replace(b"\r\n", b"\n")
    assert sha256(source).hexdigest() == OWNER_CAMPAIGN_VISUAL_BASELINE_SHA256
