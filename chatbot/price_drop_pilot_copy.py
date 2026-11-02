"""Conservative, personal follow-up copy for a verified price reduction.

A prior inquiry is mentioned ONLY when the caller has independently verified
the recipient's earlier inquiry for THIS property. In the isolated test that
verification is explicitly simulated, never inferred from the tester's CRM.
"""
from __future__ import annotations

import re
from typing import Any


def _uf(value: float) -> str:
    val = float(value)
    if val <= 0 or val > 10000000:
        raise ValueError("Invalid UF amount")
    if val.is_integer():
        return f"{int(val):,}".replace(",", ".")
    return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _verified_features(property_doc: dict) -> str:
    characteristics = property_doc.get("caracteristicas") or {}
    if not isinstance(characteristics, dict):
        return ""
    details: list[str] = []
    bedrooms = characteristics.get("dormitorios")
    bathrooms = characteristics.get("banos")
    if type(bedrooms) is int and 1 <= bedrooms <= 30:
        details.append(f"{bedrooms} dormitorios")
    if type(bathrooms) is int and 1 <= bathrooms <= 30:
        details.append(f"{bathrooms} baños")
    features = characteristics.get("features") or []
    terrace_area = characteristics.get("superficie_terraza")
    if ((isinstance(features, list) and "has_terrace" in features) or
        (type(terrace_area) in (float, int) and 0 < terrace_area < 500)):
        details.append("terraza")
    if len(details) < 2:
        return ""
    return "Es el departamento de " + ", ".join(details[:-1]) + " y " + details[-1] + ". "


def build_price_drop_followup(
    *,
    code: str,
    commune: str,
    old_uf: float,
    new_uf: float,
    property_doc: dict,
    prior_inquiry_verified: bool = False,
) -> str:
    """Produce a ready-to-review copy, not an authorization to contact a lead."""
    if not re.fullmatch(r"\d{4,6}", str(code)):
        raise ValueError("Invalid property code")
    if not commune or not isinstance(commune, str) or len(commune.strip()) > 60:
        raise ValueError("Missing verified commune")
    old, new = float(old_uf), float(new_uf)
    if not (2000 <= new <= 5000 and new < old):
        raise ValueError("Price reduction not eligible")
    difference = _uf(old - new)
    previous = (
        f"Te escribo de PROCASA porque hace un tiempo nos consultaste por "
        f"un departamento en {commune}."
        if prior_inquiry_verified else
        f"Te escribo de PROCASA para contarte una novedad del departamento de {commune}."
    )
    features = _verified_features(property_doc)
    reminder = features + "Te dejo el enlace para que le des otra mirada:"
    return (
        "¡Hola! ¿Cómo estás? 😊\n\n"
        f"{previous}\n\n"
        f"Quería contarte que bajó de UF {_uf(old)} a UF {_uf(new)}, "
        f"¡son {difference} UF menos!\n\n"
        f"{reminder}\n\n"
        f"🏡 https://www.procasa.cl/{code}\n\n"
        "¿Todavía estás buscando departamento? Quizás ahora te resulte más interesante."
    )
