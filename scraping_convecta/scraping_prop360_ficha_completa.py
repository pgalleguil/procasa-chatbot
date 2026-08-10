"""
scraping_prop360_ficha_completa.py  (v2.0.0 — 100% HTTP)
──────────────────────────────────────────────────────────
Scraper de fichas completas Prop360 para la oficina PROCASA SUCRE.

Reescrito sin Playwright ni Excel:
  - Login HTTP (login.ashx) con httpx
  - Listado de la oficina via propiedades.ashx (fuente de verdad)
  - Ficha completa via propEditar / propPropietario / propEstado /
    propPublicacion + bitácora (prop360.ashx)
  - Escribe SOLO en universo_cartera_prop360 (con $set)

Sincronización (cada ejecución):
  1. Descarga el listado Activa de la oficina.
  2. NUEVAS  : códigos Activa no existentes en la DB → scrape + insertar.
  3. CAMBIOS : códigos Activa en DB cuyo resumen difiere del listado → re-scrape.
  4. BAJAS   : docs con disponible_prop360=True que ya NO están Activa → False.
  5. NO scrapea propiedades Pasiva / No disponible.

Uso:
    python scraping_convecta/scraping_prop360_ficha_completa.py --dry-run
    python scraping_convecta/scraping_prop360_ficha_completa.py
    python scraping_convecta/scraping_prop360_ficha_completa.py --codigo 6576
    python scraping_convecta/scraping_prop360_ficha_completa.py --backfill
    python scraping_convecta/scraping_prop360_ficha_completa.py --max-new 10 --max-update 5

Variables de entorno:
    PROP360_EMAIL, PROP360_PASSWORD, MONGO_URI, DB_NAME
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from pymongo import MongoClient

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from config import Config

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ficha_completa")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
BASE_URL = "https://procasa.prop360.cl"
LOGIN_URL = f"{BASE_URL}/index?ReturnUrl=%2FbackOffice%2Fpropiedades%2Fpropiedades"
LOGIN_ASHX = f"{BASE_URL}/recursos/login.ashx"
PROPIEDADES_ASHX = f"{BASE_URL}/backOffice/recursos/propiedades.ashx"
PROP360_ASHX = f"{BASE_URL}/backOffice/recursos/prop360.ashx"
PROPIEDAD_FICHA_ASHX = f"{BASE_URL}/backOffice/recursos/propiedadFicha.ashx"
FICHA_URL_TPL = f"{BASE_URL}/backOffice/propiedades/propEditar?i={{codigo}}"
PROPIETARIO_URL_TPL = f"{BASE_URL}/backOffice/propiedades/propPropietario?i={{codigo}}"
ESTADO_URL_TPL = f"{BASE_URL}/backOffice/propiedades/propEstado?i={{codigo}}"
PUBLICACION_URL_TPL = f"{BASE_URL}/backOffice/propiedades/propPublicacion?i={{codigo}}"
FICHA_PRINT_URL_TPL = f"{BASE_URL}/backOffice/propiedades/propFicha.aspx?print=1&i={{codigo}}"

OFFICE_ID = int(os.getenv("PROP360_OFFICE_ID", "7"))

OFICINAS = {
    1: "PROCASA CARLOS HURTADO",
    2: "PROCASA FRANCISCO VIAL",
    3: "PROCASA GRUPO ORIENTE",
    4: "OFICINA 4",
    5: "PROCASA LA GLORIA",
    6: "PROCASA MAURICIO PINO",
    7: "PROCASA SUCRE",
    8: "PROCASA VILLARRICA",
}
OFICINA_NOMBRE = OFICINAS.get(OFFICE_ID, f"OFICINA {OFFICE_ID}")
COLLECTION_NAME = getattr(Config, "PROPERTY_COLLECTION_NAME", "universo_cartera_prop360")
SCRAPER_VERSION = "2.0.0"

_CARACTERISTICAS_INT = {
    "formsuites": "suite",
    "formdormitorios": "dormitorios",
    "formescritorio": "dormitorio_servicio",
    "formbanos": "banos",
    "formsalaDeEstar": "salas_estar",
    "formestacionamientosCubiertos": "estacionamientos_cubiertos",
    "formestacionamientosDescubiertos": "estacionamientos_descubiertos",
    "formWAREHOUSES": "bodegas",
    "formanos": "ano_construccion",
    "formpisos": "numero_pisos",
    "formprivados": "privados",
    "formoficinas": "oficinas",
    "formhabitacion": "habitaciones",
    "formcamasServicio": "camas_servicio",
    "formascensores": "ascensores",
    "formdepartamentosPorPiso": "departamentos_por_piso",
    "formpiso": "piso",
    "formestacionamientos": "estacionamientos_form",
}

_CARACTERISTICAS_FLOAT = {
    "formmtsTerraza": "superficie_terraza",
    "formmtsUtiles": "superficie_util",
    "formmtsConstruidos": "superficie_total",
    "formmtsTerreno": "superficie_terreno",
    "formmtsTotal": "superficie_construida",
    "formmtFrente": "frente",
    "formmtFondo": "fondo",
    "formmtsVitrina": "vitrina",
    "formmtsBodega": "bodega_m2",
    "formmtsCasa": "casa_m2",
    "formmtsOficina": "oficina_m2",
}

_CARACTERISTICAS_SELECT = {
    "ddlidOrientacion": "orientacion",
    "ddltipoPiso": "tipo_piso",
    "ddlpisoDormitorios": "tipo_piso_dormitorios",
    "ddltipoPisoBanos": "tipo_piso_banos",
    "ddltipoPisoCocina": "tipo_piso_cocina",
    "ddltipoPisoComedor": "tipo_piso_comedor",
    "ddltipoPisoLiving": "tipo_piso_living",
    "ddlpisoHallEntrada": "tipo_piso_hall",
    "ddlidEstiloCasa": "estilo_casa",
    "ddlidTipoCasa": "tipo_casa",
    "ddltipoDepto": "tipo_depto",
    "ddltipoLocal": "tipo_local",
    "ddlpropRecep": "prop_recep",
    "ddltipoMuebleCoc": "tipo_mueble_cocina",
    "ddlidAislacion": "aislacion",
    "ddlidTipoOficina": "tipo_oficina",
    "ddltipoGas": "tipo_gas",
    "ddltermopanel": "termopanel",
    "ddltipoAguaCal": "tipo_agua_caliente",
    "ddltipoCalef": "tipo_calefaccion",
    "ddltipoCocina": "tipo_cocina",
    "ddltipoConst": "tipo_construccion",
    "ddltipoTecho": "tipo_techo",
    "ddltipoVentana": "tipo_ventana",
    "ddlalcantarillado": "alcantarillado",
    "ddltipoAgua": "tipo_agua",
    "ddltipoEnergia": "tipo_energia",
    "ddlLAND_ACCESS": "land_access",
    "ddlGARAGE_ACCESS": "garage_access",
    "ddlDISPOSITION": "disposition",
    "ddlLOT_DISPOSITION": "lot_disposition",
    "ddlLOT_SHAPE": "lot_shape",
    "ddlCHECK_IN": "check_in",
    "ddlCHECK_OUT": "check_out",
    "ddlTYPE_OF_WAREHOUSE": "tipo_warehouse",
    "ddlFARM_TYPE": "tipo_farm",
    "ddlCOVERAGE_TYPE": "tipo_cobertura",
    "ddlGARAGE_TYPE": "tipo_garage",
}

TRACKED_FIELDS = [
    "precio_clp",
    "precio_uf",
    "ejecutivo",
    "datos_propietario",
    "descripcion",
    "oficina",
    "ultima_actualizacion",
    "disponible_prop360",
]

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS GENÉRICOS
# ──────────────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_value(val: str | None) -> str | None:
    if not val:
        return None
    v_lower = val.lower().strip()
    if v_lower in [
        "seleccione...", "seleccione", "seleccionar", "- seleccione -",
        "selecciona", "seleccionar...", "", "0", "0.0", ".", "-",
        "vacío", "vacio", "none", "nan", "null",
    ]:
        return None
    return val


def safe_int(val: str | None) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace(".", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def safe_float(val: str | None) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(".", "").replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def clean_price(raw: str | None) -> int | None:
    if raw is None:
        return None
    s = re.sub(r"[^\d]", "", str(raw))
    return int(s) if s else None


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
        out[prefix] = [strip_volatile_for_audit(v) for v in value]
        return out
    out[prefix] = value
    return out


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


def get_uf_rate():
    """Último valor UF válido persistido (uf_cache) o configuración.

    NUNCA llama a la API por ficha/propiedad: usa el cache que mantiene el
    proceso periódico (uf_sync_loop). Devuelve float o None.
    """
    try:
        from chatbot.uf_service import obtener_uf_cache_o_fallback
        info = obtener_uf_cache_o_fallback()
        if info and info.get("valor"):
            return float(info["valor"])
    except Exception:
        pass
    # Último recurso sin red: configuración unificada
    try:
        from config import Config
        val = float(getattr(Config, "UF_VALUE", 0) or 0)
        if val > 0:
            return val
    except Exception:
        pass
    return None


def canonicalize_prices(doc: dict):
    """Completa la divisa derivada + metadata en cada precio de tipo_operacion.

    Usa el ÚLTIMO valor UF válido persistido (uf_service), nunca llama la API
    por ficha. Regla: la divisa publicada es ORIGINAL; solo se genera la otra.
    Si no hay UF cache válida, deja el precio original sin derivado (warning);
    el proceso periódico (uf_sync_loop) completa el derivado después.
    """
    try:
        from chatbot.uf_service import obtener_uf_cache_o_fallback, completar_precio
    except Exception:
        return doc

    uf_info = obtener_uf_cache_o_fallback()
    uf_valor = uf_info["valor"] if uf_info else None
    uf_fecha = uf_info.get("fecha") if uf_info else None

    to = doc.get("tipo_operacion")
    if isinstance(to, dict):
        for key in ("precio_venta", "precio_arriendo"):
            precio = to.get(key)
            if isinstance(precio, dict):
                to[key] = completar_precio(precio, uf_valor, uf_fecha)

    # Resumen plano heredado: reflejar precios completados (solo informativo)
    res = doc.get("resumen")
    if isinstance(res, dict):
        pv = to.get("precio_venta") if isinstance(to, dict) else None
        pa = to.get("precio_arriendo") if isinstance(to, dict) else None
        clp = None
        uf = None
        if isinstance(pv, dict):
            clp = pv.get("precio_clp") or clp
            uf = pv.get("precio_uf") or uf
        if isinstance(pa, dict):
            clp = pa.get("precio_clp") or clp
            uf = pa.get("precio_uf") or uf
        if clp is not None:
            res["precio_clp"] = clp
        if uf is not None:
            res["precio_uf"] = uf
    return doc


def get_tracked_value(doc: dict, field: str):
    if field in doc:
        return doc.get(field)
    if isinstance(doc.get("resumen"), dict) and field in doc["resumen"]:
        return doc["resumen"].get(field)
    if field == "precio_clp":
        return doc.get("precio_clp")
    if field == "precio_uf":
        return doc.get("precio_uf")
    return None


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
    if not existing or not existing.get("audit_hash"):
        return []
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
        if old_val is None or old_val == "":
            continue
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
    snapshot["__meta"] = {"fecha": now_iso(), "tipo": version_type, "hash": hash_value}
    return snapshot


def _bs(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def _input_val(soup, element_id: str) -> str | None:
    el = soup.find(id=element_id) if soup else None
    if el is None:
        return None
    if el.name == "textarea":
        return _clean_value(el.get_text()) or None
    val = el.get("value", "")
    if isinstance(val, list):
        val = val[0] if val else ""
    return _clean_value(str(val).strip()) or None


def _radio_checked(soup, element_id: str) -> bool:
    el = soup.find(id=element_id) if soup else None
    return bool(el is not None and el.has_attr("checked"))


def _selected_option(sel) -> str | None:
    if sel is None:
        return None
    for opt in sel.find_all("option"):
        if opt.has_attr("selected"):
            text = opt.get_text(strip=True)
            return _clean_value(text) or None
    return None


def _extract_form_extra(container, excluded: set[str]) -> dict:
    extra = {}
    if container is None:
        return extra
    for inp in container.find_all("input"):
        iid = inp.get("id")
        if not iid or iid in excluded:
            continue
        itype = (inp.get("type") or "").lower()
        if itype in ("radio", "checkbox"):
            if inp.has_attr("checked"):
                extra[iid] = inp.get("value", "1")
        else:
            val = inp.get("value", "")
            if isinstance(val, list):
                val = val[0] if val else ""
            val = str(val).strip()
            if val:
                extra[iid] = val
    for sel in container.find_all("select"):
        iid = sel.get("id")
        if not iid or iid in excluded:
            continue
        val = _selected_option(sel)
        if val:
            extra[iid] = val
    for ta in container.find_all("textarea"):
        iid = ta.get("id")
        if not iid or iid in excluded:
            continue
        val = ta.get_text(strip=True)
        if val:
            extra[iid] = val
    return extra


def _feature_name(radio_id: str) -> str:
    name = radio_id
    if name.startswith("rb"):
        name = name[2:]
    name = re.sub(r"[01]$", "", name)
    return name.lower()


# ──────────────────────────────────────────────────────────────────────────────
# CLIENTE HTTP PROP360
# ──────────────────────────────────────────────────────────────────────────────

class Prop360AuthError(Exception):
    pass


class Prop360Client:
    def __init__(self, email: str, password: str, delay: float = 0.3):
        self.email = email
        self.password = password
        self.delay = delay
        self.client = httpx.Client(
            verify=False,
            follow_redirects=True,
            timeout=45.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Referer": "https://procasa.prop360.cl/backOffice/propiedades/propiedades",
            },
        )
        self.session_active = False

    def _wait(self):
        time.sleep(self.delay * (0.6 + random.random() * 0.8))

    def _get(self, url: str, **kwargs):
        last = None
        for _ in range(3):
            try:
                r = self.client.get(url, **kwargs)
                if r.status_code in (429, 500, 502, 503) and r.status_code != 200:
                    time.sleep(1.5 * (_ + 1))
                    continue
                return r
            except Exception as e:
                last = e
                time.sleep(1.0)
        raise last or RuntimeError(f"GET falló: {url}")

    def _post(self, url: str, data: dict, **kwargs):
        last = None
        for _ in range(3):
            try:
                r = self.client.post(url, data=data, **kwargs)
                if r.status_code in (429, 500, 502, 503):
                    time.sleep(1.5 * (_ + 1))
                    continue
                return r
            except Exception as e:
                last = e
                time.sleep(1.0)
        raise last or RuntimeError(f"POST falló: {url}")

    def login(self) -> bool:
        log.info("Iniciando sesión en Prop360…")
        resp = self.client.get(LOGIN_URL)
        if resp.status_code >= 400:
            raise Prop360AuthError(f"GET login falló: HTTP {resp.status_code}")
        login_data = {
            "accion": "login",
            "rfield": "",
            "mail": self.email,
            "password": self.password,
            "usr": 0,
            "_": time.time() % 10,
        }
        resp2 = self._post(
            LOGIN_ASHX,
            login_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            result = resp2.json()
        except json.JSONDecodeError:
            raise Prop360AuthError(f"Respuesta no JSON en login: {resp2.text[:200]}")
        if result.get("acceso") != "sí":
            raise Prop360AuthError(f"Login fallido: {result.get('mensajeError', 'desconocido')}")
        redirect = result.get("redireccion", "/backoffice/inicio/index.aspx")
        self.client.get(f"{BASE_URL}{redirect}")
        self.session_active = True
        log.info("Sesión Prop360 establecida correctamente.")
        return True

    # ── Listado ───────────────────────────────────────────────────────────────
    def fetch_listing(self, office_id: int = OFFICE_ID) -> list[dict]:
        if not self.session_active:
            raise Prop360AuthError("Sesión no activa. Ejecutar login() primero.")
        rows = []
        page = 1
        while True:
            params = {
                "ac": "listadoPropiedades",
                "ofi": office_id,
                "op": 2,
                "pa": page,
                "nr": 500,
                "or": 1,
                "od": 2,
                "vi": 2,
                "ca": "10,1,2,3,4,5,6,7,8,9",
                "_": time.time() % 100,
            }
            r = self._get(PROPIEDADES_ASHX, params=params)
            if r.status_code != 200:
                raise Prop360AuthError(f"Listado falló: HTTP {r.status_code}")
            try:
                payload = r.json()
            except json.JSONDecodeError:
                raise Prop360AuthError(f"Listado no JSON: {r.text[:200]}")
            listing_html = ""
            if isinstance(payload, list) and payload:
                listing_html = payload[0].get("listing", "")
            page_rows = re.split(r"<tr id='filaProp\d+'>", listing_html)[1:]
            if page_rows:
                rows.extend(page_rows)
            self._wait()
            if len(page_rows) < 500:
                break
            page += 1
        return [self._parse_listing_row(row) for row in rows]

    @staticmethod
    def _cell_text(cell: str) -> str | None:
        cell = re.sub(r"<[^>]+>", " ", cell)
        cleaned = re.sub(r"\s+", " ", cell).strip()
        return _clean_value(cleaned) or None

    def _parse_listing_row(self, row: str) -> dict:
        codigo_m = re.search(r"rel='(\d+)'", row)
        codigo = codigo_m.group(1) if codigo_m else None
        est_m = re.search(r"lnkEditEstado'[^>]*>([^<]+)<", row)
        ops_m = re.findall(r"label label-sm label-(?:primary|info|danger)'[^>]*>([^<]+)<", row)
        tds = re.split(r"</td><td[^>]*>", row)
        return {
            "codigo": codigo,
            "tipo": self._cell_text(tds[2]) if len(tds) > 2 else None,
            "operacion": (", ".join(o.strip() for o in ops_m) if ops_m else None),
            "estado": (est_m.group(1).strip() if est_m else None),
            "captador": self._cell_text(tds[5]) if len(tds) > 5 else None,
            "direccion": self._cell_text(tds[6]) if len(tds) > 6 else None,
            "precio": self._cell_text(tds[7]) if len(tds) > 7 else None,
            "comuna": self._cell_text(tds[8]) if len(tds) > 8 else None,
            "region": self._cell_text(tds[9]) if len(tds) > 9 else None,
        }

    # ── Páginas de ficha ──────────────────────────────────────────────────────
    def get_propeditar(self, codigo: str) -> str:
        r = self._get(FICHA_URL_TPL.format(codigo=codigo))
        return r.text

    def get_propietario(self, codigo: str) -> str:
        r = self._get(PROPIETARIO_URL_TPL.format(codigo=codigo))
        return r.text

    def get_estado(self, codigo: str) -> str:
        r = self._get(ESTADO_URL_TPL.format(codigo=codigo))
        return r.text

    def get_publicacion(self, codigo: str) -> str:
        r = self._get(PUBLICACION_URL_TPL.format(codigo=codigo))
        return r.text

    def get_ficha_imprimible(self, codigo: str) -> str:
        r = self._get(FICHA_PRINT_URL_TPL.format(codigo=codigo))
        return r.text

    def get_portales(self, codigo: str) -> dict:
        r = self._post(
            PROPIEDAD_FICHA_ASHX,
            {
                "accion": "propCard_portals",
                "idprop": codigo,
                "cache": time.time() % 10,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            payload = r.json()
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def get_bitacora(self, codigo: str, max_pages: int = 10) -> list[dict]:
        items = []
        pagina = 1
        while pagina <= max_pages:
            r = self._post(
                PROP360_ASHX,
                {
                    "accion": "fichaPropiedad_pestagna",
                    "idprop": codigo,
                    "pestag": "a-propBitacora",
                    "pagina": pagina,
                    "cache": time.time() % 10,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                payload = r.json()
            except json.JSONDecodeError:
                break
            if not isinstance(payload, list) or not payload:
                break
            entry = payload[0]
            listado = entry.get("listado", "")
            items.extend(self._parse_bitacora(listado, offset=len(items)))
            siguiente = entry.get("paginaSiguiente", 0)
            if not siguiente or int(siguiente) <= 0:
                break
            pagina = int(siguiente)
            self._wait()
        return items

    @staticmethod
    def _parse_bitacora(listado: str, offset: int = 0) -> list[dict]:
        if not listado or "comentarios registrados" in listado:
            return []
        soup = _bs(listado)
        items = []
        nodes = soup.select(".timeline_custom-item")
        if not nodes:
            nodes = soup.select(".timeline-item")
        for idx, node in enumerate(nodes, start=offset + 1):
            author_el = node.select_one(".bitacora-author") or node.select_one(".timeline-header")
            fecha_el = node.select_one(".bitacora-date")
            valor_el = node.select_one(".bitacora-text") or node.select_one(".timeline-body span:last-of-type")
            quien = None
            fecha = None
            if author_el:
                if fecha_el:
                    author_el = author_el.extract() if fecha_el in list(author_el.descendants) else author_el
                quien = _clean_value(author_el.get_text(" ", strip=True)) or None
                fecha = _clean_value(fecha_el.get_text(strip=True)) if fecha_el else None
            valor = _clean_value(valor_el.get_text(" ", strip=True)) if valor_el else None
            if any(v for v in (quien, fecha, valor)):
                items.append({"indice": idx, "valor": valor, "quien": quien, "fecha": fecha})
        return items


# ──────────────────────────────────────────────────────────────────────────────
# PARSERS DE PÁGINAS
# ──────────────────────────────────────────────────────────────────────────────

def parse_tipo_operacion(html: str, audit: dict) -> dict:
    soup = _bs(html)
    result = {}
    result["tipo"] = _selected_option(soup.find(id="ddltp"))
    rol1 = _input_val(soup, "tbRol")
    rol2 = _input_val(soup, "tbRol2")
    if rol1 or rol2:
        result["rol"] = f"{rol1 or ''}-{rol2 or ''}".strip("-")
    result["venta"] = _radio_checked(soup, "rbVenta1")
    result["arriendo"] = _radio_checked(soup, "rbArriendo1")

    pv = _input_val(soup, "tbPrecioVenta")
    if pv is not None:
        if _radio_checked(soup, "rbDiv2"):
            result["precio_venta"] = {"precio_uf": clean_price_uf(pv)}
        else:
            result["precio_venta"] = {"precio_clp": clean_price(pv)}

    pa = _input_val(soup, "tbPrecioArriendo")
    if pa is not None:
        if _radio_checked(soup, "rbDivA2"):
            result["precio_arriendo"] = {"precio_uf": clean_price_uf(pa)}
        else:
            result["precio_arriendo"] = {"precio_clp": clean_price(pa)}

    gc = _input_val(soup, "tbGastosComunes")
    result["gastos_comunes"] = clean_price(gc)
    return result


def parse_ubicacion(html: str, audit: dict) -> dict:
    soup = _bs(html)
    result = {}
    for sid, name in [("ddlre", "region"), ("ddlco", "comuna"), ("ddlse", "sector")]:
        result[name] = _selected_option(soup.find(id=sid))
    for sid, name in [
        ("tbCalle", "calle"), ("tbNumero", "numero"), ("tbUnidad", "unidad"),
        ("tbLetra", "letra"), ("tbEtapa", "etapa"), ("tbDireccionWeb", "direccion_referencial"),
    ]:
        result[name] = _input_val(soup, sid)
    extra = _extract_form_extra(
        soup.find(id="form_ubicacion"),
        excluded={"ddlre", "ddlco", "ddlse", "tbCalle", "tbNumero", "tbUnidad", "tbLetra", "tbEtapa", "tbDireccionWeb"},
    )
    if extra:
        result["extra"] = extra
    return result


def parse_caracteristicas(html: str, audit: dict) -> dict:
    soup = _bs(html)
    form = soup.find(id="form_caracteristicas")
    values: dict = {}
    if form is not None:
        for inp in form.find_all("input"):
            iid = inp.get("id")
            if not iid:
                continue
            itype = (inp.get("type") or "").lower()
            if itype in ("radio", "checkbox"):
                if inp.has_attr("checked"):
                    values[iid] = inp.get("value", "1")
            else:
                val = inp.get("value", "")
                if isinstance(val, list):
                    val = val[0] if val else ""
                val = str(val).strip()
                if val:
                    values[iid] = val
        for sel in form.find_all("select"):
            iid = sel.get("id")
            if not iid:
                continue
            val = _selected_option(sel)
            if val:
                values[iid] = val
        for ta in form.find_all("textarea"):
            iid = ta.get("id")
            if not iid:
                continue
            val = ta.get_text(strip=True)
            if val:
                values[iid] = val

    result = {}
    for form_id, norm in _CARACTERISTICAS_INT.items():
        if form_id in values:
            result[norm] = safe_int(values[form_id])
    for form_id, norm in _CARACTERISTICAS_FLOAT.items():
        if form_id in values:
            result[norm] = safe_float(values[form_id])
    for form_id, norm in _CARACTERISTICAS_SELECT.items():
        if form_id in values:
            result[norm] = values[form_id]

    cub = result.get("estacionamientos_cubiertos") or 0
    des = result.get("estacionamientos_descubiertos") or 0
    result["estacionamientos"] = (cub + des) if (cub or des) else None

    features = [_feature_name(iid) for iid in values if iid.startswith("rb") and iid.endswith("1")]
    if features:
        result["features"] = sorted(set(features))

    mapped = set(_CARACTERISTICAS_INT) | set(_CARACTERISTICAS_FLOAT) | set(_CARACTERISTICAS_SELECT)
    extra = {k: v for k, v in values.items() if k not in mapped}
    if extra:
        result["extra"] = extra
    return result


def parse_observaciones(html: str, audit: dict) -> dict:
    soup = _bs(html)
    result = {}
    result["descripcion"] = _input_val(soup, "tbObservaciones")
    result["observaciones_internas"] = _input_val(soup, "tbObservacionesInternas")
    titulo = None
    for el in soup.find_all(id=re.compile(r"^tbMeliTitulo")):
        val = el.get("value", "") if el.name == "input" else el.get_text(strip=True)
        if val:
            titulo = _clean_value(str(val).strip()) or None
            if titulo:
                break
    result["titulo"] = titulo
    result["forma_visita"] = titulo
    return result


def parse_datos_propietario(html: str, audit: dict) -> dict:
    soup = _bs(html)
    fp = soup.find(id="form_propietario")
    result = {
        "nombre": None, "email": None, "rut": None,
        "comuna": None, "fecha": None, "telefonos": [],
    }
    if fp is None:
        return result
    h4 = fp.find("h4")
    if h4:
        result["nombre"] = _clean_value(h4.get_text(strip=True)) or None
    mail_a = fp.select_one("a[href^=mailto]")
    if mail_a:
        result["email"] = _clean_value(mail_a.get_text(strip=True)) or None
    rut_m = re.search(r"\b\d{1,2}(?:\.?\d{3}){2}-[\dkK]\b", fp.get_text())
    if rut_m:
        result["rut"] = rut_m.group(0)
    telefonos = []
    for li in fp.select("li"):
        if li.select_one("i.fa-phone"):
            t = _clean_value(li.get_text(strip=True)) or None
            if t:
                telefonos.append(t)
    result["telefonos"] = telefonos
    result["telefono"] = telefonos[0] if telefonos else None
    result["fono_1"] = telefonos[0] if len(telefonos) > 0 else None
    result["fono_2"] = telefonos[1] if len(telefonos) > 1 else None
    result["fono_3"] = telefonos[2] if len(telefonos) > 2 else None
    for li in fp.select("li"):
        if li.select_one("i.fa-map-marker"):
            result["comuna"] = _clean_value(li.get_text(strip=True)) or None
        elif li.select_one("i.fa-calendar"):
            result["fecha"] = _clean_value(li.get_text(strip=True)) or None
    for dt in fp.select("dt"):
        label = dt.get_text(strip=True).lower()
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        value = _clean_value(dd.get_text(strip=True)) or None
        if label == "tipo" and not result.get("tipo_cliente"):
            result["tipo_cliente"] = value
        elif label.startswith("ingresado") and not result.get("ingresado_el"):
            result["ingresado_el"] = value
    return result


def parse_estado(html: str, audit: dict, listing_estado: str = "Activa") -> dict:
    soup = _bs(html)
    result = {
        "ejecutivo": _selected_option(soup.find(id="ddlej")),
        "oficina": OFICINA_NOMBRE,
        "disponible_prop360": True,
        "estado_prop360": listing_estado,
        "ultima_actualizacion": None,
    }
    patterns = [
        r"Última actualización\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?)",
        r"Ultima actualizacion\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?)",
    ]
    text = re.sub(r"<[^>]+>", " ", html)
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            result["ultima_actualizacion"] = m.group(1).strip()
            break
    return result


def parse_publicaciones(html: str, codigo: str, audit: dict) -> dict:
    data = {}
    soup = _bs(html)
    buckets = {
        "mercado_libre": [],
        "portalinmobiliario": [],
        "toctoc": [],
        "yapo": [],
        "procasa": [],
        "whatsapp": [],
    }
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href:
            continue
        href_lower = href.lower().strip()
        if "mailto:" in href_lower or "javascript:" in href_lower or "@" in href_lower:
            continue
        if "procasa.cl" in href_lower and ("/propiedades/" in href_lower or "/detalle/" in href_lower or str(codigo) in href_lower):
            buckets["procasa"].append(href)
        elif "portalinmobiliario" in href_lower:
            buckets["portalinmobiliario"].append(href)
        elif "mercadolibre" in href_lower:
            buckets["mercado_libre"].append(href)
        elif "toctoc.com" in href_lower:
            buckets["toctoc"].append(href)
        elif "yapo.cl" in href_lower:
            buckets["yapo"].append(href)
        elif "api.whatsapp.com" in href_lower:
            buckets["whatsapp"].append(href)

    if buckets["procasa"]:
        data["procasa"] = {"url_procasa": buckets["procasa"][0]}
    else:
        data["procasa"] = {"url_procasa": f"https://www.procasa.cl/{codigo}"}

    if buckets["whatsapp"]:
        data["whatsapp"] = {"url_whatsapp": buckets["whatsapp"][0]}

    pi = {}
    if buckets["mercado_libre"]:
        pi["url_mercado_libre"] = buckets["mercado_libre"][0]
        if len(buckets["mercado_libre"]) > 1:
            pi["urls_mercado_libre"] = buckets["mercado_libre"]
    if buckets["portalinmobiliario"]:
        pi["url_pi"] = buckets["portalinmobiliario"][0]
        if len(buckets["portalinmobiliario"]) > 1:
            pi["urls_pi"] = buckets["portalinmobiliario"]
    if pi:
        data["portal_inmobiliario"] = pi

    if buckets["toctoc"]:
        t = {"url_toctoc": buckets["toctoc"][0]}
        if len(buckets["toctoc"]) > 1:
            t["urls_toctoc"] = buckets["toctoc"]
        data["toctoc"] = t
    if buckets["yapo"]:
        y = {"url_yapo": buckets["yapo"][0]}
        if len(buckets["yapo"]) > 1:
            y["urls_yapo"] = buckets["yapo"]
        data["yapo"] = y
    return data


def _ml_to_pi(url: str) -> str:
    i = url.find("mercadolibre.cl/")
    if i > 0:
        return "https://portalinmobiliario.cl/" + url[i + len("mercadolibre.cl/"):]
    return ""


def _pub_entry(p: dict) -> dict:
    op = str(p.get("PropOperation") or "").upper()
    return {
        "url": p.get("Url") or "",
        "code": p.get("Code") or "",
        "code_unique": p.get("CodeUnique") or "",
        "title": p.get("Title") or "",
        "operacion": "Venta" if op == "V" else "Arriendo",
        "publicada": bool(p.get("Status") == 1),
        "estado": p.get("Status"),
        "expiration_date": p.get("ExpirationDate"),
        "quality": p.get("Quality"),
        "highlight": p.get("HighlightType"),
        "publication_type": p.get("PublicationType"),
    }


def parse_publicaciones_json(payload: dict, codigo: str) -> dict:
    if not isinstance(payload, dict):
        return {}
    by_portal = {}
    for portal in payload.get("publications") or []:
        name = (portal.get("Portal") or "").strip()
        entries = {}
        for p in portal.get("Publications") or []:
            op = str(p.get("PropOperation") or "").upper()
            if op not in ("V", "A"):
                continue
            entries[op] = _pub_entry(p)
        if entries:
            by_portal[name] = entries

    data = {}

    if "Sitio web propio" in by_portal:
        ops = by_portal["Sitio web propio"]
        procasa = {}
        if "V" in ops:
            procasa["url_procasa"] = ops["V"]["url"] or f"https://www.procasa.cl/{codigo}"
        if "A" in ops and ops["A"]["url"]:
            procasa["url_procasa_arriendo"] = ops["A"]["url"]
        procasa["publicaciones"] = ops
        data["procasa"] = procasa

    if "Portal Inmobiliario" in by_portal:
        ops = by_portal["Portal Inmobiliario"]
        pi = {}
        ml_urls = [ops[o]["url"] for o in ("V", "A") if o in ops and ops[o]["url"]]
        if ml_urls:
            pi["url_mercado_libre"] = ml_urls[0]
            pi["urls_mercado_libre"] = ml_urls
        pi_urls = [u for u in (_ml_to_pi(u) for u in ml_urls) if u]
        if pi_urls:
            pi["url_pi"] = pi_urls[0]
            pi["urls_pi"] = pi_urls
        pi["publicaciones"] = ops
        data["portal_inmobiliario"] = pi

    for name, key in (("TocToc", "toctoc"), ("Proppit", "proppit"), ("Yapo", "yapo")):
        if name in by_portal:
            ops = by_portal[name]
            d = {}
            url_field = {"toctoc": "url_toctoc", "proppit": "url_proppit", "yapo": "url_yapo"}[key]
            if "V" in ops and ops["V"]["url"]:
                d[url_field] = ops["V"]["url"]
            if "A" in ops and ops["A"]["url"]:
                d[url_field + "_arriendo"] = ops["A"]["url"]
            urls = [ops[o]["url"] for o in ("V", "A") if o in ops and ops[o]["url"]]
            if len(urls) > 1:
                d[url_field.replace("url_", "urls_")] = urls
            d["publicaciones"] = ops
            data[key] = d

    if "ChilePropiedades" in by_portal:
        ops = by_portal["ChilePropiedades"]
        d = {"publicaciones": ops}
        for o in ("V", "A"):
            if o in ops and ops[o]["code"]:
                d["codigo_" + ("venta" if o == "V" else "arriendo")] = ops[o]["code"]
        data["chilepropiedades"] = d

    return data


def parse_listing_price(precio_text: str | None):
    uf = None
    clp = None
    if precio_text:
        uf_m = re.search(r"UF\s*([\d.,]+)", precio_text, re.IGNORECASE)
        if uf_m:
            uf = normalize_numeric_for_compare(uf_m.group(1))
        clp_m = re.search(r"\$\s*([\d.,]+)", precio_text)
        if clp_m:
            clp = clean_price(clp_m.group(1))
    return uf, clp


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPING + DOCUMENTO
# ──────────────────────────────────────────────────────────────────────────────

_FICHA_PRINT_FIELD_MAP = {
    "Dormitorios": "dormitorios",
    "Baños": "banos",
    "Banos": "banos",
    "S. construida": "superficie_construida",
    "S. terreno": "superficie_terreno",
    "Sup. construida": "superficie_construida",
    "Sup. de terreno": "superficie_terreno",
    "Sup. total": "superficie_total",
    "Orientación": "orientacion",
    "Orientacion": "orientacion",
    "Año de construcción": "ano_construccion",
    "Ano de construcción": "ano_construccion",
    "Nº de pisos": "numero_pisos",
    "N de pisos": "numero_pisos",
    "Tipo de local": "tipo_local",
    "Recepción final": "prop_recep",
    "Centro comercial": "centro_comercial",
    "Tipo de calefacción": "tipo_calefaccion",
    "Tipo de agua": "tipo_agua",
    "Alcantarillado": "alcantarillado",
    "Nº de estacionamientos": "estacionamientos",
    "N de estacionamientos": "estacionamientos",
}


def _detect_print_price(txt: str | None):
    """Return (unit, value) for a print-ficha price string.

    ``UF 57.836`` → ("uf", 57836); ``$ 2.362.299.274`` → ("clp", ...);
    ``UF 118,03`` (comma decimal) → ("uf", 118.03) — UF reales, no centésimas.
    ``$ 68,55`` (comma decimal) → ("uf", 68.55).
    """
    if not txt:
        return None, None
    if "UF" in txt.upper():
        return "uf", clean_price_uf(txt)
    num = re.search(r"([\d.,]+)", txt)
    if not num:
        return None, None
    raw = num.group(1)
    if "," in raw:
        return "uf", clean_price_uf(raw)
    return "clp", clean_price(raw)


def clean_price_uf(raw: str | None) -> float | int | None:
    """Convierte un precio UF con coma decimal chilena a UF reales.

    ``"118,03"`` → 118.03 ; ``"57.836"`` → 57836 ; ``"50"`` → 50.
    ``"$ 4.820.910 UF 118,03"`` → 118.03 (extrae el bloque UF).
    Los valores se guardan como UF reales (sin factor ×100).
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    m = re.search(r"UF\s*([\d.,]+)", s)
    if m:
        s = m.group(1)
    else:
        s = s.replace("UF", "").replace("$", "").strip()
    # Detecta coma decimal: "118,03" → separador decimal = coma
    if "," in s:
        s = s.replace(".", "")      # puntos = miles
        s = s.replace(",", ".")     # coma = decimal
        try:
            val = float(s)
        except Exception:
            return None
        return round(val, 2)
    # Sin coma: entero UF puro ("57.836" → 57836)
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return None
    return int(digits)


