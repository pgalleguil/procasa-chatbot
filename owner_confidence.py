"""
Helper functions for owner confidence display.
Independent module with no circular imports.
"""


def build_owner_confidence_doc(doc):
    """Build confidence display fields from a MongoDB document.
    Returns dict with: owner_confidence_display, owner_confidence_sort,
    owner_confidence_title, owner_confidence_type.
    """
    classification = doc.get("classification") or {}
    state = classification.get("state") or classification.get("final_state") or ""
    confidence = classification.get("confidence")

    # This is confidence in the resulting classification. Show it for both
    # states distributed to the team. For INCIERTO it does not mean owner odds.
    if state in {"DUE\u00d1O_SEGURO", "INCIERTO"}:
        try:
            value = float(confidence)
            if value <= 1:
                value *= 100
            value = max(0, min(100, value))
            return {
                "owner_confidence_display": f"{round(value)}%",
                "owner_confidence_sort": round(value),
                "owner_confidence_title": (
                    f"Confianza del clasificador en el estado {state}"
                ),
                "owner_confidence_type": "percentage",
            }
        except (TypeError, ValueError):
            pass

    return {
        "owner_confidence_display": "\u2014",
        "owner_confidence_sort": -1,
        "owner_confidence_title": (
            "No existe confianza de clasificaci\u00f3n calculada"
        ),
        "owner_confidence_type": "unknown",
    }


def _format_price_uf(val):
    try:
        v = float(val)
        if v == 0:
            return ""
        s = f"{v:,.1f}".replace(",", "#").replace(".", ",").replace("#", ".")
        return s[:-2] if s.endswith(",0") else s
    except (ValueError, TypeError):
        return ""


def _format_price_clp(val):
    try:
        v = float(val)
        if v == 0:
            return ""
        return f"{v:,.0f}".replace(",", ".")
    except (ValueError, TypeError):
        return ""


def resolve_price_display(document):
    """Build unified price display from a MongoDB document.
    Returns dict with: precio_display, precio_uf_display, precio_clp_display.
    """
    doc = document or {}
    
    precio_uf = doc.get("precio_uf") or doc.get("price_uf") or 0
    precio_clp = doc.get("precio_clp") or doc.get("price_clp") or 0
    precio_raw = doc.get("precio_raw") or doc.get("price") or ""
    
    uf_display = _format_price_uf(precio_uf)
    clp_display = _format_price_clp(precio_clp)
    
    parts = []
    if uf_display:
        parts.append(f"UF {uf_display}")
    if clp_display:
        parts.append(f"${clp_display}")
    
    if parts:
        precio_display = " / ".join(parts)
    else:
        precio_display = "S/I"
    
    return {
        "precio_display": precio_display,
        "precio_uf_display": uf_display,
        "precio_clp_display": clp_display,
    }


def detect_source_price_warning(operation, precio_uf, precio_clp):
    """Flag implausibly low sale prices without inventing a replacement value."""
    if str(operation).upper() != "VENTA":
        return ""
    try:
        uf = float(precio_uf or 0)
    except (TypeError, ValueError):
        uf = 0
    try:
        clp = float(precio_clp or 0)
    except (TypeError, ValueError):
        clp = 0
    if (0 < uf < 100) or (0 < clp < 5_000_000):
        return "Precio inconsistente en origen"
    return ""
