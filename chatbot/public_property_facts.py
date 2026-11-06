"""Customer-facing property facts. Only strict public Prop360 fields are read."""
from __future__ import annotations

import re
import unicodedata
from .property_read_guard import _filter_property

_AMENITIES = {
    "piscina": "piscina", "has_grill": "quincho/parrilla",
    "has_terrace": "terraza", "gimnasio": "gimnasio",
    "jardinformado": "jardín", "has_patio": "patio",
    "has_balcony": "balcón", "amoblado": "amoblado",
    "has_guest_parking": "estacionamiento de visitas",
    "has_security": "seguridad", "has_air_conditioning": "aire acondicionado",
    "has_multipurpose_room": "sala multiuso",
    "has_party_room": "salón de eventos", "has_playground": "juegos infantiles",
    "has_common_laundry": "lavandería común", "has_natural_gas": "gas natural",
    "walkincloset": "walk-in clóset", "has_closet": "clóset",
    "lavadero": "lavadero", "with_laundry_connection": "conexión para lavadora",
    "recepcion": "recepción", "banovisita": "baño de visitas",
}
_FIELDS = {
    "precio": r"\b(precio|valor|uf|rebaj[ao]|cuanto cuesta)\b",
    "gastos": r"\b(gastos?\s+comunes?|ggcc)\b",
    "dormitorios": r"\b(dormitorios?|habitaciones?|piezas?)\b",
    "banos": r"\b(ba[nñ]os?)\b",
    "util": r"\b(superficie\s+util|metros?\s+utiles?|m2\s+utiles?)\b",
    "total": r"\b(superficie\s+total|metros?\s+totales?|metraje|m2\s+totales?)\b",
    "terreno": r"\b(terreno)\b",
    "construida": r"\b(superficie\s+construida|metros?\s+construidos?)\b",
    "terraza": r"\b(terraza|balcon)\b",
    "estacionamientos": r"\b(estacionamientos?|parking|garage|garaje)\b",
    "bodegas": r"\b(bodegas?)\b",
    "orientacion": r"\b(orientacion|hacia\s+donde)\b",
    "piso": r"\b(en\s+que\s+piso|que\s+piso|piso\s+(del\s+)?(departamento|depto))\b",
    "pisos": r"\b(pisos\s+(del\s+)?edificio|cuantos\s+pisos)\b",
    "ascensores": r"\b(ascensores?|elevadores?)\b",
    "ano": r"\b(ano\s+de\s+construccion|antiguedad|cuando\s+se\s+construyo)\b",
    "equipamiento": r"\b(equipamiento|amenidades?|comodidades?|piscina|quincho|gimnasio|seguridad|jardin|lavanderia)\b",
    "comuna": r"\b(comuna|ubicacion|donde\s+queda)\b",
    "disponible": r"\b(disponible|disponibilidad|sigue\s+en\s+venta)\b",
    "fotos": r"\b(fotos?|imagenes?|fotografias?)\b",
    "mascotas": r"\b(mascotas?|pets?|perros?|gatos?)\b",
}
_FULL = re.compile(
    r"\b(ficha|informacion|todos?\s+los\s+datos|todos?\s+los\s+detalles|"
    r"caracteristicas|resumen\s+completo|cuentame\s+todo|"
    r"detalles?\s+de\s+la\s+propiedad|como\s+es\s+(la\s+propiedad|el\s+departamento))\b"
)
_PRIVATE = re.compile(
    r"\b(direccion|calle|numero\s+(de\s+la\s+)?casa|numero\s+(del\s+)?departamento|"
    r"nombre\s+(del\s+)?(dueno|propietario)|"
    r"(telefono|celular|whatsapp|rut|correo|email)\s+(del?\s+)?(dueno|propietario)|"
    r"contacto\s+(del?\s+)?(dueno|propietario))\b"
)
_LABELS = {
    "precio":"Precio", "gastos":"Gastos comunes", "dormitorios":"Dormitorios",
    "banos":"Baños", "util":"Superficie útil", "total":"Superficie total",
    "terreno":"Superficie terreno", "construida":"Superficie construida",
    "terraza":"Superficie terraza", "estacionamientos":"Estacionamientos",
    "bodegas":"Bodegas", "orientacion":"Orientación", "piso":"Piso",
    "pisos":"Pisos del edificio", "ascensores":"Ascensores",
    "ano":"Año de construcción", "equipamiento":"Equipamiento informado",
    "comuna":"Comuna", "disponible":"Disponibilidad", "fotos":"Fotos",
    "mascotas":"Mascotas",
}
_SURFACES = frozenset(("util","total","terreno","construida","terraza"))


