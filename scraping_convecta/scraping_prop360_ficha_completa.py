"""
scraping_prop360_ficha_completa.py
──────────────────────────────────
Scraper experimental de fichas completas Prop360.

IMPORTANTE
- NO modifica ningún archivo existente.
- NO escribe en universo_cartera.
- Escribe únicamente en la colección de prueba: prop360_ficha_completa_test
- Reutiliza Login / MongoDB / Config / Playwright del stack existente.

MODOS DE EJECUCIÓN  (ajustar RUN_MODE antes de correr)
────────────────────────────────────────────────────────
RUN_MODE = {"scope": "validacion"}                    # Primeras 10 propiedades (pruebas rápidas)
# Otras opciones: codigo, nuevas, actualizar, toda_sucre, toda_cartera, validacion

URL de cada ficha:
  https://procasa.prop360.cl/backOffice/propiedades/propEditar?i={codigo}
"""

import asyncio
import os
import re
import sys
import json
import logging
import time
import urllib.request
import hashlib
from datetime import datetime, timezone

# ──────────────────────────────────────────────────────────────────────────────
# DEPENDENCIAS CON CHEQUEO EXPLÍCITO
# ──────────────────────────────────────────────────────────────────────────────
_missing = []

try:
    from playwright.async_api import async_playwright, Page
except ModuleNotFoundError:
    _missing.append("playwright")

try:
    from playwright_stealth import Stealth
except ModuleNotFoundError:
    _missing.append("playwright-stealth")

try:
    from pymongo import MongoClient
except ModuleNotFoundError:
    _missing.append("pymongo")

if _missing:
    print(
        "ERROR: faltan dependencias: "
        + ", ".join(_missing)
        + "\nInstala con: python -m pip install "
        + " ".join(_missing)
    )
    if "playwright" in _missing:
        print("Luego ejecuta: python -m playwright install chromium")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# IMPORTAR CONFIG  (misma lógica que scraping_prop360_portales.py)
# ──────────────────────────────────────────────────────────────────────────────
sys.path.append(os.path.join(os.path.dirname(__file__), "ChatBot_v4_Grok"))
try:
    from config import Config
except ImportError:
    print(
        "ERROR: No se pudo importar Config. "
        "Asegúrate de que el script esté en la raíz del proyecto."
    )
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN GLOBAL
# ──────────────────────────────────────────────────────────────────────────────
LOGIN_URL      = "https://procasa.prop360.cl/"
FICHA_URL_TPL  = "https://procasa.prop360.cl/backOffice/propiedades/propEditar?i={codigo}"

# Credenciales  (mismas que scraping_prop360_portales.py)
USERNAME = "pgalleguillos@procasa.cl"
PASSWORD = "Procasa.2026"
HEADLESS = True

# Colección de prueba — NO tocar universo_cartera
TEST_COLLECTION = "prop360_ficha_completa_test"

# ─── MODO DE EJECUCIÓN ───────────────────────────────────────────────────────
# Ajusta este dict antes de correr el script.
RUN_MODE: dict = {
    "scope": "codigo",
    "codigo": "7390",
    "inspect": True
}
# Otras opciones válidas:
# RUN_MODE = {"scope": "nuevas"}
# RUN_MODE = {"scope": "actualizar"}
# RUN_MODE = {"scope": "toda_sucre"}
# RUN_MODE = {"scope": "toda_cartera"}

# ─── DRY RUN & DEBUG JSON (MEJORA INSPECCIÓN) ───────────────────────────────
DRY_RUN: bool = False
DEBUG_JSON_DIR: str   = os.path.join(os.path.dirname(__file__), "debug_json")

# ─── DEBUG HTML & SCREENSHOTS ───────────────────────────────────────────────
SAVE_DEBUG_HTML: bool = True
DEBUG_HTML_DIR: str   = os.path.join(os.path.dirname(__file__), "debug_html")

SAVE_SCREENSHOTS: bool = True
DEBUG_SCREENSHOTS_DIR: str = os.path.join(os.path.dirname(__file__), "debug_screenshots")

# ─── SOPORTE FUTURO POR TIPO DE PROPIEDAD (MEJORA 9) ─────────────────────────
# Por ahora todos los handlers son None.
# Más adelante se agregarán extractores específicos por tipo.
# Ejemplo:  PROPERTY_TYPE_HANDLERS["Parcela"] = extract_parcela_extra
PROPERTY_TYPE_HANDLERS: dict = {
    "Casa":             None,
    "Departamento":     None,
    "Parcela":          None,
    "Sitio":            None,
    "Oficina":          None,
    "Local Comercial":  None,
    "Industrial":       None,
}

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ficha_completa")


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 1 — MONGODB
# Reutiliza Config.MONGO_URI y Config.DB_NAME, igual que sync_convecta_master.py
# ══════════════════════════════════════════════════════════════════════════════

def get_mongo_collection(collection_name: str):
    """
    Devuelve (client, colección) usando la misma Config que el resto del stack.
    Patrón idéntico al de sync_convecta_master.py:
        client = MongoClient(Config.MONGO_URI)
        db = client[Config.DB_NAME]
    """
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    return client, db[collection_name]


def get_source_collection():
    """
    Colección fuente de propiedades (universo_cartera).
    Solo lectura — igual que en scraping_prop360_portales.py.
    """
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    return client, db["universo_cartera"]


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 2 — OBTENCIÓN DE CÓDIGOS DESDE universo_cartera
# Reutiliza la colección y campos que pobló sync_convecta_master.py
# ══════════════════════════════════════════════════════════════════════════════