def parse_ficha_imprimible(html: str, audit: dict | None = None) -> dict:
    """Parse the printable ficha (propFicha.aspx?print=1).

    Used for offices where propEditar does not render the editable form
    (permission by office). Returns the same section shape as the form-based
    parsers so ``build_doc`` works unchanged.
    """
    start = html.find("<!--Ficha en pantalla-->")
    end = html.find("//End Ficha en pantalla", start if start >= 0 else 0)
    seg = html[start:end] if (start >= 0 and end > start) else html
    soup = _bs(seg)

    tipo_op = {"tipo": None, "venta": False, "arriendo": False,
               "gastos_comunes": None, "precio_venta": None, "precio_arriendo": None}
    ubicacion = {k: None for k in ("region", "comuna", "sector", "calle", "numero",
                                   "unidad", "letra", "etapa", "direccion_referencial")}
    caracteristicas: dict = {}
    observaciones = {"descripcion": None, "observaciones_internas": None,
                     "titulo": None, "forma_visita": None}

    # Header: tipo + comuna from <h2 class='font-blue'>
    h2 = soup.find("h2", class_="font-blue")
    if h2:
        parts = [p.strip() for p in h2.get_text(strip=True).split(",")]
        if parts:
            tipo_op["tipo"] = parts[0] or None
        if len(parts) > 1:
            ubicacion["comuna"] = parts[1] or None
    # Region + addresses from the header text
    for label, key in [("Dirección web", "direccion_referencial"),
                       ("Dirección exacta", "calle")]:
        m = re.search(rf"<b>.*?{label}.*?</b>\s*([^<]+)<", seg, re.IGNORECASE)
        if m:
            ubicacion[key] = _clean_value(m.group(1).strip()) or None
    reg_m = re.search(r"Reg\.?\s*([A-ZÁ-Úa-zá-úñÑ]+(?:\s+[A-ZÁ-Úa-zá-úñÑ]+)*)", seg)
    if reg_m:
        ubicacion["region"] = reg_m.group(1).strip()

    # Price block: operation word + label (primary) + small (secondary)
    op_m = re.search(r"<h4[^>]*>([^<]*?)(?:<label|$)", seg, re.S)
    op_txt = _clean_value(op_m.group(1)) if op_m else None
    if op_txt:
        lower = op_txt.lower()
        if "venta" in lower or "vende" in lower:
            tipo_op["venta"] = True
        if "arriendo" in lower or "arrienda" in lower:
            tipo_op["arriendo"] = True
    label_m = re.search(r"<label[^>]*>([^<]+)</label>", seg, re.S)
    small_m = re.search(r"<small[^>]*>([^<]+)</small>", seg, re.S)
    prices = []
    if label_m:
        prices.append(label_m.group(1))
    if small_m:
        prices.append(small_m.group(1))
    venta_p = {"precio_uf": None, "precio_clp": None}
    arriendo_p = {"precio_uf": None, "precio_clp": None}
    target = venta_p if tipo_op["venta"] and not tipo_op["arriendo"] else (
        arriendo_p if tipo_op["arriendo"] and not tipo_op["venta"] else venta_p)
    for txt in prices:
        unit, val = _detect_print_price(txt)
        if unit and val is not None:
            target[f"precio_{unit}"] = val
    if any(v is not None for v in venta_p.values()):
        tipo_op["precio_venta"] = venta_p
    if any(v is not None for v in arriendo_p.values()):
        tipo_op["precio_arriendo"] = arriendo_p

    # Caracteristicas: <b>Label: </b>value + checkmark features
    for bm in re.finditer(r"<b>(.*?):\s*</b>\s*([^<]+)", seg):
        label = bm.group(1).strip()
        value = _clean_value(bm.group(2).strip())
        norm = _FICHA_PRINT_FIELD_MAP.get(label)
        if norm and value:
            if norm in ("dormitorios", "banos", "ano_construccion", "numero_pisos",
                        "estacionamientos"):
                caracteristicas[norm] = safe_int(value)
            elif norm in ("superficie_construida", "superficie_terreno", "superficie_total"):
                caracteristicas[norm] = safe_float(value.replace(".", "").replace(",", "."))
            else:
                caracteristicas[norm] = value
    feats = [f.strip() for f in re.findall(
        r"<i class='fa fa-check[^>]*></i>\s*([^<]+)", seg)]
    feats = [f for f in feats if f]
    if feats:
        caracteristicas["features"] = sorted(set(feats))

    # Observaciones: sections Descripcion / Forma de visitar / Observaciones internas
    desc_m = re.search(r">Descripción</h4>.*?</div>\s*<div[^>]*>(.*?)</div>", seg, re.S)
    if desc_m:
        observaciones["descripcion"] = _clean_value(re.sub(r"<[^>]+>", " ", desc_m.group(1)))
    fv_m = re.search(r">Forma de visitar</h4>.*?</div>\s*<div[^>]*>(.*?)</div>", seg, re.S)
    if fv_m:
        observaciones["forma_visita"] = _clean_value(re.sub(r"<[^>]+>", " ", fv_m.group(1)))
    oi_m = re.search(r">Observaciones internas</h4>.*?</div>\s*<div[^>]*>(.*?)</div>", seg, re.S)
    if oi_m:
        observaciones["observaciones_internas"] = _clean_value(re.sub(r"<[^>]+>", " ", oi_m.group(1)))

    if audit is not None:
        audit.setdefault("campos_esperados", []).extend(list(tipo_op) + list(ubicacion))
    return {
        "tipo_operacion": tipo_op,
        "ubicacion": ubicacion,
        "caracteristicas": caracteristicas,
        "observaciones": observaciones,
    }


