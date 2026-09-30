"""Central, stable configuration for the PROCASA SUCRE owner campaign."""

from __future__ import annotations

import os


BOSS_CC = os.getenv("OWNER_CAMPAIGN_BOSS_CC", "jpcaro@procasa.cl").strip().casefold()
PRODUCTION_CAMPAIGN_ID = os.getenv(
    "OWNER_CAMPAIGN_PRODUCTION_ID", "owner_price_sucre_wave1_20260928"
).strip()
# One-property-per-email wave with owners who may have other active properties.
# Keep this exception exact and campaign-scoped; Wave 1 remains single-property.
WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID = "owner_price_sucre_wave2_20260930"
WAVE2_AMBIGUOUS_OWNER_CODES = frozenset({"5923", "6841", "6854", "6856"})
# Manually supplied recent additions for the current PROCASA SUCRE campaign.
# Keep environment-provided exclusions additive so deployment configuration
# cannot accidentally re-include a code already excluded by the campaign.
_CAMPAIGN_RECENT_EXCLUSION_CODES = frozenset(
    {"17253", "17252", "17250", "17215", "17214", "17213", "17211", "17200"}
)
_ENV_RECENT_EXCLUSION_CODES = frozenset(
    code.strip() for code in os.getenv("MANUAL_RECENT_EXCLUSION_CODES", "").split(",")
    if code.strip()
)
MANUAL_RECENT_EXCLUSION_CODES = _CAMPAIGN_RECENT_EXCLUSION_CODES | _ENV_RECENT_EXCLUSION_CODES
MANUAL_RECENT_EXCLUSION_PENDING = not bool(MANUAL_RECENT_EXCLUSION_CODES)
MANUAL_CAMPAIGN_EXCLUDED_CODES = frozenset({"6754"})
EXCLUDED_OWNER_EMAILS = frozenset({"incorrecto@procasa.cl", "firmasjpc@gmail.com"})

