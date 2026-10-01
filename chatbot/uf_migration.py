"""Legacy UF migration diagnostics.

The write entry point `migrar()` is permanently disabled. Portfolio price
updates are performed only by the explicitly invoked local Prop360 scraper.
`analizar()` remains a read-only legacy report.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("uf.migration")

ACTIVE = {"$or": [{"disponible_prop360": True}, {"disponible": True}]}


def _price_clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        try:
            return float(s.replace(".", "").replace(",", "."))
        except (TypeError, ValueError):
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clasificar(uf, clp, metadata=None):
    """Devuelve (moneda_publicada, precio_publicado) o (None, None).

    Prioriza metadata previa (moneda_publicada). Si no hay metadata, infiere:
      - Solo UF -> ("UF", uf)
      - Solo CLP -> ("CLP", clp)
      - Ambos -> (None, None) indeterminado
      - Ninguno -> (None, None)
    """
    # Metadata previa manda (idempotencia / no re-derivar desde derivado)
    if metadata:
        meta_moneda = metadata.get("moneda_publicada")
        meta_orig = metadata.get("precio_publicado")
        if meta_moneda in ("UF", "CLP") and meta_orig:
            return meta_moneda, float(meta_orig)

    u = _price_clean(uf)
    c = _price_clean(clp)
    if u is not None and u > 0 and c is not None and c > 0:
        return None, None  # indeterminado
    if u is not None and u > 0:
        return "UF", u
    if c is not None and c > 0:
        return "CLP", c
    return None, None


def _precios_doc(operacion: str, to: dict) -> dict | None:
    """Devuelve el objeto precio (precio_venta/precio_arriendo) o None."""
    if operacion == "Venta":
        p = to.get("precio_venta")
        return p if isinstance(p, dict) else None
    if operacion == "Arriendo":
        p = to.get("precio_arriendo")
        return p if isinstance(p, dict) else None
    return None


def analizar(db, uf_valor: float, uf_fecha: str) -> dict:
    """Scan read-only: conteos y muestras. No escribe nada."""
    from .uf_service import convertir_precio, build_metadata
    coll = db["universo_cartera_prop360"]
    venta = {"uf_orig": [], "clp_orig": [], "ambos": 0, "indet": 0}
    arriendo = {"uf_orig": [], "clp_orig": [], "ambos": 0, "indet": 0}
    arr_temp = 0
    deriv_clp = 0
    deriv_uf = 0

    for d in coll.find(ACTIVE):
        to = d.get("tipo_operacion") or {}
        is_venta = to.get("venta") is True
        is_arriendo = to.get("arriendo") is True
        cod = d.get("codigo")
        if is_venta:
            precio = _precios_doc("Venta", to)
            bucket = venta
            op = "Venta"
        elif is_arriendo:
            precio = _precios_doc("Arriendo", to)
            bucket = arriendo
            op = "Arriendo"
        else:
            arr_temp += 1
            continue

        if not precio:
            bucket["indet"] += 1
            continue
        moneda = precio.get("moneda_publicada")
        metadata = None
        if moneda in ("UF", "CLP"):
            metadata = {"moneda_publicada": moneda,
                        "precio_publicado": precio.get("precio_publicado")}
        m, publicado = _clasificar(precio.get("precio_uf"), precio.get("precio_clp"), metadata)
        if m == "UF":
            bucket["uf_orig"].append((cod, publicado))
            deriv_clp += 1
        elif m == "CLP":
            bucket["clp_orig"].append((cod, publicado))
            deriv_uf += 1
        elif precio.get("precio_uf") and precio.get("precio_clp"):
            bucket["ambos"] += 1
        else:
            bucket["indet"] += 1

    total_v = len(venta["uf_orig"]) + len(venta["clp_orig"]) + venta["ambos"] + venta["indet"]
    total_a = len(arriendo["uf_orig"]) + len(arriendo["clp_orig"]) + arriendo["ambos"] + arriendo["indet"]
    return {
        "venta": {k: (len(v) if isinstance(v, list) else v) for k, v in venta.items()},
        "venta_total": total_v,
        "arriendo": {k: (len(v) if isinstance(v, list) else v) for k, v in arriendo.items()},
        "arriendo_total": total_a,
        "arr_temp": arr_temp,
        "deriv_clp_a_crear": deriv_clp,
        "deriv_uf_a_crear": deriv_uf,
    }


def migrar(db, uf_valor: float, uf_fecha: str, dry_run: bool = True) -> dict:
    """Migrador heredado permanentemente deshabilitado.

    La actualización de precios se realiza únicamente desde el scraper local,
    que verifica la ficha editable y valida una UF para esa ejecución. Este
    stub no consulta ni escribe la cartera, incluso si se invoca con
    ``dry_run=False``.
    """
    logger.warning("[UF-MIGR] Migrador heredado deshabilitado; sin lectura/escritura de cartera")
    return {
        "status": "disabled",
        "reason": "portfolio_prices_are_updated_only_by_manual_scraper",
        "uf_valor": uf_valor,
        "uf_fecha": uf_fecha,
        "clp_a_uf": 0,
        "uf_a_clp": 0,
        "total_conversiones": 0,
        "dry_run": True,
        "writes": 0,
    }