def build_doc(codigo: str, listing_row: dict, parsed: dict) -> dict:
    tipo_op = parsed["tipo_operacion"]
    estado = parsed["estado"]
    datos_propietario = parsed["datos_propietario"]

    precio_clp = None
    precio_uf = None
    if isinstance(tipo_op.get("precio_venta"), dict):
        precio_clp = tipo_op["precio_venta"].get("precio_clp") or precio_clp
        precio_uf = tipo_op["precio_venta"].get("precio_uf") or precio_uf
    if isinstance(tipo_op.get("precio_arriendo"), dict):
        precio_clp = tipo_op["precio_arriendo"].get("precio_clp") or precio_clp
        precio_uf = tipo_op["precio_arriendo"].get("precio_uf") or precio_uf

    snapshot = {
        "codigo": codigo,
        "estado": listing_row.get("estado"),
        "operacion": listing_row.get("operacion"),
        "captador": listing_row.get("captador"),
        "comuna": listing_row.get("comuna"),
        "region": listing_row.get("region"),
        "tipo": listing_row.get("tipo"),
        "precio": listing_row.get("precio"),
        "direccion": listing_row.get("direccion"),
    }

    doc = {
        "codigo": codigo,
        "oficina_id": OFFICE_ID,
        "oficina_nombre": OFICINA_NOMBRE,
        "resumen": {
            "oficina": estado.get("oficina") or OFICINA_NOMBRE,
            "ejecutivo": estado.get("ejecutivo"),
            "ultima_actualizacion": estado.get("ultima_actualizacion"),
            "precio_clp": precio_clp,
            "precio_uf": precio_uf,
            "telefono": datos_propietario.get("telefono"),
            "disponible_prop360": True,
            "estado_prop360": estado.get("estado_prop360", "Activa"),
            "snapshot_listado": snapshot,
        },
        "tipo_operacion": tipo_op,
        "ubicacion": parsed["ubicacion"],
        "caracteristicas": parsed["caracteristicas"],
        "observaciones": parsed["observaciones"],
        "publicaciones": parsed["publicaciones"],
        "datos_propietario": datos_propietario,
        "estado": estado,
        "bitacora": parsed["bitacora"],
        "metadata": parsed["metadata"],
        "disponible_prop360": True,
    }
    return doc


