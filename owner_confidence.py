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
    semantic = classification.get("semantic_check") or {}
    state = classification.get("state") or classification.get("final_state") or ""
    status = semantic.get("status") if isinstance(semantic, dict) else None
    confidence = classification.get("confidence")

    if state == "DUE\u00d1O_SEGURO" and status == "SKIPPED_EXPLICIT_OWNER":
        return {
            "owner_confidence_display": "Due\u00f1o expl\u00edcito",
            "owner_confidence_sort": 101,
            "owner_confidence_title": (
                "Evidencia expl\u00edcita de propietario encontrada en la publicaci\u00f3n"
            ),
            "owner_confidence_type": "explicit",
        }

    if state == "DUE\u00d1O_SEGURO" and status == "VALID":
        try:
            value = float(confidence)
            if value <= 1:
                value *= 100
            value = max(0, min(100, value))
            return {
                "owner_confidence_display": f"{round(value)}%",
                "owner_confidence_sort": round(value),
                "owner_confidence_title": (
                    "Confianza de DeepSeek en la clasificaci\u00f3n DUE\u00d1O_SEGURO"
                ),
                "owner_confidence_type": "percentage",
            }
        except (TypeError, ValueError):
            pass

    return {
        "owner_confidence_display": "\u2014",
        "owner_confidence_sort": -1,
        "owner_confidence_title": (
            "No existe una probabilidad de due\u00f1o calculada"
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
