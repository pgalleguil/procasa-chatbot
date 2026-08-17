import re
from typing import Any
from urllib.parse import urlparse


def _parse_chilean_number(value: str, *, decimal_hint: bool = False) -> float | None:
    """Parsea un número chileno sin mezclar segmentos de monedas.

    Un punto aislado se interpreta como separador de miles; una coma como
    decimal. Para UF se acepta además la forma ``5200.50`` cuando hay dos
    dígitos después del punto.
    """
    raw = re.sub(r"[^0-9.,]", "", str(value or "")).strip()
    if not raw:
        return None
    if "," in raw:
        integer, decimal = raw.rsplit(",", 1)
        integer = integer.replace(".", "")
        try:
            return float(f"{integer}.{decimal}")
        except ValueError:
            return None
    if "." in raw:
        parts = raw.split(".")
        # 5.200 = miles; 5.20 = decimal UF.
        if len(parts) == 2 and len(parts[1]) in (1, 2) and decimal_hint:
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
    """Extrae CLP y UF por separado desde el texto publicado por Toctoc.

    Nunca concatena los dígitos de ambas monedas. Si solo viene UF, calcula
    CLP con el valor UF vigente; si solo viene CLP, conserva solo CLP aquí.
    """
    text = str(price_text or "")
    uf_match = re.search(r"\bUF\s*([0-9][0-9.,]*)", text, re.IGNORECASE)
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

    price_text = parsed.get("price", "") or parsed.get("precio", "") or parsed.get("precio_raw", "")
    if price_text:
        components = parse_toctoc_price(price_text, uf_valor_clp)
        if components["precio_uf"] is not None:
            parsed["precio_uf"] = components["precio_uf"]
            parsed["precio_numerico"] = components["precio_uf"]
            parsed["moneda"] = "UF"
        if components["precio_clp"] is not None:
            parsed["precio_clp"] = components["precio_clp"]
            if components["precio_uf"] is None:
                parsed["precio_numerico"] = components["precio_clp"]
                parsed["moneda"] = "CLP"

    parsed["uf_valor_clp"] = uf_valor_clp
    parsed["uf_fecha"] = uf_fecha
    return parsed
