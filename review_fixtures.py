"""Deterministic, sanitized payloads for the public visual review route."""


def territorial_review_payload():
    region_values = [
        ("Arica y Parinacota", 2), ("Tarapacá", 3), ("Antofagasta", 4), ("Atacama", 2),
        ("Coquimbo", 17), ("Valparaíso", 53), ("Metropolitana", 175), ("Bernardo O'Higgins", 3),
        ("Maule", 24), ("Ñuble", 0), ("Biobío", 14), ("La Araucanía", 0),
        ("Los Ríos", 0), ("Los Lagos", 0), ("Aysén", 0), ("Magallanes", 0),
    ]
    total = sum(value for _, value in region_values)

    def key(value):
        table = str.maketrans("áéíóúñ", "aeioun")
        return value.lower().translate(table)

    regions = []
    for name, leads in region_values:
        demand_share = round(leads / total * 100, 1) if total else 0
        consulted = max(0, round(leads * 0.44))
        regions.append({
            "name": name, "geo_key": key(name), "leads": leads,
            "demand_share_pct": demand_share, "properties_with_demand": consulted,
            "leads_per_property": round(leads / max(1, consulted), 2),
            "stock_sucre": max(0, round(leads * 0.55)),
            "supply_share_pct": round(demand_share * 0.8, 1),
            "gap_pp": round(demand_share * 0.2, 1), "top_segments": [],
        })

    return {
        "inventory": {"active": 442},
        "demand": {"leads": total, "properties_with_demand": 119, "coverage_pct": 26.9,
                    "qualified_signals": {"counts": {"contact_effective": 74}}},
        "meta": {"period_start": "2026-07-19", "period_end": "2026-08-17"},
        "data_quality": {"price_dimension": {}, "bedrooms_dimension": {}},
        "attribution": {"coverage_pct": 97.4, "leads_with_identifiable_office": total, "leads_total": total},
        "demand_intelligence": {
            "dimensions": {name: [] for name in ("operation", "type", "zone_rm", "price_range", "bedrooms")},
            "geography": {"region": regions, "commune": [], "metric_rule": {"default": "leads"}, "matching": {}},
        },
        "opportunities": [], "benchmark": {}, "simulator_options": {"types": [], "communes": []},
        "forecast": {"available": False},
    }
