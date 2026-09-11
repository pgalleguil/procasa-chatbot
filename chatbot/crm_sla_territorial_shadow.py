"""Pure territorial helpers for the CRM SLA Phase 1C audit."""
from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable, Mapping

from comuna_utils import normalize_commune_slug


def commune_key(value: Any) -> str:
    return normalize_commune_slug(value) or ""


def compact_region_key(value: Any) -> str:
    raw = str(value or "").lower()
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n",
    }
    raw = "".join(replacements.get(char, char) for char in raw)
    raw = re.sub(r"[^a-z0-9\s-]", "", raw)
    tokens = [token for token in raw.replace("-", " ").split() if token not in {"region", "del", "de", "la", "los", "las", "y"}]
    compact = "".join(tokens)
    # Analytical aliases only: the local catalog uses official long names,
    # while CRM property records use shorter labels. Nothing is written back.
    if compact in {"metropolitana", "metropolitanadesantiago"}:
        return "metropolitanasantiago"
    if compact in {"bernardohiggins", "bernardoohiggins", "libertadorbernardohiggins", "libertadorbernardoohiggins"}:
        return "ohiggins"
    return compact


def profile_communes(user: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("comunas_interes_norm", "comunas_interes"):
        raw = user.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
        elif isinstance(raw, str):
            values.extend(part.strip() for part in raw.replace(";", ",").split(",") if part.strip())
    output = []
    for value in values:
        normalized = commune_key(value)
        if normalized and normalized not in output:
            output.append(normalized)
    return sorted(output)


def catalog_regions_for_commune(catalog: Mapping[str, Iterable[str]], commune: Any) -> set[str]:
    return set(catalog.get(commune_key(commune), ()))


def explicit_commune_candidates(
    users: Iterable[Mapping[str, Any]],
    commune: Any,
    *,
    owner_id: str = "",
) -> list[dict[str, Any]]:
    target = commune_key(commune)
    output = []
    for user in users:
        user_id = str(user.get("_id") or "")
        if user_id == str(owner_id or ""):
            continue
        if user.get("is_active") is not True or str(user.get("rol") or "").strip().lower() != "agente":
            continue
        if target and target in profile_communes(user):
            output.append(dict(user))
    return output


def regional_profile_regions(
    user: Mapping[str, Any],
    catalog: Mapping[str, Iterable[str]],
    *,
    minimum_communes: int = 2,
) -> set[str]:
    by_region: Counter[str] = Counter()
    for commune in profile_communes(user):
        for region in catalog_regions_for_commune(catalog, commune):
            by_region[region] += 1
    return {region for region, count in by_region.items() if count >= minimum_communes}


def regional_candidates(
    users: Iterable[Mapping[str, Any]],
    commune: Any,
    region: Any,
    catalog: Mapping[str, Iterable[str]],
    *,
    owner_id: str = "",
) -> list[dict[str, Any]]:
    region_key = compact_region_key(region)
    target_regions = catalog_regions_for_commune(catalog, commune)
    if region_key:
        target_regions = {candidate for candidate in target_regions if candidate == region_key or candidate in region_key or region_key in candidate}
    output = []
    for user in users:
        user_id = str(user.get("_id") or "")
        if user_id == str(owner_id or ""):
            continue
        if user.get("is_active") is not True or str(user.get("rol") or "").strip().lower() != "agente":
            continue
        if target_regions & regional_profile_regions(user, catalog):
            output.append(dict(user))
    return output


def exclude_owner(users: Iterable[Mapping[str, Any]], owner_id: str) -> list[dict[str, Any]]:
    return [dict(user) for user in users if str(user.get("_id") or "") != str(owner_id or "")]


def unique_by_id(users: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    seen = set()
    for user in users:
        user_id = str(user.get("_id") or "")
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        output.append(dict(user))
    return output


def pool_histogram(pools: Iterable[Iterable[Any]]) -> dict[str, int]:
    counter = Counter()
    for pool in pools:
        size = len(list(pool))
        counter["0" if size == 0 else "1" if size == 1 else "2" if size == 2 else "3+"] += 1
    return {key: counter.get(key, 0) for key in ("0", "1", "2", "3+")}


def remove_absent(users: Iterable[Mapping[str, Any]], absent_user_id: str) -> list[dict[str, Any]]:
    return [dict(user) for user in users if str(user.get("_id") or "") != str(absent_user_id or "")]