def _form_has_content(html_edit: str) -> bool:
    """True when propEditar actually rendered the editable form.

    Properties from other offices redirect propEditar and render an empty
    form-body shell; those are scraped via the printable ficha instead.
    """
    if not html_edit:
        return False
    if 'id="ddltp"' in html_edit or 'id="tbPrecioVenta"' in html_edit:
        return True
    for form_id in ("form_tipo", "form_ubicacion", "form_caracteristicas"):
        m = re.search(rf'<div id="{form_id}" class="form-body">(.*?)</div>', html_edit, re.S)
        if m:
            inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if inner:
                return True
    return False


def scrape_propiedad(client: Prop360Client, codigo: str, listing_row: dict) -> dict:
    audit = {"campos_esperados": [], "campos_vacios": [], "selectors_failed": []}

    html_edit = client.get_propeditar(codigo)
    if not _form_has_content(html_edit):
        log.info(f"[{codigo}] propEditar sin form (otra oficina) → ficha imprimible.")
        return scrape_propiedad_imprimible(client, codigo, listing_row)

    tipo_op = parse_tipo_operacion(html_edit, audit)
    ubicacion = parse_ubicacion(html_edit, audit)
    caracteristicas = parse_caracteristicas(html_edit, audit)
    observaciones = parse_observaciones(html_edit, audit)

    client._wait()
    html_prop = client.get_propietario(codigo)
    datos_propietario = parse_datos_propietario(html_prop, audit)

    client._wait()
    html_est = client.get_estado(codigo)
    estado = parse_estado(html_est, audit, listing_estado=listing_row.get("estado") or "Activa")

    client._wait()
    publicaciones = parse_publicaciones_json(client.get_portales(codigo), codigo)
    html_pubs = parse_publicaciones(html_edit, codigo, audit)
    for k, v in html_pubs.items():
        if k == "whatsapp" and v:
            publicaciones["whatsapp"] = v
        elif k not in publicaciones and v:
            publicaciones[k] = v

    client._wait()
    bitacora = client.get_bitacora(codigo)

    tipo_propiedad = tipo_op.get("tipo")
    metadata = {
        "status": "ok",
        "fecha_scraping": now_iso(),
        "tipo_propiedad": tipo_propiedad,
        "tipo_propiedad_detectado": tipo_propiedad,
        "origen_ficha": "form_editable",
        "tabs": {
            "tipo_operacion": True,
            "ubicacion": bool(ubicacion),
            "caracteristicas": bool(caracteristicas),
            "observaciones": bool(observaciones),
            "portales": True,
        },
        "campos_esperados": sorted(set(audit.get("campos_esperados", []))),
        "campos_vacios": sorted(set(audit.get("campos_vacios", []))),
        "selectors_failed": sorted(set(audit.get("selectors_failed", []))),
        "source_url": FICHA_URL_TPL.format(codigo=codigo),
        "scraper": "scraping_prop360_ficha_completa.py",
        "version": SCRAPER_VERSION,
    }

    return build_doc(codigo, listing_row, {
        "tipo_operacion": tipo_op,
        "ubicacion": ubicacion,
        "caracteristicas": caracteristicas,
        "observaciones": observaciones,
        "datos_propietario": datos_propietario,
        "estado": estado,
        "publicaciones": publicaciones,
        "bitacora": bitacora,
        "metadata": metadata,
    })


