"""Independent, preview-only OWNER_CAMPAIGN_EMAIL_V2 renderer.

The production sender is deliberately not imported or changed here.  This
module consumes the already-approved QA rows, the current portfolio document,
and the runtime evidence builder, then renders a new email composition with
the same CTA URLs supplied by QA.
"""

from __future__ import annotations

import math
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config


TEMPLATE_VERSION = "OWNER_CAMPAIGN_EMAIL_V2"
TEMPLATE_FILE = "owner_campaign_email_v2.html"
SINGLE_PROPERTY_HERO_TITLE = "Revisión comercial de tu propiedad"
SINGLE_PROPERTY_HERO_DESCRIPTION = (
    "Analizamos el comportamiento reciente del mercado, las alternativas comparables disponibles y la respuesta comercial de tu propiedad "
    "para evaluar su posicionamiento actual. El objetivo es identificar oportunidades que permitan fortalecer su competitividad, "
    "captar mayor interés y mejorar sus posibilidades de concretar una venta."
)
SINGLE_PROPERTY_MACRO_COPY = (
    "El financiamiento y la capacidad de decisión siguen influyendo. El IPoM de septiembre describe una demanda interna más débil y "
    "deterioro del mercado laboral y la confianza, factores que pueden extender las decisiones de los hogares.",
    "El subsidio a la tasa y FOGAES se limita a viviendas nuevas elegibles de hasta 6.000 UF; puede reducir cerca de un punto la tasa "
    "y permitir un pie de 10%. No se atribuye ese beneficio a esta propiedad usada: puede, en cambio, aumentar la competencia relativa "
    "de parte de la oferta nueva. Por eso conviene revisar su posición frente a alternativas y la respuesta comercial observada.",
)


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip().replace("\u00a0", " ").replace(" ", "")
        if not text:
            return None
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        result = float(text)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def fmt_number(value: Any, decimals: int = 1) -> str:
    parsed = number(value)
    if parsed is None:
        return "No disponible"
    if decimals == 0:
        return f"{parsed:,.0f}".replace(",", ".")
    return f"{parsed:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_uf(value: Any) -> str:
    parsed = number(value)
    return "No disponible" if parsed is None else f"{parsed:,.0f}".replace(",", ".") + " UF"


def fmt_unit(value: Any, unit: str) -> str:
    parsed = number(value)
    if parsed is None:
        return "No disponible"
    if unit == "CLP/m²":
        return "$ " + f"{parsed:,.0f}".replace(",", ".") + "/m²"
    return fmt_number(parsed, 1) + " " + unit


def _is_rent(operation: Any) -> bool:
    return "arriend" in str(operation or "").casefold()


def _price_label(value: Any, operation: Any) -> str:
    label = fmt_uf(value)
    return label + " / mes" if _is_rent(operation) and label != "No disponible" else label


def _indicator(primary: Any) -> tuple[str, str]:
    text = str(primary or "").casefold()
    if "clp" in text:
        return "CLP/m²", "CLP/m²"
    if "terreno" in text or "land" in text:
        return "UF/m² terreno", "UF/m² terreno"
    if "constru" in text or "built" in text:
        return "UF/m² construido", "UF/m² construido"
    if "útil" in text or "util" in text or "surface" in text:
        return "UF/m² útil", "UF/m² útil"
    return "UF/m²", "UF/m²"


def _position_label(percentile: Any) -> str:
    value = number(percentile)
    if value is None:
        return "en la referencia disponible"
    if value < 30:
        return "en la zona más competitiva"
    if value < 60:
        return "en la zona media"
    if value < 75:
        return "en la zona media-alta"
    return "en la zona superior"


def _graph(distribution: Mapping[str, Any], property_value: Any) -> dict[str, Any]:
    p10 = number(distribution.get("p10"))
    p90 = number(distribution.get("p90"))
    value = number(property_value)
    median = number(distribution.get("median"))
    if p10 is None or p90 is None or p90 <= p10:
        index = 2
        reference_index = 2
    else:
        ratio = lambda current: max(0.0, min(1.0, (current - p10) / (p90 - p10)))
        index = max(0, min(4, int(round(ratio(value) * 4)))) if value is not None else 2
        reference_index = max(0, min(4, int(round(ratio(median) * 4)))) if median is not None else 2
    return {
        "cells": [
            {"active": i <= index, "marker": i == index, "reference": i == reference_index}
            for i in range(5)
        ]
    }


def _positioning_graph(distribution: Mapping[str, Any], property_value: Any) -> dict[str, Any]:
    """Higher-resolution display grid for the single-email positioning band."""
    slots = 21
    p10 = number(distribution.get("p10"))
    p90 = number(distribution.get("p90"))
    value = number(property_value)
    median = number(distribution.get("median"))
    if p10 is None or p90 is None or p90 <= p10:
        index = slots // 2
        reference_index = slots // 2
    else:
        ratio = lambda current: max(0.0, min(1.0, (current - p10) / (p90 - p10)))
        index = max(0, min(slots - 1, int(round(ratio(value) * (slots - 1))))) if value is not None else slots // 2
        reference_index = max(0, min(slots - 1, int(round(ratio(median) * (slots - 1))))) if median is not None else slots // 2
    return {
        "cells": [
            {"active": i <= index, "marker": i == index, "reference": i == reference_index}
            for i in range(slots)
        ]
    }


def _portal_label(value: Any) -> str:
    text = str(value or "Publicación observada").strip()
    normalized = text.casefold().replace("_", " ")
    if normalized in {"procasa network", "procasa"}:
        return "PROCASA"
    return text.title()


def _initials(value: Any) -> str:
    words = [part for part in str(value or "PROCASA").split() if part]
    if not words:
        return "P"
    return "".join(word[0] for word in words[:2]).upper()


def _surface_label(item: Mapping[str, Any]) -> str:
    built = number(item.get("superficie_construida"))
    land = number(item.get("superficie_terreno"))
    if built is not None and built > 0:
        return f"{fmt_number(built, 0)} m² construidos"
    if land is not None and land > 0:
        return f"{fmt_number(land, 0)} m² terreno"
    return "Superficie no disponible"


