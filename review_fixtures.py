"""Deterministic, sanitized payloads for the public visual review route."""


RM_REVIEW_LEADS = {
    "Santiago": 48,
    "Puente Alto": 20,
    "Quilicura": 19,
    "Ñuñoa": 17,
    "Providencia": 12,
    "Maipú": 8,
    "Las Condes": 7,
    "La Florida": 7,
    "San Miguel": 6,
    "Pudahuel": 5,
    "Estación Central": 4,
    "Macul": 4,
    "Peñalolén": 4,
    "Recoleta": 3,
    "La Reina": 3,
    "Cerrillos": 2,
    "Huechuraba": 2,
    "Independencia": 2,
    "Lo Barnechea": 2,
}

RM_REVIEW_SEGMENTS = {
    "Santiago": [
        {"segment": "Departamento · Venta · Santiago · 2.001–3.000 UF · 2 dormitorios", "leads": 18},
        {"segment": "Departamento · Venta · Santiago · 3.001–5.000 UF · 3 dormitorios", "leads": 12},
        {"segment": "Casa · Venta · Santiago · hasta 3.000 UF · 3 dormitorios", "leads": 7},
    ],
    "Ñuñoa": [
        {"segment": "Departamento · Venta · Ñuñoa · 3.001–5.000 UF · 2 dormitorios", "leads": 7},
        {"segment": "Departamento · Venta · Ñuñoa · 5.001–10.000 UF · 3 dormitorios", "leads": 5},
        {"segment": "Departamento · Arriendo · Ñuñoa · hasta 800.000 CLP · 2 dormitorios", "leads": 3},
    ],
    "Providencia": [
        {"segment": "Departamento · Venta · Providencia · 5.001–10.000 UF · 2 dormitorios", "leads": 5},
        {"segment": "Departamento · Arriendo · Providencia · 800.001–1.200.000 CLP · 2 dormitorios", "leads": 4},
        {"segment": "Departamento · Venta · Providencia · 10.001–20.000 UF · 3 dormitorios", "leads": 2},
    ],
}


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

    communes = []
    for name, leads in RM_REVIEW_LEADS.items():
        demand_share = round(leads / total * 100, 1) if total else 0
        properties_with_demand = max(1, round(leads * 0.44))
        stock_sucre = max(0, round(leads * 0.55))
        supply_share = round(stock_sucre / 442 * 100, 1)
        communes.append({
            "name": name,
            "geo_key": key(name),
            "region_geo_key": "metropolitana",
            "leads": leads,
            "demand_share_pct": demand_share,
            "properties_with_demand": properties_with_demand,
            "leads_per_property": round(leads / properties_with_demand, 2),
            "stock_sucre": stock_sucre,
            "supply_share_pct": supply_share,
            "gap_pp": round(demand_share - supply_share, 1),
            "top_segments": RM_REVIEW_SEGMENTS.get(name, []),
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
            "geography": {"region": regions, "commune": communes, "metric_rule": {"default": "leads"}, "matching": {}},
        },
        "opportunities": [], "benchmark": {}, "simulator_options": {"types": [], "communes": []},
        "forecast": {"available": False},
    }
