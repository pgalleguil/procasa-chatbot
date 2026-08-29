"""Versioned cost configuration and period calculations for portal analytics.

This module is deliberately independent from MongoDB and the HTML template so
that the dashboard has one auditable source for portal billing assumptions.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable


PORTAL_COST_CONFIG = {
    "portal_inmobiliario_mercadolibre": {
        "portals": ("Portal Inmobiliario", "MercadoLibre"),
        "monthly_cost": 600_000,
        "billing_model": "shared_package",
        "label": "Paquete PI + MercadoLibre",
    },
    "toctoc": {
        "portal": "TocToc",
        "monthly_cost": 200_000,
        "billing_model": "direct",
    },
    "yapo": {
        "portal": "Yapo",
        "monthly_cost": 120_000,
        "billing_model": "direct",
    },
}


def portal_key(value: Any) -> str:
    """Return the stable lookup key used by the cost configuration."""
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def get_cost_config(portal: Any) -> tuple[str | None, dict | None]:
    """Return ``(config_key, config)`` for a normalized dashboard portal."""
    key = portal_key(portal)
    for config_key, config in PORTAL_COST_CONFIG.items():
        if config.get("billing_model") == "shared_package":
            if any(portal_key(item) == key for item in config.get("portals", ())):
                return config_key, config
        elif portal_key(config.get("portal")) == key:
            return config_key, config
    return None, None


def _parse_iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def prorate_monthly_cost(monthly_cost: int | float, period_start: Any, period_end: Any) -> int:
    """Prorate a monthly price by calendar days, including both endpoints."""
    start = _parse_iso(period_start)
    end = _parse_iso(period_end)
    if not start or not end or start > end:
        return 0
    total = 0.0
    cursor = start
    while cursor <= end:
        month_end = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
        month_end = month_end.fromordinal(month_end.toordinal() - 1)
        overlap_end = min(end, month_end)
        days = (overlap_end - cursor).days + 1
        total += float(monthly_cost) * days / month_end.day
        cursor = overlap_end.fromordinal(overlap_end.toordinal() + 1)
    return round(total)


def build_portal_cost_summary(items: Iterable[dict], period_start: Any, period_end: Any) -> dict:
    """Attach current-period cost facts without fabricating historical prices.

    Shared-package members are covered by the package as a whole. Their rows
    therefore have no individual spend or CPL; the package summary is emitted
    separately and is not another operational portal.
    """
    rows = list(items or [])
    current_counts = {str(row.get("nombre") or ""): int(row.get("cantidad") or 0) for row in rows}
    by_name: dict[str, dict] = {}
    package_summaries: list[dict] = []
    known_leads = 0
    known_period_cost = 0

    for name, count in current_counts.items():
        config_key, config = get_cost_config(name)
        if not config:
            by_name[name] = {"status": "unknown", "billing_model": None}
            continue
        if config.get("billing_model") == "shared_package":
            by_name[name] = {
                "status": "shared_package",
                "billing_model": "shared_package",
                "package_key": config_key,
            }
        else:
            period_cost = prorate_monthly_cost(config["monthly_cost"], period_start, period_end)
            by_name[name] = {
                "status": "direct",
                "billing_model": "direct",
                "monthly_cost": config["monthly_cost"],
                "period_cost": period_cost,
                "cpl": round(period_cost / count) if count else None,
            }
            known_leads += count
            known_period_cost += period_cost

    for config_key, config in PORTAL_COST_CONFIG.items():
        if config.get("billing_model") != "shared_package":
            continue
        portals = tuple(config.get("portals", ()))
        package_leads = sum(current_counts.get(name, 0) for name in portals)
        if not package_leads:
            continue
        period_cost = prorate_monthly_cost(config["monthly_cost"], period_start, period_end)
        package_summaries.append({
            "key": config_key,
            "label": config.get("label") or config_key,
            "portals": list(portals),
            "leads": package_leads,
            "monthly_cost": config["monthly_cost"],
            "period_cost": period_cost,
            "cpl": round(period_cost / package_leads),
        })
        known_leads += package_leads
        known_period_cost += period_cost

    total_leads = sum(current_counts.values())
    return {
        "items": by_name,
        "packages": package_summaries,
        "known_period_cost": known_period_cost,
        "known_leads": known_leads,
        "known_cpl": round(known_period_cost / known_leads) if known_leads else None,
        "coverage_pct": round(known_leads / total_leads * 100, 1) if total_leads else 0.0,
        "total_leads": total_leads,
    }
