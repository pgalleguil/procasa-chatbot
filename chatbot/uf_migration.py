"""uf_migration.py — Migración inicial + actualización periódica de derivados.

SOLO toca propiedades ACTIVAS (disponible_prop360=True OR disponible=True).
Regla absoluta:
  - moneda_publicada=CLP -> recalcula SOLO precio_uf (derivado); CLP original intacto.
  - moneda_publicada=UF  -> recalcula SOLO precio_clp (derivado); UF original intacto.
  - Docs con AMBOS precios y sin metadata -> moneda indeterminada; NO se tocan
    sus precios, solo se documenta.
"""
from __future__ import annotations

import json
import logging
import os
import traceback
from datetime import datetime

logger = logging.getLogger("uf.migration")

ACTIVE = {"$or": [{"disponible_prop360": True}, {"disponible": True}]}


def _now_local_iso() -> str:
    try:
        from .constants import CHILE_TZ
        return datetime.now(CHILE_TZ).isoformat()
    except Exception:
        return datetime.utcnow().isoformat()


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


def _backup_dir() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bdir = os.path.join(base, "backups")
    os.makedirs(bdir, exist_ok=True)
    return bdir


def _registrar_batch(db, batch_id, uf_valor, uf_fecha, esperados) -> None:
    """Registra el batch de migración en Mongo (auditoría/rollback)."""
    doc = {
        "_id": batch_id,
        "tipo": "migracion_uf_clp",
        "uf_valor": float(uf_valor),
        "uf_fecha": uf_fecha,
        "esperado_clp_a_uf": esperados.get("clp_a_uf", 0),
        "esperado_uf_a_clp": esperados.get("uf_a_clp", 0),
        "creado_at": _now_local_iso(),
    }
    try:
        db["uf_migraciones"].update_one({"_id": batch_id}, {"$set": doc}, upsert=True)
    except Exception as exc:
        logger.warning("[UF-MIGR] registrar_batch failed: %s", exc)


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
    """Migración inicial o actualización periódica de derivados.

    dry_run=True -> NO escribe. Solo devuelve métricas.
    dry_run=False -> crea backup ANTES de escribir, migra, registra batch.
    """
    from .uf_service import convertir_precio
    coll = db["universo_cartera_prop360"]
    batch_id = f"uf_buge_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    clp_a_uf = 0
    uf_a_clp = 0
    indeterminados = 0
    ya_ambos = 0
    originales_alterados = 0
    muestras_uf = []
    muestras_clp = []
    sin_precio = 0
    backup_rows = []
    writes = []  # (codigo, set_field, nuevo_precio)

    cursor = coll.find(ACTIVE)
    for d in cursor:
        to = d.get("tipo_operacion") or {}
        is_venta = to.get("venta") is True
        is_arriendo = to.get("arriendo") is True
        cod = d.get("codigo")
        if is_venta:
            op = "Venta"
            precio = _precios_doc("Venta", to)
        elif is_arriendo:
            op = "Arriendo"
            precio = _precios_doc("Arriendo", to)
        else:
            continue  # Arr.Temp. fuera de scope

        if not precio:
            sin_precio += 1
            continue

        metadata = None
        if precio.get("moneda_publicada") in ("UF", "CLP"):
            metadata = {"moneda_publicada": precio.get("moneda_publicada"),
                        "precio_publicado": precio.get("precio_publicado")}
        moneda, publicado = _clasificar(precio.get("precio_uf"), precio.get("precio_clp"), metadata)

        # Backup del precio ANTES de cualquier escritura
        backup_rows.append({
            "codigo": cod, "operacion": op,
            "precio_venta": to.get("precio_venta"),
            "precio_arriendo": to.get("precio_arriendo"),
            "moneda_publicada": moneda,
        })

        if moneda is None:
            if precio.get("precio_uf") and precio.get("precio_clp"):
                ya_ambos += 1
            else:
                indeterminados += 1
            continue

        precio_uf, precio_clp = convertir_precio(moneda, publicado, uf_valor)
        if precio_uf is None:
            indeterminados += 1
            continue

        # Verificar que NO se altera el original
        nuevo_uf = precio.get("precio_uf")
        nuevo_clp = precio.get("precio_clp")
        if moneda == "CLP":
            if nuevo_clp is not None and abs(float(nuevo_clp) - publicado) > 0.5:
                originales_alterados += 1
            clp_a_uf += 1
            if len(muestras_clp) < 20:
                muestras_clp.append({
                    "codigo": cod, "operacion": op, "moneda_publicada": "CLP",
                    "precio_publicado": publicado, "precio_uf_derivado": precio_uf,
                    "precio_clp": precio_clp, "uf": uf_valor,
                })
        else:  # UF
            if nuevo_uf is not None and abs(float(nuevo_uf) - publicado) > 0.05:
                originales_alterados += 1
            uf_a_clp += 1
            if len(muestras_uf) < 20:
                muestras_uf.append({
                    "codigo": cod, "operacion": op, "moneda_publicada": "UF",
                    "precio_publicado": publicado, "precio_uf": precio_uf,
                    "precio_clp_derivado": precio_clp, "uf": uf_valor,
                })

        if dry_run:
            continue

        nuevo_precio = dict(precio)
        nuevo_precio["precio_uf"] = precio_uf
        nuevo_precio["precio_clp"] = precio_clp
        nuevo_precio["moneda_publicada"] = moneda
        nuevo_precio["precio_publicado"] = float(publicado)
        nuevo_precio["uf_valor_conversion"] = float(uf_valor)
        nuevo_precio["uf_fecha_conversion"] = uf_fecha or ""
        nuevo_precio["precio_derivado"] = precio_uf if moneda == "CLP" else precio_clp
        nuevo_precio["precio_derivado_moneda"] = "UF" if moneda == "CLP" else "CLP"
        set_field = f"tipo_operacion.precio_{'venta' if op == 'Venta' else 'arriendo'}"
        writes.append((cod, set_field, nuevo_precio))

    result = {
        "batch_id": batch_id,
        "uf_valor": float(uf_valor),
        "uf_fecha": uf_fecha,
        "clp_a_uf": clp_a_uf,
        "uf_a_clp": uf_a_clp,
        "total_conversiones": clp_a_uf + uf_a_clp,
        "indeterminados_ambos": ya_ambos,
        "indeterminados_otros": indeterminados,
        "sin_precio": sin_precio,
        "originales_alterados": originales_alterados,
        "muestras_clp": muestras_clp,
        "muestras_uf": muestras_uf,
    }

    if dry_run:
        return result

    # 1) BACKUP ANTES de escribir
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(_backup_dir(), f"precios_universo_{ts}.json")
    try:
        with open(backup_path, "w", encoding="utf-8") as fh:
            json.dump({"batch_id": batch_id, "uf_valor": uf_valor,
                       "uf_fecha": uf_fecha, "rows": backup_rows},
                      fh, ensure_ascii=False, default=str, indent=2)
        result["backup_path"] = backup_path
    except Exception as exc:
        logger.warning("[UF-MIGR] backup file failed: %s", exc)
        result["backup_path"] = None

    # 2) Escribir
    for cod, set_field, nuevo_precio in writes:
        coll.update_one({"codigo": cod}, {"$set": {set_field: nuevo_precio}})

    # 3) Registrar batch
    _registrar_batch(db, batch_id, uf_valor, uf_fecha,
                     {"clp_a_uf": clp_a_uf, "uf_a_clp": uf_a_clp})

    return result