def _normalized(text):
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def _present(value):
    return value is not None and value != "" and not isinstance(value, bool)


def _num(value):
    try:
        n = float(value)
    except (ValueError, TypeError):
        return str(value)
    if n == int(n):
        return f"{int(n):,}".replace(",", ".")
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".").rstrip("0").rstrip(",")


def public_property_facts(property_doc):
    """Use an independent allowlist even if a caller supplies an unsafe document."""
    p = _filter_property(property_doc) or {}
    o, c, loc = (p.get("tipo_operacion") or {}), (p.get("caracteristicas") or {}), (p.get("ubicacion") or {})
    is_sale = bool(o.get("venta"))
    operation = "Venta" if is_sale else "Arriendo" if o.get("arriendo") else str(p.get("operacion") or "N/D")
    pricing = o.get("precio_venta" if is_sale else "precio_arriendo") or {}
    uf, clp = pricing.get("precio_uf"), pricing.get("precio_clp")
    f = {
        "codigo": str(p.get("codigo") or "N/D"),
        "tipo": str(o.get("tipo") or p.get("tipo") or "Propiedad"),
        "operacion": operation,
        "comuna": str(loc.get("comuna") or p.get("comuna") or "No informada"),
        "region": str(loc.get("region") or p.get("region") or ""),
        "precio": ("UF " + _num(uf) if _present(uf) else
                   "$" + _num(clp) if _present(clp) else "No informado"),
    }
    if is_sale and _present(uf) and _present(clp):
        f["precio"] += " (referencia $" + _num(clp) + ")"
    gc = o.get("gastos_comunes")
    f["gastos"] = "$" + _num(gc) if _present(gc) and float(gc)>0 else "No informado"
    for key, raw in (
        ("dormitorios",c.get("dormitorios",p.get("dormitorios"))),
        ("banos",c.get("banos",p.get("banos"))),
        ("util",c.get("superficie_util",p.get("m2_utiles"))),
        ("total",c.get("superficie_total",p.get("m2_totales"))),
        ("terreno",c.get("superficie_terreno")),
        ("construida",c.get("superficie_construida")),
        ("terraza",c.get("superficie_terraza")),
        ("bodegas",c.get("bodegas",p.get("bodega"))),
        ("orientacion",c.get("orientacion",p.get("orientacion"))),
        ("piso",c.get("piso")),
        ("pisos",c.get("numero_pisos")),
        ("ascensores",c.get("ascensores")),
        ("ano",c.get("ano_construccion")),
    ):
        f[key] = _num(raw) if _present(raw) else "No informado"
    parking = c.get("estacionamientos",p.get("estacionamientos"))
    if not _present(parking):
        covered, uncovered = c.get("estacionamientos_cubiertos"),c.get("estacionamientos_descubiertos")
        if _present(covered) or _present(uncovered):
            parking = int(covered or 0) + int(uncovered or 0)
    f["estacionamientos"] = _num(parking) if _present(parking) else "No informado"
    f["equipamiento"] = ", ".join(sorted(set(_AMENITIES[x] for x in c.get("features",[]) if x in _AMENITIES))) or "No informado"
    f["disponible"] = ("Activa según ficha; confirmar antes de reservar"
                       if p.get("disponible_prop360") is True else "No confirmada")
    f["fotos"] = ("https://www.procasa.cl/" + f["codigo"]
                   if f["codigo"].isdigit() else "Consultar publicación")
    f["mascotas"] = "La ficha no indica si acepta mascotas; hay que confirmarlo"
    return f