def get_codigos(run_mode: dict, test_coll) -> list[str]:
    """
    Devuelve la lista de códigos a procesar según RUN_MODE.

    Los datos provienen de universo_cartera (poblada por sync_convecta_master.py).
    Campos usados:
        - codigo       → código único de la propiedad
        - oficina      → "PROCASA SUCRE" para filtrar por oficina
        - disponible   → True / False

    test_coll se usa para:
      - modo "nuevas"    → detectar qué códigos ya existen en test_coll
      - modo "actualizar"→ leer qué códigos existen, ordenados por fecha_scraping asc
    """
    src_client, src_coll = get_source_collection()
    scope = run_mode.get("scope", "toda_sucre")

    try:
        if scope == "codigo":
            # ── Un único código especificado manualmente ───────────────────────
            codigo = str(run_mode.get("codigo", "")).strip()
            if not codigo:
                raise ValueError("RUN_MODE 'codigo' requiere la clave 'codigo'.")
            log.info(f"Scope: código único → {codigo}")
            return [codigo]

        elif scope == "nuevas":
            # ── MEJORA 1: toda la cartera de universo_cartera menos los ya ────
            # ── procesados en prop360_ficha_completa_test ─────────────────────
            #
            # Ejemplo:  universo_cartera = 1000 props
            #           test_coll        =  200 docs
            #           nuevas           =  800 props
            #
            # NO filtra por oficina: sirve para toda la cartera disponible.
            # Si quieres solo SUCRE usa scope="toda_sucre" la primera vez.
            codigos_ya_en_test = {
                str(doc["codigo"])
                for doc in test_coll.find({}, {"codigo": 1})
                if doc.get("codigo")
            }
            total_origen = src_coll.count_documents({"disponible": True})
            query = {
                "disponible": True,
                "codigo": {"$nin": list(codigos_ya_en_test)},
            }
            docs = list(src_coll.find(query, {"codigo": 1}))
            codigos = [str(d["codigo"]) for d in docs if d.get("codigo")]
            log.info(
                f"Scope: nuevas → universo_cartera={total_origen} | "
                f"ya en test={len(codigos_ya_en_test)} | "
                f"pendientes={len(codigos)}"
            )
            return codigos

        elif scope == "actualizar":
            # ── MEJORA 2: refrescar las fichas ya existentes ──────────────────
            # Ordena por metadata.fecha_scraping ASC → las más antiguas primero.
            # Permite refrescar progresivamente sin repetir siempre los mismos.
            docs_test = list(
                test_coll.find(
                    {},
                    {"codigo": 1, "metadata.fecha_scraping": 1}
                ).sort("metadata.fecha_scraping", 1)   # 1 = ascendente (más antiguo primero)
            )
            codigos = [str(d["codigo"]) for d in docs_test if d.get("codigo")]
            log.info(
                f"Scope: actualizar → {len(codigos)} fichas en test_coll "
                f"(ordenadas por fecha_scraping ASC, más antiguas primero)."
            )
            return codigos

        elif scope == "toda_sucre":
            # ── Toda la oficina PROCASA SUCRE disponible ──────────────────────
            query = {"disponible": True, "oficina": "PROCASA SUCRE"}
            docs = list(src_coll.find(query, {"codigo": 1}))
            codigos = [str(d["codigo"]) for d in docs if d.get("codigo")]
            log.info(f"Scope: toda_sucre → {len(codigos)} propiedades.")
            return codigos

        elif scope == "toda_cartera":
            # ── Toda la cartera disponible ────────────────────────────────────
            query = {"disponible": True}
            docs = list(src_coll.find(query, {"codigo": 1}))
            codigos = [str(d["codigo"]) for d in docs if d.get("codigo")]
            log.info(f"Scope: toda_cartera → {len(codigos)} propiedades.")
            return codigos

        elif scope == "validacion":
            # ── Procesar únicamente las primeras 10 propiedades (pruebas rápidas)
            query = {"disponible": True}
            docs = list(src_coll.find(query, {"codigo": 1}).limit(10))
            codigos = [str(d["codigo"]) for d in docs if d.get("codigo")]
            log.info(f"Scope: validacion → {len(codigos)} propiedades (límite 10).")
            return codigos

        else:
            raise ValueError(
                f"Scope desconocido: '{scope}'. "
                "Usa: codigo, nuevas, actualizar, toda_sucre, toda_cartera."
            )

    finally:
        src_client.close()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 3 — HELPERS DE EXTRACCIÓN (genéricos, sin lanzar excepciones)
# Toda función devuelve None si el elemento no existe o hay error.
# ══════════════════════════════════════════════════════════════════════════════

async def _check_and_log(page: Page, locator_str: str, codigo: str, campo: str, audit: dict) -> bool:
    if audit is not None and campo and campo not in audit.get("campos_esperados", []):
        audit.setdefault("campos_esperados", []).append(campo)

    try:
        if await page.locator(locator_str).count() > 0:
            return True
    except Exception:
        pass
    
    if codigo and campo:
        log.warning(f"[WARN] codigo={codigo} campo={campo} selector no existe ({locator_str})")
    if audit is not None:
        if campo and campo not in audit["campos_vacios"]:
            audit["campos_vacios"].append(campo)
        if locator_str not in audit["selectors_failed"]:
            audit["selectors_failed"].append(locator_str)
    return False

async def _log_empty(codigo: str, campo: str, audit: dict):
    if codigo and campo:
        log.warning(f"[WARN] codigo={codigo} campo={campo} selector existe pero vacío")
    if audit is not None:
        if campo and campo not in audit["campos_vacios"]:
            audit["campos_vacios"].append(campo)

