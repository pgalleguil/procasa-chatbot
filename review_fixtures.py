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

REVIEW_DIMENSION_SPECS = {
    "operation": [
        ("Venta", 180, 278, "Cobertura estratégica"),
        ("Arriendo", 67, 121, "Oportunidad de captación"),
        ("Venta + Arriendo", 25, 29, "Menor señal actual"),
        ("Otra situación", 25, 14, "Sobreexposición relativa"),
    ],
    "type": [
        ("Casa", 96, 54, "Oportunidad de captación"),
        ("Departamento", 132, 188, "Sobreexposición relativa"),
        ("Oficina", 26, 41, "Cobertura estratégica"),
        ("Parcela", 18, 9, "Oportunidad de captación"),
        ("Local comercial", 15, 31, "Menor señal actual"),
        ("Sitio", 10, 15, "Menor señal actual"),
    ],
    "zone_rm": [
        ("Oriente", 58, 91, "Oportunidad de captación"),
        ("Centro", 47, 85, "Cobertura estratégica"),
        ("Poniente", 31, 57, "Menor señal actual"),
        ("Norte", 22, 36, "Sobreexposición relativa"),
        ("Sur", 17, 19, "Oportunidad de captación"),
    ],
    "price_range": [
        ("Venta ≤ 2.000 UF", 62, 27, "Oportunidad de captación"),
        ("Venta 2.001–5.000 UF", 98, 176, "Sobreexposición relativa"),
        ("Venta > 5.000 UF", 40, 71, "Cobertura estratégica"),
        ("Arriendo ≤ 800.000 CLP", 45, 82, "Oportunidad de captación"),
        ("Arriendo > 800.000 CLP", 30, 55, "Menor señal actual"),
        ("Precio no comparable", 22, 31, "Menor señal actual"),
    ],
    "bedrooms": [
        ("1 dormitorio", 38, 67, "Menor señal actual"),
        ("2 dormitorios", 81, 136, "Cobertura estratégica"),
        ("3 dormitorios", 108, 94, "Oportunidad de captación"),
        ("4+ dormitorios", 43, 91, "Sobreexposición relativa"),
        ("Sin dormitorios comparables", 27, 54, "Menor señal actual"),
    ],
}


def review_recent_band(row):
    """Derive the review label from the same evidence rule as production."""
    if (row.get("historical_leads_total", 0) < 5 or
            row.get("historical_properties_with_demand", 0) < 3):
        return "Sin evidencia suficiente"
    recent = row.get("recency", {}).get("w0_leads", 0)
    if recent >= 5:
        return "Demanda reciente alta"
    if recent >= 1:
        return "Demanda reciente media"
    return "Demanda reciente baja"


