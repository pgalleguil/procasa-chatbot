import re
from typing import Any
from urllib.parse import urlparse


def _parse_chilean_number(value: str, *, decimal_hint: bool = False) -> float | None:
    raw = re.sub(r"[^0-9.,]", "", str(value or "")).strip()
    if not raw:
        return None
    if "," in raw:
        integer, decimal = raw.rsplit(",", 1)
        try:
            return float(f"{integer.replace('.', '')}.{decimal}")
        except ValueError:
            return None
    if "." in raw:
        parts = raw.split(".")
        if len(parts) == 2 and len(parts[1]) in (1, 2) and (decimal_hint or len(parts[0]) >= 7):
            try:
                return float(raw)
            except ValueError:
                return None
        try:
            return float("".join(parts))
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_toctoc_price(price_text: str, uf_valor_clp: float | None = None) -> dict[str, Any]:
    """Parse published CLP/UF without concatenating the two currencies."""
    text = str(price_text or "")
    uf_match = re.search(r"\bUF\s*([0-9][0-9.,]*)", text, re.IGNORECASE)
    if not uf_match:
        uf_match = re.search(r"([0-9][0-9.,]*)\s*UF\b", text, re.IGNORECASE)
    clp_match = re.search(r"\$\s*([0-9][0-9.,]*)", text)
    precio_uf = _parse_chilean_number(uf_match.group(1), decimal_hint=True) if uf_match else None
    precio_clp = _parse_chilean_number(clp_match.group(1)) if clp_match else None
    if precio_uf is not None and precio_clp is None and uf_valor_clp:
        precio_clp = round(precio_uf * float(uf_valor_clp))
    return {"precio_uf": precio_uf, "precio_clp": precio_clp}


def _enrich_property_fields(parsed: dict[str, Any], url: str, uf_valor_clp: float, uf_fecha: str) -> dict[str, Any]:
    main_fields = {"listing_id": "", "operacion": "", "tipo_propiedad": "", "comuna": "", "region": ""}
    for field, default in main_fields.items():
        if field not in parsed or not parsed.get(field):
            parsed[field] = default

    if not parsed["listing_id"] and url:
        m = re.search(r"-(\d+)$", url) or re.search(r"/(\d+)$", url)
        if m:
            parsed["listing_id"] = m.group(1)

    if not parsed["operacion"] and url:
        low = url.lower()
        if "venta" in low and "arriendo" not in low:
            parsed["operacion"] = "venta"
        elif "arriendo" in low:
            parsed["operacion"] = "arriendo"

    if not parsed["tipo_propiedad"] and url:
        low = url.lower()
        if "/casa" in low and "/departamento" not in low: parsed["tipo_propiedad"] = "casa"
        elif "/departamento" in low: parsed["tipo_propiedad"] = "departamento"
        elif "/terreno" in low: parsed["tipo_propiedad"] = "terreno"
        elif "/parcela" in low: parsed["tipo_propiedad"] = "parcela"
        elif "/oficina" in low: parsed["tipo_propiedad"] = "oficina"
        elif "/local" in low: parsed["tipo_propiedad"] = "local"
        elif "/estacionamiento" in low: parsed["tipo_propiedad"] = "estacionamiento"
        elif "/bodega" in low: parsed["tipo_propiedad"] = "bodega"
        elif "/industrial" in low: parsed["tipo_propiedad"] = "industrial"
        elif "/vacacional" in low: parsed["tipo_propiedad"] = "vacacional"
        elif re.search(r"/(?:agricola|campo-agricola|campoagricola)(?:/|$)", low):
            parsed["tipo_propiedad"] = "agricola"

    attrs = parsed.get("attributes", {})

    if not parsed.get("comuna"):
        for key in ("comuna", "sector", "barrio", "city"):
            parsed["comuna"] = parsed.get(key) or attrs.get(key) or ""

    if not parsed.get("dormitorios"):
        dorm_match = re.search(r'(\d+)\s*(?:dor|dormitorio|dorm|habitacion)', str(attrs) + parsed.get("title", ""), re.I)
        if dorm_match: parsed["dormitorios"] = int(dorm_match.group(1))

    if not parsed.get("banos"):
        bano_match = re.search(r'(\d+)\s*(?:baño|bano|banio|ba)', str(attrs) + parsed.get("title", ""), re.I)
        if bano_match: parsed["banos"] = int(bano_match.group(1))

    # Keep both currencies delivered by the same detail page together.  The
    # old first-nonempty selection preferred UF and discarded an explicit CLP
    # value when TOCTOC rendered both.
    detail_price_parts = [
        str(parsed.get(key) or "").strip()
        for key in ("price", "precio", "precio_raw", "price_uf", "price_clp")
        if str(parsed.get(key) or "").strip()
    ]
    price_text = " ".join(dict.fromkeys(detail_price_parts))
    # Never infer a published price from title/description. Those fields can
    # contain commissions, surface areas, dates, and other unrelated numbers.
    # A missing detail price must remain UNKNOWN and fail closed at the gate.
    parsed["price_raw"] = price_text
    parsed["detail_price_raw"] = price_text
    parsed["price_source"] = "DETAIL" if price_text else "UNKNOWN"
    components = parse_toctoc_price(price_text, uf_valor_clp)
    detail_uf = components.get("precio_uf")
    detail_clp = components.get("precio_clp")
    if detail_clp is None and detail_uf is not None and uf_valor_clp:
        detail_clp = round(detail_uf * float(uf_valor_clp))
    parsed["detail_price_uf"] = detail_uf
    parsed["detail_price_clp"] = detail_clp
    parsed["precio_uf"] = detail_uf
    parsed["precio_clp"] = detail_clp
    parsed["price_clp"] = f"$ {detail_clp}" if detail_clp is not None else ""
    parsed["price_uf"] = f"UF {detail_uf:g}" if detail_uf is not None else ""
    parsed["detail_currency"] = (
        "UF+CLP" if re.search(r"\$\s*[0-9]", price_text) and detail_uf is not None
        else "UF" if detail_uf is not None and not re.search(r"\$\s*[0-9]", price_text)
        else "CLP" if detail_clp is not None
        else "UNKNOWN"
    )
    if detail_uf is not None:
        parsed["precio_numerico"] = detail_uf
        parsed["moneda"] = "UF"
    elif detail_clp is not None:
        parsed["precio_numerico"] = detail_clp
        parsed["moneda"] = "CLP"
    parsed["price_status"] = "VALID" if detail_clp is not None else "UNKNOWN"

    parsed["uf_valor_clp"] = uf_valor_clp
    parsed["uf_fecha"] = uf_fecha
    return parsed

