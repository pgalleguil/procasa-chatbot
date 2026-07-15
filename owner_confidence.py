"""Presentation helpers for classification confidence, owner score and prices."""


def _percentage(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value <= 1:
        value *= 100
    if value > 100:
        return None
    return round(value)


def build_classification_confidence_doc(doc):
    """Render only the technical confidence of the selected classification."""
    classification = (doc or {}).get("classification") or {}
    value = _percentage(classification.get("confidence"))
    if value is None:
        return {
            "classification_confidence_display": "S/I",
            "classification_confidence_sort": -1,
            "classification_confidence_title": "No existe confianza técnica válida",
        }
    return {
        "classification_confidence_display": f"{value}%",
        "classification_confidence_sort": value,
        "classification_confidence_title": "Confianza técnica en que la categoría asignada es correcta",
    }


def build_owner_score_doc(doc):
    """Render only the independent, explainable owner-prioritization score."""
    classification = (doc or {}).get("classification") or {}
    try:
        raw_value = float(classification.get("owner_score"))
        value = round(raw_value) if 0 <= raw_value <= 100 else None
    except (TypeError, ValueError):
        value = None
    if value is None:
        return {
            "owner_score_display": "S/I",
            "owner_score_sort": -1,
            "owner_score_title": "No existe Score dueño calculado",
        }
    signals = classification.get("owner_score_signals") or {}
    positives = signals.get("positive") or [] if isinstance(signals, dict) else []
    negatives = signals.get("negative") or [] if isinstance(signals, dict) else []
    labels = []
    for prefix, items in (("+", positives), ("−", negatives)):
        for item in items[:3]:
            labels.append(f"{prefix} {item.get('code')}: {item.get('evidence', '')}")
    title = " | ".join(labels) if labels else "Score neutral: no existen señales útiles"
    return {
        "owner_score_display": f"{value} pts",
        "owner_score_sort": value,
        "owner_score_title": title,
    }


def build_owner_probability_doc(doc):
    """Render the only owner-likelihood metric exposed by the CRM."""
    classification = (doc or {}).get("classification") or {}
    value = _percentage(classification.get("owner_probability"))
    if value is None:
        return {
            "owner_probability_display": "S/I",
            "owner_probability_sort": -1,
            "owner_probability_title": "Cálculo pendiente o evidencia insuficiente",
        }
    signals = classification.get("owner_probability_signals") or {}
    applied = signals.get("applied") or [] if isinstance(signals, dict) else []
    labels = [
        f"{item.get('code')}: {item.get('evidence', '')}"
        for item in applied[:5] if isinstance(item, dict)
    ]
    title = " | ".join(labels) if labels else "Resultado neutral: sin señales útiles"
    return {
        "owner_probability_display": f"{value}%",
        "owner_probability_sort": value,
        "owner_probability_title": title,
    }


def build_owner_confidence_doc(doc):
    """Compatibility wrapper; never fabricates a probability or a 50 fallback."""
    confidence = build_classification_confidence_doc(doc)
    return {
        "owner_confidence_display": confidence["classification_confidence_display"],
        "owner_confidence_sort": confidence["classification_confidence_sort"],
        "owner_confidence_title": confidence["classification_confidence_title"],
        "owner_confidence_type": "technical_confidence" if confidence["classification_confidence_sort"] >= 0 else "unknown",
    }


def _format_price_uf(val):
    try:
        v = float(val)
        if v <= 0:
            return ""
        s = f"{v:,.1f}".replace(",", "#").replace(".", ",").replace("#", ".")
        return s[:-2] if s.endswith(",0") else s
    except (ValueError, TypeError):
        return ""


def _format_price_clp(val):
    try:
        v = float(val)
        if v <= 0:
            return ""
        return f"{v:,.0f}".replace(",", ".")
    except (ValueError, TypeError):
        return ""


def resolve_price_display(document):
    doc = document or {}
    details = doc.get("details") or {}
    precio_uf = doc.get("precio_uf") or doc.get("price_uf") or details.get("precio_uf") or 0
    precio_clp = doc.get("precio_clp") or doc.get("price_clp") or details.get("precio_clp") or 0
    uf_display = _format_price_uf(precio_uf)
    clp_display = _format_price_clp(precio_clp)
    parts = ([f"UF {uf_display}"] if uf_display else []) + ([f"${clp_display}"] if clp_display else [])
    return {
        "precio_display": " / ".join(parts) if parts else "S/I",
        "precio_uf_display": uf_display,
        "precio_clp_display": clp_display,
    }


def detect_source_price_warning(operation, precio_uf, precio_clp):
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