REVIEW_OPPORTUNITIES = [
    {"type": "Casa", "operation": "Venta", "commune": "Talca", "price_range": "≤ 3.000 UF", "bedrooms": "3D", "recommendation": "Demanda reciente alta", "analytical_recommendation": "Candidata a captación", "gap_pp": 11.2, "leads_per_property": 2.2, "stock_sucre": 7, "historical_leads_total": 31, "historical_properties_with_demand": 14, "months_with_demand": 6, "weeks_with_demand": 17, "first_demand_at": "2026-02-14", "last_demand_at": "2026-08-17", "recency": {"w0_leads": 11, "w1_leads": 5, "w2_leads": 1, "trend": "Aceleración"}, "persistence": "Recurrente"},
    {"type": "Departamento", "operation": "Venta", "commune": "Valparaíso", "price_range": "2.001–5.000 UF", "bedrooms": "2D", "recommendation": "Demanda reciente alta", "analytical_recommendation": "Candidata a captación", "gap_pp": 4.7, "leads_per_property": 2.21, "stock_sucre": 9, "historical_leads_total": 44, "historical_properties_with_demand": 20, "months_with_demand": 7, "weeks_with_demand": 19, "first_demand_at": "2026-01-31", "last_demand_at": "2026-08-17", "recency": {"w0_leads": 11, "w1_leads": 1, "w2_leads": 9, "trend": "Reactivación"}, "persistence": "Persistente"},
    {"type": "Departamento", "operation": "Venta", "commune": "Providencia", "price_range": "5.001–10.000 UF", "bedrooms": "2D", "recommendation": "Demanda reciente alta", "analytical_recommendation": "Candidata a captación", "gap_pp": 3.8, "leads_per_property": 1.6, "stock_sucre": 12, "historical_leads_total": 27, "historical_properties_with_demand": 15, "months_with_demand": 5, "weeks_with_demand": 13, "first_demand_at": "2026-03-07", "last_demand_at": "2026-08-17", "recency": {"w0_leads": 8, "w1_leads": 0, "w2_leads": 3, "trend": "Reactivación"}, "persistence": "Recurrente"},
    {"type": "Casa", "operation": "Venta", "commune": "Puente Alto", "price_range": "2.001–3.000 UF", "bedrooms": "3D", "recommendation": "Demanda reciente alta", "analytical_recommendation": "Candidata a captación", "gap_pp": 6.5, "leads_per_property": 1.9, "stock_sucre": 14, "historical_leads_total": 22, "historical_properties_with_demand": 12, "months_with_demand": 4, "weeks_with_demand": 11, "first_demand_at": "2026-04-12", "last_demand_at": "2026-08-17", "recency": {"w0_leads": 7, "w1_leads": 3, "w2_leads": 2, "trend": "Estable"}, "persistence": "Recurrente"},
    {"type": "Departamento", "operation": "Arriendo", "commune": "Ñuñoa", "price_range": "≤ 800.000 CLP", "bedrooms": "2D", "recommendation": "Demanda reciente alta", "analytical_recommendation": "Candidata a captación", "gap_pp": 2.4, "leads_per_property": 1.45, "stock_sucre": 18, "historical_leads_total": 15, "historical_properties_with_demand": 9, "months_with_demand": 3, "weeks_with_demand": 7, "first_demand_at": "2026-06-19", "last_demand_at": "2026-08-17", "recency": {"w0_leads": 6, "w1_leads": 2, "w2_leads": 0, "trend": "Aceleración"}, "persistence": "Reciente"},
    {"type": "Casa", "operation": "Venta", "commune": "Quilicura", "price_range": "≤ 2.000 UF", "bedrooms": "3D", "recommendation": "Demanda reciente media", "analytical_recommendation": "Evaluar captación", "gap_pp": -1.3, "leads_per_property": 1.0, "stock_sucre": 22, "historical_leads_total": 6, "historical_properties_with_demand": 5, "months_with_demand": 2, "weeks_with_demand": 3, "first_demand_at": "2026-07-21", "last_demand_at": "2026-08-17", "recency": {"w0_leads": 3, "w1_leads": 0, "w2_leads": 0, "trend": "Evidencia insuficiente"}, "persistence": "Esporádica"},
    {"type": "Casa", "operation": "Arriendo", "commune": "Maipú", "price_range": "801.000–1.200.000 CLP", "bedrooms": "3D", "recommendation": "Demanda reciente baja", "analytical_recommendation": "Observar evolución", "gap_pp": 0.8, "leads_per_property": 0.8, "stock_sucre": 16, "historical_leads_total": 12, "historical_properties_with_demand": 8, "months_with_demand": 4, "weeks_with_demand": 9, "first_demand_at": "2026-03-21", "last_demand_at": "2026-07-03", "recency": {"w0_leads": 0, "w1_leads": 0, "w2_leads": 2, "trend": "Desaceleración"}, "persistence": "Recurrente"},
]

