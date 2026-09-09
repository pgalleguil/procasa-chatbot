"""Deterministic property identity resolution.

Only verified identifiers are accepted.  No fuzzy matching, address matching,
owner matching, probabilistic logic, or URL parsing is performed here.
"""

from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Optional

from .models import AliasKey, LinkageStatus, PropertyIdentityResolution


CANONICAL_PATH = "prospecto.codigo"
LEAD_ALIAS_PATHS: tuple[tuple[str, str], ...] = (
    ("mercadolibre", "prospecto.codigo_mercadolibre"),
    ("yapo", "prospecto.codigo_yapo"),
)

# These publication structures were verified against the current master.
# portal_inmobiliario.code contains the MercadoLibre identifier (MLC...), so
# both sides intentionally use the namespace ``mercadolibre``.
MASTER_PUBLICATION_PORTALS: tuple[tuple[str, str], ...] = (
    ("mercadolibre", "portal_inmobiliario"),
    ("yapo", "yapo"),
    ("toctoc", "toctoc"),
    ("chilepropiedades", "chilepropiedades"),
    ("proppit", "proppit"),
    ("procasa", "procasa"),
)


def normalize_identifier(value: Any) -> Optional[str]:
    """Safely normalize a scalar identifier without changing its structure."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        normalized = unicodedata.normalize("NFKC", str(value)).strip()
        return normalized or None
    return None


def _path_value(document: Mapping[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _iter_master_aliases(document: Mapping[str, Any]) -> Iterable[tuple[AliasKey, str]]:
    publications = document.get("publicaciones")
    if not isinstance(publications, Mapping):
        return

    for source, portal_key in MASTER_PUBLICATION_PORTALS:
        portal = publications.get(portal_key)
        if not isinstance(portal, Mapping):
            continue

        operation_records = portal.get("publicaciones")
        if isinstance(operation_records, Mapping):
            for record in operation_records.values():
                if not isinstance(record, Mapping):
                    continue
                for field_name in ("code", "code_unique"):
                    value = normalize_identifier(record.get(field_name))
                    if value:
                        yield AliasKey(source, value), f"publicaciones.{portal_key}.publicaciones.*.{field_name}"

        # ChilePropiedades also has verified root-level operation codes.
        if portal_key == "chilepropiedades":
            for field_name in ("codigo_venta", "codigo_arriendo"):
                value = normalize_identifier(portal.get(field_name))
                if value:
                    yield AliasKey(source, value), f"publicaciones.{portal_key}.{field_name}"


class PropertyIdentityResolver:
    """Resolve leads against a frozen, in-memory master index."""

    def __init__(self, properties: Iterable[Mapping[str, Any]]) -> None:
        self._canonical_index: dict[str, set[str]] = defaultdict(set)
        self._alias_index: dict[AliasKey, set[str]] = defaultdict(set)
        self._alias_path_index: dict[AliasKey, set[str]] = defaultdict(set)

        for property_doc in properties:
            property_code = normalize_identifier(property_doc.get("codigo"))
            if not property_code:
                continue
            self._canonical_index[property_code].add(property_code)
            for alias, path in _iter_master_aliases(property_doc):
                self._alias_index[alias].add(property_code)
                self._alias_path_index[alias].add(path)

    @property
    def canonical_count(self) -> int:
        return len(self._canonical_index)

    @property
    def ambiguous_alias_count(self) -> int:
        return sum(len(candidates) > 1 for candidates in self._alias_index.values())

    def alias_usage_inventory(self) -> dict[str, int]:
        return dict(Counter(alias.source for alias in self._alias_index))

    def ambiguous_aliases(self) -> tuple[AliasKey, ...]:
        return tuple(sorted(alias for alias, candidates in self._alias_index.items() if len(candidates) > 1))

    def _lead_aliases(self, lead: Mapping[str, Any]) -> list[tuple[AliasKey, str]]:
        prospecto = lead.get("prospecto")
        if not isinstance(prospecto, Mapping):
            return []
        aliases: list[tuple[AliasKey, str]] = []
        for source, path in LEAD_ALIAS_PATHS:
            field_name = path.rsplit(".", 1)[-1]
            value = normalize_identifier(prospecto.get(field_name))
            if value:
                aliases.append((AliasKey(source, value), path))
        return aliases

    def resolve(self, lead: Mapping[str, Any]) -> PropertyIdentityResolution:
        prospecto = lead.get("prospecto")
        if not isinstance(prospecto, Mapping):
            prospecto = {}

        canonical_value = normalize_identifier(prospecto.get("codigo"))
        canonical_candidates = set(self._canonical_index.get(canonical_value, set())) if canonical_value else set()
        aliases = self._lead_aliases(lead)

        alias_candidates: set[str] = set()
        alias_evidence: list[str] = []
        alias_sources: set[str] = set()
        alias_is_ambiguous = False
        for alias, path in aliases:
            candidates = self._alias_index.get(alias, set())
            if len(candidates) > 1:
                alias_is_ambiguous = True
            alias_candidates.update(candidates)
            if candidates:
                alias_sources.add(alias.source)
                alias_evidence.append(f"alias:{alias.source}:{path}")

        canonical_evidence = (f"canonical:{CANONICAL_PATH}",) if canonical_candidates else ()
        all_candidates = tuple(sorted(canonical_candidates | alias_candidates))

        if len(canonical_candidates) == 1:
            canonical_code = next(iter(canonical_candidates))
            if alias_candidates and alias_candidates != {canonical_code}:
                return PropertyIdentityResolution(
                    status=LinkageStatus.CONFLICT,
                    resolved_property_code=None,
                    evidence=canonical_evidence + tuple(alias_evidence),
                    candidates=all_candidates,
                    reason="Canonical identifier and external alias resolve to different properties",
                )
            return PropertyIdentityResolution(
                status=LinkageStatus.EXACT_CANONICAL,
                resolved_property_code=canonical_code,
                evidence=canonical_evidence + tuple(alias_evidence),
                candidates=(canonical_code,),
                reason="Canonical identifier resolved uniquely",
            )

        if len(canonical_candidates) > 1 or alias_is_ambiguous or len(alias_candidates) > 1:
            return PropertyIdentityResolution(
                status=LinkageStatus.AMBIGUOUS,
                resolved_property_code=None,
                evidence=canonical_evidence + tuple(alias_evidence),
                candidates=all_candidates,
                reason="Identifier resolves to multiple property candidates",
            )

        if len(alias_candidates) == 1:
            return PropertyIdentityResolution(
                status=LinkageStatus.EXACT_ALIAS,
                resolved_property_code=next(iter(alias_candidates)),
                evidence=tuple(alias_evidence),
                candidates=tuple(sorted(alias_candidates)),
                reason=f"External alias resolved uniquely ({', '.join(sorted(alias_sources))})",
            )

        return PropertyIdentityResolution(
            status=LinkageStatus.UNMATCHED,
            resolved_property_code=None,
            evidence=(),
            candidates=(),
            reason="No verified canonical or external alias matched",
        )


def build_property_identity_resolver(properties: Iterable[Mapping[str, Any]]) -> PropertyIdentityResolver:
    return PropertyIdentityResolver(properties)