def _clean_value(val: str | None) -> str | None:
    if not val:
        return None
    v_lower = val.lower().strip()
    if v_lower in [
        "seleccione...", "seleccione", "seleccionar", "- seleccione -", 
        "selecciona", "seleccionar...", "", "0", "0.0", ".", "-", "vacío", "vacio"
    ]:
        return None
    return val

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def normalize_text_for_compare(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        try:
            val = json.dumps(val, sort_keys=True, ensure_ascii=False)
        except Exception:
            val = str(val)
    s = str(val).strip().lower()
    s = re.sub(r"\s+", " ", s)
    if s in ("", "-", "nan", "none", "null"):
        return None
    return s

def normalize_numeric_for_compare(val):
    if val is None:
        return None
    s = str(val).strip()
    if s in ("", "-", "nan", "none", "null"):
        return None
    s = s.replace("$", "").replace("uf", "").replace("clp", "")
    s = s.replace(".", "").replace(" ", "")
    s = s.replace(",", ".")
    try:
        if "." in s:
            return float(s)
        return int(s)
    except Exception:
        return None

def price_change_is_material(old_val, new_val, threshold_pct=1.0):
    old_num = normalize_numeric_for_compare(old_val)
    new_num = normalize_numeric_for_compare(new_val)
    if old_num is None or new_num is None:
        return True
    if old_num == 0:
        return new_num != 0
    return abs(new_num - old_num) / abs(old_num) * 100.0 >= threshold_pct

def deduplicate_historial_cambios(historial):
    limpio = []
    vistos = set()
    for entry in historial or []:
        if not isinstance(entry, dict):
            continue
        campo = entry.get("campo")
        if not campo:
            continue
        valor_anterior = entry.get("valor_anterior")
        valor_nuevo = entry.get("valor_nuevo")
        if normalize_text_for_compare(valor_anterior) == normalize_text_for_compare(valor_nuevo):
            continue
        if campo in ("precio_clp", "precio_uf") and not price_change_is_material(valor_anterior, valor_nuevo):
            continue
        key = (
            campo,
            normalize_text_for_compare(valor_anterior) if normalize_text_for_compare(valor_anterior) is not None else normalize_numeric_for_compare(valor_anterior),
            normalize_text_for_compare(valor_nuevo) if normalize_text_for_compare(valor_nuevo) is not None else normalize_numeric_for_compare(valor_nuevo),
        )
        if key in vistos:
            continue
        vistos.add(key)
        limpio.append({
            "fecha": entry.get("fecha") or now_iso(),
            "campo": campo,
            "valor_anterior": valor_anterior,
            "valor_nuevo": valor_nuevo,
        })
    return limpio

def deep_sort(value):
    if isinstance(value, dict):
        return {k: deep_sort(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [deep_sort(v) for v in value]
    return value

def strip_volatile_for_audit(value, parent_key=""):
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            if parent_key == "metadata" and k in {"fecha_scraping", "status"}:
                continue
            if parent_key == "metadata" and k in {"campos_vacios", "selectors_failed", "campos_esperados"}:
                continue
            if k in {"historial_cambios", "versiones", "versiones_historial"}:
                continue
            cleaned[k] = strip_volatile_for_audit(v, k)
        return cleaned
    if isinstance(value, list):
        return [strip_volatile_for_audit(v, parent_key) for v in value]
    return value

def canonical_for_audit(doc: dict) -> dict:
    return deep_sort(strip_volatile_for_audit(doc))

def audit_hash(doc: dict) -> str:
    payload = json.dumps(canonical_for_audit(doc), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def flatten_for_history(value, prefix=""):
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else k
            out.update(flatten_for_history(v, key))
        return out
    if isinstance(value, list):
        normalized_list = [strip_volatile_for_audit(v) for v in value]
        out[prefix] = normalized_list
        return out
    out[prefix] = value
    return out

def normalize_price_pair(clp_val=None, uf_val=None, uf_rate=None):
    clp = clean_price(clp_val)
    uf = None

    if uf_val is not None:
        uf_num = normalize_numeric_for_compare(uf_val)
        if uf_num is not None:
            uf = round(float(uf_num), 1)

    if uf is None and clp is not None and uf_rate:
        try:
            uf = round(float(clp) / float(uf_rate), 1)
        except Exception:
            uf = None

    if clp is None and uf is not None and uf_rate:
        try:
            clp = int(round(float(uf) * float(uf_rate)))
        except Exception:
            clp = None

    return clp, uf

def get_uf_rate():
    env_rate = os.getenv("UF_VALUE")
    if env_rate:
        try:
            return float(str(env_rate).replace(",", "."))
        except Exception:
            pass

    try:
        url = "https://mindicador.cl/api/uf"
        with urllib.request.urlopen(url, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        valor = payload.get("serie", [{}])[0].get("valor")
        if valor is not None:
            return float(valor)
    except Exception:
        return None

    return None

def canonicalize_prices(doc: dict):
    uf_rate = normalize_numeric_for_compare(doc.get("_uf_rate")) or get_uf_rate()
    clp = doc.get("precio_clp")
    uf = doc.get("precio_uf")

    clp_n, uf_n = normalize_price_pair(clp, uf, uf_rate)
    if clp_n is not None:
        doc["precio_clp"] = clp_n
    if uf_n is not None:
        doc["precio_uf"] = uf_n
    return doc

def get_tracked_value(doc: dict, field: str):
    if field in doc:
        return doc.get(field)
    if field == "precio_clp":
        return doc.get("precio_clp")
    if field == "precio_uf":
        return doc.get("precio_uf")
    return None

TRACKED_FIELDS = [
    "precio_clp",
    "precio_uf",
    "ejecutivo",
    "datos_propietario",
    "dormitorios",
    "banos",
    "m2_construida",
    "m2_utiles",
    "m2_terreno",
    "m2_total",
    "descripcion",
    "oficina",
    "ultima_actualizacion",
]

async def extract_ultima_actualizacion(page: Page, codigo: str, audit: dict) -> str | None:
    patterns = [
        r"Última actualización\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?)",
        r"Ultima actualizacion\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?)",
    ]
    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        return None
    for pattern in patterns:
        m = re.search(pattern, body_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def derive_oficina(codigo: str, estado: dict | None = None) -> str | None:
    oficina = None
    if isinstance(estado, dict):
        oficina = estado.get("oficina")
    return oficina or "PROCASA SUCRE"

def build_history(existing: dict | None, new_doc: dict) -> list[dict]:
    existing_hist = list(existing.get("historial_cambios", [])) if existing else []
    historial = deduplicate_historial_cambios(existing_hist)
    if not existing:
        return historial

    for field in TRACKED_FIELDS:
        old_val = get_tracked_value(existing, field)
        new_val = get_tracked_value(new_doc, field)
        if field in ("precio_clp", "precio_uf") and not price_change_is_material(old_val, new_val):
            continue
        if normalize_text_for_compare(old_val) == normalize_text_for_compare(new_val):
            continue
        if normalize_numeric_for_compare(old_val) == normalize_numeric_for_compare(new_val):
            continue
        historial.append({
            "fecha": now_iso(),
            "campo": field,
            "valor_anterior": old_val,
            "valor_nuevo": new_val,
        })
    return deduplicate_historial_cambios(historial)

def build_deep_history(existing: dict | None, new_doc: dict) -> list[dict]:
    existing_core = canonical_for_audit(existing or {})
    new_core = canonical_for_audit(new_doc or {})
    old_flat = flatten_for_history(existing_core)
    new_flat = flatten_for_history(new_core)
    keys = sorted(set(old_flat) | set(new_flat))

    historial = list(existing.get("historial_cambios", [])) if existing else []

    for key in keys:
        if key in {"metadata.fecha_scraping", "metadata.status"}:
            continue
        old_val = old_flat.get(key)
        new_val = new_flat.get(key)
        if normalize_text_for_compare(old_val) == normalize_text_for_compare(new_val):
            continue
        if normalize_numeric_for_compare(old_val) == normalize_numeric_for_compare(new_val):
            continue
        if key in {"precio_clp", "precio_uf"} and not price_change_is_material(old_val, new_val):
            continue
        historial.append({
            "fecha": now_iso(),
            "campo": key,
            "valor_anterior": old_val,
            "valor_nuevo": new_val,
        })

    return deduplicate_historial_cambios(historial)

def build_snapshot(doc: dict, version_type: str, hash_value: str) -> dict:
    snapshot = canonical_for_audit(doc)
    snapshot["__meta"] = {
        "fecha": now_iso(),
        "tipo": version_type,
        "hash": hash_value,
    }
    return snapshot

async def exists(page: Page, xpath: str) -> bool:
    try:
        return await page.locator(f"xpath={xpath}" if xpath.startswith("//") else xpath).count() > 0
    except Exception:
        return False

async def get_text(page: Page, xpath: str, codigo: str = "", campo: str = "", audit: dict = None) -> str | None:
    locator_str = f"xpath={xpath}" if xpath.startswith("//") else xpath
    if not await _check_and_log(page, locator_str, codigo, campo, audit):
        return None
    try:
        val = (await page.locator(locator_str).first.inner_text()).strip()
        val = _clean_value(val)
        if not val:
            await _log_empty(codigo, campo, audit)
            return None
        return val
    except Exception as e:
        log.debug(f"get_text({locator_str}): {e}")
        await _log_empty(codigo, campo, audit)
        return None

async def get_value(page: Page, xpath: str, codigo: str = "", campo: str = "", audit: dict = None) -> str | None:
    locator_str = f"xpath={xpath}" if xpath.startswith("//") else xpath
    if not await _check_and_log(page, locator_str, codigo, campo, audit):
        return None
    try:
        val = await page.locator(locator_str).first.input_value()
        val = _clean_value(val.strip() if val else None)
        if not val:
            await _log_empty(codigo, campo, audit)
            return None
        return val
    except Exception as e:
        log.debug(f"get_value({locator_str}): {e}")
        await _log_empty(codigo, campo, audit)
        return None

async def get_selected_option(page: Page, css_id: str, codigo: str = "", campo: str = "", audit: dict = None) -> str | None:
    if not await _check_and_log(page, css_id, codigo, campo, audit):
        return None
    try:
        val = await page.locator(css_id).input_value()
        option_text = await page.eval_on_selector(
            css_id,
            "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : null",
        )
        res = _clean_value(option_text) or _clean_value(val) or None
        if not res:
            await _log_empty(codigo, campo, audit)
            return None
        return res
    except Exception as e:
        log.debug(f"get_selected_option({css_id}): {e}")
        await _log_empty(codigo, campo, audit)
        return None

async def get_class(page: Page, xpath: str, codigo: str = "", campo: str = "", audit: dict = None) -> str | None:
    locator_str = f"xpath={xpath}" if xpath.startswith("//") else xpath
    if not await _check_and_log(page, locator_str, codigo, campo, audit):
        return None
    try:
        val = await page.locator(locator_str).first.get_attribute("class")
        if not val:
            await _log_empty(codigo, campo, audit)
            return None
        return val
    except Exception as e:
        log.debug(f"get_class({locator_str}): {e}")
        await _log_empty(codigo, campo, audit)
        return None


def safe_int(val: str | None) -> int | None:
    """Convierte a int sin lanzar excepción. None si falla."""
    if val is None:
        return None
    try:
        return int(str(val).replace(".", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def safe_float(val: str | None) -> float | None:
    """Convierte a float sin lanzar excepción. None si falla."""
    if val is None:
        return None
    try:
        return float(str(val).replace(".", "").replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def clean_price(raw: str | None) -> int | None:
    """
    Limpia un precio:  elimina puntos de miles, espacios y caracteres extraños.
    Devuelve int o None.
    """
    if raw is None:
        return None
    s = re.sub(r"[^\d]", "", str(raw))
    return int(s) if s else None


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 3b — HELPER DEBUG HTML (MEJORA 7)
# ══════════════════════════════════════════════════════════════════════════════

def save_debug_json(codigo: str, doc: dict) -> None:
    try:
        os.makedirs(DEBUG_JSON_DIR, exist_ok=True)
        filename = os.path.join(DEBUG_JSON_DIR, f"{codigo}.json")
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        log.debug(f"[{codigo}] JSON guardado → {filename}")
    except Exception as e:
        log.warning(f"[{codigo}] No se pudo guardar JSON: {e}")

async def save_debug_screenshot(page: Page, codigo: str, seccion: str) -> None:
    if not SAVE_SCREENSHOTS:
        return
    try:
        os.makedirs(DEBUG_SCREENSHOTS_DIR, exist_ok=True)
        filename = os.path.join(DEBUG_SCREENSHOTS_DIR, f"{codigo}_{seccion}.png")
        await page.screenshot(path=filename, full_page=True)
        log.debug(f"[{codigo}] Screenshot guardado → {filename}")
    except Exception as e:
        log.warning(f"[{codigo}] No se pudo guardar screenshot de '{seccion}': {e}")

async def save_debug_html(page: Page, codigo: str, seccion: str) -> None:
    """
    Guarda el HTML completo de la página actual en:
        debug_html/{codigo}_{seccion}.html

    Solo actúa si SAVE_DEBUG_HTML es True.
    Nunca lanza excepción: un fallo aquí no debe interrumpir el scraping.
    """
    if not SAVE_DEBUG_HTML:
        return
    try:
        os.makedirs(DEBUG_HTML_DIR, exist_ok=True)
        filename = os.path.join(DEBUG_HTML_DIR, f"{codigo}_{seccion}.html")
        html_content = await page.content()
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html_content)
        log.debug(f"[{codigo}] HTML guardado → {filename}")
    except Exception as e:
        log.warning(f"[{codigo}] No se pudo guardar HTML de '{seccion}': {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 4 — LOGIN
# Lógica idéntica a scraping_prop360_portales.py, con logs más descriptivos
# ══════════════════════════════════════════════════════════════════════════════

async def login(page: Page) -> None:
    """
    Autentica en Prop360.
    Estrategia: fill → Enter; si no redirige, clic en botón de ingreso.
    (Igual que en scraping_prop360_portales.py)
    """
    log.info("Navegando a login…")
    await page.goto(LOGIN_URL)
    await page.fill("#tbMail", USERNAME)
    await page.fill("#tbPassword", PASSWORD)
    await page.keyboard.press("Enter")

    try:
        await page.wait_for_url(
            re.compile(r".*/backoffice/.*", re.IGNORECASE), timeout=15_000
        )
        log.info("Sesión iniciada correctamente.")
    except Exception:
        log.warning("Enter no redirigió; intentando clic en botón de ingreso…")
        await page.click("#btnIngresar, .btn-login, button:has-text('Ingresar')")
        await page.wait_for_url(
            re.compile(r".*/backoffice/.*", re.IGNORECASE), timeout=20_000
        )
        log.info("Sesión iniciada (vía clic).")


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 5 — NAVEGACIÓN
# Acceso directo por URL (no requiere búsqueda en listado)
# ══════════════════════════════════════════════════════════════════════════════

async def navigate_to_ficha(page: Page, codigo: str) -> bool:
    """
    Navega directamente a la ficha de edición de la propiedad.
    URL:  https://procasa.prop360.cl/backOffice/propiedades/propEditar?i={codigo}

    Devuelve True si la carga fue exitosa (el formulario principal existe).
    """
    url = FICHA_URL_TPL.format(codigo=codigo)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Verificar que cargó la ficha real (presencia del formulario de tipo)
        await page.wait_for_selector("#form_tipo", timeout=15_000)
        return True
    except Exception as e:
        log.warning(f"[{codigo}] No se pudo cargar ficha: {e}")
        return False


async def click_tab(page: Page, tab_anchor: str, codigo: str) -> bool:
    try:
        selector = f"a[href='{tab_anchor}']"
        loc = page.locator(selector).first
        
        if await loc.count() == 0:
            log.warning(f"[WARN] codigo={codigo} pestaña={tab_anchor.replace('#tab_', '')} no encontrada")
            return False
        await loc.click()
        await asyncio.sleep(0.8)
        return True
    except Exception as e:
        log.warning(f"[WARN] codigo={codigo} Error al clicar pestaña '{tab_anchor}': {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 6 — EXTRACCIÓN POR PESTAÑAS
# Cada función devuelve un dict con los campos de esa pestaña.
# Ninguna lanza excepción hacia afuera: errores → log + None en el campo.
# ══════════════════════════════════════════════════════════════════════════════

# ─── 6.1 TIPO DE OPERACIÓN (pestaña principal / form_tipo) ───────────────────

async def extract_tipo_operacion(page: Page, codigo: str, audit: dict) -> dict:
    """
    Extrae el bloque Tipo de Operación de la ficha.

    Campos:
        tipo        → dropdown tipo de propiedad
        rol         → ROL-MANZANA (p.ej. "1234-56")
        venta       → True / False
        arriendo    → True / False
        precio_venta.precio_clp  (si moneda = CLP)
        precio_venta.precio_uf   (si moneda = UF)
        precio_arriendo.precio_clp
        precio_arriendo.precio_uf
        gastos_comunes
    """
    resultado = {}

    # ── TIPO DE PROPIEDAD ─────────────────────────────────────────────────────
    # Validar etiqueta antes de leer el valor
    label_tipo = await get_text(page, "//*[@id='fgTP']/label")
    if label_tipo and "tipo de propiedad" in label_tipo.lower():
        tipo_val = await get_selected_option(page, "#ddltp", codigo, "tipo", audit)
        if tipo_val:
            resultado["tipo"] = tipo_val
        else:
            log.debug(f"[{codigo}] tipo_operacion.tipo: elemento vacío.")
    else:
        log.warning(f"[{codigo}] Etiqueta 'Tipo de propiedad' no encontrada (label={label_tipo}).")

    # ── ROL ───────────────────────────────────────────────────────────────────
    label_rol = await get_text(page, "//*[@id='form_tipo']/div[1]/div[2]/div/label")
    if label_rol and "rol" in label_rol.lower():
        parte1 = await get_value(page, "//*[@id='tbRol']", codigo, "rol", audit)
        parte2 = await get_value(page, "//*[@id='tbRol2']", codigo, "rol", audit)
        if parte1 or parte2:
            resultado["rol"] = f"{parte1 or ''}-{parte2 or ''}".strip("-")
        else:
            resultado["rol"] = None
    else:
        log.warning(f"[{codigo}] Etiqueta 'Rol' no encontrada (label={label_rol}).")

    # ── VENTA ─────────────────────────────────────────────────────────────────
    audit.setdefault("campos_esperados", []).append("venta")
    try:
        loc_venta = page.locator("#rbVenta1")
        if await loc_venta.count() > 0:
            resultado["venta"] = await loc_venta.is_checked()
        else:
            resultado["venta"] = False
            audit.setdefault("selectors_failed", []).append("#rbVenta1")
    except Exception:
        resultado["venta"] = False

    # ── ARRIENDO ──────────────────────────────────────────────────────────────
    audit.setdefault("campos_esperados", []).append("arriendo")
    try:
        loc_arr = page.locator("#rbArriendo1")
        if await loc_arr.count() > 0:
            resultado["arriendo"] = await loc_arr.is_checked()
        else:
            resultado["arriendo"] = False
            audit.setdefault("selectors_failed", []).append("#rbArriendo1")
    except Exception:
        resultado["arriendo"] = False

    # ── PRECIO VENTA ─────────────────────────────────────────────────────────
    precio_venta: dict = {}
    clp_span_cls = await get_class(page, "//*[@id='uniform-rbDiv1']/span")
    uf_span_cls  = await get_class(page, "//*[@id='uniform-rbDiv2']/span")
    raw_precio_venta = await get_value(page, "//*[@id='tbPrecioVenta']", codigo, "precio_venta", audit)

    if "checked" in (clp_span_cls or ""):
        precio_venta["precio_clp"] = clean_price(raw_precio_venta)
    elif "checked" in (uf_span_cls or ""):
        precio_venta["precio_uf"] = clean_price(raw_precio_venta)

    if precio_venta:
        resultado["precio_venta"] = precio_venta

    # ── PRECIO ARRIENDO ──────────────────────────────────────────────────────
    precio_arriendo: dict = {}
    clp_arr_cls = await get_class(page, "//*[@id='uniform-rbDivA1']/span")
    uf_arr_cls  = await get_class(page, "//*[@id='uniform-rbDivA2']/span")
    raw_precio_arr = await get_value(page, "//*[@id='tbPrecioArriendo']", codigo, "precio_arriendo", audit)

    if "checked" in (clp_arr_cls or ""):
        precio_arriendo["precio_clp"] = clean_price(raw_precio_arr)
    elif "checked" in (uf_arr_cls or ""):
        precio_arriendo["precio_uf"] = clean_price(raw_precio_arr)

    if precio_arriendo:
        resultado["precio_arriendo"] = precio_arriendo

    # ── GASTOS COMUNES ────────────────────────────────────────────────────────
    label_gc = await get_text(page, "//*[@id='form_tipo']/div[4]/div[1]/div/label")
    if label_gc and "gastos comunes" in label_gc.lower():
        gc_raw = await get_value(page, "//*[@id='tbGastosComunes']", codigo, "gastos_comunes", audit)
        gc_val = clean_price(gc_raw)
        resultado["gastos_comunes"] = gc_val if gc_val else None
    else:
        log.debug(f"[{codigo}] Etiqueta 'Gastos Comunes' no encontrada.")
        resultado["gastos_comunes"] = None

    return resultado


# ─── 6.2 UBICACIÓN ───────────────────────────────────────────────────────────

async def extract_ubicacion(page: Page, codigo: str, audit: dict) -> dict:
    """
    Extrae la pestaña #tab_ubicacion.
    Todos los campos con helper get_selected_option() o get_value().
    """
    resultado = {}

    campos_select = {
        "region":  "#ddlre",
        "comuna":  "#ddlco",
        "sector":  "#ddlse",
    }
    campos_input = {
        "calle":                 "//*[@id='tbCalle']",
        "numero":                "//*[@id='tbNumero']",
        "unidad":                "//*[@id='tbUnidad']",
        "letra":                 "//*[@id='tbLetra']",
        "etapa":                 "//*[@id='tbEtapa']",
        "direccion_referencial": "//*[@id='tbDireccionWeb']",
    }

    for campo, css_id in campos_select.items():
        val = await get_selected_option(page, css_id, codigo, campo, audit)
        resultado[campo] = val
        if not val:
            log.debug(f"[{codigo}] ubicacion.{campo}: vacío.")

    for campo, xpath in campos_input.items():
        val = await get_value(page, xpath, codigo, campo, audit)
        resultado[campo] = val
        if not val:
            log.debug(f"[{codigo}] ubicacion.{campo}: vacío.")

    return resultado


# ─── 6.3 CARACTERÍSTICAS ─────────────────────────────────────────────────────

async def extract_caracteristicas(page: Page, codigo: str, audit: dict) -> dict:
    """
    Extrae la pestaña #tab_caracteristicas.

    Campos numéricos: convertidos con safe_int().
    estacionamientos = cubiertos + descubiertos
    """
    resultado = {}

    campos_int = {
        "suite":                        "//*[@id='formsuites']",
        "dormitorios":                  "//*[@id='formdormitorios']",
        "dormitorio_servicio":          "//*[@id='formescritorio']",
        "banos":                        "//*[@id='formbanos']",
        "salas_estar":                  "//*[@id='formsalaDeEstar']",
        "estacionamientos_cubiertos":   "//*[@id='formestacionamientosCubiertos']",
        "estacionamientos_descubiertos":"//*[@id='formestacionamientosDescubiertos']",
        "bodegas":                      "//*[@id='formWAREHOUSES']",
        "ano_construccion":             "//*[@id='formanos']",
        "numero_pisos":                 "//*[@id='formpisos']",
    }

    campos_float = {
        "superficie_terraza": "//*[@id='formmtsTerraza']",
        "superficie_util":    "//*[@id='formmtsUtiles']",
        "superficie_total":   "//*[@id='formmtsConstruidos']",
        "superficie_terreno": "//*[@id='formmtsTerreno']",
        "superficie_construida": "//*[@id='formmtsTotal']",
    }

    for campo, xpath in campos_int.items():
        raw = await get_value(page, xpath, codigo, campo, audit)
        resultado[campo] = safe_int(raw)
        if resultado[campo] is None and raw is not None:
            log.debug(f"[{codigo}] caracteristicas.{campo}: no convertible a int (raw={raw!r}).")

    for campo, xpath in campos_float.items():
        raw = await get_value(page, xpath, codigo, campo, audit)
        resultado[campo] = safe_float(raw)
        if resultado[campo] is None and raw is not None:
            log.debug(f"[{codigo}] caracteristicas.{campo}: no convertible a float (raw={raw!r}).")

    # Orientación (select)
    resultado["orientacion"] = await get_selected_option(page, "#ddlidOrientacion", codigo, "orientacion", audit)

    # Estacionamientos totales calculados
    cub = resultado.get("estacionamientos_cubiertos") or 0
    des = resultado.get("estacionamientos_descubiertos") or 0
    resultado["estacionamientos"] = (cub + des) if (cub or des) else None

    return resultado


# ─── 6.4 OBSERVACIONES ───────────────────────────────────────────────────────

async def extract_observaciones(page: Page, codigo: str, audit: dict) -> dict:
    """
    Extrae la pestaña #tab_observaciones.
    Campos de texto largo — se usa get_value() para <textarea> / get_text() si fuera <div>.
    """
    resultado = {}

    campos = {
        "descripcion":              "//*[@id='tbObservaciones']",
        "observaciones_internas":   "//*[@id='tbObservacionesInternas']",
        "titulo":                   "//*[@id='tbMeliTitulo_1_2']",
    }

    for campo, xpath in campos.items():
        # Intentar input_value primero (textarea), luego inner_text (div)
        val = await get_value(page, xpath, codigo, campo, audit)
        if val is None:
            val = await get_text(page, xpath, codigo, campo, audit)
        resultado[campo] = val
        if not val:
            log.debug(f"[{codigo}] observaciones.{campo}: vacío.")

    # forma_visita apunta al mismo elemento que titulo en la spec
    resultado["forma_visita"] = resultado.get("titulo")

    return resultado


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 7 — SCRAPER PRINCIPAL POR PROPIEDAD
# ══════════════════════════════════════════════════════════════════════════════

async def extract_publicaciones(page: Page, codigo: str, audit: dict) -> dict:
    data = {}
    try:
        all_links = page.locator("a[href]")
        for i in range(await all_links.count()):
            link = all_links.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue

            href_lower = href.lower().strip()
            if "mailto:" in href_lower or "javascript:" in href_lower or "@" in href_lower:
                continue

            if "procasa.cl" in href_lower:
                if "/propiedades/" in href_lower or "/detalle/" in href_lower or str(codigo) in href_lower:
                    data.setdefault("procasa", {})["url_procasa"] = href
            elif "portalinmobiliario." in href_lower:
                data.setdefault("portal_inmobiliario", {})["url_pi"] = href
            elif "mercadolibre.cl" in href_lower:
                data.setdefault("portal_inmobiliario", {})["url_mercado_libre"] = href
            elif "toctoc.com" in href_lower:
                data.setdefault("toctoc", {})["url_toctoc"] = href
            elif "yapo.cl" in href_lower:
                data.setdefault("yapo", {})["url_yapo"] = href
    except Exception as e:
        log.warning(f"[{codigo}] Error al extraer publicaciones: {e}")
    return data

async def extract_datos_propietario(page: Page, codigo: str, audit: dict) -> dict:
    import re
    resultado = {}
    mapping = {
        "nombre": "//*[@id='form_propietario']/div[2]/div[1]/h4",
        "email": "//*[@id='form_propietario']/div[2]/div[1]/ul[1]/li/a",
        "rut": "//*[@id='form_propietario']/div[2]/div[1]/ul[1]/li",
        "comuna": "//*[@id='form_propietario']/div[2]/div[1]/ul[3]/li[1]",
        "fecha": "//*[@id='form_propietario']/div[2]/div[1]/ul[3]/li[2]",
    }
    audit.setdefault("campos_esperados", []).extend(["nombre", "email", "rut", "fono_1", "fono_2", "fono_3", "comuna", "fecha"])
    resultado["nombre"] = await get_text(page, mapping["nombre"], codigo, "nombre", audit)
    resultado["email"] = await get_text(page, mapping["email"], codigo, "email", audit)
    rut_raw = await get_text(page, mapping["rut"], codigo, "rut", audit)
    if rut_raw:
        m = re.search(r"\b\d{1,2}(?:\.?\d{3}){2}-[\dkK]\b", rut_raw)
        resultado["rut"] = m.group(0) if m else None
    else:
        resultado["rut"] = None
    resultado["comuna"] = await get_text(page, mapping["comuna"], codigo, "comuna", audit)
    resultado["fecha"] = await get_text(page, mapping["fecha"], codigo, "fecha", audit)
    for i in range(1, 4):
        xpath_fono = f"//*[@id='form_propietario']/div[2]/div[1]/ul[2]/li[{i}]"
        val = await get_text(page, xpath_fono, codigo, f"fono_{i}", audit)
        resultado[f"fono_{i}"] = val
    return resultado

async def extract_estado(page: Page, codigo: str, audit: dict) -> dict:
    resultado = {}
    audit.setdefault("campos_esperados", []).append("ejecutivo")
    resultado["ejecutivo"] = await get_selected_option(page, "#ddlej", codigo, "ejecutivo", audit)
    resultado["oficina"] = "PROCASA SUCRE"
    resultado["ultima_actualizacion"] = await extract_ultima_actualizacion(page, codigo, audit)
    return resultado

async def scrape_ficha(page: Page, codigo: str, inspect: bool = False) -> dict | None:
    """
    Orquesta la extracción completa de una ficha.

    Flujo:
    1. Navegar directo a propEditar?i={codigo}
    2. Extraer tipo_operacion  (pestaña principal, ya visible al cargar)
       → guardar HTML general si SAVE_DEBUG_HTML
    3. Clic en #tab_ubicacion      → extraer ubicacion      → HTML
    4. Clic en #tab_caracteristicas→ extraer caracteristicas→ HTML
    5. Clic en #tab_observaciones  → extraer observaciones  → HTML
    6. Determinar status: ok / partial / error
    7. Armar doc final agrupado por secciones con metadata completa

    Devuelve None solo si la navegación inicial falló (error fatal).
    Para errores parciales devuelve el doc con status='partial'.
    """
    ok = await navigate_to_ficha(page, codigo)
    if not ok:
        return None

    # Timestamp único para esta ficha (MEJORA 5)
    fecha_scraping = datetime.now().isoformat()

    audit = {
        "campos_vacios": [],
        "selectors_failed": [],
        "campos_esperados": []
    }

    def _print_inspect(seccion_nombre, datos):
        if inspect:
            print(f"\n=================================\n{seccion_nombre.upper()}\n=================================")
            print(json.dumps(datos, indent=2, ensure_ascii=False))

    # ── TIPO DE OPERACIÓN (pestaña principal) ─────────────────────────────────
    log.info(f"[{codigo}] Extrayendo tipo_operacion…")
    tipo_op = await extract_tipo_operacion(page, codigo, audit)
    _print_inspect("TIPO_OPERACION", tipo_op)
    await save_debug_html(page, codigo, "tipo_operacion")
    await save_debug_screenshot(page, codigo, "tipo_operacion")

    # ── UBICACIÓN ─────────────────────────────────────────────────────────────
    log.info(f"[{codigo}] Navegando a pestaña Ubicación…")
    tab_ubi_ok = await click_tab(page, "#tab_ubicacion", codigo)
    ubicacion = await extract_ubicacion(page, codigo, audit) if tab_ubi_ok else {}
    _print_inspect("UBICACION", ubicacion)
    await save_debug_html(page, codigo, "ubicacion")
    await save_debug_screenshot(page, codigo, "ubicacion")

    # ── CARACTERÍSTICAS ───────────────────────────────────────────────────────
    log.info(f"[{codigo}] Navegando a pestaña Características…")
    tab_car_ok = await click_tab(page, "#tab_caracteristicas", codigo)
    caracteristicas = await extract_caracteristicas(page, codigo, audit) if tab_car_ok else {}
    _print_inspect("CARACTERISTICAS", caracteristicas)
    await save_debug_html(page, codigo, "caracteristicas")
    await save_debug_screenshot(page, codigo, "caracteristicas")

    # ── OBSERVACIONES ─────────────────────────────────────────────────────────
    log.info(f"[{codigo}] Navegando a pestaña Observaciones…")
    tab_obs_ok = await click_tab(page, "#tab_observaciones", codigo)
    observaciones = await extract_observaciones(page, codigo, audit) if tab_obs_ok else {}
    _print_inspect("OBSERVACIONES", observaciones)
    await save_debug_html(page, codigo, "observaciones")
    await save_debug_screenshot(page, codigo, "observaciones")

    # ── PUBLICACIONES ─────────────────────────────────────────────────────────
    log.info(f"[{codigo}] Navegando a Portales desde menú lateral…")
    try:
        # Clic en el botón "Portales" del menú lateral izquierdo
        xpath_menu_portales = "//*[@id='resumenRegistro']/div/div[5]/ul/li[10]/a[1]"
        await page.wait_for_selector(f"xpath={xpath_menu_portales}", timeout=15000)
        await page.click(f"xpath={xpath_menu_portales}")
        await asyncio.sleep(3) # Esperar que cargue la vista/modal de Portales
    except Exception as e:
        log.warning(f"[{codigo}] Error al hacer clic en menú lateral Portales: {e}")

    log.info(f"[{codigo}] Extrayendo publicaciones…")
    publicaciones = await extract_publicaciones(page, codigo, audit)
    tab_portales_ok = True
    _print_inspect("PUBLICACIONES", publicaciones)
    await save_debug_html(page, codigo, "portales")
    await save_debug_screenshot(page, codigo, "portales")

    # ── DATOS PROPIETARIO ─────────────────────────────────────────────────────
    log.info(f"[{codigo}] Navegando a propPropietario…")
    url_prop = f"https://procasa.prop360.cl/backOffice/propiedades/propPropietario?i={codigo}"
    await page.goto(url_prop)
    await asyncio.sleep(2)
    datos_propietario = await extract_datos_propietario(page, codigo, audit)
    _print_inspect("DATOS_PROPIETARIO", datos_propietario)
    await save_debug_html(page, codigo, "propietario")
    await save_debug_screenshot(page, codigo, "propietario")

    # ── ESTADO ────────────────────────────────────────────────────────────────
    log.info(f"[{codigo}] Navegando a propEstado…")
    url_est = f"https://procasa.prop360.cl/backoffice/propiedades/propEstado?i={codigo}"
    await page.goto(url_est)
    await asyncio.sleep(2)
    estado = await extract_estado(page, codigo, audit)
    _print_inspect("ESTADO", estado)
    await save_debug_html(page, codigo, "estado")
    await save_debug_screenshot(page, codigo, "estado")

    pestanas_fallidas = [
        tab for tab, ok_flag in [
            ("#tab_ubicacion",       tab_ubi_ok),
            ("#tab_caracteristicas", tab_car_ok),
            ("#tab_observaciones",   tab_obs_ok),
            ("#a-propPortales",      tab_portales_ok),
        ] if not ok_flag
    ]

    status = "partial" if pestanas_fallidas else "ok"

    # ── TIPO DE PROPIEDAD
    tipo_propiedad = tipo_op.get("tipo")
    if tipo_propiedad:
        log.info(f"[INFO] codigo={codigo} tipo={tipo_propiedad}")
        if tipo_propiedad not in PROPERTY_TYPE_HANDLERS:
            log.info(f"[{codigo}] Tipo '{tipo_propiedad}' no mapeado en PROPERTY_TYPE_HANDLERS.")

    # ── DOCUMENTO FINAL
    oficina = derive_oficina(codigo, estado)
    precio_clp = None
    precio_uf = None
    if isinstance(tipo_op.get("precio_venta"), dict):
        precio_clp = tipo_op["precio_venta"].get("precio_clp") or precio_clp
        precio_uf = tipo_op["precio_venta"].get("precio_uf") or precio_uf
    if isinstance(tipo_op.get("precio_arriendo"), dict):
        precio_clp = tipo_op["precio_arriendo"].get("precio_clp") or precio_clp
        precio_uf = tipo_op["precio_arriendo"].get("precio_uf") or precio_uf

    doc = {
        "codigo": codigo,
        "tipo_operacion":   tipo_op,
        "ubicacion":        ubicacion,
        "caracteristicas":  caracteristicas,
        "observaciones":    observaciones,
        "publicaciones":    publicaciones,
        "datos_propietario": datos_propietario,
        "estado":           estado,
        "oficina":          oficina,
        "ultima_actualizacion": estado.get("ultima_actualizacion"),
        "precio_clp":       precio_clp,
        "precio_uf":        precio_uf,
        "ejecutivo":        estado.get("ejecutivo"),
        "dormitorios":      caracteristicas.get("dormitorios"),
        "banos":            caracteristicas.get("banos"),
        "m2_construida":    caracteristicas.get("superficie_construida"),
        "m2_utiles":        caracteristicas.get("superficie_util"),
        "m2_terreno":       caracteristicas.get("superficie_terreno"),
        "m2_total":         caracteristicas.get("superficie_total"),
        "descripcion":      observaciones.get("descripcion"),
        "metadata": {
            "status":           status,
            "fecha_scraping":   fecha_scraping,
            "tipo_propiedad":   tipo_propiedad,
            "tipo_propiedad_detectado": tipo_propiedad,
            "tabs": {
                "tipo_operacion": True,
                "ubicacion": tab_ubi_ok,
                "caracteristicas": tab_car_ok,
                "observaciones": tab_obs_ok,
                "portales": tab_portales_ok
            },
            "campos_esperados": list(set(audit.get("campos_esperados", []))),
            "campos_vacios":    list(set(audit["campos_vacios"])),
            "selectors_failed": list(set(audit["selectors_failed"])),
            "source_url":       FICHA_URL_TPL.format(codigo=codigo),
            "scraper":          "scraping_prop360_ficha_completa.py",
            "version":          "1.3.0"
        },
    }

    return doc


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 8 — PERSISTENCIA MONGO
# Escribe SOLO en prop360_ficha_completa_test
# ══════════════════════════════════════════════════════════════════════════════

def upsert_ficha(test_coll, doc: dict) -> tuple[bool, bool]:
    """
    Inserta o actualiza un documento en la colección de prueba.
    Devuelve (nuevo: bool, actualizado: bool).

    Lógica idéntica al patrón de sync_convecta_master.py:
        coll.update_one({filtro}, {"$set": doc}, upsert=True)
    """
    existing = test_coll.find_one({"codigo": doc["codigo"]}) or {}
    doc = canonicalize_prices(doc)
    previous_hash = existing.get("audit_hash")
    current_hash = audit_hash(doc)
    changed = previous_hash != current_hash

    historial = build_deep_history(existing if existing else None, doc)
    doc["historial_cambios"] = historial
    doc["audit_hash"] = current_hash

    versiones_previas = list(existing.get("versiones", [])) if existing else []
    if changed:
        snapshot = build_snapshot(doc, "initial" if not existing else "change", current_hash)
        versiones_previas.append(snapshot)
        doc["versiones"] = versiones_previas[-50:]
        doc["ultima_version_hash"] = current_hash
        doc["ultima_version_at"] = now_iso()
    elif versiones_previas:
        doc["versiones"] = versiones_previas[-50:]
        doc["ultima_version_hash"] = existing.get("ultima_version_hash")
        doc["ultima_version_at"] = existing.get("ultima_version_at")

    result = test_coll.update_one(
        {"codigo": doc["codigo"]},
        {"$set": doc},
        upsert=True,
    )
    nuevo       = bool(result.upserted_id)
    actualizado = not nuevo and result.modified_count > 0
    return nuevo, actualizado


def upsert_error(test_coll, codigo: str, error_msg: str) -> None:
    """
    MEJORA 3 — Persistencia de errores por propiedad.

    Si una propiedad falla, guarda igualmente un documento en test_coll
    para no perder el registro del error. Permite revisar a posteriori
    qué propiedades fallaron y con qué mensaje.

    Estructura guardada:
    {
        "codigo": "16905",
        "metadata": {
            "status":         "error",
            "error":          "mensaje del error",
            "fecha_scraping": "2026-06-15T10:00:00",
            "scraper":        "scraping_prop360_ficha_completa.py",
            "version":        "1.1.0"
        }
    }
    """
    doc_error = {
        "codigo": codigo,
        "metadata": {
            "status":         "error",
            "error":          str(error_msg),
            "fecha_scraping": datetime.now().isoformat(),
            "scraper":        "scraping_prop360_ficha_completa.py",
            "version":        "1.1.0",
        },
    }
    try:
        test_coll.update_one(
            {"codigo": codigo},
            {"$set": doc_error},
            upsert=True,
        )
        log.debug(f"[{codigo}] Documento de error persistido en {TEST_COLLECTION}.")
    except Exception as e:
        log.error(f"[{codigo}] No se pudo persistir el error en Mongo: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 9 — RUNNER PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

async def run(run_mode: dict) -> None:
    """
    Punto de entrada principal del scraper.

    1. Conectar Mongo (colección de prueba)
    2. Obtener lista de códigos según RUN_MODE
    3. Abrir Playwright + Stealth + Login
    4. Iterar códigos → scrape_ficha → upsert_ficha  (o upsert_error si falla)
    5. Reporte final con tiempo total
    """
    # ── INICIO DE CRONÓMETRO (MEJORA 8) ──────────────────────────────────────
    tiempo_inicio = time.monotonic()

    # ── MONGO ─────────────────────────────────────────────────────────────────
    mongo_client, test_coll = get_mongo_collection(TEST_COLLECTION)
    test_coll.create_index("codigo", unique=True)
    # Índice en fecha_scraping para soportar el modo "actualizar" eficientemente
    test_coll.create_index("metadata.fecha_scraping")

    # ── CÓDIGOS ───────────────────────────────────────────────────────────────
    codigos = get_codigos(run_mode, test_coll)
    total   = len(codigos)
    scope   = run_mode.get("scope", "?")

    if total == 0:
        log.info("No hay propiedades para procesar con el modo seleccionado.")
        mongo_client.close()
        return

    log.info(f"Total de propiedades a procesar: {total}  [scope={scope}]")
    log.info(f"Colección de prueba: {TEST_COLLECTION}")
    log.info(f"Debug HTML: {'ACTIVADO → ' + DEBUG_HTML_DIR if SAVE_DEBUG_HTML else 'desactivado'}")

    # Contadores del reporte (MEJORA 8)
    ok_count      = 0
    nuevo_count   = 0
    upd_count     = 0
    partial_count = 0
    err_count     = 0
    errores: list[str] = []
    
    tipos_detectados = {}
    errores_pestanas = {
        "tipo_operacion": 0,
        "ubicacion": 0,
        "caracteristicas": 0,
        "observaciones": 0
    }

    # ── PLAYWRIGHT ───────────────────────────────────────────────────────────
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page    = await context.new_page()

        # Stealth (mismo patrón que scraping_prop360_portales.py)
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        # Login
        await login(page)

        inspect_mode = run_mode.get("inspect", False)
        
        # ── LOOP DE SCRAPING ─────────────────────────────────────────────────
        for i, codigo in enumerate(codigos, start=1):
            log.info(f"[{i}/{total}] Procesando código {codigo}…")
            try:
                doc = await scrape_ficha(page, codigo, inspect=inspect_mode)

                if doc is None:
                    # Navegación falló totalmente — persistir error (MEJORA 3)
                    msg = "navigate_to_ficha devolvió False (página no cargó o formulario ausente)"
                    log.warning(f"[{codigo}] {msg}")
                    if not DRY_RUN:
                        upsert_error(test_coll, codigo, msg)
                    else:
                        log.info(f"[{codigo}] DRY_RUN: Omitiendo guardado de error en Mongo.")
                    err_count += 1
                    errores.append(codigo)
                    continue

                # Contabilizar parciales (MEJORA 4)
                doc_status = doc.get("metadata", {}).get("status", "ok")
                if doc_status == "partial":
                    partial_count += 1

                if inspect_mode:
                    save_debug_json(codigo, doc)
                    
                    esperados = len(doc["metadata"]["campos_esperados"])
                    vacios = len(doc["metadata"]["campos_vacios"])
                    encontrados = esperados - vacios
                    cobertura = (encontrados / esperados * 100) if esperados > 0 else 0.0
                    
                    print(f"\n=================================")
                    print(f"RESUMEN DE INSPECCIÓN ANTES DE BD [{codigo}]")
                    print(f"=================================")
                    print(f"Total campos esperados : {esperados}")
                    print(f"Campos encontrados     : {encontrados}")
                    print(f"Campos vacíos          : {vacios}")
                    print(f"Cobertura              : {cobertura:.1f}%")
                    print(f"Selectores fallidos    : {len(doc['metadata']['selectors_failed'])}")
                    print(f"Status                 : {doc_status}")
                    print(f"Tipo propiedad         : {doc['metadata']['tipo_propiedad_detectado']}")
                    
                    print(f"\n--- CAMPOS AGRUPADOS POR SECCIÓN (ALFABÉTICO) ---")
                    for sec in ["tipo_operacion", "ubicacion", "caracteristicas", "observaciones"]:
                        print(f"\n[{sec.upper()}]")
                        sec_data = doc.get(sec, {})
                        for k in sorted(sec_data.keys()):
                            print(f"  {k}: {sec_data[k]}")
                    print(f"=================================\n")

                if DRY_RUN:
                    log.info(f"[{codigo}] DRY_RUN activo. Omitiendo guardado en Mongo.")
                    nuevo, actualizado = False, False
                    if doc_status == "ok":
                        ok_count += 1
                else:
                    nuevo, actualizado = upsert_ficha(test_coll, doc)
                    if doc_status == "ok":
                        ok_count += 1
                
                # Tracking para reporte extendido
                t_det = doc.get("metadata", {}).get("tipo_propiedad_detectado")
                if t_det:
                    tipos_detectados[t_det] = tipos_detectados.get(t_det, 0) + 1
                
                tabs_ok = doc.get("metadata", {}).get("tabs", {})
                for t_name, t_val in tabs_ok.items():
                    if not t_val:
                        errores_pestanas[t_name] = errores_pestanas.get(t_name, 0) + 1

                if nuevo:
                    nuevo_count += 1
                    log.info(f"[{codigo}] → NUEVO [{doc_status.upper()}] insertado.")
                elif actualizado:
                    upd_count += 1
                    log.info(f"[{codigo}] → Actualizado [{doc_status.upper()}].")
                else:
                    log.info(f"[{codigo}] → Sin cambios [{doc_status.upper()}].")

            except Exception as exc:
                # Error inesperado — persistir en Mongo (MEJORA 3)
                log.error(f"[{codigo}] Error inesperado: {exc}", exc_info=True)
                if not DRY_RUN:
                    upsert_error(test_coll, codigo, str(exc))
                else:
                    log.info(f"[{codigo}] DRY_RUN: Omitiendo guardado de error en Mongo.")
                err_count += 1
                errores.append(codigo)

        await browser.close()

    mongo_client.close()

    # ── TIEMPO TOTAL (MEJORA 8) ───────────────────────────────────────────────
    tiempo_total_seg = time.monotonic() - tiempo_inicio
    minutos, segundos = divmod(int(tiempo_total_seg), 60)
    tiempo_str = f"{minutos}m {segundos}s" if minutos else f"{segundos}s"

    # ── REPORTE FINAL (MEJORA 8) ──────────────────────────────────────────────
    W = 56
    print("\n" + "═" * W)
    print("  REPORTE SCRAPING PROP360 — FICHA COMPLETA")
    print("═" * W)
    print(f"  Tipo de ejecución   : {scope}")
    print(f"  Modo Inspección     : {'SÍ' if run_mode.get('inspect') else 'NO'}")
    print(f"  DRY_RUN (No Mongo)  : {'SÍ' if DRY_RUN else 'NO'}")
    print(f"  Colección de prueba : {TEST_COLLECTION}")
    print(f"  Debug HTML          : {'SÍ → ' + DEBUG_HTML_DIR if SAVE_DEBUG_HTML else 'NO'}")
    print("─" * W)
    print(f"  Procesadas          : {total}")
    print(f"  Correctas (ok)      : {ok_count}")
    print(f"    ├─ Nuevas         : {nuevo_count}")
    print(f"    └─ Actualizadas   : {upd_count}")
    print(f"  Parciales           : {partial_count}")
    print(f"  Errores             : {err_count}")
    if errores:
        print(f"  Códigos con error  : {', '.join(errores)}")
    print("─" * W)
    print("  Tipos de propiedad detectados:")
    for td_k, td_v in tipos_detectados.items():
        print(f"    {td_k}: {td_v}")
    print("─" * W)
    print("  Errores por pestaña:")
    for ep_k, ep_v in errores_pestanas.items():
        print(f"    {ep_k}: {ep_v}")
    print("─" * W)
    print(f"  Tiempo total        : {tiempo_str}")
    print("═" * W + "\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    asyncio.run(run(RUN_MODE))
