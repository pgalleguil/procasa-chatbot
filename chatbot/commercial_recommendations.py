"""Motor read-only de recomendaciones comerciales: RAG híbrido sin K-Means.

Reutiliza el vector_descripcion precomputado del chatbot (384 dimensiones),
coincidencia léxica y filtros estructurados. No realiza escrituras ni envíos.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Mapping, Sequence

from .geo_utils import get_neighboring_communes

ENGINE_VERSION = "commercial_hybrid_rag_v1"
DEFAULT_OFFICE = "PROCASA SUCRE"
VECTOR_DIM = 384
STOPWORDS = frozenset(
    "a al de del el en es la las lo los para por que se su sus un una uno y "
    "venta arriendo vende arrienda oportunidad propiedad procasa casa "
    "departamento excelente ubicado ubicada codigo".split()
)


def norm(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(c for c in value if not unicodedata.combining(c)).strip()


def nested(doc: Mapping, *parts: str) -> Any:
    for part in parts:
        if not isinstance(doc, Mapping):
            return None
        doc = doc.get(part)
    return doc


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError):
        return None


def integer(value: Any) -> int | None:
    result = number(value)
    return int(result) if result is not None and result == int(result) else None


def operation(doc: Mapping) -> str | None:
    ops = doc.get("tipo_operacion") or {}
    if ops.get("venta") and not ops.get("arriendo"):
        return "venta"
    if ops.get("arriendo") and not ops.get("venta"):
        return "arriendo"
    # Dual or unknown operation: do not mix rental with purchase intent.
    return None


def price_uf(doc: Mapping, op: str) -> float | None:
    key = "precio_venta" if op == "venta" else "precio_arriendo"
    value = number(nested(doc, "tipo_operacion", key, "precio_uf"))
    return value or (number(nested(doc, "resumen", "precio_uf")) if operation(doc) == op else None)


def surface(doc: Mapping) -> float | None:
    typ = norm(nested(doc, "tipo_operacion", "tipo"))
    keys = (
        ("superficie_terreno", "superficie_total")
        if any(t in typ for t in ("parcela", "sitio", "terreno"))
        else ("superficie_util", "superficie_construida", "superficie_total")
    )
    for key in keys:
        value = number(nested(doc, "caracteristicas", key))
        if value is not None:
            return value
    return None


def vector_cosine(a: Any, b: Any) -> float | None:
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        return None
    if len(a) != VECTOR_DIM or len(b) != VECTOR_DIM:
        return None
    try:
        a = [float(x) for x in a]
        b = [float(x) for x in b]
        if not all(math.isfinite(v) for v in a + b):
            return None
        denom = math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(x * x for x in b))
        return max(-1.0, min(1.0, math.fsum(x * y for x, y in zip(a, b)) / denom)) if denom else None
    except (TypeError, ValueError, OverflowError):
        return None


def tokens(doc: Mapping) -> set[str]:
    text = " ".join(str(nested(doc, "observaciones", key) or "") for key in ("titulo", "descripcion"))
    return {v for v in re.findall(r"[a-z0-9]{3,}", norm(text)) if v not in STOPWORDS}


def source_code_from_lead(lead: Mapping) -> str | None:
    for value in (
        lead.get("codigo_propiedad"), lead.get("source_property_code"),
        nested(lead, "cartera_data", "canonical_code"),
        nested(lead, "prospecto", "codigo"), lead.get("codigo"),
    ):
        code = str(value or "").strip()
        if re.fullmatch(r"\d{3,7}", code):
            return code
    return None


def lead_criteria(lead: Mapping) -> dict:
    sources = (
        lead.get("search_criteria"),
        nested(lead, "prospecto", "search_criteria"),
        nested(lead, "rag_search_state", "criteria"),
        nested(lead, "prospecto", "rag_search_state", "criteria"),
    )
    result = {}
    for source in sources:
        if isinstance(source, Mapping):
            result.update({k: v for k, v in source.items() if v not in (None, "")})
    return result


def budget_uf(criteria: Mapping) -> float | None:
    for key in ("precio_uf_max", "budget_uf", "presupuesto_uf"):
        value = number(criteria.get(key))
        if value:
            return value
    raw = criteria.get("presupuesto") or criteria.get("budget")
    if isinstance(raw, (int, float)):
        value = number(raw)
        return value if value and value < 30000 else None
    if isinstance(raw, str):
        # Ambiguous "900 mil" is not converted to UF.
        match = re.search(r"\buf\s*(\d[\d.,]*)\b|\b(\d[\d.,]*)\s*uf\b", norm(raw))
        if match:
            value = (match.group(1) or match.group(2)).replace(".", "").replace(",", ".")
            return number(value)
    return None


def build_profile(source: Mapping, lead: Mapping) -> dict:
    criteria = lead_criteria(lead)
    commune = criteria.get("comuna") or criteria.get("commune")
    if not isinstance(commune, str) or not commune.strip():
        commune = nested(source, "ubicacion", "comuna") or ""
    op = operation(source)
    return {
        "operation": op,
        "kind": norm(nested(source, "tipo_operacion", "tipo")),
        "commune": str(commune).strip(),
        "budget": budget_uf(criteria),
        "min_beds": integer(criteria.get("dormitorios") or criteria.get("bedrooms")),
        "min_baths": integer(criteria.get("banos") or criteria.get("bathrooms")),
        "source_price": price_uf(source, op) if op else None,
        "source_surface": surface(source),
        "source_beds": integer(nested(source, "caracteristicas", "dormitorios")),
        "source_code": str(source.get("codigo") or ""),
    }


def eligible(candidate: Mapping, profile: Mapping, communes: set[str], office: str | None) -> bool:
    state = candidate.get("estado") or {}
    if candidate.get("disponible_prop360") is not True or state.get("disponible_prop360") is False:
        return False
    if str(candidate.get("codigo") or "") == profile["source_code"]:
        return False
    if office and norm(state.get("oficina") or nested(candidate, "resumen", "oficina")) != norm(office):
        return False
    if operation(candidate) != profile["operation"] or norm(nested(candidate, "tipo_operacion", "tipo")) != profile["kind"]:
        return False
    if norm(nested(candidate, "ubicacion", "comuna")) not in communes:
        return False
    price = price_uf(candidate, profile["operation"])
    if price is None or profile["budget"] is not None and price > profile["budget"]:
        return False
    if profile["budget"] is None and profile["source_price"] and price > profile["source_price"] * 1.35:
        return False
    for field, column in (("min_beds", "dormitorios"), ("min_baths", "banos")):
        if profile[field] is not None:
            value = integer(nested(candidate, "caracteristicas", column))
            if value is None or value < profile[field]:
                return False
    return True


def proximity(a: float | None, b: float | None) -> float:
    if not a or not b:
        return 0.35
    return max(0.0, 1.0 - min(abs(math.log(a / b)) / math.log(2.0), 1.0))


def rank(source: Mapping, candidate: Mapping, profile: Mapping) -> dict:
    sim = vector_cosine(source.get("vector_descripcion"), candidate.get("vector_descripcion"))
    ta, tb = tokens(source), tokens(candidate)
    lex = len(ta & tb) / math.sqrt(len(ta) * len(tb)) if ta and tb else 0.0
    price = price_uf(candidate, profile["operation"])
    size = surface(candidate)
    beds = integer(nested(candidate, "caracteristicas", "dormitorios"))
    same_commune = norm(nested(candidate, "ubicacion", "comuna")) == norm(profile["commune"])
    bed_similarity = (
        max(0.0, 1.0 - abs(beds - profile["source_beds"]) / max(1, profile["source_beds"]))
        if beds is not None and profile["source_beds"] is not None else 0.35
    )
    score = (
        0.40 * max(0.0, sim or 0.0) + 0.10 * lex
        + 0.20 * proximity(profile["source_price"], price)
        + 0.12 * proximity(profile["source_surface"], size)
        + 0.08 * bed_similarity + 0.10 * (1.0 if same_commune else 0.35)
    )
    link = nested(candidate, "publicaciones", "procasa", "url_procasa")
    code = str(candidate.get("codigo"))
    if not isinstance(link, str) or not re.fullmatch(
        rf"https?://(?:www\.)?procasa\.cl/{re.escape(code)}/?", link.strip(), re.I
    ):
        link = None
    reasons = ["misma_comuna" if same_commune else "comuna_vecina"]
    if sim is not None:
        reasons.append("similitud_semantica")
    if price and profile["source_price"] and price <= profile["source_price"]:
        reasons.append("precio_igual_o_inferior")
    if beds is not None and profile["source_beds"] and beds >= profile["source_beds"]:
        reasons.append("dormitorios_iguales_o_mayores")
    return {
        "codigo": code, "oficina": nested(candidate, "estado", "oficina"),
        "comuna": nested(candidate, "ubicacion", "comuna"),
        "tipo": nested(candidate, "tipo_operacion", "tipo"),
        "operacion": profile["operation"], "precio_uf": round(price, 2),
        "superficie_m2": size, "dormitorios": beds,
        "banos": integer(nested(candidate, "caracteristicas", "banos")),
        "url_verificada": link, "score": round(score, 4),
        "similitud_vectorial": round(sim, 4) if sim is not None else None,
        "motivos": reasons,
    }


def recommend_properties(
    source: Mapping, lead: Mapping, inventory: Sequence[Mapping], *,
    office: str | None = DEFAULT_OFFICE, allow_neighbors: bool = False,
    exclude_codes: Sequence[str] = (), limit: int = 5,
) -> dict:
    """Deterministic pilot. Read-only; recheck availability before any contact."""
    code = str(source.get("codigo") or "")
    if not code or not operation(source) or not nested(source, "tipo_operacion", "tipo"):
        return {"status": "SOURCE_INCOMPLETE", "source_code": code, "recommendations": []}
    profile = build_profile(source, lead)
    if not norm(profile["commune"]):
        return {"status": "LOCATION_UNKNOWN", "source_code": code, "recommendations": []}
    excluded = {str(v) for v in exclude_codes}
    excluded.update(str(v) for v in (lead.get("propiedades_vistas") or []) if isinstance(v, (str, int)))
    used = set()
    def collect(communes: set[str]) -> list[Mapping]:
        selected = []
        for candidate in inventory:
            key = str(candidate.get("codigo") or "")
            if key and key not in excluded and key not in used and eligible(candidate, profile, communes, office):
                selected.append(candidate)
                used.add(key)
        return selected

    matched = collect({norm(profile["commune"])})
    scope = "misma_comuna"
    if allow_neighbors and len(matched) < limit:
        neighbors = {norm(x) for x in get_neighboring_communes(profile["commune"])}
        neighbors.discard(norm(profile["commune"]))
        if neighbors:
            extra = collect(neighbors)
            matched.extend(extra)
            if extra:
                scope = "comuna_y_vecinas"
    results = [rank(source, candidate, profile) for candidate in matched]
    results.sort(key=lambda x: (-x["score"], x["codigo"]))
    return {
        "version": ENGINE_VERSION, "status": "OK" if results else "NO_COMPATIBLE_PROPERTIES",
        "source_code": code, "office_scope": office or "RED_PROCASA",
        "geographic_scope": scope, "budget_explicit_uf": profile["budget"],
        "semantic_available": vector_cosine(source.get("vector_descripcion"), source.get("vector_descripcion")) is not None,
        "candidate_count": len(results),
        "recommendations": results[:max(1, min(int(limit), 10))],
        "requires_manual_review": True, "send_enabled": False,
    }


def preview_for_lead(lead: Mapping, sources: Mapping[str, Mapping],
                     inventory: Sequence[Mapping], **kwargs) -> dict:
    """Only commercial fields are returned; no personal lead details."""
    code = source_code_from_lead(lead)
    empty = {"source_code": code, "recommendations": [], "send_enabled": False}
    if str(lead.get("pipeline_stage") or "").upper() in {"CLOSED_WON", "VISIT_SCHEDULED"} or str(lead.get("stage") or "").upper() == "CLOSED_WON":
        return {**empty, "status": "STAGE_NOT_ELIGIBLE"}
    if any(lead.get(flag) is True for flag in ("opt_out", "do_not_contact", "unsubscribed", "no_contact")):
        return {**empty, "status": "DO_NOT_CONTACT"}
    if not code or code not in sources:
        return {**empty, "status": "PROPERTY_NOT_VERIFIED"}
    return {**recommend_properties(sources[code], lead, inventory, **kwargs), "source_verified": True}