def _line(key, facts):
    val = facts[key]
    if val == "No informado":
        return _LABELS[key] + ": la ficha no informa este dato; hay que confirmarlo."
    if key in _SURFACES:
        val += " m²"
    return _LABELS[key] + ": " + val


def public_property_summary(property_doc):
    f = public_property_facts(property_doc)
    lines = ["Propiedad " + f["codigo"] + " · " + f["tipo"] + " · " + f["operacion"],
             "Comuna: " + f["comuna"] + (" (" + f["region"] + ")" if f["region"] else "")]
    for key in ("precio","gastos","dormitorios","banos","util","total",
                "terreno","construida","terraza","estacionamientos","bodegas",
                "orientacion","piso","pisos","ascensores","ano",
                "equipamiento","disponible"):
        if f[key] != "No informado":
            lines.append(_line(key,f))
    return "\n".join(lines)


def requested_public_fact_keys(message):
    text = _normalized(message)
    found = [key for key, regex in _FIELDS.items() if re.search(regex, text)]
    if re.search(r"\b(superficie|metros?|m2)\b", text) and not any(
        key in found for key in ("util", "total", "terreno", "construida")
    ):
        found.extend(("util", "total"))
    return found


def answer_public_property_question(message, property_doc, *, campaign_context=None):
    if not property_doc:
        return None
    normalized = _normalized(message)
    keys = requested_public_fact_keys(message)
    private = bool(_PRIVATE.search(normalized))
    asks_drop = bool(re.search(
        r"\b(cuanto\s+(bajo|rebajaron|rebajo)|rebaja\s+de\s+precio|"
        r"cuantas?\s+uf\s+(bajo|bajaron)|bajo\s+de\s+precio)\b", normalized
    ))
    asks_negotiation = bool(re.search(
        r"\b(negociar|negociable|negociacion|contraoferta|hacer\s+una\s+oferta)\b",
        normalized
    ))
    explicitly_full = bool(re.search(
        r"\b(toda\s+(?:la\s+)?(?:ficha|informacion)|informacion\s+completa|"
        r"ficha\s+(?:tecnica|completa)|todos?\s+los\s+(?:datos|detalles)|"
        r"cuentame\s+todo|resumen\s+completo|caracteristicas\s+completas)\b",
        normalized,
    ))
    # '¿La ficha dice si acepta mascotas?' is a single-field question,
    # not a request to overwrite the answer with an entire fact sheet.
    full = bool(_FULL.search(normalized)) and (
        explicitly_full or (not keys and not private)
    )
    if not (keys or full or private or asks_drop or asks_negotiation):
        return None
    facts = public_property_facts(property_doc)
    if full:
        response = public_property_summary(property_doc)
    else:
        response = "\n".join(_line(k,facts) for k in keys if not (asks_drop and k == "precio"))
    if asks_drop:
        ctx = campaign_context or {}
        old_price, new_price = ctx.get("old_price_uf"), ctx.get("new_price_uf")
        if ctx.get("version") == "price_drop_leads_v2" and old_price is not None and new_price is not None:
            reduction = float(old_price) - float(new_price)
            drop_text = (
                "La propiedad bajó " + _num(reduction) + " UF: de UF "
                + _num(old_price) + " a UF " + _num(new_price) + "."
            ) if reduction > 0 else "No tengo una rebaja adicional verificada."
        else:
            drop_text = "No tengo una rebaja confirmada para informar; precio actual: " + facts["precio"] + "."
        response = (response + "\n" if response else "") + drop_text
    if asks_negotiation:
        negotiation = ("Una posible negociación debe evaluarla administración con "
                       "el propietario; no puedo confirmar descuentos adicionales.")
        response = (response + "\n" if response else "") + negotiation
    if private:
        response += ("\n" if response else "") + (
            "Por privacidad no compartimos la dirección exacta ni los datos "
            "personales o de contacto del propietario. Puedo indicar la comuna "
            "y ayudar a coordinar una visita.")
    return response or None
