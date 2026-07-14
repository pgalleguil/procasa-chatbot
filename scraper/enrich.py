import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

def _enrich_property_fields(parsed: dict[str, Any], url: str, uf_valor_clp: float, uf_fecha: str) -> dict[str, Any]:
    # URL extraction
    parsed["listing_id"] = ""
    parsed["operacion"] = ""
    parsed["tipo_propiedad"] = ""
    
    if url:
        m = re.search(r'/(\d+)$', url)
        if m:
            parsed["listing_id"] = m.group(1)
        
        low_url = url.lower()
        if "venta" in low_url:
            parsed["operacion"] = "venta"
        elif "alquiler" in low_url or "arriendo" in low_url:
            if "temporada" in low_url:
                parsed["operacion"] = "arriendo_temporada"
            else:
                parsed["operacion"] = "arriendo"
                
        if "casa" in low_url:
            parsed["tipo_propiedad"] = "casa"
        elif "apartamento" in low_url or "departamento" in low_url:
            parsed["tipo_propiedad"] = "departamento"
        elif "lote" in low_url or "terreno" in low_url or "sitio" in low_url:
            parsed["tipo_propiedad"] = "sitio"
        elif "parcela" in low_url:
            parsed["tipo_propiedad"] = "parcela"
        elif "oficina" in low_url:
            parsed["tipo_propiedad"] = "oficina"
        elif "local" in low_url or "bodega" in low_url:
            parsed["tipo_propiedad"] = "local/bodega"
        elif "estacionamiento" in low_url:
            parsed["tipo_propiedad"] = "estacionamiento"

    # Attributes extraction
    attrs = parsed.get("attributes", {})
    
    # Comuna and region from title or body if not in attrs? Yapo usually puts it in breadcrumbs or location.
    # We will try to parse basic fields from attrs
    def get_attr(*keys) -> str:
        def norm(value: str) -> str:
            value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
            return re.sub(r"\s+", " ", value.lower()).strip()
        for k in keys:
            for ak, av in attrs.items():
                if norm(k) in norm(ak):
                    return str(av)
        return ""

    # Normalization
    def parse_int(val: str) -> int | None:
        m = re.search(r'\d+', str(val))
        return int(m.group()) if m else None

    def parse_float(val: str) -> float | None:
        v = str(val).replace(".", "").replace(",", ".")
        m = re.search(r'[\d.]+', v)
        if m:
            try:
                return float(m.group())
            except ValueError:
                pass
        return None

    parsed["comuna"] = get_attr("comuna", "ubicacion", "localizacion", "ciudad") or parsed.get("discovery_comuna", "")
    parsed["region"] = get_attr("region")
    
    parsed["fecha_publicacion_raw"] = get_attr("publicado", "fecha")
    parsed["fecha_publicacion"] = "" # TODO map DD/MM/YYYY to YYYY-MM-DD
    if parsed["fecha_publicacion_raw"]:
        m = re.search(r'(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})', parsed["fecha_publicacion_raw"])
        if m:
            parsed["fecha_publicacion"] = f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"

    parsed["dormitorios"] = parse_int(get_attr("dormitorio", "habitación", "habitacion"))
    parsed["banos"] = parse_int(get_attr("baño", "bano"))
    parsed["estacionamientos"] = parse_int(get_attr("estacionamiento"))
    parsed["m2_construidos"] = parse_float(get_attr("superficie construida", "m2 construido", "m² construido", "útil"))
    parsed["m2_totales"] = parse_float(get_attr("superficie total", "m2 total", "m² total", "terreno"))
    parsed["gastos_comunes"] = parse_int(get_attr("gasto común", "gastos comunes"))
    parsed["direccion_exacta"] = get_attr("dirección", "direccion exact")

    # Price Normalization
    price_raw = parsed.get("price", "")
    parsed["precio_raw"] = price_raw
    parsed["precio_moneda_original"] = "UNKNOWN"
    parsed["precio_original_num"] = None
    parsed["precio_uf"] = None
    parsed["precio_clp"] = None
    parsed["uf_valor_usado"] = uf_valor_clp
    parsed["uf_fecha"] = uf_fecha
    parsed["precio_validacion"] = ""
    parsed["precio_detectado_alternativo"] = ""
    parsed["precio_conversion_source"] = "ENV"

    if price_raw:
        # Check if UF
        low_price = price_raw.lower()
        if "uf" in low_price:
            parsed["precio_moneda_original"] = "UF"
        elif "$" in low_price or "clp" in low_price or "pesos" in low_price:
            parsed["precio_moneda_original"] = "CLP"

        num_val = parse_float(price_raw.replace("UF", "").replace("$", ""))
        parsed["precio_original_num"] = num_val

        if num_val is not None:
            if parsed["precio_moneda_original"] == "UF":
                parsed["precio_uf"] = num_val
                parsed["precio_clp"] = int(round(num_val * uf_valor_clp))
                
                # Validation: Plausibility
                if num_val > 100000:
                    parsed["precio_validacion"] = "sospechoso_uf_excesivo"
                    
                    # Try to find alternative price in description or body
                    body = parsed.get("body_text", "")
                    alt_m = re.search(r'(\d{1,3}(?:\.\d{3})*|\d+)\s*UF', body, re.I)
                    if not alt_m:
                        alt_m = re.search(r'UF\s*(\d{1,3}(?:\.\d{3})*|\d+)', body, re.I)
                        
                    if alt_m:
                        alt_val = parse_float(alt_m.group(1))
                        if alt_val and alt_val < 100000:
                            parsed["precio_detectado_alternativo"] = alt_m.group(0)
                            parsed["precio_validacion"] = "conflicto_precio_raw_vs_descripcion"
                            # Prefer plausible
                            parsed["precio_uf"] = alt_val
                            parsed["precio_clp"] = int(round(alt_val * uf_valor_clp))
                            parsed["precio_original_num"] = alt_val
                            
            elif parsed["precio_moneda_original"] == "CLP":
                parsed["precio_clp"] = int(num_val)
                parsed["precio_uf"] = round(num_val / uf_valor_clp, 2)

    return parsed
