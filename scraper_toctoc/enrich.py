import re
from typing import Any
from urllib.parse import urlparse


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

    price_text = parsed.get("price", "") or parsed.get("precio", "")
    if price_text and not parsed.get("precio_numerico"):
        price_clean = re.sub(r"[^\d,.]", "", price_text.replace(".", "").replace(",", "."))
        try: parsed["precio_numerico"] = float(price_clean)
        except ValueError: parsed["precio_numerico"] = 0.0
        if "uf" in price_text.lower():
            parsed["moneda"] = "UF"
            parsed["precio_uf"] = parsed["precio_numerico"]
            parsed["precio_clp"] = round(parsed["precio_numerico"] * uf_valor_clp, 0)

    parsed["uf_valor_clp"] = uf_valor_clp
    parsed["uf_fecha"] = uf_fecha
    return parsed
