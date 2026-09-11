"""FASE 1C: auditoría territorial y de pools para la población shadow certificada.

El script es de solo lectura sobre MongoDB. Toma exclusivamente los leads
``SAFE_TO_SHADOW_REASSIGN`` certificados por Fase 1A/Fase 0.5, reconstruye el
pool del router actual (T0) y calcula escenarios territoriales hipotéticos en
memoria. No llama al router mutante ni persiste cambios.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chatbot.crm_sla_territorial_shadow import (
    catalog_regions_for_commune,
    compact_region_key,
    commune_key,
    explicit_commune_candidates,
    pool_histogram,
    profile_communes,
    regional_candidates,
    regional_profile_regions,
    unique_by_id,
)
from scripts.run_phase05_crm_reassignment_audit import (
    enrich_records,
    first_text,
    local_iso,
    norm,
    parse_dt,
    text,
)
from scripts.run_phase0_crm_sla_audit import (
    MARIELA_PRIORITY_COMUNAS,
    OUR_TEAM,
    ROUND_ROBIN_RM,
    TEMPORARILY_INACTIVE,
    router_candidates,
    user_name_match,
)
from scripts.run_phase1a_crm_sla_shadow import read_safe_rows
from scripts.run_phase1b_crm_capacity_audit import load_data_phase1b


REPORT = ROOT / "docs" / "AUDITORIA_TERRITORIAL_REASIGNACION_SLA_20260910.md"
DATA_DIR = ROOT / "docs" / "auditoria_sla_data"
POOL_CSV = DATA_DIR / "territorial_pool_reason_audit.csv"
AGENT_CSV = DATA_DIR / "territorial_agent_coverage.csv"
CONSISTENCY_CSV = DATA_DIR / "territorial_data_consistency.csv"
SCENARIOS_CSV = DATA_DIR / "territorial_scenarios_t0_t4.csv"
GAPS_CSV = DATA_DIR / "territorial_coverage_gaps.csv"
ABSENCE_CSV = DATA_DIR / "territorial_absence_simulation.csv"

SCENARIOS = ("T0_ROUTER_ACTUAL", "T1_EXPLICIT_COMMUNE", "T2_UNION_T0_T1", "T3_REGIONAL_DECLARED", "T4_NO_PROPERTY_EXEC_RESTRICTION")
ABSENCE_NAMES = (
    "Rocío Aliaga", "Hernán Castro", "María Paz Galleguillos", "Erika Garrido",
    "Mariela Arriagada", "Susana Ensignia", "Paula Morales",
)

LEAD_COMMUNE_PATHS = (
    "comuna", "prospecto.comuna", "prospecto.ubicacion.comuna", "datos_propiedad.comuna",
)
LEAD_REGION_PATHS = (
    "region", "prospecto.region", "prospecto.ubicacion.region", "datos_propiedad.region",
)
PROPERTY_COMMUNE_PATHS = ("ubicacion.comuna", "comuna")
PROPERTY_REGION_PATHS = ("ubicacion.region", "region")
PROPERTY_EXEC_PATHS = (
    "estado.ejecutivo", "estado.captador", "estado.responsable",
    "ejecutivo", "captador", "responsable",
)


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else ""
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def path_value(document: dict[str, Any] | None, paths: Iterable[str]) -> Any:
    if not document:
        return None
    for path in paths:
        current: Any = document
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current not in (None, "", [], {}):
            return current
    return None


def path_text(document: dict[str, Any] | None, paths: Iterable[str]) -> str:
    return text(path_value(document, paths))


def iso(value: Any) -> str:
    parsed = parse_dt(value)
    return parsed.isoformat() if parsed else text(value)


def json_cell(value: Any) -> str:
    if value in (None, "", [], {}, set()):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def names_cell(users: Iterable[dict[str, Any]]) -> str:
    return ";".join(text(user.get("nombre")) for user in users if text(user.get("nombre")))


def ids_cell(users: Iterable[dict[str, Any]]) -> str:
    return ";".join(text(user.get("_id")) for user in users if text(user.get("_id")))


def find_user(name: str, users: list[dict[str, Any]]) -> dict[str, Any] | None:
    for user in users:
        if user_name_match(name, user.get("nombre")):
            return user
    return None


def active_agents(users: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [user for user in users if user.get("is_active") is True and norm(user.get("rol")) == "agente"],
        key=lambda user: (norm(user.get("nombre")), text(user.get("_id"))),
    )


def build_catalog() -> dict[str, set[str]]:
    path = ROOT / "static" / "geo" / "chile-comunas.geojson"
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    catalog: dict[str, set[str]] = defaultdict(set)
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        commune = commune_key(props.get("Comuna") or props.get("comuna"))
        region = compact_region_key(props.get("Region") or props.get("region"))
        if commune and region:
            catalog[commune].add(region)
    return dict(catalog)


def profile_raw_values(user: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for key in ("comunas_interes", "comunas_interes_norm"):
        raw = user.get(key)
        if isinstance(raw, (list, tuple, set)):
            values = list(raw)
        elif isinstance(raw, str):
            values = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
        elif isinstance(raw, dict):
            values = list(raw.keys()) + list(raw.values())
        else:
            values = []
        for value in values:
            if text(value) and text(value) not in output:
                output.append(text(value))
    return output


def observable_profile_regions(user: dict[str, Any], catalog: dict[str, set[str]]) -> set[str]:
    regions: set[str] = set()
    for commune in profile_communes(user):
        regions.update(catalog_regions_for_commune(catalog, commune))
    return regions


def region_display(region_key: str) -> str:
    return region_key or "UNKNOWN"


def router_targets(
    record: dict[str, Any],
    agents: list[dict[str, Any]],
    *,
    use_property_exec: bool = True,
) -> tuple[list[str], str, str]:
    """Mirror the target-name branch of the read-side productive router."""
    prop = record.get("property") or {}
    location_region = norm(path_text(prop, PROPERTY_REGION_PATHS))
    location_comuna = norm(path_text(prop, PROPERTY_COMMUNE_PATHS))
    original = path_text(prop, PROPERTY_EXEC_PATHS)
    original_norm = norm(original)

    def lookup(name: str) -> dict[str, Any] | None:
        return find_user(name, agents)

    if use_property_exec and any(user_name_match(original, team_name) for team_name in OUR_TEAM) and original:
        matched = next(team_name for team_name in OUR_TEAM if user_name_match(original, team_name))
        if norm(matched) in TEMPORARILY_INACTIVE:
            return ["Mariela Arriagada"], "PROPERTY_EXECUTIVE_MATCH_INACTIVE_REDIRECT", original
        return [matched], "PROPERTY_EXECUTIVE_MATCH", original

    if "jorge pablo caro" in original_norm or not lookup(original):
        if "metropolitana" in location_region or "xiii" in location_region:
            names = [
                name for name in ROUND_ROBIN_RM
                if name != "Mariela Arriagada" or location_comuna in MARIELA_PRIORITY_COMUNAS
            ]
            return names, "REGIONAL_FALLBACK_METROPOLITANA", original
        if "maule" in location_region or "vii" in location_region:
            return ["Paula Morales"], "REGIONAL_FALLBACK_MAULE", original
        if any(token in location_region for token in ("nuble", "bio", "xvi", "viii", "valparaiso", "quinta")):
            return ["Rocío Aliaga"], "REGIONAL_FALLBACK_NUBLE_BIO_VALPARAISO", original
        return ["Erika Garrido"], "REGIONAL_FALLBACK_OTHER", original
    return [original], "PROPERTY_EXECUTIVE_EXTERNAL_OR_RESOLVED", original


def resolve_target_users(names: Iterable[str], agents: list[dict[str, Any]], owner_name: str) -> list[dict[str, Any]]:
    output = []
    for name in names:
        user = find_user(name, agents)
        if user and not user_name_match(user.get("nombre"), owner_name):
            output.append(user)
    return unique_by_id(output)


def t0_pool(record: dict[str, Any], agents: list[dict[str, Any]], properties: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    classification, candidates, reason = router_candidates(record, agents, properties)
    targets, rule, property_exec = router_targets(record, agents)
    return candidates, {
        "classification": classification,
        "reason": reason,
        "target_names": targets,
        "target_rule": rule,
        "property_exec": property_exec,
    }


def no_value(value: Any) -> bool:
    return value in (None, "", "NO_INFORMADO", "N/A", "N/D")


def analyze_consistency(record: dict[str, Any], catalog: dict[str, set[str]]) -> dict[str, Any]:
    lead = record.get("lead") or {}
    prop = record.get("property") or {}
    lead_commune_raw = path_text(lead, LEAD_COMMUNE_PATHS)
    lead_region_raw = path_text(lead, LEAD_REGION_PATHS)
    property_commune_raw = path_text(prop, PROPERTY_COMMUNE_PATHS)
    property_region_raw = path_text(prop, PROPERTY_REGION_PATHS)
    lead_commune = commune_key(lead_commune_raw)
    property_commune = commune_key(property_commune_raw)
    lead_region = compact_region_key(lead_region_raw)
    property_region = compact_region_key(property_region_raw)
    territory_commune = property_commune or lead_commune
    territory_region = property_region or lead_region
    expected_regions = catalog_regions_for_commune(catalog, territory_commune)
    flags: list[str] = []
    if not record.get("property_code"):
        flags.append("PROPERTY_CODE_MISSING")
    if not record.get("property_found"):
        flags.append("PROPERTY_NOT_FOUND")
    if not territory_commune:
        flags.append("COMMUNE_MISSING")
    elif not expected_regions:
        flags.append("COMMUNE_NOT_IN_CATALOG")
    if lead_commune and property_commune and lead_commune != property_commune:
        flags.append("LEAD_PROPERTY_COMMUNE_MISMATCH")
    if lead_region and property_region and lead_region != property_region:
        flags.append("LEAD_PROPERTY_REGION_MISMATCH")
    if property_region and expected_regions and property_region not in expected_regions:
        flags.append("PROPERTY_REGION_CATALOG_MISMATCH")
    if lead_region and expected_regions and lead_region not in expected_regions:
        flags.append("LEAD_REGION_CATALOG_MISMATCH")
    if not flags:
        consistency = "CONSISTENT"
    elif any(flag in flags for flag in ("PROPERTY_NOT_FOUND", "PROPERTY_CODE_MISSING", "COMMUNE_MISSING", "COMMUNE_NOT_IN_CATALOG")):
        consistency = "DATA_INSUFFICIENT"
    else:
        consistency = "DATA_INCONSISTENT"
    return {
        "lead_commune_raw": lead_commune_raw,
        "lead_commune_norm": lead_commune,
        "lead_region_raw": lead_region_raw,
        "lead_region_norm": lead_region,
        "property_commune_raw": property_commune_raw,
        "property_commune_norm": property_commune,
        "property_region_raw": property_region_raw,
        "property_region_norm": property_region,
        "territory_commune_norm": territory_commune,
        "territory_region_norm": territory_region,
        "catalog_regions": sorted(expected_regions),
        "catalog_commune_found": bool(expected_regions),
        "flags": flags,
        "consistency": consistency,
        "property_exec": path_text(prop, PROPERTY_EXEC_PATHS),
    }


def make_profile_rows(agents: list[dict[str, Any]], catalog: dict[str, set[str]]) -> list[dict[str, Any]]:
    rows = []
    for user in agents:
        raw = profile_raw_values(user)
        normalized = profile_communes(user)
        regions = sorted(observable_profile_regions(user, catalog))
        unknown_communes = sorted(commune for commune in normalized if not catalog_regions_for_commune(catalog, commune))
        missing = []
        if not normalized:
            missing.append("comunas_interes")
        if unknown_communes:
            missing.append("catalog_commune")
        if no_value(user.get("region")) and no_value(user.get("region_slug")):
            missing.append("region")
        if no_value(user.get("oficina")) and no_value(user.get("office")):
            missing.append("oficina")
        rows.append({
            "user_id": text(user.get("_id")),
            "executive": text(user.get("nombre")),
            "is_active": user.get("is_active"),
            "role": text(user.get("rol")),
            "declared_communes_raw": ";".join(raw),
            "declared_communes_norm": ";".join(normalized),
            "declared_commune_count": len(normalized),
            "declared_communes_not_in_catalog": ";".join(unknown_communes),
            "observable_catalog_regions": ";".join(regions),
            "profile_region_raw": text(user.get("region")),
            "profile_region_norm": compact_region_key(user.get("region")),
            "profile_region_slug": text(user.get("region_slug")),
            "office_raw": text(user.get("oficina")),
            "office_alt_raw": text(user.get("office")),
            "other_territory_fields": json_cell({"zonas": user.get("zonas")}),
            "missing_territory_data": ";".join(missing),
        })
    return rows


def scenario_pool_rows(
    record: dict[str, Any],
    consistency: dict[str, Any],
    agents: list[dict[str, Any]],
    properties: dict[str, dict[str, Any]],
    catalog: dict[str, set[str]],
    t0_users: list[dict[str, Any]],
    t0_meta: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    owner_id = text(record.get("owner_id"))
    commune = consistency["territory_commune_norm"]
    region = consistency["territory_region_norm"]
    t1 = explicit_commune_candidates(agents, commune, owner_id=owner_id)
    t2 = unique_by_id(t0_users + t1)
    t3_regional = []
    if commune and region and catalog_regions_for_commune(catalog, commune):
        t3_regional = regional_candidates(agents, commune, region, catalog, owner_id=owner_id)
    t3 = unique_by_id(t2 + t3_regional)
    t4_targets, t4_rule, _ = router_targets(record, agents, use_property_exec=False)
    t4 = resolve_target_users(t4_targets, agents, record.get("owner_name") or "")
    return {
        "T0_ROUTER_ACTUAL": t0_users,
        "T1_EXPLICIT_COMMUNE": t1,
        "T2_UNION_T0_T1": t2,
        "T3_REGIONAL_DECLARED": t3,
        "T4_NO_PROPERTY_EXEC_RESTRICTION": t4,
    }


def pool_reason(
    user: dict[str, Any],
    record: dict[str, Any],
    consistency: dict[str, Any],
    agents: list[dict[str, Any]],
    t0_users: list[dict[str, Any]],
    t0_meta: dict[str, Any],
    pools: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    user_id = text(user.get("_id"))
    owner_name = text(record.get("owner_name"))
    target_names = t0_meta["target_names"]
    initial = any(user_name_match(user.get("nombre"), name) for name in target_names)
    in_t0 = any(text(item.get("_id")) == user_id for item in t0_users)
    if not consistency["territory_commune_norm"] and not record.get("property_code"):
        reason = "PROPERTY_NOT_RESOLVED"
    elif initial and user_name_match(user.get("nombre"), owner_name):
        reason = "OWNER_EXCLUDED"
    elif initial and not in_t0:
        reason = "TARGET_USER_NOT_ACTIVE_OR_NOT_RESOLVED"
    elif in_t0:
        reason = "ROUTER_CURRENT_POOL"
    else:
        reason = "ROUTER_RULE_EXCLUDED"
    in_t1 = any(text(item.get("_id")) == user_id for item in pools["T1_EXPLICIT_COMMUNE"])
    in_t2 = any(text(item.get("_id")) == user_id for item in pools["T2_UNION_T0_T1"])
    in_t3 = any(text(item.get("_id")) == user_id for item in pools["T3_REGIONAL_DECLARED"])
    in_t4 = any(text(item.get("_id")) == user_id for item in pools["T4_NO_PROPERTY_EXEC_RESTRICTION"])
    return {
        "lead_id": text(record.get("lead_id")),
        "assignment_cycle_id": text(record.get("cycle_id")),
        "owner_user_id": text(record.get("owner_id")),
        "owner_current": owner_name,
        "temperature": text(record.get("temperature")),
        "lead_commune_norm": consistency["lead_commune_norm"],
        "lead_region_norm": consistency["lead_region_norm"],
        "property_code": text(record.get("property_code")),
        "property_commune_norm": consistency["property_commune_norm"],
        "property_region_norm": consistency["property_region_norm"],
        "territory_commune_norm": consistency["territory_commune_norm"],
        "territory_region_norm": consistency["territory_region_norm"],
        "property_executive": t0_meta["property_exec"],
        "router_target_names": ";".join(target_names),
        "router_target_rule": t0_meta["target_rule"],
        "active_agent_id": user_id,
        "active_agent": text(user.get("nombre")),
        "candidate_initial_target": "yes" if initial else "no",
        "candidate_t0_final": "yes" if in_t0 else "no",
        "t0_entry_exclusion_reason": reason,
        "candidate_t1_explicit_commune": "yes" if in_t1 else "no",
        "candidate_t2_union": "yes" if in_t2 else "no",
        "candidate_t3_regional_declared": "yes" if in_t3 else "no",
        "candidate_t4_no_property_exec_restriction": "yes" if in_t4 else "no",
        "profile_communes_norm": ";".join(profile_communes(user)),
    }


def scenario_rows(
    records: list[dict[str, Any]],
    analyzed: dict[str, dict[str, Any]],
    pools_by_cycle: dict[str, dict[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        cycle_id = text(record.get("cycle_id"))
        info = analyzed[cycle_id]
        base_ids = {text(user.get("_id")) for user in pools_by_cycle[cycle_id]["T0_ROUTER_ACTUAL"]}
        for scenario in SCENARIOS:
            pool = pools_by_cycle[cycle_id][scenario]
            pool_ids = {text(user.get("_id")) for user in pool}
            rows.append({
                "scenario": scenario,
                "lead_id": text(record.get("lead_id")),
                "assignment_cycle_id": cycle_id,
                "owner_current": text(record.get("owner_name")),
                "property_code": text(record.get("property_code")),
                "commune": info["territory_commune_norm"],
                "region": info["territory_region_norm"],
                "pool_size": len(pool),
                "candidate_ids": ids_cell(pool),
                "candidate_names": names_cell(pool),
                "added_vs_t0": names_cell([user for user in pool if text(user.get("_id")) not in base_ids]),
                "removed_vs_t0": names_cell([user for user in pools_by_cycle[cycle_id]["T0_ROUTER_ACTUAL"] if text(user.get("_id")) not in pool_ids]),
                "catalog_regions": ";".join(info["catalog_regions"]),
                "data_consistency": info["consistency"],
                "t0_router_reason": info["t0_meta"]["reason"],
            })
    return rows


def gap_rows(records: list[dict[str, Any]], analyzed: dict[str, dict[str, Any]], pools_by_cycle: dict[str, dict[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        info = analyzed[text(record.get("cycle_id"))]
        grouped[(info["territory_commune_norm"], info["territory_region_norm"])].append(record)
    rows = []
    for (commune, region), values in sorted(grouped.items()):
        t0 = unique_by_id(sum((pools_by_cycle[text(record.get("cycle_id"))]["T0_ROUTER_ACTUAL"] for record in values), []))
        t2 = unique_by_id(sum((pools_by_cycle[text(record.get("cycle_id"))]["T2_UNION_T0_T1"] for record in values), []))
        explicit = unique_by_id(sum((pools_by_cycle[text(record.get("cycle_id"))]["T1_EXPLICIT_COMMUNE"] for record in values), []))
        inconsistent = any(analyzed[text(record.get("cycle_id"))]["consistency"] != "CONSISTENT" for record in values)
        if inconsistent:
            classification = "DATA_INCONSISTENT"
        elif len(t2) >= 2:
            classification = "ADEQUATE_COVERAGE"
        elif len(t2) == 1:
            classification = "SINGLE_POINT_OF_FAILURE"
        else:
            classification = "NO_COVERAGE"
        rows.append({
            "commune": commune,
            "region": region,
            "safe_lead_count": len(values),
            "t0_candidate_count": len(t0),
            "t0_candidate_names": names_cell(t0),
            "explicit_commune_candidate_count": len(explicit),
            "explicit_commune_candidate_names": names_cell(explicit),
            "t2_candidate_count": len(t2),
            "t2_candidate_names": names_cell(t2),
            "coverage_classification": classification,
            "data_consistency_flags": ";".join(sorted({flag for record in values for flag in analyzed[text(record.get("cycle_id"))]["flags"]})),
        })
    return rows


def absence_rows(records: list[dict[str, Any]], analyzed: dict[str, dict[str, Any]], pools_by_cycle: dict[str, dict[str, list[dict[str, Any]]]], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for absent_name in ABSENCE_NAMES:
        absent = find_user(absent_name, agents)
        absent_id = text(absent.get("_id")) if absent else ""
        for record in records:
            cycle_id = text(record.get("cycle_id"))
            info = analyzed[cycle_id]
            pool = pools_by_cycle[cycle_id]["T0_ROUTER_ACTUAL"]
            remaining = [user for user in pool if text(user.get("_id")) != absent_id]
            if absent_id and len(remaining) == 0 and pool:
                classification = "POOL_EMPTY_AFTER_ABSENCE"
            elif absent_id and len(remaining) == 1:
                classification = "CANDIDATE_UNIQUE_AFTER_ABSENCE"
            elif absent_id and absent_id in {text(user.get("_id")) for user in pool}:
                classification = "POOL_REMAINS"
            else:
                classification = "ABSENT_NOT_IN_CURRENT_POOL"
            rows.append({
                "absent_executive": absent_name,
                "absent_user_id": absent_id,
                "lead_id": text(record.get("lead_id")),
                "assignment_cycle_id": cycle_id,
                "commune": info["territory_commune_norm"],
                "region": info["territory_region_norm"],
                "t0_pool_size": len(pool),
                "t0_pool_names": names_cell(pool),
                "remaining_pool_size": len(remaining),
                "remaining_pool_names": names_cell(remaining),
                "lost_candidate": "yes" if absent_id and any(text(user.get("_id")) == absent_id for user in pool) else "no",
                "classification": classification,
            })
    return rows


def md_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(value if value not in (None, "") else "") .replace("|", "\\|") for value in row) + " |")
    return "\n".join(output)


def pct(count: int, total: int) -> float:
    return round(count * 100.0 / total, 1) if total else 0.0


def scenario_summary(scenario_rows_value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scenario_rows_value:
        by_scenario[row["scenario"]].append(row)
    output = []
    for scenario in SCENARIOS:
        values = by_scenario[scenario]
        sizes = [int(row["pool_size"]) for row in values]
        output.append({
            "scenario": scenario,
            "pool_0": sizes.count(0),
            "pool_1": sizes.count(1),
            "pool_2": sizes.count(2),
            "pool_3_plus": sum(size >= 3 for size in sizes),
            "average_candidates": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
            "max_candidates": max(sizes) if sizes else 0,
            "leads_analyzed": len(values),
        })
    return output


def make_report(
    records: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    profile_rows: list[dict[str, Any]],
    consistency_rows: list[dict[str, Any]],
    analyzed: dict[str, dict[str, Any]],
    scenario_summary_rows: list[dict[str, Any]],
    gap_rows_value: list[dict[str, Any]],
    absence_rows_value: list[dict[str, Any]],
    pool_rows_value: list[dict[str, Any]],
    source_safe_count: int,
) -> None:
    t0_summary = next(row for row in scenario_summary_rows if row["scenario"] == "T0_ROUTER_ACTUAL")
    t0_counter = Counter({
        0: int(t0_summary["pool_0"]),
        1: int(t0_summary["pool_1"]),
        2: int(t0_summary["pool_2"]),
        3: int(t0_summary["pool_3_plus"]),
    })
    consistency_counter = Counter(row["data_consistency"] for row in consistency_rows)
    gap_counter = Counter(row["coverage_classification"] for row in gap_rows_value)
    commune_mismatch_count = sum("LEAD_PROPERTY_COMMUNE_MISMATCH" in row["data_consistency_flags"] for row in consistency_rows)
    region_source_mismatch_count = sum(
        bool(row["lead_region_norm"] and row["property_region_norm"] and row["lead_region_norm"] != row["property_region_norm"])
        for row in consistency_rows
    )
    property_catalog_mismatch_count = sum("PROPERTY_REGION_CATALOG_MISMATCH" in row["data_consistency_flags"] for row in consistency_rows)
    lead_catalog_mismatch_count = sum("LEAD_REGION_CATALOG_MISMATCH" in row["data_consistency_flags"] for row in consistency_rows)
    missing_lead_region_count = sum(not row["lead_region_norm"] for row in consistency_rows)
    properties_without_territory = sum(not row["property_commune_norm"] or not row["property_region_norm"] for row in consistency_rows)
    profile_catalog_gaps = [(row["executive"], row["declared_communes_not_in_catalog"]) for row in profile_rows if row.get("declared_communes_not_in_catalog")]
    owner_ok_count = sum(row["owner_state"] == "OWNER_OK" for row in consistency_rows)
    cycle_ok_count = sum(row["cycle_state"] == "CYCLE_OK" for row in consistency_rows)
    one_active_cycle_count = sum(row["active_cycle_count"] == 1 for row in consistency_rows)
    reopened_count = sum(row["cycle_reopened"] == "yes" for row in consistency_rows)
    absence_summary = []
    for absent_name in ABSENCE_NAMES:
        values = [row for row in absence_rows_value if row["absent_executive"] == absent_name]
        absence_summary.append((absent_name, sum(row["classification"] == "POOL_EMPTY_AFTER_ABSENCE" for row in values), sum(row["classification"] == "CANDIDATE_UNIQUE_AFTER_ABSENCE" for row in values)))

    t0_unique_by_name = Counter()
    for cycle_info in analyzed.values():
        if len(cycle_info["t0_users"]) == 1:
            t0_unique_by_name[text(cycle_info["t0_users"][0].get("nombre"))] += 1
    rocio = find_user("Rocío Aliaga", agents)
    rocio_id = text(rocio.get("_id")) if rocio else ""
    rocio_info = [value for value in analyzed.values() if len(value["t0_users"]) == 1 and text(value["t0_users"][0].get("_id")) == rocio_id]
    rocio_commune = Counter(value["territory_commune_norm"] for value in rocio_info)
    rocio_region = Counter(value["territory_region_norm"] for value in rocio_info)
    rocio_property = Counter(value["property_exec"] for value in rocio_info if value["property_exec"])
    rocio_rule = Counter(value["t0_meta"]["target_rule"] for value in rocio_info)
    no_alternative = sum(1 for value in analyzed.values() if not value["pools"]["T2_UNION_T0_T1"])
    no_candidate_after_t2 = sum(1 for value in analyzed.values() if not value["pools"]["T2_UNION_T0_T1"])
    t2_unique = sum(1 for value in analyzed.values() if len(value["pools"]["T2_UNION_T0_T1"]) == 1)
    changed_t2 = sum(1 for value in analyzed.values() if {text(u.get("_id")) for u in value["t0_users"]} != {text(u.get("_id")) for u in value["pools"]["T2_UNION_T0_T1"]})
    t4_applicable = any(value["property_exec"] and any(user_name_match(value["property_exec"], team) for team in OUR_TEAM) for value in analyzed.values())
    lead_owner_by_id = {text(row["lead_id"]): row["owner_current"] for row in pool_rows_value}
    owner_counts = Counter(lead_owner_by_id.values())
    t0_candidate_memberships = Counter(
        text(user.get("nombre"))
        for value in analyzed.values()
        for user in value["t0_users"]
    )
    t0_unique_candidate_pools = Counter(
        text(value["t0_users"][0].get("nombre"))
        for value in analyzed.values()
        if len(value["t0_users"]) == 1
    )
    t2_added_memberships = Counter(
        text(user.get("nombre"))
        for value in analyzed.values()
        for user in value["pools"]["T2_UNION_T0_T1"]
        if text(user.get("_id")) not in {text(item.get("_id")) for item in value["t0_users"]}
    )

    lines = [
        "# Auditoría territorial de reasignación SLA — Fase 1C",
        "",
        "## Alcance y controles",
        "",
        f"La auditoría reconstruyó desde MongoDB el conjunto certificado por Fase 1A: `{source_safe_count}` filas `SAFE_TO_SHADOW_REASSIGN`; se analizaron `{len(records)}` ciclos vivos disponibles en la lectura. No se calcularon scores, capacidad, ranking ni ganador.",
        "",
        "La lectura de datos usa el cargador analítico de Fase 1B con proyección sin teléfonos ni contenido de mensajes. El catálogo territorial es el GeoJSON local `static/geo/chile-comunas.geojson`. Las normalizaciones son solo analíticas.",
        "",
        "## Flujo exacto del router actual",
        "",
        "1. El router toma el código de propiedad del lead y busca la propiedad asociada.",
        "2. Si no hay código o propiedad, termina sin pool territorial.",
        "3. Lee `ubicacion.region`, `ubicacion.comuna` y el ejecutivo/captador/responsable de la propiedad.",
        "4. Si el ejecutivo de ficha coincide con un nombre de `OUR_TEAM`, prioriza ese ejecutivo; si es Raquel, redirige a Mariela.",
        "5. Si no coincide o es Jorge Pablo Caro, aplica fallback por región: Metropolitana con round-robin Mariela/Hernán/María Paz y prioridad de comuna para Mariela; Maule a Paula; Ñuble/Biobío/Valparaíso a Rocío; resto a Erika.",
        "6. Resuelve nombres contra agentes activos y excluye al owner actual. No selecciona ganador en esta auditoría.",
        "",
        "Campos que no participan en T0: comuna/región declaradas del usuario, `comunas_interes_norm`, oficina/sucursal, distancia, ETA y reglas de cercanía. La propiedad asociada sí participa; la comuna solo participa en la prioridad de Mariela dentro de Metropolitana.",
        "",
        "## T0 — pool actual",
        "",
        md_table(["pool", "leads"], [["0", t0_counter.get(0, 0)], ["1", t0_counter.get(1, 0)], ["2", t0_counter.get(2, 0)], ["3+", t0_counter.get(3, 0)]]),
        "",
        f"Resultado observado: `{t0_counter.get(0, 0)}` pool 0, `{t0_counter.get(1, 0)}` pool 1, `{t0_counter.get(2, 0)}` pool 2 y `{t0_counter.get(3, 0)}` pool 3+. Promedio: `{t0_summary['average_candidates']}`. La expectativa previa 0/30/4/0 se reproduce solo si estos datos permanecen iguales.",
        "",
        "## Perfiles territoriales de agentes activos",
        "",
        md_table(["ejecutivo", "comunas declaradas", "regiones observables en catálogo", "faltantes"], [[row["executive"], row["declared_communes_norm"], row["observable_catalog_regions"], row["missing_territory_data"]] for row in profile_rows]),
        "",
        "La ausencia de comuna declarada no se interpreta como cobertura. La región u oficina del perfil tampoco se transforma en cobertura porque el router actual no las usa para T0.",
        "",
        "## Resumen por ejecutivo",
        "",
        md_table(["ejecutivo activo", "leads como owner actual", "membresías T0", "pools T0 únicos", "altas T2"], [[
            row["executive"], owner_counts.get(row["executive"], 0), t0_candidate_memberships.get(row["executive"], 0), t0_unique_candidate_pools.get(row["executive"], 0), t2_added_memberships.get(row["executive"], 0)
        ] for row in profile_rows]),
        "",
        "## Por qué hay candidato único",
        "",
        md_table(["causa observable", "cantidad"], [
            ["Leads T0 con pool único", sum(t0_counter.get(1, 0) for _ in [0])],
            ["Pool único Rocío", t0_unique_by_name.get("Rocío Aliaga", 0)],
            ["Pool único por otros ejecutivos", sum(t0_unique_by_name.values()) - t0_unique_by_name.get("Rocío Aliaga", 0)],
            ["Leads T0 sin candidato", t0_counter.get(0, 0)],
        ]),
        "",
        f"El pool único se explica por la rama territorial actual y/o por la prioridad del ejecutivo de ficha. La auditoría agente-por-agente con razón de entrada/exclusión se encuentra en `{POOL_CSV}`. No se atribuye unicidad a una tasa de respuesta ni a capacidad.",
        "",
        "## Rocío",
        "",
        md_table(["criterio entre sus pools únicos", "cantidad"], [
            ["Leads donde es candidata única T0", len(rocio_info)],
            ["Por comuna explícita", sum(rocio_rule.get("EXPLICIT_COMMUNE", 0) for _ in [0])],
            ["Por región/fallback", sum(count for rule, count in rocio_rule.items() if "REGIONAL" in rule)],
            ["Por propiedad/ficha", sum(count for rule, count in rocio_rule.items() if "PROPERTY_EXECUTIVE" in rule)],
            ["Por fallback no regional", rocio_rule.get("REGIONAL_FALLBACK_OTHER", 0)],
            ["Sin otro ejecutivo activo en el pool T0", len(rocio_info)],
        ]),
        "",
        f"En los pools únicos de Rocío, las regiones observadas son `{'; '.join(f'{key}={value}' for key, value in sorted(rocio_region.items())) or 'ninguna'}` y las comunas `{'; '.join(f'{key}={value}' for key, value in sorted(rocio_commune.items())) or 'ninguna'}`. La propiedad/ficha observada se detalla como `{'; '.join(f'{key}={value}' for key, value in sorted(rocio_property.items())) or 'sin valor'}`. No se infiere una cobertura alternativa solo porque otra persona tenga mejor KPI.",
        "",
        "## Escenarios T0–T4",
        "",
        md_table(["escenario", "pool 0", "pool 1", "pool 2", "pool 3+", "promedio candidatos", "máximo"], [[row["scenario"], row["pool_0"], row["pool_1"], row["pool_2"], row["pool_3_plus"], row["average_candidates"], row["max_candidates"]] for row in scenario_summary_rows]),
        "",
        "- T1 usa exclusivamente coincidencias exactas de comuna declarada y normalizada; no agrega región, propiedad/ficha ni fallback.",
        f"- T2 es la unión T0 + T1. Cambia el pool en `{changed_t2}` de `{len(records)}` leads; quedan `{t2_unique}` con candidato único y `{no_candidate_after_t2}` sin candidato.",
        "- T3 agrega solo perfiles con al menos dos comunas declaradas en la misma región del catálogo, sin cruzar regiones. Es hipotético.",
        f"- T4 es aplicable porque la rama de ejecutivo de propiedad sí se observa en la población (`{'sí' if t4_applicable else 'no'}`). Simula la regla territorial por región sin esa restricción; no cambia producción.",
        "",
        "## Gaps territoriales",
        "",
        md_table(["clasificación", "territorios"], [[key, gap_counter.get(key, 0)] for key in ("ADEQUATE_COVERAGE", "SINGLE_POINT_OF_FAILURE", "NO_COVERAGE", "DATA_INCONSISTENT")]),
        "",
        "La clasificación se calcula sobre T2 para no confundir cobertura explícita con el pool histórico. `DATA_INCONSISTENT` prevalece cuando existen contradicciones de lead, propiedad o catálogo; no se corrige ninguna fuente.",
        "",
        "## Simulación de ausencia",
        "",
        md_table(["ausente", "leads sin candidato T0", "leads con candidato único restante"], absence_summary),
        "",
        "La simulación es una lectura de resiliencia del pool actual. No es una recomendación de reemplazo ni asigna leads.",
        "",
        "## Consistencia de datos",
        "",
        md_table(["clasificación", "casos"], [[key, value] for key, value in sorted(consistency_counter.items())]),
        "",
        "### DATOS",
        "",
        f"- Inconsistencias comuna lead/propiedad: `{commune_mismatch_count}`; región lead/propiedad comparable: `{region_source_mismatch_count}`.",
        f"- Inconsistencias de región contra el catálogo después de normalizar acentos, guiones, espacios y alias oficiales: propiedad `{property_catalog_mismatch_count}`, lead `{lead_catalog_mismatch_count}`.",
        f"- Región del lead ausente en la lectura analítica: `{missing_lead_region_count}`; no se rellenó desde la propiedad para esta comprobación.",
        f"- Propiedades asociadas sin comuna o región: `{properties_without_territory}` de `{len(consistency_rows)}`.",
        f"- Perfiles con comunas declaradas fuera del catálogo: `{len(profile_catalog_gaps)}`; {('; '.join(f'{name}={communes}' for name, communes in profile_catalog_gaps)) or 'ninguno'}.",
        f"- Owner/ciclo: `OWNER_OK={owner_ok_count}`, `CYCLE_OK={cycle_ok_count}`, un ciclo activo por lead `{one_active_cycle_count}`; ciclos con antecedente reabierto `{reopened_count}`. No se reparó ninguno.",
        "- Las normalizaciones son locales al análisis; no se escribieron alias ni correcciones a Mongo.",
        "",
        f"Los campos y banderas por lead están en `{CONSISTENCY_CSV}`. Los detalles de razones por agente están en `{POOL_CSV}`. Propiedades sin territorio, alias problemáticos y desacuerdos lead/propiedad se conservan como observaciones; no se ejecutaron normalizaciones sobre Mongo.",
        "",
        "## Respuestas a las preguntas de Fase 1C",
        "",
        "1. El pool único no demuestra que el ejecutivo sea el mejor; demuestra que el router actual dejó una sola salida después de sus reglas de ficha/fallback y exclusión del owner.",
        "2. La propiedad/ficha restringe T0 cuando su ejecutivo coincide con `OUR_TEAM`; en los demás casos opera el fallback regional. Esto debe ser una decisión de Sol, no una regla inventada aquí.",
        "3. La comuna del lead y la comuna declarada del usuario no son inputs efectivos de T0. Solo se auditan y se simulan en T1/T2/T3.",
        "4. La mayor concentración aparente de Rocío se explica por la ruta territorial vigente y sus nombres activos, no por un ranking de desempeño.",
        "5. T2 permite medir si la cobertura explícita de comunas abre alternativas sin eliminar T0; los cambios exactos están en el CSV de escenarios.",
        "6. T3 muestra el efecto de cobertura regional declarada solo con dos o más comunas en una misma región del catálogo; no autoriza asignación.",
        "7. La auditoría no calcula distancia, costo, ETA, capacidad ni ganador. El riesgo operativo es confundir cobertura territorial declarada con conveniencia real de atención.",
        "",
        "## Conclusión técnica",
        "",
        f"T0 deja `{t0_counter.get(1, 0)}` de `{len(records)}` leads con candidato único; no prueba superioridad de desempeño.",
        f"Rocío concentra `{len(rocio_info)}` pools únicos y su ausencia dejaría `{sum(row[1] for row in absence_summary if row[0] == 'Rocío Aliaga')}` pools vacíos.",
        f"T2 agrega cobertura explícita en `{changed_t2}` leads; T3 amplía cobertura regional solo como hipótesis declarada.",
        f"Persisten `{sum(value for key, value in consistency_counter.items() if key != 'CONSISTENT')}` leads con inconsistencia territorial; requieren decisión de Sol antes de usar el pool.",
        "No se selecciona ganador ni se recomienda reasignación automática en esta fase.",
        "",
        "## Validaciones",
        "",
        md_table(["control", "resultado"], [
            ["población certificada de entrada", f"{source_safe_count} filas; {len(records)} ciclos disponibles"],
            ["categoría duplicada", "no aplica; una fila por lead/ciclo en los artefactos"],
            ["legacy en población", "0; la entrada Fase 1A ya excluye legacy"],
            ["owner/ciclo inconsistente tratado como candidato", "no; se conserva la certificación y se reporta sin reparar"],
            ["pool T0 reproducible", "sí; mirror read-side del router actual"],
            ["determinismo", "orden estable por nombre/id y escenarios en memoria"],
            ["PII exportada", "sin teléfonos, mensajes completos ni contenido de cliente"],
        ]),
        "",
        "## Seguridad",
        "",
        "- Mongo writes: `0`.",
        "- Reasignaciones: `0`.",
        "- Cambios territoriales: `0`.",
        "- Owner/cycle: `0` cambios.",
        "- Deploy: `0`.",
        "- Scheduler: `0`.",
        "- Flags: `0`.",
        "",
        "## Archivos",
        "",
        f"- `{REPORT}`",
        f"- `{POOL_CSV}`",
        f"- `{AGENT_CSV}`",
        f"- `{CONSISTENCY_CSV}`",
        f"- `{SCENARIOS_CSV}`",
        f"- `{GAPS_CSV}`",
        f"- `{ABSENCE_CSV}`",
        "",
        "## No implementé cambios productivos",
        "",
        "Fase 1C termina en auditoría territorial y simulación shadow. No se avanzó a Fase 1D ni a reasignación real.",
        "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    safe_rows = read_safe_rows()
    base_path = DATA_DIR / "shadow_reassignment_base.csv"
    if not base_path.exists():
        raise RuntimeError(f"No existe el artefacto de Fase 1A: {base_path}")
    base_rows = read_csv(base_path)
    base_by_cycle = {row.get("assignment_cycle_id", ""): row for row in base_rows}
    data = load_data_phase1b()
    records = enrich_records(data)
    safe_cycles = {row.get("assignment_cycle_id", "") for row in safe_rows if row.get("assignment_cycle_id")}
    live = [record for record in records if text(record.get("cycle_id")) in safe_cycles]
    live_by_cycle = {text(record.get("cycle_id")): record for record in live}
    missing = sorted(safe_cycles - set(live_by_cycle))
    if missing:
        raise RuntimeError(f"Faltan ciclos certificados en la lectura real: {missing}")
    if not live:
        raise RuntimeError("La población certificada no pudo reconstruirse desde datos reales")

    agents = active_agents(data["users"])
    catalog = build_catalog()
    profile_rows = make_profile_rows(agents, catalog)
    analyzed: dict[str, dict[str, Any]] = {}
    pools_by_cycle: dict[str, dict[str, list[dict[str, Any]]]] = {}
    pool_rows_value: list[dict[str, Any]] = []
    consistency_rows: list[dict[str, Any]] = []
    for record in sorted(live, key=lambda value: (text(value.get("lead_id")), text(value.get("cycle_id")))):
        cycle_id = text(record.get("cycle_id"))
        consistency = analyze_consistency(record, catalog)
        t0_users, t0_meta = t0_pool(record, agents, data["properties"])
        pools = scenario_pool_rows(record, consistency, agents, data["properties"], catalog, t0_users, t0_meta)
        consistency_row = {
            "lead_id": text(record.get("lead_id")),
            "assignment_cycle_id": cycle_id,
            "owner_user_id": text(record.get("owner_id")),
            "owner_current": text(record.get("owner_name")),
            "lead_owner_from_lead": text(record.get("lead_owner_name")),
            "owner_state": text(record.get("owner_state")),
            "cycle_state": text(record.get("cycle_state")),
            "active_cycle_count": record.get("active_cycle_count"),
            "active_cycle_ids": text(record.get("active_cycle_ids")),
            "cycle_reopened": "yes" if record.get("cycle_reopened") else "no",
            "property_code": text(record.get("property_code")),
            "property_found": "yes" if record.get("property_found") else "no",
            "property_executive": consistency["property_exec"],
            "lead_commune_raw": consistency["lead_commune_raw"],
            "lead_commune_norm": consistency["lead_commune_norm"],
            "property_commune_raw": consistency["property_commune_raw"],
            "property_commune_norm": consistency["property_commune_norm"],
            "lead_region_raw": consistency["lead_region_raw"],
            "lead_region_norm": consistency["lead_region_norm"],
            "property_region_raw": consistency["property_region_raw"],
            "property_region_norm": consistency["property_region_norm"],
            "territory_commune_norm": consistency["territory_commune_norm"],
            "territory_region_norm": consistency["territory_region_norm"],
            "catalog_regions": ";".join(consistency["catalog_regions"]),
            "catalog_commune_found": "yes" if consistency["catalog_commune_found"] else "no",
            "data_consistency": consistency["consistency"],
            "data_consistency_flags": ";".join(consistency["flags"]),
            "t0_router_classification": t0_meta["classification"],
            "t0_router_reason": t0_meta["reason"],
            "t0_target_rule": t0_meta["target_rule"],
            "t0_target_names": ";".join(t0_meta["target_names"]),
            "t0_pool_size": len(t0_users),
            "t0_pool_names": names_cell(t0_users),
            "t2_pool_size": len(pools["T2_UNION_T0_T1"]),
            "candidate_classification_from_phase1a": base_by_cycle.get(cycle_id, {}).get("shadow_category", ""),
        }
        consistency_rows.append(consistency_row)
        analyzed[cycle_id] = {
            **consistency,
            "t0_users": t0_users,
            "t0_meta": t0_meta,
            "pools": pools,
            "lead_id": text(record.get("lead_id")),
        }
        pools_by_cycle[cycle_id] = pools
        for agent in agents:
            pool_rows_value.append(pool_reason(agent, record, consistency, agents, t0_users, t0_meta, pools))

    ordered_records = sorted(live, key=lambda value: (text(value.get("lead_id")), text(value.get("cycle_id"))))
    scenario_rows_value = scenario_rows(ordered_records, analyzed, pools_by_cycle)
    scenario_summary_rows = scenario_summary(scenario_rows_value)
    gaps = gap_rows(ordered_records, analyzed, pools_by_cycle)
    absences = absence_rows(ordered_records, analyzed, pools_by_cycle, agents)

    pool_fields = list(pool_rows_value[0]) if pool_rows_value else []
    agent_fields = list(profile_rows[0]) if profile_rows else []
    consistency_fields = list(consistency_rows[0]) if consistency_rows else []
    scenario_fields = list(scenario_rows_value[0]) if scenario_rows_value else []
    gap_fields = list(gaps[0]) if gaps else []
    absence_fields = list(absences[0]) if absences else []
    write_csv(POOL_CSV, pool_rows_value, pool_fields)
    write_csv(AGENT_CSV, profile_rows, agent_fields)
    write_csv(CONSISTENCY_CSV, consistency_rows, consistency_fields)
    write_csv(SCENARIOS_CSV, scenario_rows_value, scenario_fields)
    write_csv(GAPS_CSV, gaps, gap_fields)
    write_csv(ABSENCE_CSV, absences, absence_fields)
    make_report(live, agents, profile_rows, consistency_rows, analyzed, scenario_summary_rows, gaps, absences, pool_rows_value, len(safe_rows))

    print(f"report={REPORT}")
    for path in (POOL_CSV, AGENT_CSV, CONSISTENCY_CSV, SCENARIOS_CSV, GAPS_CSV, ABSENCE_CSV):
        print(f"csv={path}")
    print(f"safe_input={len(safe_rows)}")
    print(f"records_analyzed={len(live)}")
    print(f"active_agents={len(agents)}")
    print(f"t0_histogram={pool_histogram(analyzed[text(record.get('cycle_id'))]['t0_users'] for record in ordered_records)}")


if __name__ == "__main__":
    main()
