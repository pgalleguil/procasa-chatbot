"""Canonical, period-frozen market context snapshots and offline seed planning."""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any, Iterable, Mapping

MARKET_CONTEXT_SNAPSHOT_COLLECTION = "market_context_snapshots"
MARKET_CONTEXT_NATURAL_KEY = (
    "period", "indicator_kind", "geography_level", "geography_code",
)
MARKET_CONTEXT_UNIQUE_INDEX_NAME = "uq_market_context_period_kind_geo"
SEPTEMBER_2026_PERIOD = "2026-09"


def _seed_document(
    kind: str, value: float, display_value: str, unit: str, observation_period: str,
    geography_level: str, geography_code: str, geography_label: str,
    source_name: str, source_reference: str, relevant_operations: list[str],
    methodology_notes: str, *, source_published_at: str | None = None,
) -> dict[str, Any]:
    geography = {"geography_level": geography_level, "geography_code": geography_code}
    return {
        "period": SEPTEMBER_2026_PERIOD,
        "indicator_kind": kind,
        "value": value,
        "display_value": display_value,
        "unit": unit,
        "observation_period": observation_period,
        **geography,
        "geography_label": geography_label,
        "source_name": source_name,
        "source_reference": source_reference,
        "source_published_at": source_published_at,
        "verified_at": "2026-10-06",
        "relevant_operations": relevant_operations,
        "methodology_notes": methodology_notes,
        "verified": True,
        "active": True,
    }


def _regional_unemployment_seed(
    geography_code: str, geography_label: str, value: float, source_reference: str,
) -> dict[str, Any]:
    return _seed_document(
        "UNEMPLOYMENT", value, f"{value:.1f}%".replace(".", ","), "%", "jun-ago 2026",
        "REGION", geography_code, geography_label, "INE · Encuesta Nacional de Empleo",
        source_reference, ["VENTA", "ARRIENDO"],
        "Tasa de desocupación regional, trimestre móvil junio-agosto 2026.",
        source_published_at="2026-09-30",
    )


