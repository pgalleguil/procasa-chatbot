"""Validated, reusable Yapo search configuration and URL construction.

The category paths and filter keys below were recovered from saved Yapo HTML
and previously observed Yapo search URLs. Runtime searches still verify the
canonical URL returned by Yapo before treating a page as in-scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

try:
    from comuna_utils import normalize_commune_slug
except ImportError:  # direct module execution
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from comuna_utils import normalize_commune_slug


YAPO_BASE_URL = "https://www.yapo.cl"

# Each path is a category observed in saved Yapo HTML. `parcela` and `terreno`
# share Yapo's actual "lotes y terrenos" category; the detail is checked again.
PROPERTY_CATEGORIES: dict[str, dict[str, str]] = {
    "casa": {
        "venta": "bienes-raices-venta-de-propiedades-casas",
        "arriendo": "bienes-raices-alquiler-casas",
    },
    "departamento": {
        "venta": "bienes-raices-venta-de-propiedades-apartamentos",
        "arriendo": "bienes-raices-alquiler-apartamentos",
    },
    "terreno": {
        "venta": "bienes-raices-venta-de-propiedades-lotes-y-terrenos",
        "arriendo": "bienes-raices-alquiler-lotes-y-terrenos",
    },
    "parcela": {
        "venta": "bienes-raices-venta-de-propiedades-lotes-y-terrenos",
        "arriendo": "bienes-raices-alquiler-lotes-y-terrenos",
    },
    "local_comercial": {
        "venta": "bienes-raices-venta-de-propiedades-comercios",
        "arriendo": "bienes-raices-alquiler-comercios",
    },
    "oficina": {
        "venta": "bienes-raices-venta-de-propiedades-oficinas",
        "arriendo": "bienes-raices-alquiler-alquiler-de-oficinas",
    },
    "edificio": {
        "venta": "bienes-raices-venta-de-propiedades-edificios",
    },
    "negocio": {
        "venta": "bienes-raices-venta-de-propiedades-negocios",
        "arriendo": "bienes-raices-alquiler-negocios",
    },
    "estacionamiento": {
        "venta": "bienes-raices-venta-de-propiedades-estacionamientos-sepultura-otros",
        "arriendo": "bienes-raices-alquiler-estacionamientos-sepultura-otros",
    },
}

PROPERTY_TYPE_ALIASES = {
    "casa": "casa", "casas": "casa",
    "departamento": "departamento", "departamentos": "departamento",
    "depto": "departamento", "dpto": "departamento", "apartamento": "departamento",
    "apartamentos": "departamento",
    "terreno": "terreno", "terrenos": "terreno", "lote": "terreno", "lotes": "terreno",
    "sitio": "terreno", "sitios": "terreno",
    "parcela": "parcela", "parcelas": "parcela",
    "local-comercial": "local_comercial", "locales-comerciales": "local_comercial",
    "comercio": "local_comercial", "comercios": "local_comercial",
    "oficina": "oficina", "oficinas": "oficina",
    "edificio": "edificio", "edificios": "edificio",
    "negocio": "negocio", "negocios": "negocio",
    "estacionamiento": "estacionamiento", "estacionamientos": "estacionamiento",
}

OPERATION_ALIASES = {
    "venta": "venta", "vender": "venta",
    "arriendo": "arriendo", "arriendo-mensual": "arriendo",
    "alquiler": "arriendo", "arrendar": "arriendo",
}

CURRENCY_CODES = {"UF": "CLF", "CLF": "CLF", "CLP": "CLP"}

# Evidence references are kept in the run report so users can distinguish
# saved-HTML evidence from a filter that has passed a live canonical-page check.
FILTER_EVIDENCE = {
    "category_and_commune_route": {
        "status": "observed_in_saved_html",
        "evidence": "Yapo canonical links and detail paths in the saved Maipu audit HTML",
        "runtime_check": "canonical URL checked on every downloaded search page",
    },
    "operation_sale": {
        "status": "observed_in_saved_html",
        "evidence": "bienes-raices-venta-de-propiedades-* category paths",
    },
    "operation_rent": {
        "status": "observed_in_saved_html",
        "evidence": "bienes-raices-alquiler-* category paths",
    },
    "price_currency_and_range": {
        "status": "observed_in_saved_html",
        "evidence": "canonical q=f_currency.CLF|f_price.3000-6000 and legacy f_price.<min>- URLs",
        "runtime_check": "canonical URL checked on every downloaded search page",
    },
    "bedrooms": {
        "status": "one_bounded_range_observed_in_saved_html",
        "evidence": "observed q=f_rooms.5-5 and filter-room[data-field=f_rooms]",
        "runtime_check": "other bounds require canonical URL confirmation",
    },
    "bathrooms": {
        "status": "unverified_query_encoding",
        "evidence": "filter-bath[data-field=f_bath] only; no saved canonical query token",
    },
    "surface_m2": {
        "status": "unverified_query_encoding",
        "evidence": "filter-area[data-field=f_square] only; no saved canonical query token",
    },
    "all_communes_in_region": {
        "status": "unsupported",
        "evidence": "no verified complete Yapo commune index for a region-wide query",
    },
}


class SearchConfigurationError(ValueError):
    """Raised when a requested scope cannot be represented safely in Yapo."""


def _number(value: Any, name: str, *, integer: bool = False) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise SearchConfigurationError(f"{name} debe ser numérico.") from None
    if not result.is_finite() or result < 0:
        raise SearchConfigurationError(f"{name} debe ser un número finito no negativo.")
    if integer and result != result.to_integral_value():
        raise SearchConfigurationError(f"{name} debe ser entero.")
    return result


def _fmt(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def normalize_region_slug(value: str) -> str:
    slug = normalize_commune_slug(value) or ""
    aliases = {
        "rm": "region-metropolitana",
        "metropolitana": "region-metropolitana",
        "region-metropolitana-de-santiago": "region-metropolitana",
        "region-de-metropolitana": "region-metropolitana",
        "region-de-tarapaca": "region-tarapaca",
        "region-de-antofagasta": "region-antofagasta",
        "region-de-atacama": "region-atacama",
        "region-de-coquimbo": "region-coquimbo",
        "region-de-valparaiso": "region-valparaiso",
        "region-del-libertador-general-bernardo-ohiggins": "region-ohiggins",
        "region-del-maule": "region-maule",
        "region-de-nuble": "region-nuble",
        "region-del-biobio": "region-biobio",
        "region-de-la-araucania": "region-araucania",
        "region-de-los-rios": "region-los-rios",
        "region-de-los-lagos": "region-los-lagos",
        "region-de-aysen": "region-aysen",
        "region-de-magallanes": "region-magallanes",
    }
    slug = aliases.get(slug, slug)
    if not slug:
        raise SearchConfigurationError("Indica una región válida.")
    if not slug.startswith("region-"):
        slug = f"region-{slug}"
    slug = aliases.get(slug, slug)
    return slug


def _commune_values(values: tuple[str, ...] | list[str] | str) -> tuple[str, ...]:
    raw_values = [values] if isinstance(values, str) else list(values)
    expanded: list[str] = []
    for raw in raw_values:
        expanded.extend(part.strip() for part in str(raw).split(",") if part.strip())
    normalized: list[str] = []
    seen: set[str] = set()
    for value in expanded:
        slug = normalize_commune_slug(value)
        if not slug:
            raise SearchConfigurationError("Cada comuna debe tener un nombre válido.")
        if slug not in seen:
            seen.add(slug)
            normalized.append(slug)
    if not normalized:
        raise SearchConfigurationError(
            "Debes indicar al menos una comuna. La búsqueda de todas las comunas de una región aún no está habilitada."
        )
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class SearchConfig:
    region: str
    communes: tuple[str, ...]
    operation: str
    property_type: str
    price_min: Decimal | int | float | str | None = None
    price_max: Decimal | int | float | str | None = None
    price_currency: str | None = None
    bedrooms_min: Decimal | int | str | None = None
    bedrooms_max: Decimal | int | str | None = None
    bathrooms_min: Decimal | int | str | None = None
    bathrooms_max: Decimal | int | str | None = None
    surface_min_m2: Decimal | int | str | None = None
    surface_max_m2: Decimal | int | str | None = None
    max_pages: int | None = None
    max_listings: int | None = None
    output_dir: str = ""
    batch_id: str = ""
    live: bool = False
    region_slug: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        region_slug = normalize_region_slug(self.region)
        communes = _commune_values(self.communes)
        operation_key = normalize_commune_slug(self.operation) or ""
        operation = OPERATION_ALIASES.get(operation_key)
        if not operation:
            raise SearchConfigurationError("Operación no soportada: usa venta o arriendo.")
        property_key = normalize_commune_slug(self.property_type) or ""
        property_type = PROPERTY_TYPE_ALIASES.get(property_key)
        if not property_type:
            supported = ", ".join(sorted(PROPERTY_CATEGORIES))
            raise SearchConfigurationError(f"Tipo de propiedad no verificado. Soportados: {supported}.")
        if operation not in PROPERTY_CATEGORIES[property_type]:
            raise SearchConfigurationError(
                f"Yapo no tiene una categoría guardada/verificada para {property_type} en {operation}."
            )

        price_min = _number(self.price_min, "precio mínimo")
        price_max = _number(self.price_max, "precio máximo")
        if price_min is not None and price_max is not None and price_min > price_max:
            raise SearchConfigurationError("El precio mínimo no puede superar al máximo.")
        currency = str(self.price_currency or "").strip().upper()
        if currency and currency not in CURRENCY_CODES:
            raise SearchConfigurationError("Moneda no soportada: usa UF o CLP.")
        if (price_min is not None or price_max is not None) and not currency:
            raise SearchConfigurationError("Indica --moneda UF o CLP cuando defines un rango de precios.")

        # Price max-only syntax has not been found in saved Yapo URLs. Fail
        # closed until a live proxy probe proves the portal applies it.
        if price_min is None and price_max is not None:
            raise SearchConfigurationError(
                "El filtro de precio solo con máximo no está verificado por Yapo; no se construye una búsqueda potencialmente más amplia."
            )

        bedrooms_min = _number(self.bedrooms_min, "dormitorios mínimos", integer=True)
        bedrooms_max = _number(self.bedrooms_max, "dormitorios máximos", integer=True)
        bathrooms_min = _number(self.bathrooms_min, "baños mínimos", integer=True)
        bathrooms_max = _number(self.bathrooms_max, "baños máximos", integer=True)
        surface_min = _number(self.surface_min_m2, "superficie mínima")
        surface_max = _number(self.surface_max_m2, "superficie máxima")
        if bathrooms_min is not None or bathrooms_max is not None:
            raise SearchConfigurationError(
                "El filtro de baños tiene un campo de interfaz observado, pero no una codificación de URL verificada; se bloquea hasta confirmarla."
            )
        if surface_min is not None or surface_max is not None:
            raise SearchConfigurationError(
                "El filtro de superficie tiene un campo de interfaz observado, pero no una codificación de URL verificada; se bloquea hasta confirmarla."
            )
        for label, lower, upper in (
            ("dormitorios", bedrooms_min, bedrooms_max),
            ("baños", bathrooms_min, bathrooms_max),
            ("superficie", surface_min, surface_max),
        ):
            if lower is not None and upper is not None and lower > upper:
                raise SearchConfigurationError(f"El mínimo de {label} no puede superar al máximo.")
            if lower is None and upper is not None:
                raise SearchConfigurationError(f"El máximo de {label} sin mínimo no está verificado en las URLs guardadas.")

        if self.max_pages is not None and self.max_pages < 1:
            raise SearchConfigurationError("--max-pages debe ser al menos 1.")
        if self.max_listings is not None and self.max_listings < 1:
            raise SearchConfigurationError("--max-listings debe ser al menos 1.")
        batch_id = str(self.batch_id or "").strip()
        if batch_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", batch_id):
            raise SearchConfigurationError("El identificador del lote solo admite letras, números, guion y guion bajo.")

        object.__setattr__(self, "region_slug", region_slug)
        object.__setattr__(self, "communes", communes)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "property_type", property_type)
        object.__setattr__(self, "price_min", price_min)
        object.__setattr__(self, "price_max", price_max)
        object.__setattr__(self, "price_currency", "UF" if currency in {"UF", "CLF"} else currency)
        object.__setattr__(self, "bedrooms_min", bedrooms_min)
        object.__setattr__(self, "bedrooms_max", bedrooms_max)
        object.__setattr__(self, "bathrooms_min", bathrooms_min)
        object.__setattr__(self, "bathrooms_max", bathrooms_max)
        object.__setattr__(self, "surface_min_m2", surface_min)
        object.__setattr__(self, "surface_max_m2", surface_max)

    @property
    def category_slug(self) -> str:
        return PROPERTY_CATEGORIES[self.property_type][self.operation]

    def filter_tokens(self) -> tuple[str, ...]:
        tokens: list[str] = []
        if self.price_currency:
            tokens.append(f"f_currency.{CURRENCY_CODES[self.price_currency]}")
        if self.price_min is not None or self.price_max is not None:
            tokens.append(f"f_price.{_fmt(self.price_min)}-{_fmt(self.price_max)}")
        for key, lower, upper in (
            ("f_rooms", self.bedrooms_min, self.bedrooms_max),
            ("f_bath", self.bathrooms_min, self.bathrooms_max),
            ("f_square", self.surface_min_m2, self.surface_max_m2),
        ):
            if lower is not None or upper is not None:
                tokens.append(f"{key}.{_fmt(lower)}-{_fmt(upper)}")
        return tuple(tokens)

    def start_url(self, commune_slug: str) -> str:
        if commune_slug not in self.communes:
            raise SearchConfigurationError(f"La comuna {commune_slug} no pertenece a esta configuración.")
        location = f"{self.region_slug}-{commune_slug}"
        path = f"/{self.category_slug}/{location}"
        tokens = self.filter_tokens()
        return f"{YAPO_BASE_URL}{path}" + ("?" + urlencode({"q": "|".join(tokens)}) if tokens else "")

    def start_urls(self) -> list[tuple[str, str]]:
        return [(self.start_url(commune), commune) for commune in self.communes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_input": self.region,
            "region_slug": self.region_slug,
            "communes": list(self.communes),
            "operation": self.operation,
            "property_type": self.property_type,
            "category_slug": self.category_slug,
            "price_min": _fmt(self.price_min),
            "price_max": _fmt(self.price_max),
            "price_currency": self.price_currency or None,
            "bedrooms_min": _fmt(self.bedrooms_min),
            "bedrooms_max": _fmt(self.bedrooms_max),
            "bathrooms_min": _fmt(self.bathrooms_min),
            "bathrooms_max": _fmt(self.bathrooms_max),
            "surface_min_m2": _fmt(self.surface_min_m2),
            "surface_max_m2": _fmt(self.surface_max_m2),
            "filter_tokens": list(self.filter_tokens()),
            "max_pages": self.max_pages,
            "max_listings": self.max_listings,
            "output_dir": str(Path(self.output_dir).expanduser()) if self.output_dir else "",
            "batch_id": self.batch_id,
            "mode": "live_read_only" if self.live else "offline_simulation",
        }


def inspect_search_url(url: str) -> dict[str, Any]:
    """Extract route/query evidence without declaring the search verified."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    q_tokens = [token for value in query.get("q", []) for token in value.split("|") if token]
    return {
        "host": parsed.hostname or "",
        "path_parts": path_parts,
        "query_tokens": q_tokens,
        "canonical_url": url,
    }


