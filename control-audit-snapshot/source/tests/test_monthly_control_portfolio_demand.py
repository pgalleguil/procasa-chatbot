from analytics.leads_queries import (
    _control_count_unique_leads_by_property,
    _control_current_relation,
    _control_property_candidate_query,
    _control_resolve_episode_property,
)
from analytics.pricing_intelligence.property_identity import PropertyIdentityResolver


def test_control_demand_deduplicates_leads_per_property_not_cycles():
    counts = _control_count_unique_leads_by_property([
        ("lead-1", "6414"), ("lead-1", "6414"), ("lead-2", "6414"),
        ("lead-1", "7733"),
    ])
    assert counts == {"6414": 2, "7733": 1}


def test_control_zero_demand_remains_zero_with_covered_cohort():
    counts = _control_count_unique_leads_by_property([])
    assert counts.get("17184", 0) == 0


def test_control_demand_resolves_verified_portal_alias_to_canonical_property():
    lead = {"prospecto": {"codigo_mercadolibre": "MLC123", "origen": "Mercado Libre"}}
    candidate_query = _control_property_candidate_query([lead])
    assert any("publicaciones.portal_inmobiliario.publicaciones.V.code" in clause
               for clause in candidate_query["$or"])
    resolver = PropertyIdentityResolver([{
        "codigo": "6414",
        "publicaciones": {"portal_inmobiliario": {"publicaciones": {
            "V": {"code": "MLC123"},
        }}},
    }])
    assert resolver.resolve(lead).resolved_property_code == "6414"


def test_control_ambiguous_alias_is_not_attributed_and_conflicting_episode_is_ambiguous():
    lead = {"prospecto": {"codigo": "6414", "codigo_mercadolibre": "MLC123", "origen": "Mercado Libre"}}
    resolver = PropertyIdentityResolver([
        {"codigo": "6414", "publicaciones": {"portal_inmobiliario": {"publicaciones": {"V": {"code": "MLC123"}}}}},
        {"codigo": "5783", "publicaciones": {"portal_inmobiliario": {"publicaciones": {"V": {"code": "MLC123"}}}}},
    ])
    assert resolver.resolve(lead).resolved_property_code is None
    assert _control_resolve_episode_property({"property_code": "5783"}, lead, resolver) == (None, "ambiguous")


def test_control_does_not_expand_lead_to_all_viewed_properties_and_uses_current_owner_fields():
    lead = {"prospecto": {"propiedades_vistas": ["6414", "5783"]}}
    assert PropertyIdentityResolver([{ "codigo": "6414" }, {"codigo": "5783"}]).resolve(lead).resolved_property_code is None
    assert _control_current_relation("Executive D", "Executive A", "Executive D") == "Otro responsable"
    assert _control_current_relation("Executive D", "Executive A", "Executive C") == "Otro responsable"
    assert _control_current_relation("Executive D", None, "Executive D") == "Cartera del ejecutivo seleccionado"
    assert _control_current_relation("Executive D", None, None) == "Sin clasificación verificable"