for _review_opportunity in REVIEW_OPPORTUNITIES:
    _review_opportunity["recommendation"] = review_recent_band(_review_opportunity)
    _review_opportunity["analytical_recommendation"] = {
        "Demanda reciente alta": "Candidata a captación",
        "Demanda reciente media": "Evaluar captación",
        "Demanda reciente baja": "Observar evolución",
        "Sin evidencia suficiente": "No priorizar todavía",
    }[_review_opportunity["recommendation"]]


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

    def segment_rows(dimension):
        def split_for_geo(leads, stock, labels):
            if not leads or not labels:
                return []
            weights = [46, 27, 17, 10]
            values = []
            remaining = leads
            for index, (name, _) in enumerate(labels[:4]):
                amount = remaining if index == min(3, len(labels) - 1) else min(remaining, leads * weights[index] // 100)
                values.append((name, amount))
                remaining -= amount
            if remaining and values:
                values[0] = (values[0][0], values[0][1] + remaining)
            total = max(1, sum(amount for _, amount in values))
            return [{
                "name": name,
                "geo_key": key(name),
                "leads": amount,
                "demand_share_pct": round(amount / total * 100, 1),
                "properties_with_demand": max(1, round(amount * 0.45)),
                "stock_sucre": max(0, round(stock * amount / total)),
                "supply_share_pct": round((stock * amount / total) / 442 * 100, 1),
                "gap_pp": round(amount / total * 100 - (stock * amount / total) / 442 * 100, 1),
                "leads_per_property": round(amount / max(1, round(amount * 0.45)), 2),
            } for name, amount in values]

        def breakdown_for(leads, stock, dimension):
            if dimension == "zone_rm":
                labels = list(RM_REVIEW_LEADS.items())
                regions = []
            else:
                labels = [(name, value) for name, value in region_values if value > 0]
                regions = split_for_geo(leads, stock, labels)
            communes = split_for_geo(leads, stock, list(RM_REVIEW_LEADS.items())) if dimension == "zone_rm" else []
            complementary = "bedrooms" if dimension == "type" else "type"
            components = {complementary: [{"segment": label, "leads": max(1, round(leads * ratio)), "share_pct": round(ratio * 100, 1)} for label, ratio in (("2 dormitorios", .48), ("3 dormitorios", .34), ("4+ dormitorios", .18))]}
            if complementary == "type":
                components = {"type": [{"segment": label, "leads": max(1, round(leads * ratio)), "share_pct": round(ratio * 100, 1)} for label, ratio in (("Departamento", .56), ("Casa", .32), ("Oficina", .12))]}
            return {"regions": regions, "communes": communes, "components": components}

        rows = []
        for segment, leads, stock, quadrant in REVIEW_DIMENSION_SPECS[dimension]:
            row = {
                "dimension": dimension,
                "segment": segment,
                "leads": leads,
                "demand_share_pct": round(leads / total * 100, 1),
                "stock_sucre": stock,
                "supply_share_pct": round(stock / 442 * 100, 1),
                "gap_pp": round((leads / total * 100) - (stock / 442 * 100), 1),
                "properties_with_demand": max(1, round(leads * 0.45)),
                "leads_per_property": round(leads / max(1, round(leads * 0.45)), 2),
                "quadrant": quadrant,
            }
            row["geo_breakdown"] = breakdown_for(leads, stock, dimension)
            rows.append(row)
        return rows

    composition_operation = [{"label": label, "count": count, "pct": round(count / 442 * 100, 1)} for label, count in (("Venta", 278), ("Arriendo", 121), ("Venta + Arriendo", 29), ("Otra situación", 14))]
    composition_type = [{"label": label, "count": count, "pct": round(count / 442 * 100, 1)} for label, count in (("Departamento", 188), ("Casa", 154), ("Oficina", 41), ("Local comercial", 31), ("Parcela", 28))]
    composition_commune = [{"label": label, "count": count, "pct": round(count / 442 * 100, 1)} for label, count in (("Santiago", 72), ("Puente Alto", 48), ("Ñuñoa", 38), ("Providencia", 31), ("Quilicura", 27), ("Maipú", 24))]
    responsible_specs = [("Ana Morales", 96, 31), ("Carlos Pérez", 84, 24), ("Daniela Soto", 77, 20), ("Felipe Rojas", 68, 18), ("María González", 61, 15), ("Sin responsable", 56, 11)]
    responsibles = [{"responsible": name, "active": active, "pct_inventory": round(active / 442 * 100, 1), "with_demand": with_demand, "without_demand": active - with_demand, "coverage_pct": round(with_demand / active * 100, 1)} for name, active, with_demand in responsible_specs]

    def property_row(index, commune, property_type, responsible, leads, active=True):
        return {
            "code": f"REV-{index:03d}", "type": property_type, "operation": "Venta", "commune": commune,
            "responsible": responsible, "price": {"venta_uf": 1800 + index * 120}, "leads_period": leads,
            "publications": {"procasa": {"has_evidence": bool(index % 3)}, "portal_inmobiliario": {"has_evidence": bool(index % 2)}},
            "active": active,
        }

    properties = [
        property_row(1, "Santiago", "Departamento", "Ana Morales", 8), property_row(2, "Puente Alto", "Casa", "Carlos Pérez", 5),
        property_row(3, "Ñuñoa", "Departamento", "Daniela Soto", 4), property_row(4, "Providencia", "Departamento", "Felipe Rojas", 3),
        property_row(5, "Quilicura", "Casa", "María González", 2), property_row(6, "Maipú", "Casa", "Ana Morales", 1),
    ]
    intervention = [
        property_row(101, "Santiago", "Departamento", "Ana Morales", 0), property_row(102, "Puente Alto", "Casa", "Carlos Pérez", 0),
        property_row(103, "Maipú", "Casa", "Daniela Soto", 0), property_row(104, "Quilicura", "Departamento", "Sin responsable", 0),
        property_row(105, "La Florida", "Casa", "Felipe Rojas", 0), property_row(106, "Recoleta", "Departamento", "María González", 0),
    ]
    review_simulator = {
        "available": True,
        "evidence": {"classification": "DEMANDA RECIENTE ALTA", "text": "Caso sanitizado de review para evaluar una captación en Talca.", "historical_leads_compatible": 31, "historical_properties_with_demand": 14, "w0": 11, "w1": 5, "w2": 1, "trend": "Aceleración", "months_with_demand": 6, "weeks_with_demand": 17, "stock_active_comparable": 7, "gap_pp": 11.2, "coverage_pct": 26.9, "intensity": 2.2},
        "matching": {"quality": "exacta", "properties_used": 14, "exact_properties": 4, "close_properties": 6, "segment_properties": 14, "relaxation": []},
        "comparables": [{"code": "REV-SIM-01", "match_level": "exact", "leads_historical": 11, "active_current": False, "first_demand_at": "2026-02-14", "last_demand_at": "2026-08-17"}, {"code": "REV-SIM-02", "match_level": "close", "leads_historical": 8, "active_current": True, "first_demand_at": "2026-04-12", "last_demand_at": "2026-08-17"}],
    }

    return {
        "inventory": {"active": 442, "with_demand": 119, "without_demand": 323, "coverage_pct": 26.9, "reconciliation": True},
        "demand": {"leads": total, "properties_with_demand": 119, "coverage_pct": 26.9,
                    "qualified_signals": {"counts": {"contact_effective": 74, "visit": 31}}},
        "meta": {"period_start": "2026-07-19", "period_end": "2026-08-17"},
        "data_quality": {"price_dimension": {"included": 248, "excluded_missing_price": 18}, "bedrooms_dimension": {"included": 231, "excluded_missing_or_non_residential": 17}},
        "attribution": {"coverage_pct": 97.4, "leads_with_identifiable_office": total, "leads_total": total},
        "demand_intelligence": {
            "dimensions": {name: segment_rows(name) for name in ("operation", "type", "zone_rm", "price_range", "bedrooms")},
            "geography": {"region": regions, "commune": communes, "metric_rule": {"default": "leads"}, "matching": {}},
        },
        "opportunities": REVIEW_OPPORTUNITIES,
        "benchmark": {"note": "Benchmark interno de oferta de la red; no representa el mercado inmobiliario completo.", "offices_active_stock": [{"office": "Procasa Sucre", "active": 442}, {"office": "Procasa Centro", "active": 318}, {"office": "Procasa Oriente", "active": 267}, {"office": "Procasa Costa", "active": 194}], "composition": {"type": [{"segment": "Departamento", "count": 412}, {"segment": "Casa", "count": 356}, {"segment": "Oficina", "count": 88}, {"segment": "Local comercial", "count": 61}], "commune": [{"segment": "Santiago", "count": 148}, {"segment": "Puente Alto", "count": 121}, {"segment": "Ñuñoa", "count": 98}, {"segment": "Providencia", "count": 86}]}},
        "simulator_options": {"types": ["Casa", "Departamento", "Oficina"], "communes": ["Talca", "Santiago", "Puente Alto", "Ñuñoa", "Providencia", "Valparaíso"]},
        "review_simulator": review_simulator,
        "composition": {"operation": composition_operation, "type": composition_type, "commune": composition_commune},
        "demand_coverage": {"with_demand": {"count": 119, "pct": 26.9}, "without_demand": {"count": 323, "pct": 73.1}, "interpretation": "119 de 442 propiedades activas recibieron al menos un lead en el período."},
        "responsibles": responsibles, "intervention": intervention, "properties": properties,
        "filter_options": {"operation": [{"label": "Venta", "count": 278}, {"label": "Arriendo", "count": 121}], "type": [{"label": "Departamento", "count": 188}, {"label": "Casa", "count": 154}], "commune": [{"label": "Santiago", "count": 72}, {"label": "Puente Alto", "count": 48}, {"label": "Ñuñoa", "count": 38}], "responsible": [{"label": name, "count": active} for name, active, _ in responsible_specs]},
        "forecast": {"available": False},
    }
