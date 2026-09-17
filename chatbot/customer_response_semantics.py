"""Deterministic customer-facing semantic guards for the V1 pipeline."""
from __future__ import annotations

import re

from .conversation_policy import is_specific_property_question
from .property_lookup import get_prop_location, get_prop_operation


def _format_uf(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount == int(amount):
        return f"{int(amount):,}".replace(",", ".")
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    integer, _, decimal = text.partition(".")
    return f"{int(integer):,}".replace(",", ".") + (f",{decimal}" if decimal else "")


def build_specific_property_response(message: str, property_doc: dict | None) -> str | None:
    if not property_doc or not is_specific_property_question(message):
        return None
    normalized = str(message or "").casefold()
    if not re.search(r"\bmascot(?:a|as)\b", normalized):
        return None
    containers = [property_doc, property_doc.get("caracteristicas") or {},
                  property_doc.get("observaciones") or {}, property_doc.get("amenities") or {}]
    keys = ("mascotas", "mascota", "acepta_mascotas", "admite_mascotas",
            "pet_friendly", "pets_allowed", "permite_mascotas")
    value = None
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            if key in container and container.get(key) not in (None, ""):
                value = container.get(key)
                break
        if value is not None:
            break
    if isinstance(value, bool):
        return "La ficha indica que sí se aceptan mascotas." if value else "La ficha indica que no se aceptan mascotas."
    if value is not None:
        return f"La ficha indica lo siguiente sobre mascotas: {str(value).strip()}."
    return "La ficha no indica si acepta mascotas, así que prefiero no confirmártelo sin verificarlo."


def rag_customer_response(properties: list[dict] | None) -> str:
    properties = list(properties or [])[:2]
    if not properties:
        return "No encontré coincidencias exactas con esos criterios. Si quieres, podemos ampliar un poco la búsqueda."
    lines = ["Encontré estas opciones que coinciden con lo que buscas:"]
    for prop in properties:
        location = get_prop_location(prop)
        operation = get_prop_operation(prop)
        tipo = operation.get("tipo") or "Propiedad"
        comuna = location.get("comuna") or location.get("sector") or "ubicación por confirmar"
        sector = location.get("sector")
        place = f"{comuna} · {sector}" if sector and sector.casefold() != comuna.casefold() else comuna
        if operation.get("precio_uf") not in (None, ""):
            price = f"{_format_uf(operation['precio_uf'])} UF"
        elif operation.get("precio_clp") not in (None, ""):
            price = f"${operation['precio_clp']}"
        else:
            price = "precio por confirmar"
        characteristics = prop.get("caracteristicas") or {}
        bedrooms = characteristics.get("dormitorios") or prop.get("dormitorios")
        surface = characteristics.get("superficie_util") or prop.get("m2_utiles")
        useful = (f"{bedrooms} dormitorio(s)" if bedrooms not in (None, "") else
                  f"{surface} m² útiles" if surface not in (None, "") else
                  (f"{characteristics.get('banos')} baño(s)" if characteristics.get("banos") not in (None, "") else "ficha por revisar"))
        publications = prop.get("publicaciones") or {}
        procasa = publications.get("procasa") if isinstance(publications, dict) else {}
        link = ((procasa or {}).get("url_procasa") if isinstance(procasa, dict) else None)
        link = link or prop.get("url_procasa") or f"https://www.procasa.cl/{prop.get('codigo')}"
        code = str(prop.get("codigo") or "").strip()
        code_label = f"Código {code}; " if code else ""
        lines.append(f"• {code_label}{tipo} en {place} ({operation.get('operacion') or 'operación por confirmar'}), {price}; {useful}.\n  {link}")
    lines.append("¿Cuál te gustaría revisar?")
    return "\n".join(lines)


def new_link_template_contaminated(response: str) -> bool:
    normalized = str(response or "").casefold()
    return bool(re.search(r"\b(?:no\s+descarto|dejamos\s+activa|propiedad\s+descartada|no\s+me\s+gust(?:a|o)|no\s+qued[oó]\s+conforme)\b", normalized))


def new_link_context_response(property_doc: dict, external_id: str | None = None) -> str:
    location = get_prop_location(property_doc)
    operation = get_prop_operation(property_doc)
    identity = str(property_doc.get("codigo") or external_id or "la publicación")
    place = location.get("comuna") or location.get("sector") or "la ubicación indicada"
    detail = f"{operation.get('tipo') or 'propiedad'} en {place}"
    if operation.get("operacion"):
        detail += f" para {operation['operacion'].casefold()}"
    return f"Gracias por compartir el enlace. Identifiqué la propiedad {identity}: {detail}. Tomaré esta propiedad como el nuevo contexto para revisar sus antecedentes."


def evaluate_customer_response(customer_message: str, response: str, *,
                               rag_results: list[dict] | None = None,
                               resolved_property: dict | None = None) -> dict:
    """Semantic evaluator used by the V1 replay gate.

    Internal state is not enough to pass: the customer-facing text must answer
    the asked field, present concrete RAG results when available, and avoid a
    previous-property rejection template after a new link.
    """
    message = str(customer_message or "")
    text = str(response or "")
    normalized = text.casefold()
    checks = {
        "ANSWER_RELEVANCE": True,
        "QUESTION_ACTUALLY_ANSWERED": True,
        "NO_TEMPLATE_CONTAMINATION": True,
        "RAG_RESULTS_ACTUALLY_PRESENTED": True,
    }
    if re.search(r"\bmascot(?:a|as)\b", message.casefold()):
        checks["QUESTION_ACTUALLY_ANSWERED"] = bool(
            re.search(r"mascot(?:a|as)", normalized)
            and any(marker in normalized for marker in ("indica", "acept", "admit", "no indica"))
        )
        checks["ANSWER_RELEVANCE"] = checks["QUESTION_ACTUALLY_ANSWERED"]
    if rag_results:
        identifiers = {str(item.get("codigo") or "").casefold() for item in rag_results[:2]}
        links = {f"https://www.procasa.cl/{code}" for code in identifiers if code}
        checks["RAG_RESULTS_ACTUALLY_PRESENTED"] = any(
            token and (token in normalized or token in text)
            for token in (*identifiers, *links)
        )
        checks["ANSWER_RELEVANCE"] = checks["ANSWER_RELEVANCE"] and checks["RAG_RESULTS_ACTUALLY_PRESENTED"]
    if resolved_property and re.search(r"https?://|www\.", message):
        checks["NO_TEMPLATE_CONTAMINATION"] = not new_link_template_contaminated(text)
        checks["ANSWER_RELEVANCE"] = checks["ANSWER_RELEVANCE"] and checks["NO_TEMPLATE_CONTAMINATION"]
    return {**checks, "PASS": all(checks.values())}
