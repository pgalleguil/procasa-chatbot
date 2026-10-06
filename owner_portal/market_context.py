"""Canonical, period-frozen market context snapshots and offline seed planning."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

MARKET_CONTEXT_SNAPSHOT_COLLECTION = "market_context_snapshots"
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
    return tuple(str(document.get(key) or "").strip() for key in (
        "period", "indicator_kind", "geography_level", "geography_code",
    ))  # type: ignore[return-value]


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
