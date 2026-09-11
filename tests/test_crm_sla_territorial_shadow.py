from __future__ import annotations

from pathlib import Path

from chatbot.crm_sla_territorial_shadow import (
    catalog_regions_for_commune,
    compact_region_key,
    commune_key,
    explicit_commune_candidates,
    pool_histogram,
    profile_communes,
    regional_candidates,
    regional_profile_regions,
    remove_absent,
    unique_by_id,
)


def agent(user_id: str, name: str, communes=None, **overrides):
    value = {
        "_id": user_id,
        "nombre": name,
        "rol": "agente",
        "is_active": True,
        "comunas_interes": communes or [],
        "comunas_interes_norm": None,
    }
    value.update(overrides)
    return value


def test_commune_normalization_handles_accents_mojibake_spaces_and_hyphens():
    assert commune_key("Maipú") == "maipu"
    assert commune_key("MaipÃº") == "maipu"
    assert commune_key("La Florida") == "la-florida"
    assert commune_key("Ñuñoa") == "nunoa"
    assert compact_region_key("Región del Bío-Bío") == "biobio"


def test_profile_communes_uses_norm_field_and_is_unique():
    user = agent("a", "Agente", ["Ñuñoa"], comunas_interes_norm=["nunoa", "providencia", "nunoa"])
    assert profile_communes(user) == ["nunoa", "providencia"]


def test_explicit_commune_candidates_exclude_owner_and_inactive_users_are_not_inferred():
    users = [
        agent("owner", "Owner", ["Maipú"]),
        agent("a", "A", ["Maipú"]),
        agent("b", "B", ["Providencia"], is_active=False),
    ]
    result = explicit_commune_candidates(users, "maipu", owner_id="owner")
    assert [row["_id"] for row in result] == ["a"]


def test_regional_profiles_require_two_declared_communes_in_same_catalog_region():
    catalog = {
        "maipu": {"metropolitana"},
        "providencia": {"metropolitana"},
        "colina": {"metropolitana"},
        "talca": {"maule"},
        "linares": {"maule"},
        "pinto": {"nuble"},
    }
    same_region = agent("a", "A", ["Maipú", "Providencia"])
    cross_region = agent("b", "B", ["Maipú", "Talca"])
    one_commune = agent("c", "C", ["Pinto"])
    assert regional_profile_regions(same_region, catalog) == {"metropolitana"}
    assert regional_profile_regions(cross_region, catalog) == set()
    assert regional_profile_regions(one_commune, catalog) == set()
    assert [row["_id"] for row in regional_candidates([same_region, cross_region, one_commune], "Colina", "Metropolitana", catalog)] == ["a"]


def test_t2_union_is_deterministic_and_deduplicated():
    a = agent("a", "A", ["Maipú"])
    b = agent("b", "B", ["Maipú"])
    assert [row["_id"] for row in unique_by_id([a, b, a])] == ["a", "b"]
    assert [row["_id"] for row in remove_absent([a, b], "a")] == ["b"]


def test_pool_histogram_reports_zero_one_two_and_three_plus():
    assert pool_histogram([[], ["a"], ["a", "b"], ["a", "b", "c"], ["a", "b", "c", "d"]]) == {"0": 1, "1": 1, "2": 1, "3+": 2}


def test_catalog_lookup_is_normalized():
    catalog = {"nunoa": {"metropolitana"}}
    assert catalog_regions_for_commune(catalog, "Ñuñoa") == {"metropolitana"}


def test_no_territory_does_not_create_explicit_or_regional_candidates():
    users = [agent("a", "A", [])]
    catalog = {}
    assert explicit_commune_candidates(users, "", owner_id="owner") == []
    assert regional_candidates(users, "", "", catalog, owner_id="owner") == []


def test_property_or_lead_inconsistency_can_be_detected_without_fixing_data():
    catalog = {"algarrobo": {"valparaiso"}, "coquimbo": {"coquimbo"}}
    lead_commune = commune_key("Algarrobo")
    property_commune = commune_key("Coquimbo")
    assert lead_commune != property_commune
    assert catalog_regions_for_commune(catalog, property_commune) == {"coquimbo"}


def test_helpers_are_pure_and_do_not_contain_mongo_write_calls():
    source = Path("chatbot/crm_sla_territorial_shadow.py").read_text(encoding="utf-8")
    assert "insert_one" not in source
    assert "update_one" not in source
    assert "delete_one" not in source