def scrape_propiedad_imprimible(client: Prop360Client, codigo: str, listing_row: dict) -> dict:
    """Fallback for offices where propEditar is not editable (permisos por
    oficina): uses propFicha.aspx?print=1 + propPropietario + portales JSON."""
    audit = {"campos_esperados": [], "campos_vacios": [], "selectors_failed": []}

    html_print = client.get_ficha_imprimible(codigo)
    if not html_print or "fichaPropiedad" not in html_print:
        raise RuntimeError("propFicha imprimible no cargó la ficha")

    parsed = parse_ficha_imprimible(html_print, audit)
    tipo_op = parsed["tipo_operacion"]
    ubicacion = parsed["ubicacion"]
    caracteristicas = parsed["caracteristicas"]
    observaciones = parsed["observaciones"]

    client._wait()
    html_prop = client.get_propietario(codigo)
    datos_propietario = parse_datos_propietario(html_prop, audit)

    estado = {
        "ejecutivo": listing_row.get("captador"),
        "oficina": OFICINA_NOMBRE,
        "disponible_prop360": True,
        "estado_prop360": listing_row.get("estado") or "Activa",
        "ultima_actualizacion": None,
    }

    client._wait()
    publicaciones = parse_publicaciones_json(client.get_portales(codigo), codigo)

    client._wait()
    bitacora = client.get_bitacora(codigo)

    tipo_propiedad = tipo_op.get("tipo")
    metadata = {
        "status": "ok",
        "fecha_scraping": now_iso(),
        "tipo_propiedad": tipo_propiedad,
        "tipo_propiedad_detectado": tipo_propiedad,
        "origen_ficha": "ficha_imprimible",
        "tabs": {
            "tipo_operacion": True,
            "ubicacion": bool(ubicacion),
            "caracteristicas": bool(caracteristicas),
            "observaciones": bool(observaciones),
            "portales": True,
        },
        "campos_esperados": sorted(set(audit.get("campos_esperados", []))),
        "campos_vacios": sorted(set(audit.get("campos_vacios", []))),
        "selectors_failed": sorted(set(audit.get("selectors_failed", []))),
        "source_url": FICHA_PRINT_URL_TPL.format(codigo=codigo),
        "scraper": "scraping_prop360_ficha_completa.py",
        "version": SCRAPER_VERSION,
    }

    return build_doc(codigo, listing_row, {
        "tipo_operacion": tipo_op,
        "ubicacion": ubicacion,
        "caracteristicas": caracteristicas,
        "observaciones": observaciones,
        "datos_propietario": datos_propietario,
        "estado": estado,
        "publicaciones": publicaciones,
        "bitacora": bitacora,
        "metadata": metadata,
    })