# Regional unemployment values below were checked against the official INE
# regional ENE pages for the June-August 2026 moving quarter. Seeding every
# region also covers the owner's portfolio without embedding property codes.
SEPTEMBER_2026_REGIONAL_UNEMPLOYMENT: tuple[dict[str, Any], ...] = (
    _regional_unemployment_seed("AP", "Región de Arica y Parinacota", 9.4, "https://regiones.ine.gob.cl/arica-y-parinacota/estadisticas-regionales/sociales/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("TA", "Región de Tarapacá", 7.8, "https://regiones.ine.gob.cl/tarapaca/estadisticas-regionales/economia/economia-regional"),
    _regional_unemployment_seed("AN", "Región de Antofagasta", 6.3, "https://regiones.ine.gob.cl/antofagasta/estadisticas-regionales/sociales/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("AT", "Región de Atacama", 9.5, "https://regiones.ine.gob.cl/atacama/estadisticas-regionales/sociales/mercado-laboral"),
    _regional_unemployment_seed("CO", "Región de Coquimbo", 9.4, "https://regiones.ine.gob.cl/coquimbo/estadisticas-por-tema/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("VS", "Región de Valparaíso", 10.2, "https://regiones.ine.gob.cl/valparaiso/estadisticas-regionales/sociales/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("RM", "Región Metropolitana", 9.8, "https://www.ine.gob.cl/sala-de-prensa/prensa/general/noticia/2026/09/30/la-tasa-de-desocupaci%C3%B3n-nacional-fue-9-6-en-el-trimestre-junio---agosto-de-2026"),
    _regional_unemployment_seed("LI", "Región de O'Higgins", 10.8, "https://regiones.ine.gob.cl/ohiggins/estadisticas-regionales/sociales/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("ML", "Región del Maule", 9.5, "https://regiones.ine.gob.cl/maule/estadisticas-por-tema/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("NB", "Región de Ñuble", 11.2, "https://regiones.ine.gob.cl/nuble/estadisticas-regionales/economia/economia-regional"),
    _regional_unemployment_seed("BI", "Región del Biobío", 10.7, "https://regiones.ine.gob.cl/biobio/estadisticas-regionales/sociales/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("AR", "Región de La Araucanía", 10.1, "https://regiones.ine.gob.cl/araucania/estadisticas-por-tema/comercio-y-servicios/actividades-del-servicio-al-turismo/la-tasa-de-desocupaci%C3%B3n-en-la-regi%C3%B3n-de-la-araucan%C3%ADa-fue-10-1-en-el-trimestre-junio-agosto-2026"),
    _regional_unemployment_seed("LR", "Región de Los Ríos", 10.2, "https://regiones.ine.gob.cl/los-rios/estadisticas-regionales/sociales/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("LL", "Región de Los Lagos", 7.8, "https://regiones.ine.gob.cl/los-lagos/estadisticas-regionales/sociales/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("AI", "Región de Aysén", 4.0, "https://regiones.ine.gob.cl/aysen/estadisticas-por-tema/mercado-laboral/ocupacion-y-desocupacion"),
    _regional_unemployment_seed("MA", "Región de Magallanes", 6.9, "https://regiones.ine.gob.cl/magallanes/estadisticas-regionales/economia/economia-regional/sectores-economicos"),
)

# No regional rental series has been source-verified for this release. In
# particular, QA-only rental values must not be promoted into this seed.
SEPTEMBER_2026_SEED: tuple[dict[str, Any], ...] = (
    _seed_document(
        "MORTGAGE_REFERENCE", 4.08, "4,08%", "%", "15-09-2026",
        "REGION", "METROPOLITANA", "Región Metropolitana", "Banco Central de Chile",
        "Tasa hipotecaria >3 años / vivienda / UF; serie con cobertura RM",
        ["VENTA"], "Cobertura geográfica limitada a Región Metropolitana.", source_published_at="2026-09-15",
    ),
    _seed_document(
        "UNEMPLOYMENT", 9.6, "9,6%", "%", "jun-ago 2026",
        "COUNTRY", "CL", "Chile", "INE · Encuesta Nacional de Empleo",
        "Tasa de desocupación nacional, trimestre móvil junio-agosto 2026",
        ["VENTA", "ARRIENDO"], "Indicador nacional; fallback se etiqueta Chile.",
    ),
    _seed_document(
        "TPM", 4.5, "4,5%", "%", "septiembre 2026",
        "COUNTRY", "CL", "Chile", "Banco Central de Chile",
        "Tasa de Política Monetaria de referencia para septiembre 2026",
        ["VENTA"], "Indicador nacional.",
    ),
    _seed_document(
        "REAL_WAGES", 4.1, "+4,1%", "%", "julio 2026 · variación interanual",
        "COUNTRY", "CL", "Chile", "INE · Índice de Remuneraciones y Costos Laborales",
        "Variación real interanual de remuneraciones, julio 2026",
        ["ARRIENDO"], "Variación real interanual nacional.",
    ),
    _seed_document(
        "CPI", 4.1, "+4,1%", "%", "agosto 2026 · variación anual",
        "COUNTRY", "CL", "Chile", "INE · Índice de Precios al Consumidor",
        "Variación anual del IPC, agosto 2026",
        ["ARRIENDO"], "Variación anual nacional; no representa la variación de arriendos.",
    ),
    _seed_document(
        "REGIONAL_HOME_SALES", -2.9, "-2,9%", "%", "2T 2026 · variación interanual",
        "MARKET", "GRAN_SANTIAGO", "Gran Santiago", "CChC",
        "Ventas de viviendas en Gran Santiago, segundo trimestre 2026",
        ["VENTA"], "Solo aplicable a propiedades del mercado Gran Santiago.",
    ),
) + SEPTEMBER_2026_REGIONAL_UNEMPLOYMENT


def seed_identity(document: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Natural id: period + indicator kind + geography level + geography code."""
    return tuple(str(document.get(key) or "").strip() for key in MARKET_CONTEXT_NATURAL_KEY)  # type: ignore[return-value]


def validate_market_context_documents(
    documents: Iterable[Mapping[str, Any]], *, period: str,
) -> list[str]:
    """Validate a complete verified monthly manifest before planning any writes."""
    items = list(documents)
    errors: list[str] = []
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(period or "")):
        return ["INVALID_PERIOD"]
    required = (
        "period", "indicator_kind", "value", "display_value", "unit",
        "observation_period", "geography_level", "geography_code",
        "geography_label", "source_name", "source_reference",
        "source_published_at", "verified_at", "relevant_operations",
        "methodology_notes", "verified", "active",
    )
    identities: list[tuple[str, str, str, str]] = []
    forbidden_kinds = {"REGIONAL_RENTAL_INDICATOR", "STABLE_VACANCY", "FOGAES_CONTEXT"}
    forbidden_key_tokens = ("fixture", "qa_only", "mock", "test_value", "temporary", "debug", "sample")
    for index, document in enumerate(items):
        if not isinstance(document, Mapping):
            errors.append(f"DOC_{index}_NOT_MAPPING")
            continue
        for key in required:
            if key not in document or (key != "source_published_at" and document.get(key) in (None, "", [])):
                errors.append(f"DOC_{index}_MISSING_{key.upper()}")
        if document.get("period") != period:
            errors.append(f"DOC_{index}_PERIOD_MISMATCH")
        if document.get("verified") is not True:
            errors.append(f"DOC_{index}_NOT_VERIFIED")
        if document.get("active") is not True:
            errors.append(f"DOC_{index}_NOT_ACTIVE")
        if document.get("source_reference") in (None, "") or document.get("source_name") in (None, ""):
            errors.append(f"DOC_{index}_SOURCE_MISSING")
        value = document.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            errors.append(f"DOC_{index}_INVALID_VALUE")
        if document.get("indicator_kind") in forbidden_kinds:
            errors.append(f"DOC_{index}_FORBIDDEN_INDICATOR_KIND")
        if any(any(token in str(key).casefold() for token in forbidden_key_tokens) for key in document):
            errors.append(f"DOC_{index}_QA_FIELD_PRESENT")
        operations = document.get("relevant_operations")
        if not isinstance(operations, (list, tuple)) or not operations or any(
            str(operation).upper() not in {"VENTA", "ARRIENDO"} for operation in operations
        ):
            errors.append(f"DOC_{index}_INVALID_RELEVANT_OPERATIONS")
        identity = seed_identity(document)
        identities.append(identity)
        if any(not part for part in identity):
            errors.append(f"DOC_{index}_INCOMPLETE_NATURAL_KEY")
    if len(set(identities)) != len(identities):
        errors.append("DUPLICATE_NATURAL_IDENTITIES")
    return errors


def plan_period_update(
    *, period: str,
    incoming_documents: Iterable[Mapping[str, Any]],
    existing_period_documents: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Plan idempotent writes for one period, never mutating other periods."""
    incoming = [deepcopy(dict(item)) for item in incoming_documents if isinstance(item, Mapping)]
    existing = [dict(item) for item in existing_period_documents if isinstance(item, Mapping)]
    by_id: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    conflicts: list[dict[str, Any]] = []
    for document in existing:
        identity = seed_identity(document)
        if document.get("period") != period:
            continue
        if any(not part for part in identity):
            conflicts.append({"identity": identity, "reason": "incomplete natural key"})
        by_id.setdefault(identity, []).append(document)
    for identity, matches in by_id.items():
        if len(matches) > 1:
            conflicts.append({"identity": identity, "reason": "duplicate natural key"})

    incoming_by_id = {seed_identity(document): document for document in incoming}
    for identity in set(by_id) - set(incoming_by_id):
        conflicts.append({"identity": identity, "reason": "existing period identity absent from manifest"})

    plan: dict[str, Any] = {"inserts": [], "updates": [], "unchanged": [], "conflicts": conflicts}
    for document in incoming:
        identity = seed_identity(document)
        matches = by_id.get(identity, [])
        if not matches:
            plan["inserts"].append(document)
            continue
        if len(matches) > 1:
            continue
        current = matches[0]
        if current.get("verified") is not True or current.get("active") is not True:
            plan["conflicts"].append({"identity": identity, "reason": "existing record is not verified and active"})
            continue
        if all(current.get(key) == value for key, value in document.items()):
            plan["unchanged"].append(identity)
        else:
            plan["updates"].append({"identity": identity, "document": document})
    return plan


def inspect_unique_key_integrity(existing_documents: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Find malformed or duplicate identities that would prevent a unique index."""
    groups: dict[tuple[str, str, str, str], int] = {}
    issues: list[dict[str, Any]] = []
    for document in existing_documents:
        if not isinstance(document, Mapping):
            issues.append({"reason": "record is not a mapping"})
            continue
        identity = seed_identity(document)
        if any(not part for part in identity):
            issues.append({"identity": identity, "reason": "incomplete natural key"})
        groups[identity] = groups.get(identity, 0) + 1
    issues.extend(
        {"identity": identity, "reason": "duplicate natural key"}
        for identity, count in groups.items() if count > 1
    )
    return issues


def plan_seed(existing_documents: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare seed to current records without mutating them."""
    existing_by_id: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    for document in existing_documents:
        if isinstance(document, Mapping):
            existing_by_id.setdefault(seed_identity(document), []).append(document)
    plan: dict[str, Any] = {"inserts": [], "updates": [], "unchanged": [], "conflicts": []}
    for seed in SEPTEMBER_2026_SEED:
        identity = seed_identity(seed)
        matches = existing_by_id.get(identity, [])
        if not matches:
            plan["inserts"].append(deepcopy(seed))
        elif len(matches) > 1:
            plan["conflicts"].append({"identity": identity, "reason": "duplicate natural key"})
        elif all(matches[0].get(key) == value for key, value in seed.items()):
            plan["unchanged"].append(identity)
        else:
            plan["updates"].append({"identity": identity, "document": deepcopy(seed)})
    return plan


class MarketContextBootstrapConflict(RuntimeError):
    """Raised when the stored September baseline is not safe to initialize."""


def ensure_market_context_snapshots_initialized(db: Any) -> dict[str, Any]:
    """Ensure the approved September baseline exists in its canonical collection.

    This startup bootstrap is intentionally limited to one collection and one
    period. It inserts missing baseline identities, never updates existing
    documents, and fails closed on malformed, duplicate, extra, or divergent
    records.
    """
    documents = [deepcopy(item) for item in SEPTEMBER_2026_SEED]
    validation_errors = validate_market_context_documents(
        documents, period=SEPTEMBER_2026_PERIOD,
    )
    if len(documents) != 22 or validation_errors:
        raise MarketContextBootstrapConflict(
            "invalid canonical September 2026 manifest: "
            + ",".join(validation_errors or [f"expected 22 documents, got {len(documents)}"])
        )

    collection = db[MARKET_CONTEXT_SNAPSHOT_COLLECTION]
    key_projection = {key: 1 for key in MARKET_CONTEXT_NATURAL_KEY}
    key_issues = inspect_unique_key_integrity(collection.find({}, key_projection))
    if key_issues:
        raise MarketContextBootstrapConflict(
            f"market_context_snapshots has invalid or duplicate natural keys: {key_issues!r}"
        )

    existing = list(collection.find({"period": SEPTEMBER_2026_PERIOD}))
    plan = plan_seed(existing)
    seed_identities = {seed_identity(item) for item in documents}
    existing_identities = {seed_identity(item) for item in existing}
    unexpected = sorted(existing_identities - seed_identities)
    if unexpected:
        plan["conflicts"].extend(
            {"identity": identity, "reason": "unexpected September identity"}
            for identity in unexpected
        )
    if plan["updates"]:
        plan["conflicts"].extend(
            {"identity": item["identity"], "reason": "stored document differs from approved baseline"}
            for item in plan["updates"]
        )
    if plan["conflicts"]:
        raise MarketContextBootstrapConflict(
            f"market_context_snapshots bootstrap stopped without document writes: {plan['conflicts']!r}"
        )

    indexes = collection.index_information()
    expected_index = [(key, 1) for key in MARKET_CONTEXT_NATURAL_KEY]
    named_index = indexes.get(MARKET_CONTEXT_UNIQUE_INDEX_NAME)
    if named_index and (
        named_index.get("unique") is not True
        or list(named_index.get("key", [])) != expected_index
    ):
        raise MarketContextBootstrapConflict(
            f"index {MARKET_CONTEXT_UNIQUE_INDEX_NAME} exists with incompatible definition"
        )
    unique_index_ready = bool(named_index and named_index.get("unique") is True)
    if not unique_index_ready:
        unique_index_ready = any(
            spec.get("unique") is True and list(spec.get("key", [])) == expected_index
            for spec in indexes.values()
        )
    if not unique_index_ready:
        collection.create_index(
            expected_index,
            unique=True,
            name=MARKET_CONTEXT_UNIQUE_INDEX_NAME,
        )
        unique_index_ready = True

    inserted_count = 0
    if plan["inserts"]:
        try:
            result = collection.insert_many(plan["inserts"], ordered=True)
            inserted_count = len(result.inserted_ids)
        except Exception as exc:
            # Concurrent application starts may race after the unique index is
            # created. Accept only if the other starter completed the exact
            # approved baseline; otherwise preserve the explicit failure.
            post_race = list(collection.find({"period": SEPTEMBER_2026_PERIOD}))
            race_plan = plan_seed(post_race)
            if (
                len(post_race) != 22
                or race_plan["inserts"]
                or race_plan["updates"]
                or race_plan["conflicts"]
            ):
                raise MarketContextBootstrapConflict(
                    f"baseline insertion failed and post-race verification failed: {exc}"
                ) from exc

    final_documents = list(collection.find({"period": SEPTEMBER_2026_PERIOD}))
    final_plan = plan_seed(final_documents)
    if (
        len(final_documents) != 22
        or final_plan["inserts"]
        or final_plan["updates"]
        or final_plan["conflicts"]
        or len(final_plan["unchanged"]) != 22
    ):
        raise MarketContextBootstrapConflict(
            "post-write baseline verification failed; expected exactly 22 canonical September documents"
        )

    return {
        "status": "initialized" if inserted_count else "unchanged",
        "inserts": inserted_count,
        "updates": 0,
        "unchanged": len(final_plan["unchanged"]),
        "conflicts": 0,
        "unique_index_ready": unique_index_ready,
        "collection_documents": collection.count_documents({}),
    }


def canonical_documents_to_context(period: str, documents: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Adapt canonical collection documents to the owner portal resolver contract."""
    indicators = []
    for document in documents:
        if not isinstance(document, Mapping) or document.get("period") != period or document.get("active") is not True:
            continue
        indicators.append({
            **document,
            "kind": document.get("indicator_kind"),
            "source": document.get("source_name"),
            "period_label": document.get("observation_period"),
            "available": document.get("active") is True,
            "relevant_for": document.get("relevant_operations"),
        })
    return {"period": period, "indicators": indicators} if indicators else {}
