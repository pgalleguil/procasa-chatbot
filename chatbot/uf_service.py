"""uf_service.py — Servicio común de UF.

Fuente única para obtener, validar, cachear y convertir la UF.

REGLAS DE ARQUITECTURA:
  - La API Mindicador se consulta UNA VEZ por ejecución del proceso periódico.
  - NUNCA se llama la API desde una búsqueda RAG.
  - NUNCA se llama la API una vez por propiedad.
  - El scraper usa SOLO el último valor válido persistido (uf_cache), no la API.
  - Si no hay UF cache válida, se guarda el precio original sin derivado (warning).

Conversión (regla absoluta):
  - moneda_publicada=CLP -> precio_clp=ORIGINAL (jamás cambia), precio_uf=DERIVADO.
  - moneda_publicada=UF  -> precio_uf=ORIGINAL (jamás cambia), precio_clp=DERIVADO.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger("uf.service")

UF_API_URL = "https://mindicador.cl/api/uf"
UF_HTTP_TIMEOUT = 8  # segundos, corto y explícito
UF_CACHE_COLLECTION = "uf_cache"
UF_CACHE_ID = "uf_actual"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# OBTENCIÓN DESDE MINDICADOR (solo proceso periódico)
# ──────────────────────────────────────────────────────────────────────────────

def obtener_uf_actual(timeout: int = UF_HTTP_TIMEOUT):
    """Consulta mindicador.cl UNA vez y devuelve la UF más reciente VÁLIDA.

    Devuelve dict {valor, fecha, fuente} o None si falla (nunca lanza).
    NO confía en serie[0]: recorre la serie y elige el registro más reciente
    con valor > 0 y fecha presente.
    """
    req = urllib.request.Request(UF_API_URL, headers={"User-Agent": "procasa-uf/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning("[UF] Mindicador HTTP %s", resp.status)
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("[UF] Mindicador request failed: %s", exc)
        return None

    serie = payload.get("serie") or []
    candidatos = []
    for reg in serie:
        fecha_raw = reg.get("fecha")
        valor_raw = reg.get("valor")
        try:
            valor = float(valor_raw)
        except (TypeError, ValueError):
            continue
        if not valor or valor <= 0:
            continue
        try:
            fecha = datetime.fromisoformat(str(fecha_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        candidatos.append((fecha, valor))

    if not candidatos:
        logger.warning("[UF] Mindicador serie sin registros válidos")
        return None

    candidatos.sort(key=lambda x: x[0], reverse=True)
    fecha, valor = candidatos[0]
    return {"valor": valor, "fecha": fecha.isoformat(), "fuente": "mindicador.cl"}


# ──────────────────────────────────────────────────────────────────────────────
# CACHE PERSISTENTE
# ──────────────────────────────────────────────────────────────────────────────

def _uf_cache_coll(db=None):
    if db is None:
        from .storage import get_db
        db = get_db()
    return db[UF_CACHE_COLLECTION]


def leer_uf_cache(db=None):
    """Último valor UF válido persistido. Devuelve dict o None."""
    try:
        coll = _uf_cache_coll(db)
        doc = coll.find_one({"_id": UF_CACHE_ID}) or {}
        valor = doc.get("valor")
        if not valor or float(valor) <= 0:
            return None
        return {
            "valor": float(valor),
            "fecha": doc.get("fecha"),
            "fuente": doc.get("fuente", "cache"),
            "actualizado_at": doc.get("actualizado_at"),
        }
    except Exception as exc:
        logger.warning("[UF] leer_uf_cache failed: %s", exc)
        return None


def persistir_uf_cache(valor: float, fecha: str, fuente: str = "mindicador.cl",
                       db=None, extra: dict | None = None) -> None:
    """Persiste el último UF válido. Solo el proceso periódico la llama con éxito."""
    doc = {
        "valor": float(valor),
        "fecha": fecha,
        "fuente": fuente,
        "actualizado_at": _utcnow().isoformat(),
    }
    if extra:
        doc.update(extra)
    try:
        _uf_cache_coll(db).update_one({"_id": UF_CACHE_ID}, {"$set": doc}, upsert=True)
    except Exception as exc:
        logger.warning("[UF] persistir_uf_cache failed: %s", exc)


def obtener_uf_cache_o_fallback(db=None):
    """UF para usarse en scraper/RAG: cache persistida -> env config -> None.

    NUNCA llama a la API. Devuelve dict {valor, fecha} o None si no hay
    valor válido disponible.
    """
    cached = leer_uf_cache(db)
    if cached:
        return cached
    # Fallback a configuración por env (fuente única UF_VALUE, alias UF_VALOR_CLP)
    try:
        from config import Config
        valor = float(getattr(Config, "UF_VALUE", 0) or 0)
        if valor > 0:
            return {"valor": valor, "fecha": getattr(Config, "UF_FECHA", "") or ""}
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# CONVERSIÓN + METADATA
# ──────────────────────────────────────────────────────────────────────────────

def convertir_precio(moneda_publicada, precio_publicado, uf_valor):
    """Devuelve (precio_uf, precio_clp) aplicando la regla absoluta.

    El precio publicado se conserva exacto; el derivado se calcula.
    Devuelve (None, None) si no hay uf_valor válido o entrada inválida.
    """
    if not uf_valor or float(uf_valor) <= 0:
        return None, None
    try:
        p = float(precio_publicado)
    except (TypeError, ValueError):
        return None, None
    if not p or p <= 0:
        return None, None
    moneda = str(moneda_publicada or "").strip().upper()
    if moneda == "CLP":
        return round(p / float(uf_valor), 1), int(round(p))
    if moneda == "UF":
        return round(p, 1), int(round(p * float(uf_valor)))
    return None, None


def build_metadata(moneda_publicada, precio_publicado, uf_valor, uf_fecha,
                   precio_uf, precio_clp) -> dict:
    """Metadata de auditoría de la conversión (se guarda junto al precio)."""
    moneda = str(moneda_publicada or "").strip().upper()
    derivado = precio_uf if moneda == "CLP" else precio_clp
    moneda_derivado = "UF" if moneda == "CLP" else "CLP"
    return {
        "moneda_publicada": moneda,
        "precio_publicado": float(precio_publicado),
        "uf_valor_conversion": float(uf_valor),
        "uf_fecha_conversion": uf_fecha or "",
        "precio_derivado": derivado,
        "precio_derivado_moneda": moneda_derivado,
    }


def completar_precio(precio_obj: dict | None, uf_valor, uf_fecha):
    """Toma un precio_venta/precio_arriendo crudo del scraper y lo completa.

    El objeto de entrada refleja SOLO la divisa publicada (ej. {"precio_uf": X}).
    Salida: mismo dict + divisa derivada + metadata. Si la UF no está
    disponible, devuelve el dict original SIN derivado (warning) para que el
    próximo ciclo periódico lo complete.
    """
    if not isinstance(precio_obj, dict):
        return precio_obj

    uf_precio = precio_obj.get("precio_uf")
    clp_val = precio_obj.get("precio_clp")
    moneda = None
    publicado = None
    if clp_val is not None and str(clp_val).strip() not in ("", "None") and float(clp_val or 0) > 0:
        moneda = "CLP"
        publicado = float(clp_val)
    elif uf_precio is not None and str(uf_precio).strip() not in ("", "None") and float(uf_precio or 0) > 0:
        moneda = "UF"
        publicado = float(uf_precio)

    if moneda is None:
        return precio_obj

    # Si ya hay metadata de conversión previa, la moneda publicada es la que
    # quedó registrada (no se re-deriva desde el derivado).
    meta_prev = precio_obj.get("moneda_publicada")
    if meta_prev and meta_prev in ("UF", "CLP"):
        moneda = meta_prev
        publicado = precio_obj.get("precio_publicado") or publicado

    if not uf_valor or float(uf_valor) <= 0:
        logger.warning("[UF] Sin UF cache válida; precio original sin derivado: "
                       "moneda=%s publicado=%s", moneda, publicado)
        return precio_obj

    precio_uf, precio_clp = convertir_precio(moneda, publicado, uf_valor)
    if precio_uf is None:
        return precio_obj

    out = dict(precio_obj)
    out["precio_uf"] = precio_uf
    out["precio_clp"] = precio_clp
    out["moneda_publicada"] = moneda
    out["precio_publicado"] = float(publicado)
    out["uf_valor_conversion"] = float(uf_valor)
    out["uf_fecha_conversion"] = uf_fecha or ""
    derivado = precio_uf if moneda == "CLP" else precio_clp
    out["precio_derivado"] = derivado
    out["precio_derivado_moneda"] = "UF" if moneda == "CLP" else "CLP"
    return out