# ──────────────────────────────────────────────────────────────────────────────
# MONGODB
# ──────────────────────────────────────────────────────────────────────────────

def get_mongo_collection(collection_name: str):
    client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000)
    db = client[Config.DB_NAME]
    return client, db[collection_name]


def upsert_ficha(coll, doc: dict) -> tuple[bool, bool]:
    existing = coll.find_one({"codigo": doc["codigo"]}) or {}
    for section in (
        "publicaciones",
        "datos_propietario",
        "tipo_operacion",
        "ubicacion",
        "caracteristicas",
        "observaciones",
        "estado",
        "resumen",
    ):
        existing_sec = existing.get(section)
        new_sec = doc.get(section)
        if isinstance(existing_sec, dict) and isinstance(new_sec, dict):
            doc[section] = {**existing_sec, **new_sec}
    doc = canonicalize_prices(doc)
    previous_hash = existing.get("audit_hash")
    current_hash = audit_hash(doc)
    changed = previous_hash != current_hash

    # Reactivación: el doc estaba BAJA (disponible=False) y el nuevo scrape lo
    # trae disponible. Registra la reactivación y limpia el marcador de baja.
    if not bool(existing.get("disponible_prop360", True)) and bool(doc.get("disponible_prop360", True)):
        doc["fecha_reactivacion"] = now_iso()
        doc["fecha_baja_automatica"] = None
        doc["baja_origen"] = None
        changed = True

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

    result = coll.update_one({"codigo": doc["codigo"]}, {"$set": doc}, upsert=True)
    nuevo = bool(result.upserted_id)
    actualizado = not nuevo and result.modified_count > 0
    return nuevo, actualizado