def _rooms_label(item: Mapping[str, Any]) -> str:
    value = number(item.get("dormitorios"))
    return f"{fmt_number(value, 0)} dorm." if value is not None else ""


def _baths_label(item: Mapping[str, Any]) -> str:
    value = number(item.get("banos"))
    return f"{fmt_number(value, 0)} baño(s)" if value is not None else ""


def _surface_from_property(prop: Mapping[str, Any], primary_surface: str) -> float | None:
    value, _label = _surface_detail_from_property(prop, primary_surface)
    return value


def _surface_detail_from_property(prop: Mapping[str, Any], primary_surface: str) -> tuple[float | None, str]:
    characteristics = prop.get("caracteristicas") if isinstance(prop.get("caracteristicas"), Mapping) else {}
    candidates: list[Any]
    if primary_surface == "built_m2":
        candidates = [
            (prop.get("superficie_construida_m2"), "construidos"),
            (prop.get("superficie_construida"), "construidos"),
            (prop.get("m2_construidos"), "construidos"),
            (characteristics.get("superficie_construida"), "construidos"),
            (characteristics.get("casa_m2"), "construidos"),
            (characteristics.get("m2_construidos"), "construidos"),
        ]
    elif primary_surface in {"surface_ref_m2", "surface_util_m2"}:
        candidates = [
            (prop.get("superficie_util_m2"), "útiles"),
            (prop.get("superficie_util"), "útiles"),
            (prop.get("m2_utiles"), "útiles"),
            (characteristics.get("superficie_util"), "útiles"),
            (characteristics.get("superficie_util_m2"), "útiles"),
            (characteristics.get("m2_utiles"), "útiles"),
            (prop.get("superficie_construida_m2"), "construidos"),
            (prop.get("superficie_construida"), "construidos"),
            (prop.get("m2_construidos"), "construidos"),
            (characteristics.get("superficie_construida"), "construidos"),
            (characteristics.get("casa_m2"), "construidos"),
            (characteristics.get("m2_construidos"), "construidos"),
            (prop.get("superficie_total_m2"), "totales"),
            (prop.get("superficie_total"), "totales"),
            (characteristics.get("superficie_total_m2"), "totales"),
            (characteristics.get("superficie_total"), "totales"),
        ]
    else:
        candidates = [
            (prop.get("superficie_terreno_m2"), "de terreno"),
            (prop.get("superficie_terreno"), "de terreno"),
            (characteristics.get("superficie_terreno"), "de terreno"),
            (characteristics.get("superficie_terreno_m2"), "de terreno"),
        ]
    for candidate, label in candidates:
        parsed = number(candidate)
        if parsed is not None and parsed > 0:
            return parsed, label
    return None, ""