def configured_search_path(search: SearchConfig, commune_slug: str) -> str:
    return f"/{search.category_slug}/{search.region_slug}-{commune_slug}".rstrip("/")


def search_path_matches_config(path: str, search: SearchConfig, commune_slug: str) -> bool:
    """Accept the configured route or Yapo's observed `category.N` pager route."""
    expected = configured_search_path(search, commune_slug)
    normalized = (path or "").rstrip("/")
    if normalized == expected:
        return True
    expected_parts = expected.strip("/").split("/")
    actual_parts = normalized.strip("/").split("/")
    if len(expected_parts) != 2 or len(actual_parts) != 2:
        return False
    category_match = re.fullmatch(re.escape(expected_parts[0]) + r"\.(\d+)", actual_parts[0])
    if not category_match or int(category_match.group(1)) < 2:
        return False
    return actual_parts[1] == expected_parts[1]


def verify_search_page(url: str, html: str, search: SearchConfig, commune_slug: str) -> dict[str, Any]:
    """Require Yapo's canonical page to retain the exact requested route/filters."""
    from bs4 import BeautifulSoup

    requested = urlparse(url)
    requested_path = configured_search_path(search, commune_slug)
    if requested.hostname not in {"www.yapo.cl", "yapo.cl"}:
        return {"verified": False, "reason": "unauthorized_yapo_domain", "canonical_url": ""}
    if not search_path_matches_config(requested.path, search, commune_slug):
        return {"verified": False, "reason": "requested_route_does_not_match_config", "canonical_url": ""}
    expected_tokens = set(search.filter_tokens())
    filter_prefixes = {"f_currency", "f_price", "f_rooms", "f_bath", "f_square"}
    requested_tokens = set(inspect_search_url(url)["query_tokens"])
    requested_filter_tokens = {
        token for token in requested_tokens
        if token.split(".", 1)[0] in filter_prefixes
    }
    if not expected_tokens.issubset(requested_filter_tokens):
        return {"verified": False, "reason": "requested_query_missing_configured_filters", "canonical_url": ""}
    requested_conflicts = sorted(requested_filter_tokens - expected_tokens)
    if requested_conflicts:
        return {
            "verified": False, "reason": "requested_query_conflicting_filters",
            "canonical_url": "", "conflicting_filter_tokens": requested_conflicts,
        }

    soup = BeautifulSoup(html or "", "html.parser")
    canonical_node = soup.find("link", attrs={"rel": "canonical"})
    canonical = str(canonical_node.get("href") or "").strip() if canonical_node else ""
    if not canonical:
        og_node = soup.find("meta", attrs={"property": "og:url"})
        canonical = str(og_node.get("content") or "").strip() if og_node else ""
    if not canonical:
        return {
            "verified": False, "reason": "search_canonical_missing",
            "canonical_url": "", "expected_filter_tokens": list(search.filter_tokens()),
        }
    canonical_parsed = urlparse(canonical)
    if canonical_parsed.hostname not in {"www.yapo.cl", "yapo.cl"}:
        return {"verified": False, "reason": "canonical_domain_mismatch", "canonical_url": canonical}
    if canonical_parsed.path.rstrip("/") != requested_path:
        return {
            "verified": False, "reason": "canonical_route_mismatch",
            "canonical_url": canonical, "expected_path": requested_path,
        }
    canonical_tokens = set(inspect_search_url(canonical)["query_tokens"])
    actual_tokens = {
        token for token in canonical_tokens
        if token.split(".", 1)[0] in filter_prefixes
    }
    if not expected_tokens.issubset(actual_tokens):
        return {
            "verified": False, "reason": "canonical_filters_not_retained",
            "canonical_url": canonical, "expected_filter_tokens": sorted(expected_tokens),
            "actual_filter_tokens": sorted(actual_tokens),
        }
    unexpected_conflicts = sorted(actual_tokens - expected_tokens)
    if unexpected_conflicts:
        return {
            "verified": False, "reason": "canonical_conflicting_filters",
            "canonical_url": canonical, "conflicting_filter_tokens": unexpected_conflicts,
        }
    return {
        "verified": True, "reason": "canonical_route_and_filters_match",
        "canonical_url": canonical,
        "expected_filter_tokens": sorted(expected_tokens),
        "actual_filter_tokens": sorted(actual_tokens),
        "evidence": "canonical Yapo URL returned in the fetched search HTML",
    }