def upsert_error(coll, codigo: str, error_msg: str) -> None:
    doc_error = {
        "codigo": codigo,
        "resumen": {"disponible_prop360": False},
        "disponible_prop360": False,
        "metadata": {
            "status": "error",
            "error": str(error_msg),
            "fecha_scraping": now_iso(),
            "scraper": "scraping_prop360_ficha_completa.py",
            "version": SCRAPER_VERSION,
        },
    }
    try:
        coll.update_one({"codigo": codigo}, {"$set": doc_error}, upsert=True)
    except Exception as e:
        log.error(f"[{codigo}] No se pudo persistir error en Mongo: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# DETECCIÓN DE CAMBIOS
# ──────────────────────────────────────────────────────────────────────────────

def _row_missing_or_stale(existing: dict) -> bool:
    if not existing:
        return True
    if not existing.get("audit_hash"):
        return True
    meta = existing.get("metadata") or {}
    if meta.get("status") == "error":
        return True
    if not existing.get("caracteristicas"):
        return True
    if "bitacora" not in existing:
        return True
    return False


def _snapshot_dict(row: dict) -> dict:
    return {
        "codigo": row.get("codigo"),
        "estado": row.get("estado"),
        "operacion": row.get("operacion"),
        "captador": row.get("captador"),
        "comuna": row.get("comuna"),
        "region": row.get("region"),
        "tipo": row.get("tipo"),
        "precio": row.get("precio"),
        "direccion": row.get("direccion"),
    }


def needs_update(existing: dict, row: dict) -> bool:
    if _row_missing_or_stale(existing):
        return True
    # Reactivación: un doc marcado BAJA (disponible_prop360=False) que vuelve a
    # aparecer Activa en el listado debe re-scrapearse para volver a marcarse
    # disponible, aunque el snapshot del listado no haya cambiado.
    if not bool(existing.get("disponible_prop360", True)):
        return True
    resumen = existing.get("resumen") or {}
    snap = resumen.get("snapshot_listado")
    if isinstance(snap, dict):
        return normalize_text_for_compare(snap) != normalize_text_for_compare(_snapshot_dict(row))

    checks = [
        (resumen.get("ejecutivo"), row.get("captador")),
        ((existing.get("ubicacion") or {}).get("comuna"), row.get("comuna")),
        ((existing.get("ubicacion") or {}).get("region"), row.get("region")),
        ((existing.get("tipo_operacion") or {}).get("tipo"), row.get("tipo")),
    ]
    for stored, cur in checks:
        if normalize_text_for_compare(stored) != normalize_text_for_compare(cur):
            return True

    op_set = {x.strip().lower() for x in (row.get("operacion") or "").split(",") if x.strip()}
    tipo_op = existing.get("tipo_operacion") or {}
    for flag in ("venta", "arriendo"):
        if flag in op_set and not bool(tipo_op.get(flag)):
            return True

    uf, clp = parse_listing_price(row.get("precio"))
    if uf is not None and resumen.get("precio_uf") is not None and normalize_numeric_for_compare(resumen.get("precio_uf")) != uf:
        return True
    if clp is not None and resumen.get("precio_clp") is not None and normalize_numeric_for_compare(resumen.get("precio_clp")) != clp:
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# RUNNER
# ──────────────────────────────────────────────────────────────────────────────

def mark_bajas(coll, active_codes: set[str], dry_run: bool = False) -> int:
    # Bajas solo dentro de la oficina actual: evita marcar como baja a las
    # propiedades de otras oficinas cuando se sincroniza una oficina distinta.
    query = {
        "disponible_prop360": True,
        "codigo": {"$nin": list(active_codes)},
        "$or": [
            {"oficina_id": OFFICE_ID},
            {"resumen.oficina": OFICINA_NOMBRE},
            {"oficina_nombre": OFICINA_NOMBRE},
        ],
    }
    count = coll.count_documents(query)
    if count == 0:
        log.info("No se detectaron bajas nuevas.")
        return 0
    if dry_run:
        log.warning(f"[DRY-RUN] {count} propiedad(es) se marcarían como BAJA (no Activa en listado).")
        return count
    coll.update_many(
        query,
        {
            "$set": {
                "disponible_prop360": False,
                "resumen.disponible_prop360": False,
                "estado.disponible_prop360": False,
                "fecha_baja_automatica": now_iso(),
                "baja_origen": "sync_prop360_http_v2",
            }
        },
    )
    log.warning(f"⚠  {count} propiedad(es) marcadas como BAJA (no Activa en listado).")
    return count


def run(args, office_id: int | None = None) -> int:
    email = os.getenv("PROP360_EMAIL")
    password = os.getenv("PROP360_PASSWORD")
    if not email or not password:
        log.error("PROP360_EMAIL y PROP360_PASSWORD deben estar definidos")
        return 2

    # Oficina explícita (multi-oficina) o la del entorno.
    global OFFICE_ID, OFICINA_NOMBRE
    if office_id is not None:
        OFFICE_ID = office_id
        OFICINA_NOMBRE = OFICINAS.get(office_id, f"OFICINA {office_id}")

    mongo_client, coll = get_mongo_collection(COLLECTION_NAME)
    coll.create_index("codigo", unique=True)

    client = Prop360Client(email, password, delay=args.delay)
    try:
        client.login()
    except Prop360AuthError as e:
        log.error(f"Error de autenticación: {e}")
        mongo_client.close()
        return 2

    log.info("Descargando listado Activa de la oficina…")
    rows = client.fetch_listing(OFFICE_ID)
    by_code = {r["codigo"]: r for r in rows if r.get("codigo")}
    activa = {c: r for c, r in by_code.items() if (r.get("estado") or "").strip() == "Activa"}
    log.info(f"Listado ofi={OFFICE_ID}: total={len(by_code)} | Activa={len(activa)}")

    if args.codigo:
        codigo = str(args.codigo).strip()
        listing_row = activa.get(codigo) or by_code.get(codigo) or {"codigo": codigo, "estado": "Activa"}
        return run_codigo(client, coll, codigo, listing_row, args)

    db_docs = {str(d["codigo"]): d for d in coll.find({}, {"codigo": 1, "resumen": 1, "ubicacion": 1, "tipo_operacion": 1, "caracteristicas": 1, "bitacora": 1, "metadata": 1, "audit_hash": 1})}
    active_codes = set(activa)

    nuevas = sorted(c for c in active_codes if c not in db_docs)
    a_actualizar = sorted(
        c for c in active_codes
        if c in db_docs and needs_update(db_docs[c], activa[c])
    )

    log.info(f"NUEVAS={len(nuevas)} | A ACTUALIZAR={len(a_actualizar)} | YA AL DÍA={len(active_codes) - len(nuevas) - len(a_actualizar)}")

    bajas = 0
    if not args.no_bajas:
        bajas = mark_bajas(coll, active_codes, dry_run=args.dry_run)

    if args.backfill:
        scrape_list = sorted(active_codes)
        log.info("Modo backfill: re-scrapeando todas las propiedades Activa.")
    else:
        if args.max_new is not None:
            nuevas = nuevas[: args.max_new]
        if args.max_update is not None:
            a_actualizar = a_actualizar[: args.max_update]
        scrape_list = nuevas + a_actualizar

    if args.limit:
        scrape_list = scrape_list[: args.limit]

    if not scrape_list:
        log.info("No hay propiedades para scrapear. Bajas=%s", bajas)
        mongo_client.close()
        return 0

    log.info(f"Total a scrapear: {len(scrape_list)} (nuevas={len(nuevas)}, actualizar={len(a_actualizar)})")
    return scrape_list_batch(client, coll, scrape_list, activa, args, mongo_client)


def run_codigo(client: Prop360Client, coll, codigo: str, listing_row: dict, args) -> int:
    log.info(f"Scrapeando código único: {codigo}")
    return scrape_list_batch(client, coll, [codigo], {codigo: listing_row}, args, None)


def run_all_offices(args) -> int:
    """Corre el ciclo completo para todas las oficinas activas de OFICINAS.

    Detecta nuevas, actualizaciones y bajas por oficina, de modo que la cartera
    universo queda completa y al día. La oficina 4 está vacía y se omite.
    """
    offices = [oid for oid in sorted(OFICINAS) if oid != 4]
    worst = 0
    for oid in offices:
        log.info("=" * 60)
        log.info(f"OFICINA {oid}: {OFICINAS[oid]}")
        log.info("=" * 60)
        code = run(args, office_id=oid)
        if code:
            worst = code

    _generate_embeddings_for_pending()
    return worst


def _generate_embeddings_for_pending() -> int:
    """Genera embeddings de las propiedades nuevas/actualizadas sin vector.

    Se ejecuta al final del ciclo de todas las oficinas. Solo procesa los docs
    que aún no tienen `vector_descripcion`. Si el modelo no está disponible
    (ej. memoria insuficiente en Render), no hace nada y lo registra.
    """
    try:
        from chatbot.semantic_engine import update_embeddings_bulk
    except Exception as exc:
        log.warning(f"[EMBEDDINGS] No se pudo importar generador de embeddings: {exc}")
        return 0
    try:
        total = 0
        while True:
            count = update_embeddings_bulk(batch_size=100)
            if not count:
                break
            total += count
        if total:
            log.info(f"[EMBEDDINGS] Vectores generados en este ciclo: {total}")
        return total
    except Exception as exc:
        log.warning(f"[EMBEDDINGS] Error generando embeddings: {exc}")
        return 0


def scrape_list_batch(client, coll, scrape_list, activa: dict, args, mongo_client) -> int:
    ok_count = nuevo_count = upd_count = err_count = 0
    errores = []
    tipos_detectados = {}

    for i, codigo in enumerate(scrape_list, start=1):
        log.info(f"[{i}/{len(scrape_list)}] Procesando código {codigo}…")
        try:
            listing_row = activa.get(codigo) or {"codigo": codigo, "estado": "Activa"}
            doc = scrape_propiedad(client, codigo, listing_row)
            t_det = doc.get("metadata", {}).get("tipo_propiedad_detectado")
            if t_det:
                tipos_detectados[t_det] = tipos_detectados.get(t_det, 0) + 1
            if args.dry_run:
                log.info(f"[{codigo}] DRY-RUN: procesado ok (tipo={t_det})")
                ok_count += 1
                continue
            nuevo, actualizado = upsert_ficha(coll, doc)
            if nuevo:
                nuevo_count += 1
                log.info(f"[{codigo}] → NUEVO insertado.")
            elif actualizado:
                upd_count += 1
                log.info(f"[{codigo}] → Actualizado.")
            else:
                log.info(f"[{codigo}] → Sin cambios.")
            ok_count += 1
        except Exception as exc:
            log.error(f"[{codigo}] Error inesperado: {exc}")
            errores.append(codigo)
            err_count += 1
            if not args.dry_run:
                upsert_error(coll, codigo, str(exc))
        client._wait()

    W = 56
    print("\n" + "═" * W)
    print("  REPORTE SCRAPING PROP360 — FICHA COMPLETA (HTTP v2)")
    print("═" * W)
    print(f"  Dry-run            : {'SÍ' if args.dry_run else 'NO'}")
    print(f"  Colección          : {COLLECTION_NAME}")
    print("─" * W)
    print(f"  Procesadas         : {len(scrape_list)}")
    print(f"  Correctas (ok)     : {ok_count}")
    print(f"    ├─ Nuevas        : {nuevo_count}")
    print(f"    └─ Actualizadas  : {upd_count}")
    print(f"  Errores            : {err_count}")
    if errores:
        print(f"  Códigos con error  : {', '.join(errores)}")
    print("─" * W)
    print("  Tipos detectados:")
    for tk, tv in tipos_detectados.items():
        print(f"    {tk}: {tv}")
    print("═" * W + "\n")

    if mongo_client:
        mongo_client.close()
    return 1 if err_count and not args.dry_run else 0


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scraper de fichas completas Prop360 (HTTP v2)")
    parser.add_argument("--dry-run", action="store_true", help="No escribir en MongoDB")
    parser.add_argument("--codigo", help="Scrapear un único código de propiedad")
    parser.add_argument("--max-new", type=int, default=None, help="Limitar propiedades nuevas")
    parser.add_argument("--max-update", type=int, default=None, help="Limitar propiedades a actualizar")
    parser.add_argument("--limit", type=int, default=None, help="Límite total de propiedades a scrapear")
    parser.add_argument("--backfill", action="store_true", help="Re-scrapear todas las propiedades Activa")
    parser.add_argument("--all-offices", action="store_true", help="Correr todas las oficinas activas de OFICINAS")
    parser.add_argument("--no-bajas", action="store_true", help="No marcar bajas")
    parser.add_argument("--delay", type=float, default=0.3, help="Delay entre peticiones (segundos)")
    args = parser.parse_args()

    if args.all_offices:
        sys.exit(run_all_offices(args))
    sys.exit(run(args))


if __name__ == "__main__":
    main()