def _first_positive(prop: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    characteristics = prop.get("caracteristicas") if isinstance(prop.get("caracteristicas"), Mapping) else {}
    for source in (prop, characteristics):
        for key in keys:
            parsed = number(source.get(key))
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _property_feature_cards(prop: Mapping[str, Any]) -> list[dict[str, str]]:
    features: list[dict[str, str]] = []
    property_type = str(prop.get("tipo_propiedad") or "").casefold()
    preferred_surface = "built_m2" if any(token in property_type for token in ("casa", "parcela", "sitio")) else "surface_ref_m2"
    area, area_label = _surface_detail_from_property(prop, preferred_surface)
    if area is None:
        area, area_label = _surface_detail_from_property(prop, "land_m2")
    if area is not None:
        features.append({"kind": "area", "label": f"{fmt_number(area, 0)} m² {area_label}"})
    bedrooms = _first_positive(prop, ("dormitorios", "habitaciones", "n_dormitorios", "numero_dormitorios"))
    bathrooms = _first_positive(prop, ("banos", "baños", "n_banos", "numero_banos"))
    if bedrooms is not None:
        features.append({"kind": "bed", "label": f"{fmt_number(bedrooms, 0)} dorm."})
    if bathrooms is not None:
        features.append({"kind": "bath", "label": f"{fmt_number(bathrooms, 0)} baño(s)"})
    return features


def _percentile(values: list[float], value: float | None) -> float | None:
    if not values or value is None:
        return None
    return round(100.0 * sum(item <= value for item in values) / len(values), 1)


def _distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p10": None, "p25": None, "median": None, "p75": None, "p90": None}
    ordered = sorted(values)
    if len(ordered) == 1:
        return {"p10": ordered[0], "p25": ordered[0], "median": ordered[0], "p75": ordered[0], "p90": ordered[0]}
    def q(fraction: float) -> float:
        index = (len(ordered) - 1) * fraction
        low = int(index)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (index - low)
    return {"p10": q(.10), "p25": q(.25), "median": q(.50), "p75": q(.75), "p90": q(.90)}


def _v3_reference_item(item: Mapping[str, Any], unit: str, price_key: str, operation: Any = None) -> dict[str, Any]:
    price = item.get("price_uf")
    price_m2 = item.get(price_key)
    if number(price_m2) is None:
        price_m2 = item.get("price_m2")
    return {
        "listing_id": str(item.get("listing_id") or ""),
        "portal": _portal_label(item.get("portal")),
        "price_label": _price_label(price, operation),
        "unit_label": fmt_unit(price_m2, unit),
        "surface_label": _surface_label({"superficie_construida": item.get("built_m2"), "superficie_terreno": item.get("land_m2")}),
        "rooms_label": _rooms_label({"dormitorios": item.get("bedrooms")}),
        "baths_label": _baths_label({"banos": item.get("bathrooms")}),
        "distance_label": (fmt_number(item.get("distance"), 2) + " de distancia estructural") if number(item.get("distance")) is not None else "Referencia estructural",
    }


def _comparable_model(evidence: Mapping[str, Any], level: str, property_price_uf: Any = None, prop: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the public comparable block exclusively from persisted V3 evidence."""
    v3 = evidence.get("client_validation_v3") if isinstance(evidence.get("client_validation_v3"), Mapping) else None
    if v3 is None:
        v3 = evidence.get("v3") if isinstance(evidence.get("v3"), Mapping) else None
    if v3 is None:
        return {"visible": False}
    status = str(v3.get("comparable_display_status") or "HIDDEN").upper()
    effective_type = str(v3.get("effective_type") or "").upper()
    integral = [item for item in (v3.get("integral_comparables") or []) if isinstance(item, Mapping)]
    land = [item for item in (v3.get("land_references") or []) if isinstance(item, Mapping)]
    if not integral and effective_type == "PARCEL_LAND_ONLY":
        integral = land
        land = []
    if level == "INSUFFICIENT" or status in {"HIDDEN", "INSUFFICIENT"} or not integral:
        return {"visible": False}
    primary_surface = str(v3.get("primary_surface") or "built_m2")
    is_rent = _is_rent((prop or {}).get("operacion"))
    property_surface, surface_basis = _surface_detail_from_property(prop or {}, primary_surface)
    if primary_surface == "built_m2":
        price_key, unit = "price_m2_built", "UF/m²/mes" if is_rent else "UF/m² construido"
    elif primary_surface in {"surface_ref_m2", "surface_util_m2"}:
        price_key = "price_m2"
        unit = "UF/m²/mes" if is_rent else {
            "útiles": "UF/m² útil",
            "construidos": "UF/m² construido",
            "totales": "UF/m² total",
        }.get(surface_basis, "UF/m² útil")
    else:
        price_key, unit = "price_m2_land", "UF/m²/mes" if is_rent else "UF/m² terreno"
    if property_surface is None:
        # Never infer the subject property's area from a comparable. Without
        # the same valid metric on both sides there is no client-facing
        # positioning comparison to show.
        return {
            "visible": False,
            "hidden_reason": "PROPERTY_METRIC_MISSING",
            "effective_type": effective_type,
            "evidence_level": level,
        }
    values = [float(number(item.get(price_key) if number(item.get(price_key)) is not None else item.get("price_m2"))) for item in integral if number(item.get(price_key) if number(item.get(price_key)) is not None else item.get("price_m2")) is not None]
    distribution = _distribution(values)
    property_price = number(property_price_uf)
    property_value = property_price / property_surface if property_price and property_surface else None
    percentile = _percentile(values, property_value)
    position = _position_label(percentile)
    selected = min(len(integral), 20)
    if level == "LIMITED":
        scope = f"Muestra acotada de {selected} publicaciones; la lectura es orientativa y no permite una conclusión fuerte."
        interpretation = "Estas publicaciones orientan la conversación comercial, pero conviene revisarlas junto con tu asesor."
    else:
        scope = f"Referencia de {selected} publicaciones similares observadas actualmente."
        interpretation = f"Tu propiedad se ubica {position} frente a publicaciones similares. Son referencias de oferta y no precios de cierre."
    if effective_type == "PARCEL_WITH_IMPROVEMENTS" and level == "LIMITED":
        interpretation = "Las referencias disponibles muestran valores de suelo y algunas propiedades con construcciones similares, por lo que recomendamos revisar el posicionamiento junto al asesor."
    display_status = "REVIEW" if bool(v3.get("evidence_conflict")) or status == "REVIEW" else "OK"
    top3 = [_v3_reference_item(item, unit, price_key, (prop or {}).get("operacion")) for item in integral[:3]]
    land_top3 = [_v3_reference_item(item, "UF/m²/mes" if is_rent else "UF/m² terreno", "price_m2_land", (prop or {}).get("operacion")) for item in land[:3]]
    total_price_values = [float(number(item.get("price_uf"))) for item in integral if number(item.get("price_uf")) is not None and number(item.get("price_uf")) > 0]
    total_price_distribution = _distribution(total_price_values)
    if property_value is not None and distribution.get("median") is not None:
        positioning_mode = "PRICE_M2"
        positioning_property_value = property_value
        positioning_reference_value = distribution.get("median")
        positioning_unit_label = unit
        positioning_property_label = fmt_unit(property_value, unit)
        positioning_reference_label = fmt_unit(distribution.get("median"), unit)
        positioning_graph = _positioning_graph(distribution, property_value)
    elif property_price is not None and property_price > 0 and len(total_price_values) >= 5:
        positioning_mode = "TOTAL_PRICE"
        positioning_property_value = property_price
        positioning_reference_value = total_price_distribution.get("median")
        positioning_unit_label = "UF"
        positioning_property_label = _price_label(property_price, (prop or {}).get("operacion"))
        positioning_reference_label = _price_label(total_price_distribution.get("median"), (prop or {}).get("operacion"))
        positioning_graph = _positioning_graph(total_price_distribution, property_price)
    else:
        positioning_mode = "NONE"
        positioning_property_value = None
        positioning_reference_value = None
        positioning_unit_label = ""
        positioning_property_label = ""
        positioning_reference_label = ""
        positioning_graph = {"cells": []}
    return {
        "visible": True,
        "scope_label": scope,
        "badge_label": f"{selected} publicaciones similares analizadas",
        "display_status": display_status,
        "display_review_reasons": ["señales de evidencia que requieren revisión"] if display_status == "REVIEW" else [],
        "property_value_label": fmt_unit(property_value, unit),
        "median_label": fmt_unit(distribution.get("median"), unit),
        "positioning_mode": positioning_mode,
        "positioning_property_value": positioning_property_value,
        "positioning_reference_value": positioning_reference_value,
        "positioning_unit_label": positioning_unit_label,
        "positioning_property_label": positioning_property_label,
        "positioning_reference_label": positioning_reference_label,
        "positioning_graph": positioning_graph,
        "selected_n": selected,
        "universe_n": len(integral),
        "position_label": position,
        "percentile_label": (fmt_number(percentile, 0) + " percentil") if percentile is not None else "Percentil no disponible",
        "interpretation": interpretation,
        "top3": top3,
        "land_top3": land_top3,
        "land_reference_visible": bool(land_top3 and effective_type == "PARCEL_WITH_IMPROVEMENTS"),
        "land_reference_label": "Referencia de valor de suelo",
        "land_reference_note": "Estas publicaciones se muestran separadamente porque corresponden a terrenos sin una construcción comparable.",
        "graph": _graph(distribution, property_value),
        "effective_type": effective_type,
        "evidence_level": str(v3.get("evidence_level") or level),
    }


def _diagnostic_text(level: str, diagnostic: str, comparable: Mapping[str, Any]) -> str:
    if diagnostic == "REVIEW":
        return "Las distintas referencias muestran señales mixtas. Recomendamos revisar el posicionamiento con tu asesor antes de tomar una decisión."
    if not comparable.get("visible"):
        return "No contamos con una muestra suficiente de publicaciones comparables para entregar una lectura firme. Podemos revisar la estrategia contigo usando el resto de la información disponible."
    return "La evidencia disponible aporta una referencia para revisar el posicionamiento comercial con tu asesor."


def _recommendation_text(recommendation: str) -> str:
    if recommendation == "con ajuste de precio sustentado":
        return "La información disponible respalda revisar el precio publicado junto con tu asesor, considerando también las características particulares de la propiedad."
    if recommendation == "revisión con asesor":
        return "Las referencias disponibles muestran señales que conviene revisar con tu asesor antes de tomar una decisión."
    return "Sugerimos mantener la estrategia actual y seguir observando la respuesta del mercado junto con tu ejecutivo PROCASA."


def _recommendation_title(recommendation: str) -> str:
    if recommendation == "con ajuste de precio sustentado":
        return "Ajuste de precio sugerido"
    if recommendation == "revisión con asesor":
        return "Revisar el posicionamiento con tu asesor"
    return "Mantener la estrategia y observar el mercado"


def _appraisal_model(support: Mapping[str, Any], price: float | None, operation: Any = None) -> dict[str, Any]:
    appraisal = support.get("appraisal") if isinstance(support.get("appraisal"), Mapping) else None
    communal = support.get("communal_market") if isinstance(support.get("communal_market"), Mapping) else None
    if appraisal:
        low = number(appraisal.get("estimated_low_uf"))
        mid = number(appraisal.get("estimated_mid_uf"))
        high = number(appraisal.get("estimated_high_uf"))
        gap = ((price - mid) / mid * 100.0) if price is not None and mid and mid > 0 else None
        gap_amount = price - mid if price is not None and mid is not None else None
        position = str(appraisal.get("position_vs_appraisal") or "").upper()
        position_label = {
            "ABOVE_RANGE": "Sobre el rango",
            "WITHIN_RANGE": "Dentro del rango",
            "NEAR_RANGE": "Cercano al rango",
            "BELOW_RANGE": "Bajo el rango",
        }.get(position, "Referencia disponible")
        is_rent = _is_rent(operation or support.get("operation"))
        return {
            "visible": True,
            "kind": "INDIVIDUAL_APPRAISAL",
            "title": "Estimación de arriendo disponible" if is_rent else "Referencia de tasación disponible",
            "range_label": f"{_price_label(low, operation)} – {_price_label(high, operation)}" if low is not None and high is not None else "",
            "mid_label": _price_label(mid, operation) if mid is not None else "",
            "current_label": _price_label(price, operation),
            "gap_label": (f"{gap:+.1f}%".replace(".", ",") if gap is not None else "No disponible"),
            "gap_amount_label": (("+" if gap_amount > 0 else "−" if gap_amount < 0 else "") + _price_label(abs(gap_amount), operation)) if gap_amount is not None else "",
            "position_label": position_label,
            "copy": "La estimación constituye una referencia comercial y no garantiza un precio final de arriendo." if is_rent else "La tasación constituye una referencia comercial y no garantiza un precio final de venta.",
        }
    if communal:
        raw_metrics = communal.get("relevant_metrics") if isinstance(communal.get("relevant_metrics"), Mapping) else {}
        is_rent = _is_rent(operation or communal.get("operation"))
        metric_specs = (
            (("uf_m2_arriendo_actual", "UF/m²/mes", "unit"),
             ("publicaciones_arriendo_activas", "Arriendos activos", "count"),
             ("variacion_arriendo_12m", "Variación de arriendo (12 meses)", "percent"))
            if is_rent else
            (("uf_m2_publicacion_actual", "UF/m² de oferta", "unit"),
             ("uf_m2_venta_efectiva_actual", "UF/m² venta efectiva", "unit"),
             ("publicaciones_activas", "Publicaciones activas", "count"),
             ("liquidez", "Liquidez del segmento", "text"),
             ("nivel_competencia", "Competencia", "text"),
             ("brecha_publicacion_vs_cierre_pct", "Brecha oferta/cierre", "percent"))
        )
        metrics = []
        for key, label, value_kind in metric_specs:
            raw_value = raw_metrics.get(key)
            if raw_value in (None, ""):
                raw_value = communal.get(key)
            if raw_value in (None, ""):
                continue
            if value_kind == "unit":
                unit_label = " UF/m²/mes" if is_rent else " UF/m²"
                value = fmt_number(raw_value, 1) + unit_label if number(raw_value) is not None else str(raw_value).strip()
            elif value_kind == "count":
                value = fmt_number(raw_value, 0) if number(raw_value) is not None else str(raw_value).strip()
            elif value_kind == "percent":
                value = fmt_number(raw_value, 1) + "%" if number(raw_value) is not None else str(raw_value).strip()
            else:
                value = fmt_number(raw_value, 1) if number(raw_value) is not None else str(raw_value).strip()
            if value:
                metrics.append({"label": label, "value": value})
        if not metrics and not is_rent:
            for key, label, decimals in (("median_price_uf", "Mediana observada", 0), ("n_observations", "Publicaciones analizadas", 0), ("price_m2_median", "Mediana por m²", 1)):
                raw_value = communal.get(key)
                if raw_value not in (None, ""):
                    metrics.append({"label": label, "value": fmt_number(raw_value, decimals)})
        return {
            "visible": True,
            "kind": "COMMUNAL_MARKET_REPORT",
            "title": "Contexto de mercado comunal",
            "range_label": None,
            "mid_label": None,
            "current_label": None,
            "gap_label": None,
            "position_label": None,
            "metrics": metrics[:3],
            "copy": "Este contexto resume arriendos publicados observados y no representa valores de contratos cerrados." if is_rent else "Este contexto resume información agregada de publicaciones observadas y no representa precios de cierre.",
        }
    return {"visible": False}


def _market_reference_model(support: Mapping[str, Any], operation: Any = None) -> dict[str, Any]:
    communal = support.get("communal_market") if isinstance(support.get("communal_market"), Mapping) else {}
    metrics = communal.get("relevant_metrics") if isinstance(communal.get("relevant_metrics"), Mapping) else {}
    is_rental = _is_rent(operation or communal.get("operation"))
    reference_key = "uf_m2_arriendo_actual" if is_rental else "uf_m2_publicacion_actual"
    universe_keys = ("publicaciones_arriendo_activas", "n_observations") if is_rental else ("publicaciones_activas", "n_observations")
    reference = number(metrics.get(reference_key) or communal.get(reference_key))
    universe = next((number(metrics.get(key) or communal.get(key)) for key in universe_keys if number(metrics.get(key) or communal.get(key)) is not None), None)
    if reference is None or universe is None or reference <= 0 or universe < 5:
        return {"visible": False}
    source_date = communal.get("document_date")
    if source_date:
        try:
            parsed_date = source_date if isinstance(source_date, datetime) else datetime.fromisoformat(str(source_date).replace("Z", "+00:00"))
            month_names = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
            source_date = f"{parsed_date.day} de {month_names[parsed_date.month - 1]} de {parsed_date.year}"
        except (TypeError, ValueError):
            source_date = str(source_date)
    return {
        "visible": True,
        "reference_value": fmt_number(reference, 1),
        "reference_unit": "UF/m²/mes de oferta" if is_rental else "UF/m² de oferta",
        "universe_value": fmt_number(universe, 0),
        "universe_unit": "publicaciones activas",
        "source_date": str(source_date or ""),
    }


def _property_context(
    prop: Mapping[str, Any],
    qa_row: Mapping[str, Any],
    evidence: Mapping[str, Any],
    executive: Mapping[str, Any],
    property_image: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    level = str(qa_row.get("comparables_quality") or "INSUFFICIENT").upper()
    price = number(prop.get("precio_publicado_uf"))
    comparable = _comparable_model(evidence, level, price, prop)
    source_recommendation = str(qa_row.get("recommendation") or "mantener estrategia / sin ajuste")
    recommendation = source_recommendation
    recommended_price = number(prop.get("nuevo_precio_objetivo_uf")) if recommendation == "con ajuste de precio sustentado" else None
    document_type = str(qa_row.get("document_type") or "NONE")
    attachment = qa_row.get("attachment") if isinstance(qa_row.get("attachment"), Mapping) else {}
    document_visible = document_type != "NONE" and bool(attachment.get("status") == "ok")
    v3 = evidence.get("client_validation_v3") if isinstance(evidence.get("client_validation_v3"), Mapping) else {}
    support = evidence.get("supporting_evidence") if isinstance(evidence.get("supporting_evidence"), Mapping) else {}
    operation = str(qa_row.get("operacion") or prop.get("operacion") or "")
    is_rental = _is_rent(operation)
    appraisal = _appraisal_model(support, price, operation)
    market_reference = _market_reference_model(support, operation)
    review_guard = (
        bool(v3.get("evidence_conflict"))
        or str(v3.get("comparable_display_status") or "").upper() == "REVIEW"
        or comparable.get("hidden_reason") == "PROPERTY_METRIC_MISSING"
    )
    if recommendation == "con ajuste de precio sustentado" and (review_guard or (level in {"LIMITED", "INSUFFICIENT"} and document_type != "INDIVIDUAL_APPRAISAL")):
        recommendation = "revisión con asesor"
        recommended_price = None
    if appraisal.get("visible") and recommended_price is not None and recommended_price > 0 and price is not None and price > 0:
        appraisal["recommended_label"] = fmt_uf(recommended_price)
        appraisal["adjustment_label"] = f"{((recommended_price - price) / price * 100.0):+.1f}%".replace(".", ",")
    if document_type == "INDIVIDUAL_APPRAISAL":
        document_copy = "Tasación individual disponible como respaldo comercial. El PDF se mantiene como documento adjunto."
        single_document_copy = "Tu tasación individual se encuentra disponible como respaldo comercial y puede revisarse de forma privada desde este informe."
    elif document_type == "COMMUNAL_MARKET_REPORT":
        document_copy = "Informe de mercado comunal disponible como contexto. El PDF se mantiene como documento adjunto."
        single_document_copy = "El informe de mercado comunal está disponible como contexto y puede revisarse de forma privada desde este informe."
    else:
        document_copy = ""
        single_document_copy = "Puedes revisar las referencias disponibles junto con tu ejecutivo PROCASA."
    position = comparable.get("position_label") if comparable.get("visible") else "Sin referencia suficiente"
    summary_market = comparable.get("median_label") if comparable.get("visible") else "Sin referencia suficiente"
    summary_property = comparable.get("property_value_label") if comparable.get("visible") else None
    diagnostic_text = _diagnostic_text(level, str(qa_row.get("diagnostic") or "sin diagnóstico"), comparable)
    single_diagnostic_text = diagnostic_text
    if appraisal.get("visible") and appraisal.get("kind") == "INDIVIDUAL_APPRAISAL":
        diagnostic_parts = [f"El precio publicado es {fmt_uf(price)}; la tasación individual tiene una referencia central de {appraisal.get('mid_label') }."]
        if comparable.get("visible") and comparable.get("positioning_mode") == "TOTAL_PRICE":
            diagnostic_parts.append(
                f"La mediana de {comparable.get('selected_n')} publicaciones comparables es {comparable.get('positioning_reference_label')}."
            )
        elif comparable.get("visible"):
            diagnostic_parts.append(
                f"{comparable.get('selected_n')} publicaciones similares aportan contexto de oferta para revisar el posicionamiento."
            )
        single_diagnostic_text = " ".join(diagnostic_parts)
    segment_content = qa_row.get("segment_copy") if isinstance(qa_row.get("segment_copy"), Mapping) else {}
    if segment_content:
        recommendation = "con ajuste de precio sustentado" if segment_content.get("cta_type") == "PRICE_AUTHORIZATION" else "revisión con asesor"
        if recommendation != "con ajuste de precio sustentado":
            recommended_price = None
            appraisal.pop("recommended_label", None)
            appraisal.pop("adjustment_label", None)
        diagnostic_text = str(segment_content.get("body") or diagnostic_text)
        single_diagnostic_text = diagnostic_text
    cta = qa_row.get("cta") if isinstance(qa_row.get("cta"), Mapping) else {}
    primary_url = str(cta.get("primary_url") or "")
    primary_label = str(segment_content.get("cta") or cta.get("primary_label") or "Analizar estrategia con mi asesor")
    if recommendation == "revisión con asesor":
        if not segment_content:
            primary_url = str(cta.get("secondary_url") or primary_url)
        primary_label = "Revisar recomendación con mi asesor"
    elif recommendation == "con ajuste de precio sustentado":
        primary_label = str(segment_content.get("cta") or "ACEPTAR NUEVO VALOR")
    property_type = str(qa_row.get("tipo") or prop.get("tipo_propiedad") or "Propiedad")
    commune = str(qa_row.get("comuna") or prop.get("comuna") or "")
    executive_name = str(executive.get("name") or "PROCASA")
    return {
        "code": str(qa_row.get("codigo") or prop.get("codigo_propiedad") or ""),
        "property_type": property_type,
        "commune": commune,
        "property_built_m2": _surface_from_property(prop, "built_m2"),
        "property_built_m2_source": str(prop.get("superficie_construida_source") or "") or None,
        "property_heading": f"{property_type} · {commune}".upper().strip(" ·"),
        "operation_label": operation.title(),
        "operation_raw": operation,
        "is_rental": is_rental,
        "price_label": _price_label(price, operation),
        "feature_cards": _property_feature_cards(prop),
        "summary_metrics": [
            {"icon": "◉", "label": "Precio publicado", "value": _price_label(price, operation)},
            *([{"icon": "╱", "label": "Valor por m²", "value": summary_property},
               {"icon": "▥", "label": "Referencia comparable", "value": summary_market}]
              if comparable.get("visible") else []),
        ],
        "context_note": (
            "En un escenario donde podrían abrirse nuevas oportunidades de acceso a la vivienda, es clave que tu propiedad esté bien posicionada para aprovechar este mayor dinamismo."
            if not is_rental
            else "En el mercado de arriendo, una presentación clara ayuda a sostener la visibilidad de tu propiedad frente a alternativas similares."
        ),
        "image": dict(property_image or {"available": False, "url": "", "source": "NONE", "count": 0}),
        "comparable": comparable,
        "diagnostic_text": diagnostic_text,
        "single_diagnostic_text": single_diagnostic_text,
        "single_document_copy": single_document_copy,
        "single_recommendation_text": str(segment_content.get("body") or _recommendation_text(recommendation)),
        "recommendation_title": _recommendation_title(recommendation),
        "recommendation_text": str(segment_content.get("body") or _recommendation_text(recommendation)),
        "campaign_segment": str(qa_row.get("evidence_segment") or ""),
        "position_summary": str(segment_content.get("body") or ""),
        "recommended_price_label": _price_label(recommended_price, operation) if recommended_price is not None and recommended_price > 0 else None,
        "document": {"visible": document_visible, "copy": document_copy, "type": document_type},
        "appraisal": appraisal,
        "market_reference": market_reference,
        "cta": {
            "primary_url": primary_url,
            "primary_label": primary_label,
            "secondary_url": str(cta.get("secondary_url") or ""),
            "secondary_label": "Otra opción para esta propiedad",
        },
        "level": level,
        "source_recommendation": source_recommendation,
        "recommendation": recommendation,
        "diagnostic": str(qa_row.get("diagnostic") or "sin diagnóstico"),
        "executive": {**executive, "initials": _initials(executive_name)},
        "activity_90d": {"state": "UNKNOWN", "total_leads": None, "portals": [], "conversations": None, "visits": None},
    }


def _valuation_slots(property_model: Mapping[str, Any]) -> list[dict[str, str]]:
    """Four fixed KPI slots; their labels/values vary only with source availability."""
    appraisal = property_model.get("appraisal") if isinstance(property_model.get("appraisal"), Mapping) else {}
    comparable = property_model.get("comparable") if isinstance(property_model.get("comparable"), Mapping) else {}
    activity = property_model.get("activity_90d") if isinstance(property_model.get("activity_90d"), Mapping) else {}
    is_rental = bool(property_model.get("is_rental"))
    price_label = str(property_model.get("price_label") or "Revisar con asesor")
    if price_label == "No disponible":
        price_label = "Revisar con asesor"
    if appraisal.get("visible") and appraisal.get("kind") == "INDIVIDUAL_APPRAISAL":
        appraisal_value = appraisal.get("mid_label") or appraisal.get("range_label") or "Referencia disponible"
        slot2 = ("ESTIMACIÓN DE ARRIENDO", appraisal_value, "Estimación individual") if is_rental else ("TASACIÓN DE REFERENCIA", appraisal_value, "Tasación individual")
        difference = appraisal.get("gap_label")
        slot3 = ("DIFERENCIA", difference if difference and difference != "No disponible" else appraisal.get("position_label", "Referencia disponible"), appraisal.get("gap_amount_label") or "Frente a la referencia")
    elif appraisal.get("visible") and appraisal.get("kind") == "COMMUNAL_MARKET_REPORT":
        metrics = appraisal.get("metrics") or []
        slot2 = ("REFERENCIA DE MERCADO", metrics[0].get("value") if metrics else (comparable.get("positioning_reference_label") or "Publicaciones observadas"), metrics[0].get("label") if metrics else "Segmento comparable")
        slot3 = ("POSICIÓN VS. SEGMENTO", comparable.get("position_label") if comparable.get("visible") else "Referencia comunal", "Contexto de mercado")
    else:
        reference_value = comparable.get("positioning_reference_label") or comparable.get("median_label")
        if not reference_value or reference_value == "No disponible":
            reference_value = f"{comparable.get('selected_n')} publicaciones" if comparable.get("selected_n") else "Revisión con asesor"
        slot2 = ("MERCADO COMPARABLE", reference_value, "Publicaciones observadas")
        state = str(activity.get("state") or "UNKNOWN").upper()
        if state == "KNOWN_POSITIVE":
            slot3_value = f"{activity.get('total_leads', 0)} leads en 90 días"
        elif state == "KNOWN_ZERO":
            slot3_value = "0 leads registrados"
        else:
            slot3_value = "Revisión con asesor"
        slot3 = ("ACTIVIDAD COMERCIAL", slot3_value, "Últimos 90 días")
    if property_model.get("recommended_price_label"):
        slot4 = ("NUEVO VALOR", str(property_model["recommended_price_label"]), str(property_model.get("display_adjustment_label") or appraisal.get("adjustment_label") or "Revisar con asesor"), True)
    else:
        position = comparable.get("position_label") if comparable.get("visible") else "Revisión con asesor"
        slot4 = ("POSICIONAMIENTO", position, "Referencia comercial", False)
    return [
        {"label": "PRECIO PUBLICADO", "value": price_label, "note": "Valor vigente", "emphasis": False},
        {"label": slot2[0], "value": slot2[1], "note": slot2[2], "emphasis": False},
        {"label": slot3[0], "value": slot3[1], "note": slot3[2], "emphasis": False},
        {"label": slot4[0], "value": slot4[1], "note": slot4[2], "emphasis": slot4[3]},
    ]


def _single_property_valuation_slots(property_model: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Use the approved single-property KPI hierarchy and phrasing."""
    slots = _valuation_slots(property_model)
    if len(slots) < 4:
        return slots
    market = property_model.get("market_reference") if isinstance(property_model.get("market_reference"), Mapping) else {}
    comparable = property_model.get("comparable") if isinstance(property_model.get("comparable"), Mapping) else {}

    if market.get("visible"):
        unit = str(market.get("reference_unit") or "UF/m²").replace(" de oferta", "")
        reference_value = str(market.get("reference_value") or "Revisar con asesor")
        source_date = str(market.get("source_date") or "").strip()
        slot2 = {
            "label": "REFERENCIA DEL SEGMENTO",
            "value": f"{reference_value} {unit}" if reference_value != "Revisar con asesor" else reference_value,
            "note": f"Corte {source_date}" if source_date else "Referencia de mercado",
            "emphasis": False,
        }
    else:
        slot2 = slots[1]

    if comparable.get("visible"):
        def leading_number(value: Any) -> float | None:
            match = re.match(r"^\s*([+-]?[\d. ]+(?:,\d+)?)", str(value or ""))
            return number(match.group(1)) if match else None

        subject = leading_number(comparable.get("positioning_property_label"))
        reference = leading_number(comparable.get("positioning_reference_label"))
        if subject is not None and reference is not None:
            position = "Sobre la muestra comparable" if subject > reference else (
                "Bajo la muestra comparable" if subject < reference else "En la muestra comparable"
            )
        else:
            position = str(comparable.get("position_label") or "Revisar con asesor")
        slot3 = {
            "label": "POSICIONAMIENTO",
            "value": position,
            "note": "Frente a publicaciones similares",
            "emphasis": False,
        }
    else:
        slot3 = slots[2]

    slot4 = slots[3]
    return [slots[0], slot2, slot3, slot4]


def _portfolio_summary(property_model: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, presentation-only fields for the 3+ property email variant."""
    appraisal = property_model.get("appraisal") if isinstance(property_model.get("appraisal"), Mapping) else {}
    comparable = property_model.get("comparable") if isinstance(property_model.get("comparable"), Mapping) else {}
    activity = property_model.get("activity_90d") if isinstance(property_model.get("activity_90d"), Mapping) else {}
    document = property_model.get("document") if isinstance(property_model.get("document"), Mapping) else {}
    is_rental = bool(property_model.get("is_rental"))

    if appraisal.get("visible") and appraisal.get("kind") == "INDIVIDUAL_APPRAISAL":
        reference_label = "Estimación de arriendo" if is_rental else "Tasación de referencia"
        reference_value = appraisal.get("mid_label") or appraisal.get("range_label") or "Referencia individual disponible"
    elif appraisal.get("visible") and appraisal.get("kind") == "COMMUNAL_MARKET_REPORT" and appraisal.get("metrics"):
        metric = appraisal["metrics"][0]
        reference_label = str(metric.get("label") or "Referencia comunal")
        reference_value = str(metric.get("value") or "Revisar con asesor")
    elif comparable.get("visible"):
        reference_label = "Referencia de publicaciones"
        reference_value = str(comparable.get("positioning_reference_label") or comparable.get("median_label") or "Revisar con asesor")
    else:
        reference_label = "Referencia principal"
        reference_value = "Revisar con asesor"

    activity_state = str(activity.get("state") or "UNKNOWN").upper()
    if activity_state == "KNOWN_POSITIVE":
        leads = int(activity.get("total_leads") or 0)
        conversations = int(activity.get("conversations") or 0)
        visits = int(activity.get("visits") or 0)
        activity_text = (
            f"{leads} {'lead' if leads == 1 else 'leads'} · "
            f"{conversations} {'conversación' if conversations == 1 else 'conversaciones'} · "
            f"{visits} {'visita coordinada' if visits == 1 else 'visitas coordinadas'}"
        )
        origins = " · ".join(f"{item.get('name')} {item.get('count')}" for item in activity.get("portals") or [])
    elif activity_state == "KNOWN_ZERO":
        activity_text = "0 leads · 0 conversaciones · 0 visitas coordinadas"
        origins = ""
    else:
        activity_text = "Actividad 90 días no concluyente"
        origins = ""

    segment = str(property_model.get("campaign_segment") or "").upper()
    if segment == "COMPETITIVE_LOW_RESPONSE":
        positioning_text = "Precio competitivo; respuesta comercial reciente baja."
    elif segment == "INSUFFICIENT_EVIDENCE":
        positioning_text = "Evidencia insuficiente para una conclusión firme."
    elif str(property_model.get("diagnostic") or "").upper() == "REVIEW" or str(property_model.get("recommendation") or "").casefold() == "revisión con asesor":
        positioning_text = "Señales mixtas; revisar el posicionamiento con tu asesor."
    elif comparable.get("visible"):
        positioning_text = f"Posicionamiento {comparable.get('position_label') or 'en revisión'} frente a publicaciones similares."
    elif appraisal.get("visible"):
        positioning_text = "Referencia individual disponible para revisar el valor."
    else:
        positioning_text = "Revisar el posicionamiento con tu asesor."

    authorized_price = bool(
        property_model.get("recommended_price_label")
        and str(property_model.get("recommendation") or "").casefold() == "con ajuste de precio sustentado"
    )
    executive = property_model.get("executive") if isinstance(property_model.get("executive"), Mapping) else {}
    cta = property_model.get("cta") if isinstance(property_model.get("cta"), Mapping) else {}
    return {
        "reference_label": reference_label,
        "reference_value": str(reference_value),
        "activity_text": activity_text,
        "activity_origins": origins,
        "positioning_text": positioning_text,
        "authorized_price": authorized_price,
        "adjustment_label": str(property_model.get("display_adjustment_label") or appraisal.get("adjustment_label") or ""),
        "executive_name": str(executive.get("name") or ""),
        "report_url": str(cta.get("secondary_url") or "") if document.get("visible") else "",
    }


def render_owner_campaign_email_v2(properties: list[Mapping[str, Any]], *, email: str, executives: list[Mapping[str, Any]], base_url: str | None = None) -> str:
    """Render V2 for one or several properties without changing sender state."""
    environment = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = environment.get_template(TEMPLATE_FILE)
    property_models = []
    single_property_only = len(properties) == 1 and not bool(properties[0].get("is_rental"))
    for original in properties:
        model = dict(original)
        activity = model.get("activity_90d") if isinstance(model.get("activity_90d"), Mapping) else {"state": "UNKNOWN"}
        model["activity_90d"] = dict(activity)
        valuation_slots = _valuation_slots(model)
        model["valuation_slots"] = valuation_slots
        model["single_valuation_slots"] = [
            {**slot, "icon": icon}
            for slot, icon in zip(
                _single_property_valuation_slots(model) if single_property_only else valuation_slots,
                ("price", "market", "position", "new-value"),
            )
        ]
        model["portfolio_summary"] = _portfolio_summary(model)
        property_models.append(model)
    executive_models = []
    for executive in executives:
        model = dict(executive)
        model.setdefault("initials", _initials(model.get("name")))
        executive_models.append(model)
    all_rent = bool(property_models) and all(bool(item.get("is_rental")) for item in property_models)
    single_property_only = len(property_models) == 1 and not bool(property_models[0].get("is_rental"))
    mixed_operations = bool({bool(item.get("is_rental")) for item in property_models}) and len({bool(item.get("is_rental")) for item in property_models}) > 1
    if all_rent:
        hero_title = "Estamos preparando tu propiedad para un nuevo escenario de arriendo"
        hero_description = "Revisamos el mercado y las alternativas disponibles para ayudar a sostener un posicionamiento competitivo y captar nuevas oportunidades de arriendo."
        context_note = "El análisis utiliza referencias de arriendo y valores mensuales para esta propiedad."
        footer_disclaimer = "Las referencias de arriendo corresponden a publicaciones observadas y no garantizan un valor final de contrato."
    elif mixed_operations:
        hero_title = "Revisamos cada propiedad según su propio escenario de mercado"
        hero_description = "Cada análisis conserva su operación, evidencia y recomendación específica."
        context_note = "CONTEXTO DE MERCADO · Cada propiedad se evalúa por separado según su operación y segmento."
        footer_disclaimer = "Las referencias corresponden a publicaciones observadas y no garantizan un precio o valor final de contrato."
    elif single_property_only:
        hero_title = SINGLE_PROPERTY_HERO_TITLE
        hero_description = SINGLE_PROPERTY_HERO_DESCRIPTION
        context_note = ""
        footer_disclaimer = "Las referencias corresponden a publicaciones observadas y no garantizan un precio final de venta."
    else:
        hero_title = "Estamos preparando tu propiedad para un nuevo escenario de compradores"
        hero_description = "Observamos el mercado y las alternativas disponibles para ayudarte a mantener un posicionamiento competitivo y capturar nuevas oportunidades de demanda."
        context_note = str(property_models[0].get("context_note") or "") if property_models else ""
        footer_disclaimer = "Las referencias corresponden a publicaciones observadas y no garantizan un precio final de venta."
    now_chile = datetime.now(ZoneInfo("America/Santiago"))
    spanish_months = (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    )
    macro_context = {
        "copy_paragraphs": SINGLE_PROPERTY_MACRO_COPY if single_property_only else (),
        "source_line": "Fuentes: Banco Central de Chile y MINVU · septiembre 2026" if single_property_only else "",
    }
    report_date = f"{now_chile.day} de {spanish_months[now_chile.month - 1]} de {now_chile.year}"
    return template.render(
        template_version=TEMPLATE_VERSION,
        email=email,
        logo_url=(base_url or Config.CRM_BASE_URL).rstrip("/") + "/static/logo.png",
        properties=property_models,
        executives=executive_models,
        single_property_only=single_property_only,
        macro_context=macro_context,
        report_date=report_date,
        hero_title=hero_title,
        hero_description=hero_description,
        context_note=context_note,
        footer_disclaimer=footer_disclaimer,
    )