_KNOWN_REGION_PREFIXES = tuple(sorted({
    "region-arica-y-parinacota", "region-metropolitana", "region-valparaiso",
    "region-tarapaca", "region-antofagasta", "region-atacama", "region-coquimbo",
    "region-ohiggins", "region-maule", "region-nuble", "region-biobio",
    "region-araucania", "region-los-rios", "region-los-lagos", "region-aysen",
    "region-magallanes",
}, key=len, reverse=True))


def search_config_from_url(url: str, *, output_dir: str = "", batch_id: str = "",
                           max_pages: int | None = None, max_listings: int | None = None,
                           live: bool = False) -> SearchConfig:
    """Compatibility adapter for the existing owner-hunt `pilot --start-url` command."""
    parsed = urlparse(url)
    if parsed.hostname not in {"www.yapo.cl", "yapo.cl"}:
        raise SearchConfigurationError("La URL inicial debe pertenecer a Yapo.cl.")
    query = parse_qs(parsed.query)
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    category_slug = ""
    location_slug = ""
    if path_parts and path_parts[0] != "searchresult":
        category_slug = path_parts[0]
        location_slug = path_parts[1] if len(path_parts) > 1 else ""
    else:
        region_values = query.get("regionslug", [])
        location_slug = str(region_values[0] if region_values else "")

    if not category_slug:
        for value in query.get("q", []):
            for token in value.split("|"):
                token = token.removeprefix("withcat.")
                if token in {slug for operations in PROPERTY_CATEGORIES.values() for slug in operations.values()}:
                    category_slug = token
                    break
            if category_slug:
                break
    reverse: dict[str, tuple[str, str]] = {}
    for property_type, operations in PROPERTY_CATEGORIES.items():
        for operation, slug in operations.items():
            reverse.setdefault(slug, (property_type, operation))
    if category_slug not in reverse:
        raise SearchConfigurationError("La URL no contiene una categoría Yapo reconocida por el catálogo guardado.")
    property_type, operation = reverse[category_slug]

    region_slug = ""
    commune_slug = ""
    for candidate in _KNOWN_REGION_PREFIXES:
        if location_slug.startswith(candidate + "-"):
            region_slug = candidate
            commune_slug = location_slug[len(candidate) + 1:]
            break
    if not region_slug or not commune_slug:
        raise SearchConfigurationError(
            "No se pudo separar región y comuna de la URL; usa los parámetros --region y --comuna del motor general."
        )

    tokens = [token for value in query.get("q", []) for token in value.split("|") if token]
    currency = ""
    price_min = price_max = None
    bedrooms_min = bedrooms_max = None
    bathrooms_min = bathrooms_max = None
    surface_min = surface_max = None
    for token in tokens:
        if token.startswith("withcat."):
            continue
        if token.startswith("f_currency."):
            code = token.split(".", 1)[1]
            currency = {"CLF": "UF", "CLP": "CLP"}.get(code, "")
            if not currency:
                raise SearchConfigurationError(f"Moneda Yapo no reconocida: {code}.")
            continue
        match = re.fullmatch(r"(f_price|f_rooms|f_bath|f_square)\.([0-9.]*)-([0-9.]*)", token)
        if not match:
            raise SearchConfigurationError(f"Filtro Yapo de URL no reconocido: {token}.")
        key, lower, upper = match.groups()
        values: tuple[str | None, str | None] = (lower or None, upper or None)
        if key == "f_price":
            price_min, price_max = values
        elif key == "f_rooms":
            bedrooms_min, bedrooms_max = values
        elif key == "f_bath":
            bathrooms_min, bathrooms_max = values
        else:
            surface_min, surface_max = values
    return SearchConfig(
        region=region_slug, communes=(commune_slug,), operation=operation,
        property_type=property_type, price_min=price_min, price_max=price_max,
        price_currency=currency or None, bedrooms_min=bedrooms_min, bedrooms_max=bedrooms_max,
        bathrooms_min=bathrooms_min, bathrooms_max=bathrooms_max,
        surface_min_m2=surface_min, surface_max_m2=surface_max,
        max_pages=max_pages, max_listings=max_listings, output_dir=output_dir,
        batch_id=batch_id, live=live,
    )
