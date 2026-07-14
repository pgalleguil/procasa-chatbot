import os
import sys
import argparse
import asyncio
import random
import logging
import re
import json
import hashlib
from datetime import datetime, timezone, timedelta
from unicodedata import normalize
from urllib.parse import urljoin, urlparse, urlunparse
from itertools import cycle
from collections import defaultdict, OrderedDict
import time
from threading import Lock

from curl_cffi import requests as curl_requests
from fake_useragent import UserAgent
from playwright.async_api import async_playwright
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from owner_scoring import (
    build_source_signal_snapshot,
    calculate_owner_score,
    compute_publisher_activity,
    property_fingerprint,
    propose_classification_state,
)

from playwright_stealth import Stealth

async def apply_stealth(page):
    """Aplica tÃ©cnicas de sigilo al navegador."""
    await Stealth().apply_stealth_async(page)

# ====================== CONFIG ======================
CONFIG = {
    "portal_name": "yapo.cl",
    "max_pages": 45,              # MÃ¡ximo de pÃ¡ginas que puede recorrer Discovery
    "max_urls_per_session": 1000,   # MÃ¡ximo de URLs nuevas antes de pasar a Etapa 2
    "stop_after_empty_pages": 0,  # PÃ¡ginas consecutivas sin novedades para detenerse (0=siempre hasta max)
    "max_scrolls": 4,
    "scroll_delay": 0.9,
    "max_concurrency": 15,
    "page_load_timeout": 30,      # 30s â€” si no carga, proxy lento â†’ rotar
    "networkidle_timeout": 10,
    "html_max_chars": 16000,
    "desc_max_chars": 8000,
    #"base_url": "https://www.yapo.cl/bienes-raices-alquiler?regionslug=region-metropolitana-nunoa,region-metropolitana-providencia,region-metropolitana-macul,region-metropolitana-san-miguel,region-metropolitana-penalolen,region-metropolitana-la-florida&q=withcat.bienes-raices-alquiler-apartamentos,bienes-raices-alquiler-comercios,bienes-raices-alquiler-casas",
    #"base_url": "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-la-florida,region-metropolitana-macul&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,bienes-raices-venta-de-propiedades-casas|f_price.80000000-|f_currency.CLP",
    #"base_url": "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-la-florida,region-metropolitana-macul&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,bienes-raices-venta-de-propiedades-casas|f_price.80000000-|f_currency.CLP",
    #"base_url": "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=maule-talca&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,bienes-raices-venta-de-propiedades-casas,bienes-raices-venta-de-propiedades-lotes-y-terrenos,bienes-raices-venta-de-propiedades-fincas|f_price.60000000-|f_currency.CLP",
    #"base_url": "https://www.yapo.cl/bienes-raices-alquiler/maule-talca?q=withcat.bienes-raices-alquiler-apartamentos,bienes-raices-alquiler-casas,bienes-raices-alquiler-comercios,bienes-raices-alquiler-apartamentos-amueblados,bienes-raices-alquiler-lotes-y-terrenos,bienes-raices-alquiler-negocios|f_rent.400000-|f_currency.CLP",
    #"base_url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades/region-metropolitana-san-miguel?q=withcat.bienes-raices-venta-de-propiedades-casas,bienes-raices-venta-de-propiedades-apartamentos|f_price.80000000-",
    "base_url": "https://www.yapo.cl/bienes-raices-venta-de-propiedades/region-metropolitana-santiago?q=withcat.bienes-raices-venta-de-propiedades-apartamentos|f_price.140000000-",
    "max_retries_per_url": 3,
    "max_retries_per_url": 3,
    "hours_to_recheck": 12,
    "urls_per_session": 30,
}

# ====================== GLOBAL STATE ======================
PROXY_USAGE = defaultdict(int)
PROXY_MB_USAGE = defaultdict(float)
BURNED_PROXIES = {} # proxy -> cooldown_until (datetime)

# Excepciones personalizadas para manejo de proxies
class ProxyBlockedError(Exception): pass
class CaptchaError(Exception): pass

grok_client = AsyncOpenAI(
    api_key=Config.DEEPSEEK_API_KEY,
    base_url=Config.DEEPSEEK_BASE_URL
)

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
# Silenciar logs ruidosos de librerÃ­as
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ====================== HELPERS ======================
async def get_uf_value() -> float:
    try:
        # Usamos headers para evitar que la API de mindicador nos bloquee
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        async with curl_requests.AsyncSession(timeout=10.0, headers=h, impersonate="chrome120") as client:
            r = await client.get("https://api.gael.cl/general/public/valida/uf")
            if r.status_code == 200:
                val = float(r.json()['Valor'].replace(".","").replace(",","."))
                logging.info(f"ðŸ’± UF (vÃ­a Gael): ${val:,.2f}")
                return val
            # Tercer intento - mindicador
            r = await client.get("https://mindicador.cl/api/uf")
            if r.status_code == 200:
                val = float(r.json()['serie'][0]['valor'])
                return val
    except: pass
    logging.warning("âš ï¸ Sin API de UF. Usando $39,800 CLP.")
    return 39800.0

async def get_proxies_from_api(api_url: str) -> list:
    """Obtiene una lista de proxies desde una URL API."""
    try:
        async with curl_requests.AsyncSession(timeout=10.0, impersonate="chrome120") as client:
            resp = await client.get(api_url)
            if resp.status_code == 200:
                # Asumimos que la API devuelve proxies separados por lÃ­nea o en JSON
                text = resp.text.strip()
                if text.startswith("[") or text.startswith("{"):
                    # Si es JSON, intentamos parsearlo
                    data = resp.json()
                    if isinstance(data, list): return data
                    if isinstance(data, dict) and "proxies" in data: return data["proxies"]
                return [p.strip() for p in text.splitlines() if p.strip()]
    except Exception as e:
        logging.error(f"âŒ Error obteniendo proxies de API: {e}")
    return []

def parse_price_components(price_str: str):
    if not price_str or price_str == "N/A":
        return None, None
    s = price_str.upper()
    
    # Extraer UF primero (es mÃ¡s especÃ­fico)
    uf_match = re.search(r'UF\s*([\d.,]+)', s)
    uf_val = None
    if uf_match:
        try: uf_val = clean_float(uf_match.group(1))
        except: pass

    # Extraer CLP ($)
    clp_match = re.search(r'(?:CLP|\$)\s*([\d.,]+)', s)
    clp_val = None
    if clp_match:
        try: clp_val = clean_num(clp_match.group(1))
        except: pass

    return uf_val, clp_val

def normalize_text(text: str, max_chars: int = None) -> str:
    if not text or text == "N/A":
        return "N/A"
    text = normalize('NFKD', text.lower()).encode('ASCII', 'ignore').decode('ASCII')
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_chars] if max_chars else text

def clean_num(s):
    """Limpia un string de nÃºmero (quita puntos, comas, sÃ­mbolos) y retorna int o None."""
    if s is None or s == "N/A": return None
    if isinstance(s, (int, float)): return int(s)
    
    # Remover sÃ­mbolos de moneda y espacios
    val = re.sub(r'[^\d.,]', '', str(s))
    if not val: return None
    
    try:
        if '.' in val or ',' in val:
            f = clean_float(s)
            return int(f) if f is not None else None
        
        nums = re.findall(r'\d+', val)
        return int(nums[0]) if nums else None
    except Exception:
        return None
def clean_float(s):
    """Limpia un string de nÃºmero preservando decimales y eliminando sÃ­mbolos."""
    if s is None or s == "N/A": return None
    if isinstance(s, (int, float)): return float(s)
    
    # Conservar solo dÃ­gitos y separadores
    val = re.sub(r'[^\d.,]', '', str(s))
    if not val: return None

    try:
        if ',' in val and '.' in val:
            if val.rfind('.') > val.rfind(','): return float(val.replace(',', ''))
            else: return float(val.replace('.', '').replace(',', '.'))
        
        sep = ',' if ',' in val else '.' if '.' in val else None
        if not sep: return float(val)
        
        parts = val.split(sep)
        # LÃ³gica chilena: si hay mÃºltiples puntos o el Ãºltimo grupo es de 3, es separador de miles
        if val.count(sep) > 1 or len(parts[-1]) == 3:
            res_s = val.replace(sep, '')
            return float(res_s)
        
        # De lo contrario, es separador decimal
        return float(val.replace(',', '.'))
    except Exception:
        # Fallback agresivo con regex
        nums = re.findall(r'[\d.,]+', val)
        if nums:
            n = nums[0]
            try:
                if ',' in n and '.' in n: return float(n.replace(',', ''))
                return float(n.replace(',', '.'))
            except: pass
    return None

def generate_content_hash(title: str, description: str) -> str:
    """Hash robusto: normaliza texto y elimina ruidos (precios, telÃ©fonos) para evitar duplicados."""
    # 1. Combinar y bajar a minÃºsculas
    text = f"{title} {description}".lower()
    # 2. Quitar acentos (NFKD)
    text = normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII')
    # 3. Eliminar ruidos comunes que los dueÃ±os cambian frecuentemente:
    # - Precios ($ o UF seguido de nÃºmeros)
    # - TelÃ©fonos (+56, 9 xxx, etc.)
    # - Superficies (m2)
    text = re.sub(r'(clp|\$|uf|m2)\s*[\d\.,]+', '', text) 
    text = re.sub(r'(\+?56\s?9?|9)\s?\d{7,8}', '', text)  
    # 4. Quedarse solo con lo alfanumÃ©rico core
    text = re.sub(r'[^a-z0-9]', '', text)
    # 5. Tomar una muestra significativa (ej: 800 chars) para el MD5
    return hashlib.md5(text[:800].encode('utf-8')).hexdigest()

def normalize_url(href: str) -> str:
    full = urljoin("https://www.yapo.cl", href)
    p = urlparse(full)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))

# â”€â”€ Mapas de etiquetas Normalizadas (sin acento) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Se comparan contra claves que han sido procesadas con _normalize_key()
_INSIGHT_DORMITORIOS  = {"dormitorios", "dormitorio", "habitaciones", "habitacion", "recamaras"}
_INSIGHT_BANOS        = {"banos", "bano"}
_INSIGHT_ESTAC        = {"estacionamientos", "estacionamiento", "parking", "garaje", "cochera"}
_INSIGHT_M2           = {"area construida", "m2 construidos", "superficie", "m2 totales", "area", "metros cuadrados"}
_INSIGHT_PRECIO       = {"precio", "valor"}
_INSIGHT_TITULO       = {"titulo", "nombre"}
_DETAIL_GASTOS        = {"costos de mantenimiento", "gastos comunes", "mantenimiento", "gastos de mantenimiento"}
_DETAIL_M2_TOTAL      = {"m2 totales", "m2 total", "superficie total"}
_DETAIL_M2_UTIL       = {"m2 utiles", "m2 util", "superficie util"}
_DETAIL_ESTAC         = {"estacionamientos", "estacionamiento", "parking"}
_DETAIL_PISCINA       = {"piscina"}
_DETAIL_DIRECCION     = {"direccion exacta", "direccion", "ubicacion"}
_DETAIL_PUBLICADO     = {"publicado"}

# --- LÃ“GICA DE DETECCIÃ“N DE CORREDORES ---
_BROKER_KEYWORDS = {
    # --- Grandes Franquicias y Cadenas ---
    "remax", "re/max", "re max", "re-max", "century 21", "c21", "engel", "vÃ¶lkers", "volkers", 
    "keller williams", "kw chile", "coldwell banker", "sothebys", "realty", 
    "betterhomes", "property partners", "zillow", "houm", "isbast", "buydepa",

    # --- Corredoras Tradicionales (Chile) ---
    "fuenzalida", "ahumada", "valdivieso", "larrain", "mclean", "prevost", 
    "quinteros", "skyline", "one propiedades", "maca", "habitab", "grupocasa", 
    "buscapro", "copro", "toctoc", "procasa", "mateo sÃ¡nchez", "marcos sÃ¡nchez",
    "pablo cassini", "fajre", "besnier", "uribe", "soza", "morandÃ©", "dante",
    "matias ruffat", "vivaqui", "golden", "infofit", "p&g", "pgr", "pro urbe",
    "urzuÌa", "jaime masmela", "pizarro propiedades", "carreÃ±o", "puelma",
    "assetplan", "nexxos", "hyc", "h y c", "arrendo", "arriendo plus",
    "mueve chile", "plusrent", "rentahouse", "findep", "arrendaplus", "alucerto",

    # --- Inmobiliarias / Constructoras ---
    "socovesa", "almagro", "aconcagua", "ingevec", "imagina", "rvc", "salfacorp", 
    "sinergia", "ebco", "euroinmobiliaria", "manquehue", "moller", "siena", 
    "paz corp", "besalco", "su ksa", "fundamenta", "activa", "armas", "iman",
    "ictinos", "desa", "claro vicuÃ±a", "valmar", "enaco", "pocuro", "indesa",

    # --- ConsultorÃ­a e InversiÃ³n ---
    "colliers", "jll", "cushman", "wakefield", "gps property", "fitzroy", 
    "asset", "capital", "management", "investment", "inversiÃ³n", "inversiones",
    "renta", "patrimonio", "valoriza", "tasaciones", "gestiÃ³n", "proyectos",

    # --- Identificadores Legales ---
    "cia ltda", "compaÃ±ia limitada", "sociedad", "spa", "s.a.", "eirl", "asociados", 
    "group", "partners", "consulting", "holding", "legal", "estudio", "propiedades",
    "inmobiliaria", "corredora", "corretaje", "broker", "real estate",
    "ejecutivo", "asesor", "habitacional", "comercializadora", "bienes raices",
    "corredor de propiedades", "gestora", "admon", "administraciÃ³n",
    "comisiÃ³n mÃ¡s iva", "vende inmobiliaria", "arrienda inmobiliaria"
}

# Abreviaciones peligrosas que necesitan lÃ­mites de palabra (\b) para evitar falsos positivos
_BROKER_ABREVIATIONS = {"sa", "spa", "kw", "c21", "id", "p&g", "pgr", "m2", "sii", "esa", "val"}

_BROKER_REGEXES = [re.compile(rf'\b{re.escape(kw)}\b') for kw in _BROKER_KEYWORDS if len(kw) <= 5 or kw in _BROKER_ABREVIATIONS]

# --- OPTIMIZACIÃ“N DE COSTOS ---
# CachÃ© en memoria LRU+TTL Thread-Safe para no clasificar a la misma corredora mÃºltiples veces
class TTLLRUCache:
    def __init__(self, maxsize=5000, ttl=86400):
        self.cache = OrderedDict()
        self.maxsize = maxsize
        self.ttl = ttl
        self.lock = Lock()
    def get(self, key):
        with self.lock:
            if key in self.cache:
                val, timestamp = self.cache[key]
                if time.time() - timestamp > self.ttl:
                    del self.cache[key]
                    return None
                self.cache.move_to_end(key)
                return val
            return None
    def set(self, key, value):
        with self.lock:
            if key in self.cache:
                del self.cache[key]
            self.cache[key] = (value, time.time())
            if len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)

SELLER_CACHE = TTLLRUCache()

def classify_seller_state(
    seller_name: str,
    description: str,
    company_name: str = "N/A",
    seller_profile_id: str = "N/A",
    seller_is_pro: bool = False,
    broker_brand: str = "N/A",
    multi_publisher_count: int | None = None,
) -> dict:
    """ClasificaciÃ³n conservadora en 3 estados.

    Regla rectora: si no hay evidencia suficiente, devolver INCIERTO.
    """
    full_text = f"{seller_name} {company_name} {description} {broker_brand}".lower()
    broker_signals = []
    owner_signals = []

    # SeÃ±ales fuertes de corredor
    if broker_brand and broker_brand != "N/A":
        broker_signals.append(("broker_brand", 4, "broker_brand detectado en HTML"))
    if seller_is_pro:
        broker_signals.append(("seller_is_pro", 3, "badge Profesional real detectado"))
    if company_name and company_name != "N/A" and any(k in normalize_text(company_name).lower() for k in [
        "remax", "re/max", "century 21", "c21", "inmobiliaria", "propiedades", "corretaje", "ltda", "spa", "eirl", "real estate"
    ]):
        broker_signals.append(("company_name", 3, "nombre corporativo detectable"))
    if any(k in full_text for k in ["contact_logo", "agency_logo"]):
        broker_signals.append(("logo_corporativo", 4, "logo corporativo detectado"))
    # The raw lifetime count is not brokerage evidence. Temporal activity is
    # evaluated by the shared scoring engine with a 90-day window.
    if is_likely_broker(seller_name, description, company_name, seller_profile_id, seller_is_pro):
        broker_signals.append(("heuristica_broker", 2, "heurÃ­stica local de corredor"))

    # SeÃ±ales a favor de dueÃ±o, pero nunca por simple descarte
    normalized_desc = normalize_text(description).lower()
    normalized_company = normalize_text(company_name).lower()
    if seller_name and seller_name not in ("N/A", "") and not any(k in full_text for k in [
        "remax", "re/max", "century 21", "c21", "inmobiliaria", "propiedades", "corretaje", "ltda", "spa", "eirl", "real estate"
    ]):
        owner_signals.append(("nombre_no_corporativo", 1, "nombre no corporativo"))
    if seller_is_pro is False:
        owner_signals.append(("sin_badge_pro", 1, "sin badge Profesional"))
    if broker_brand == "N/A":
        owner_signals.append(("sin_broker_brand", 1, "sin broker_brand"))
    if company_name == "N/A":
        owner_signals.append(("sin_company", 1, "sin company_name"))
    if not any(k in normalized_desc for k in ["comision", "honorarios", "corretaje", "subsidio", "financiamiento", "inmobiliaria", "propiedades"]):
        owner_signals.append(("sin_lexico_corporativo", 1, "descripciÃ³n sin lÃ©xico corporativo"))
    if "sin comision" in normalized_desc or "sin comisiÃ³n" in normalized_desc:
        owner_signals.append(("sin_comision", 1, "menciona sin comisiÃ³n"))
    if "trato directo" in normalized_desc or "dueÃ±o" in normalized_desc or "dueno" in normalized_desc:
        owner_signals.append(("trato_directo", 2, "menciona trato directo o dueÃ±o"))

    broker_score = sum(x[1] for x in broker_signals)
    owner_score = sum(x[1] for x in owner_signals)

    # Regla de seguridad: para corredor, preferimos validaciÃ³n cruzada.
    strong_broker = broker_score >= 5 and len([x for x in broker_signals if x[0] in {"broker_brand", "seller_is_pro", "company_name", "logo_corporativo", "multi_publicador"}]) >= 2
    strong_owner = owner_score >= 4 and broker_score == 0

    if strong_broker:
        state = "CORREDOR_SEGURO"
    elif strong_owner:
        state = "DUEÃ‘O_SEGURO"
    else:
        state = "INCIERTO"

    return {
        "classification_state": state,
        "es_propietario_directo": state == "DUEÃ‘O_SEGURO",
        "es_corredor": state == "CORREDOR_SEGURO",
        "es_incierto": state == "INCIERTO",
        "score_corredor": broker_score,
        "score_dueno": owner_score,
        "motivos_corredor": [{"seÃ±al": s, "peso": p, "motivo": m} for s, p, m in broker_signals],
        "motivos_dueno": [{"seÃ±al": s, "peso": p, "motivo": m} for s, p, m in owner_signals],
    }

def is_likely_broker(seller_name: str, description: str, company_name: str = "N/A",
                     seller_profile_id: str = "N/A", seller_is_pro: bool = False) -> bool:
    """Detecta si un vendedor es realmente un corredor basÃ¡ndose en nombre y descripciÃ³n.
    
    NOTA: seller_profile_id y seller_is_pro NO son seÃ±ales de clasificaciÃ³n.
    seller_profile_id existe en el 100% de los avisos de Yapo (dueÃ±os y corredores).
    seller_is_pro solo es vÃ¡lido cuando existe un badge visual 'Profesional' real en el HTML.
    """
    score = 0
    full_text = f"{seller_name} {company_name} {description}".lower()

    # -- SEÃ‘AL BASE FUERTE: solo palabras empresariales reales --
    has_strong_base = (
        company_name != "N/A" or 
        any(k in full_text for k in ["remax", "century 21", "inmobiliaria", "propiedades", "corretaje"])
    )

    # -- A. SEÃ‘ALES FUERTES (+3) --
    if any(k in full_text for k in [
        "remax", "re/max", "century 21", "c21", "inmobiliaria", "propiedades",
        "ltda", "spa", "eirl", "real estate", "corredora", "corretaje"
    ]):
        score += 3

    # -- B. SEÃ‘ALES COMERCIALES (+2) --
    if any(k in full_text for k in [
        "comision", "honorarios", "corretaje", "subsidio", "financiamiento",
        "credito hipotecario", "gestion", "evaluacion", "preaprobado"
    ]):
        if has_strong_base or any(k in full_text for k in ["comision", "honorarios", "corretaje"]):
            score += 2

    # -- C. PATRONES DE VENTA MASIVA (+2) --
    if any(k in full_text for k in [
        "agenda tu visita", "agendar visita", "plusvalia", "rentabilidad",
        "compra sin pie", "sin pie", "inversionista", "oportunidad inversion"
    ]):
        if has_strong_base or any(k in full_text for k in ["oportunidad inversion", "rentabilidad"]):
            score += 2

    # -- D. SEÃ‘ALES DÃ‰BILES (+1) --
    if any(k in full_text for k in [
        "agente", "asesor", "ejecutivo", "vendedor"
    ]):
        score += 1

    # -- E. REGLA CRÃTICA: company_name corporativo (+2) --
    if company_name and company_name != "N/A":
        score += 2

    # -- F. DECISIÃ“N FINAL --
    if score >= 3:
        return True

    # 1. Normalizar textos
    s_name = normalize_text(seller_name).lower()
    c_name = normalize_text(company_name).lower()
    full_text_norm = normalize_text(f"{seller_name} {company_name} {description}").lower()

    
    # 2. Verificar palabras clave (Usando Regex precompilado para TODAS las palabras cortas o sospechosas)
    for rx in _BROKER_REGEXES:
        if rx.search(f"{s_name} {c_name}"):
            return True
            
    for kw in _BROKER_KEYWORDS:
        if len(kw) > 5 and kw not in _BROKER_ABREVIATIONS:
            if kw in s_name or kw in c_name:
                return True
            
    # 3. Patrones semÃ¡nticos de empresas
    if re.search(r'\by\s+cia\b|\bltda\b|\bs\.a\b|\bspa\b|\beirl\b', s_name):
        return True
        
    # 4. AnÃ¡lisis de la descripciÃ³n completa (TÃ©rminos crÃ­ticos - con lÃ­mites de palabra para evitar errores)
    broker_terms = [
        "corretaje", "orden de visita", 
        "corredor de propiedades", "gestion de arriendo", "exclusividad",
        "gastos comunes aprox", "metraje aproximado", "agendar visita", "plusvalia"
    ]
    formatted_desc = normalize_text(description).lower()
    for term in broker_terms:
        if term in formatted_desc:
            return True
            
    if "comision" in formatted_desc and "sin comision" not in formatted_desc and "no comision" not in formatted_desc:
        return True
    if "honorarios" in formatted_desc and "sin honorarios" not in formatted_desc:
        return True
            
    # 5. Nombre default anÃ³nimo de Yapo.
    # 'Agente' y 'Vendedor' son los nombres que Yapo asigna a cuentas que ocultan su identidad.
    # NO son siempre corredores, a menudo son dueÃ±os. Por lo tanto, no forzamos True aquÃ­.
    if s_name in ["agente", "vendedor"]:
        pass # Antes retornaba True, lo que clasificaba a todos los dueÃ±os anÃ³nimos como corredores.
                
    return False

async def discover_new_properties(page, db, base_url=None):
    """
    Obtiene URLs de propiedades nuevas desde los listados.
    LÃ­mites definidos en CONFIG:
    - max_pages: MÃ¡ximo de pÃ¡ginas a recorrer.
    - max_urls_per_session: LÃ­mite de URLs nuevas a descubrir.
    - stop_after_empty_pages: Cantidad de pÃ¡ginas sin novedades consecutivas para detener.
    """
    new_urls = []
    coll = db["yapo_propiedades"]
    _base_url = base_url or CONFIG.get("base_url", "")
    
    # ParÃ¡metros desde CONFIG
    max_pages = CONFIG.get("max_pages", 50)
    max_urls = CONFIG.get("max_urls_per_session", 500)
    stop_after = CONFIG.get("stop_after_empty_pages", 2)
    empty_pages_count = 0

    if not _base_url:
        logging.error("âŒ discover_new_properties: base_url no configurada. Abortando discovery.")
        return []

    await block_resources(page, mode="discovery")

    for page_num in range(1, max_pages + 1):
        if len(new_urls) >= max_urls:
            logging.info(f"ðŸ Discovery: Alcanzado lÃ­mite de URLs ({max_urls}).")
            break

        if page_num == 1:
            p_url = _base_url
        else:
            parsed = urlparse(_base_url)
            new_path = f"{parsed.path}.{page_num}"
            p_url = urlunparse((parsed.scheme, parsed.netloc, new_path, "", parsed.query, ""))

        try:
            logging.info(f"ðŸ” Discovery: Navegando a pÃ¡gina {page_num} -> {p_url}")
            await page.goto(p_url, timeout=40000, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            hrefs = await page.evaluate('''() => {
                // Focus solo en la grilla principal para evitar links de footer o similares
                return Array.from(document.querySelectorAll('.d3-ads-grid--category-list a[href], a.d3-ad-tile__link')).map(a => a.href);
            }''')
            
            page_new_urls = []
            for href in hrefs:
                if len(new_urls) >= max_urls: break
                if re.search(r'[/_]\d{7,11}(?:\?|$)', href):
                    if any(x in href for x in ["yapo.cl/comprar", "yapo.cl/vender", "ayuda.yapo.cl"]):
                        continue
                    url = normalize_url(href)
                    
                    exists = await coll.find_one({"url": url}, {"_id": 1})
                    if not exists and url not in new_urls:
                        page_new_urls.append(url)
                        new_urls.append(url)
            
            logging.info(f"ðŸ“„ PÃ¡g {page_num}: {len(page_new_urls)} nuevas de {len(hrefs)} encontradas.")
            
            # CondiciÃ³n de corte por pÃ¡ginas vacÃ­as
            if not page_new_urls:
                empty_pages_count += 1
                logging.warning(f"âš ï¸ PÃ¡g {page_num} vacÃ­a. ({empty_pages_count}/{stop_after})")
                if stop_after > 0 and empty_pages_count >= stop_after:
                    logging.info(f"ðŸ›‘ Discovery: {stop_after} pÃ¡ginas consecutivas sin novedades. Deteniendo.")
                    break
            else:
                empty_pages_count = 0 # Reset si encontramos algo
                
        except Exception as e:
            logging.error(f"âš ï¸ Error en discovery pÃ¡g {page_num}: {e}")
            break

    return new_urls

async def get_properties_to_rescrape(db):
    """Obtiene 30 propiedades antiguas o de baja calidad para re-scrapear."""
    coll = db["yapo_propiedades"]
    hours_ago = datetime.now(timezone.utc) - timedelta(hours=CONFIG.get("hours_to_recheck", 12))
    
    query = {
        "fecha_scraping": {"$lt": hours_ago.isoformat()}
    }
    
    # Prioridad: 1. quality_score bajo, 2. Sin lat/lon, 3. Sin m2_total
    # Nota: MongoDB no permite sort por mÃºltiples prioridades de forma tan granular en una sola query 
    # sin agregaciones complejas, pero haremos un sort por quality_score (asc)
    cursor = coll.find(query).sort("details.quality_score", 1).limit(30)
    
    rescrape_urls = []
    async for doc in cursor:
        rescrape_urls.append(doc["url"])
        
    logging.info(f"â™»ï¸ Fallback: {len(rescrape_urls)} propiedades detectadas para re-check.")
    return rescrape_urls

def save_html_locally(html_content: str, url: str) -> str:

    """Guarda el HTML en una carpeta local y retorna la ruta relativa."""
    if not html_content:
        return None
        
    folder = os.path.join(os.path.dirname(__file__), "html_dumps")
    if not os.path.exists(folder):
        os.makedirs(folder)
        
    # Generar un nombre de archivo Ãºnico basado en la URL
    filename = hashlib.md5(url.encode()).hexdigest() + ".html"
    filepath = os.path.join(folder, filename)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    return f"html_dumps/{filename}"


def _safe_int_field(val: str, field_name: str, min_v: int = 0, max_v: int = 50) -> str:
    """Valida que val sea un entero razonable para el campo dado. Si falla â†’ 'N/A'.
    Evita contaminar dormitorios con precios, m2, etc."""
    if not val or val == "N/A":
        return "N/A"
    nums = re.findall(r'\d+', val.replace('.', '').replace(',', ''))
    if not nums:
        return "N/A"
    n = int(nums[0])
    if n < min_v or n > max_v:
        logging.debug(f"_safe_int_field: '{field_name}' valor {n} fuera de rango [{min_v},{max_v}] â€” descartado")
        return "N/A"
    return str(n)


def _safe_m2_field(val: str, field_name: str) -> str:
    """Valida que val sea un mÂ² razonable (1â€“9999). Si falla â†’ 'N/A'."""
    if not val or val == "N/A":
        return "N/A"
    nums = re.findall(r'[\d]+(?:[.,][\d]+)?', val.replace(' ', ''))
    if not nums:
        return "N/A"
    try:
        n = clean_float(nums[0])
    except:
        return "N/A"
    if n < 1 or n > 9999:
        logging.debug(f"_safe_m2_field: '{field_name}' valor {n} fuera de rango â€” descartado")
        return "N/A"
    return str(n)  # Retornamos solo el nÃºmero limpio como string


def _safe_coords(lat_s: str, lon_s: str) -> tuple:
    """Valida que lat/lon correspondan a Chile continental/Patagonia.
    Chile: lat âˆˆ [-55.9, -17.5], lon âˆˆ [-75.7, -66.0]
    Si las coords estÃ¡n fuera â†’ retorna ('N/A', 'N/A')."""
    try:
        lat = float(lat_s)
        lon = float(lon_s)
        if -55.9 <= lat <= -17.5 and -75.7 <= lon <= -66.0:
            return str(lat), str(lon)
        logging.debug(f"_safe_coords: coords ({lat}, {lon}) fuera de Chile â€” descartadas")
    except (ValueError, TypeError):
        pass
    return "N/A", "N/A"


def _normalize_key(s: str) -> str:
    """Normaliza una etiqueta de HTML: minusculas, sin acentos (NFKD) y sin simbolos extra."""
    if not s: return ""
    s = s.lower().strip()
    # Eliminar acentos y diacrÃ­ticos
    s = "".join(c for c in normalize('NFKD', s) if not re.match(r'[\u0300-\u036f]', c))
    # Limpiar simbolos m2 -> m2
    s = s.replace("Â²", "2").replace("m2", "m2")
    # Eliminar parentesis y su contenido: "Ãrea construida (mÂ²)" -> "area construida"
    s = re.sub(r'\(.*?\)', '', s).strip()
    return s

def _lookup(d: dict, keys_set: set, default: str = "N/A") -> str:
    """Busca en el dict `d` usando etiquetas ya normalizadas."""
    for k, v in d.items():
        if _normalize_key(k) in keys_set:
            return v
    return default


def _parse_yapo_date(date_str: str) -> str:
    """Convierte fechas de Yapo (Hoy, Ayer, 27 feb) a formato ISO o string leÃ­ble."""
    if not date_str or date_str == "N/A":
        return "N/A"
    now = datetime.now()
    ds = date_str.lower()
    try:
        if "hoy" in ds:
            return now.strftime("%Y-%m-%d")
        if "ayer" in ds:
            return (now - timedelta(days=1)).strftime("%Y-%m-%d")
        # Formato: "27 feb"
        meses = {
            "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
            "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12
        }
        parts = ds.split()
        if len(parts) >= 2:
            day = int(re.sub(r'\D', '', parts[0]))
            month = meses.get(parts[1][:3])
            if month:
                year = now.year
                # Si estamos en Enero y el aviso es de Diciembre, es del aÃ±o pasado
                if month > now.month:
                    year -= 1
                return f"{year}-{month:02d}-{day:02d}"
    except:
        pass
    return date_str


def _parse_html_fast(html: str) -> dict | None:
    """Extrae datos de un HTML de detalle yapo.cl usando JSON-LD + regex (sin JS).
    VersiÃ³n INDUSTRIAL: Incluye 'The Observer' (Source Tracking) y 'Quality Scoring'."""

    if not html or "</body>" not in html.lower():
        logging.warning("âš ï¸ _parse_html_fast: HTML truncado o invÃ¡lido (sin </body>). Ignorando.")
        return None

    def _clean(s: str) -> str:
        if not s:
            return "N/A"
        s = re.sub(r'<[^>]+>', '', s).strip()
        s = re.sub(r'\s+', ' ', s)
        return s if s and "Pregunta al anunciante" not in s else "N/A"

    # â”€â”€ THE OBSERVER: Registro de fuentes y calidad â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    sources = {}  # {field: source_name}
    
    # InicializaciÃ³n de campos
    title = price_str = region = comuna = direccion = "N/A"
    seller_name = descripcion_corta = og_title = "N/A"
    dormitorios_str = banos_str = estacionamientos_str = "N/A"
    m2_total_str = m2_util_str = gastos_comunes_str = "N/A"
    piscina_str = seller_type = fecha_pub = tipo_propiedad = "N/A"
    lat = lon = "N/A"
    images = []

    # â”€â”€ 1. Data Layers (Frecuentes en Browser / Loopa) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    data_layer: dict = {}
    yapo_match = re.search(r'document\.__YAPO__\.adview_event_base\s*=\s*(\{.*?\});', html, re.DOTALL)
    if yapo_match:
        try: data_layer.update(json.loads(yapo_match.group(1)))
        except: pass
    
    loopa_match = re.search(r'var\s+loopaData\s*=\s*(\{.*?\});', html, re.DOTALL | re.IGNORECASE)
    if loopa_match:
        try:
            loopa_json = json.loads(loopa_match.group(1))
            if "Bedrooms" in loopa_json: data_layer["dormitorios"] = loopa_json["Bedrooms"]
            if "Bathrooms" in loopa_json: data_layer["banos"] = loopa_json["Bathrooms"]
            if "Price" in loopa_json: data_layer["price"] = loopa_json["Price"]
            if "HousingType" in loopa_json: data_layer["housing_type"] = loopa_json["HousingType"]
        except: pass

    # â”€â”€ 2. JSON-LD: precio, seller, tÃ­tulo, direcciÃ³n â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    jsonld_data = {}
    for jm in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            jd = json.loads(jm)
            if jd.get("@type") == "Product":
                jsonld_data = jd
                break
        except: continue

    offers       = jsonld_data.get("offers", {})
    seller_obj   = offers.get("seller", {})
    address_obj  = offers.get("availableAtOrFrom", {}).get("address", {})

    if jsonld_data.get("name"):
        title = jsonld_data["name"]
        sources["title"] = "json-ld"
    
    price_raw = offers.get("price")
    price_currency = offers.get("priceCurrency", "")
    if price_raw:
        if price_currency.upper() == "CLF":
            # Es UF
            price_str = f"UF {price_raw}"
            sources["price"] = "json-ld-uf"
        else:
            # Asumir CLP u otros
            price_str = f"${int(price_raw):,}".replace(",", ".")
            sources["price"] = "json-ld"
        
    if address_obj.get("addressLocality"):
        comuna = address_obj["addressLocality"]
        sources["comuna"] = "json-ld"
        
    if address_obj.get("streetAddress"):
        direccion = address_obj["streetAddress"]
        sources["direccion"] = "json-ld"
        
    if seller_obj.get("name"):
        seller_name = seller_obj["name"]
        sources["seller_name"] = "json-ld"
        
    if jsonld_data.get("description"):
        d_tmp = jsonld_data["description"]
        if d_tmp and d_tmp != "N/A":
            descripcion_corta = d_tmp
            sources["description"] = "json-ld"

    # â”€â”€ 2b. Fallbacks de Meta Tags (Open Graph) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    m_og_title = re.search(r'<meta property="og:title" content="(.*?)"', html)
    if m_og_title:
        og_title = _clean(m_og_title.group(1))
        if title == "N/A":
            title = og_title
            sources["title"] = "og-meta"

    if descripcion_corta == "N/A":
        m_desc = re.search(r'<meta property="og:description" content="(.*?)"', html)
        if m_desc:
            descripcion_corta = _clean(m_desc.group(1))
            sources["description"] = "og-meta"

    # Fallback agresivo: Buscar la descripciÃ³n completa en el cuerpo del HTML (evita truncado JSON-LD)
    # Buscamos mÃºltiples contenedores posibles (Yapo cambia las clases segÃºn el tipo de aviso)
    desc_containers = [
        r'class="[^"]*product-comments[^"]*"[^>]*>(.*?)</(?:div|p|span)>',
        r'class="[^"]*d3-property-info__description-text[^"]*"[^>]*>(.*?)</(?:p|div)>',
        r'class="[^"]*d3-property-about__text[^"]*"[^>]*>(.*?)</(?:p|div|span)>',
        r'id="[^"]*description[^"]*"[^>]*>(.*?)</(?:p|div|span)>'
    ]
    for pattern in desc_containers:
        full_desc_match = re.search(pattern, html, re.DOTALL)
        if full_desc_match:
            full_text = _clean(full_desc_match.group(1))
            if len(full_text) > len(descripcion_corta) or descripcion_corta == "N/A":
                descripcion_corta = full_text
                sources["descripcion"] = f"html-body-full-{pattern[:15]}"
                break

    # â”€â”€ 2c. Fallbacks de HTML Puro (H1, selectores CSS) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if title == "N/A":
        h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        if h1_match:
            title = _clean(h1_match.group(1))
            sources["title"] = "h1-html"
    
    if price_str == "N/A":
        p_match = re.search(r'class="[^"]*d3-property-info__price[^"]*"[^>]*>(.*?)</span>', html, re.DOTALL)
        if p_match:
            price_str = _clean(p_match.group(1))
            sources["price"] = "html-price-tag"
        elif "CLP" in og_title:
            price_match = re.search(r'CLP\s*([\d.]+)', og_title)
            if price_match:
                p_val = clean_float(price_match.group(1))
                price_str = f"${int(p_val):,}".replace(",", ".") if p_val else "N/A"
                sources["price"] = "og-title-regex"

    # â”€â”€ 2d. Breadcrumbs: regiÃ³n, comuna, tipo propiedad â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    bc_items = re.findall(r'<li[^>]*class="[^"]*d3-breadcrumb__list-element[^"]*"[^>]*>(.*?)</li>', html, re.DOTALL)
    bc_texts = [re.sub(r'<[^>]+>', '', b).strip().split('\n')[0].strip() for b in bc_items if b]
    bc_texts = [b for b in bc_texts if b]
    
    if len(bc_texts) > 4:
        region = bc_texts[4]
        sources["region"] = "breadcrumb"
    if comuna == "N/A" and len(bc_texts) > 5:
        comuna = bc_texts[5]
        sources["comuna"] = "breadcrumb"
    if tipo_propiedad == "N/A" and len(bc_texts) > 3:
        tp_tmp = bc_texts[3]
        tipo_propiedad = tp_tmp[:-1] if tp_tmp.endswith('s') and len(tp_tmp) > 4 else tp_tmp
        sources["tipo_propiedad"] = "breadcrumb"

    # â”€â”€ 3. d3-property-insight: Dormitorios, BaÃ±os, mÂ² â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    insight: dict[str, str] = {}
    for dt_raw, dd_raw in re.findall(
        r'<dt[^>]*class="[^"]*d3-property-insight__attribute-title[^"]*"[^>]*>(.*?)</dt>'
        r'[\s\n\r]*<dd[^>]*class="[^"]*d3-property-insight__attribute-value[^"]*"[^>]*>(.*?)</dd>',
        html, re.DOTALL
    ):
        dt = re.sub(r'<[^>]+>', '', dt_raw).strip()
        dd = re.sub(r'<[^>]+>', '', dd_raw).strip()
        dd = re.sub(r'\(Rebajado.*?\)', '', dd).strip()
        if dt and dd: insight[dt] = dd

    d_raw = _lookup(insight, _INSIGHT_DORMITORIOS)
    if d_raw != "N/A":
        dormitorios_str = _safe_int_field(d_raw, "dormitorios")
        if dormitorios_str != "N/A": sources["dormitorios"] = "insight"
    if dormitorios_str == "N/A" and "dormitorios" in data_layer:
        dormitorios_str = _safe_int_field(str(data_layer["dormitorios"]), "dorm_datalayer")
        if dormitorios_str != "N/A": sources["dormitorios"] = "data-layer"

    b_raw = _lookup(insight, _INSIGHT_BANOS)
    if b_raw != "N/A":
        banos_str = _safe_int_field(b_raw, "banos")
        if banos_str != "N/A": sources["banos"] = "insight"
    if banos_str == "N/A" and "banos" in data_layer:
        banos_str = _safe_int_field(str(data_layer["banos"]), "banos_datalayer")
        if banos_str != "N/A": sources["banos"] = "data-layer"

    if title == "N/A":
        title_insight = _lookup(insight, _INSIGHT_TITULO)
        if title_insight != "N/A":
            title = title_insight
            sources["title"] = "insight"
    if price_str == "N/A":
        p_ins = _lookup(insight, _INSIGHT_PRECIO)
        if p_ins != "N/A":
            price_str = p_ins
            sources["price"] = "insight"

    m2_insight = _safe_m2_field(_lookup(insight, _INSIGHT_M2), "m2_insight")
    if m2_insight != "N/A":
        m2_total_str = m2_insight
        sources["m2_total"] = "insight"

    # 3b. NUEVO LAYOUT: product-icons (Icons metrics) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # <div class="product-icons-icon"> ... 1 baÃ±os</div>
    for icon_html in re.findall(r'<div[^>]*class="[^"]*product-icons-icon[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL):
        # Limpiar tags para que el SVG no interfiera con los nÃºmeros
        cleaned_icon = _clean(icon_html)
        normalized_icon = _normalize_key(cleaned_icon)
        
        # Dormitorios
        if any(kw in normalized_icon for kw in ["dorm", "habitacion"]):
            val = _safe_int_field(cleaned_icon, "dorm_icon")
            if val != "N/A" and dormitorios_str == "N/A":
                dormitorios_str = val
                sources["dormitorios"] = "product-icons"
        # BaÃ±os
        if any(kw in normalized_icon for kw in ["bano", "banos"]):
            val = _safe_int_field(cleaned_icon, "banos_icon")
            if val != "N/A" and banos_str == "N/A":
                banos_str = val
                sources["banos"] = "product-icons"
        # Estacionamientos
        if any(kw in normalized_icon for kw in ["park", "estac"]):
            val = _safe_int_field(cleaned_icon, "estac_icon")
            if val != "N/A" and estacionamientos_str == "N/A":
                estacionamientos_str = val
                sources["estacionamientos"] = "product-icons"
        # Superficie (m2)
        if "m2" in normalized_icon:
            val = _safe_m2_field(cleaned_icon, "m2_icon")
            if val != "N/A" and (m2_total_str == "N/A" or len(m2_total_str) > 20):
                m2_total_str = val
                sources["m2_total"] = "product-icons"

    # â”€â”€ 4. Grid de Atributos: d3-property-details / features â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    detail_pairs = re.findall(
        r'(?:class="[^"]*(?:d3-property-details__detail-label|d3-property-features__label)[^"]*")[^>]*>(.*?)<.*?'
        r'(?:p|span|div)[^>]*class="[^"]*(?:d3-property-details__detail|d3-property-features__item-value)[^"]*"[^>]*>(.*?)</',
        html, re.DOTALL
    )
    details = {re.sub(r'<[^>]+>', '', l).strip(): _clean(v) for l, v in detail_pairs if l and _clean(v) != "N/A"}

    gastos_comunes_str  = _lookup(details, _DETAIL_GASTOS)
    if gastos_comunes_str != "N/A": sources["gastos_comunes"] = "html-grid"
    
    fecha_pub_raw = _lookup(details, _DETAIL_PUBLICADO)
    if fecha_pub_raw != "N/A":
        fecha_pub = _parse_yapo_date(fecha_pub_raw)
        sources["list_time"] = "html-grid"
        
    piscina_str = _lookup(details, _DETAIL_PISCINA)
    if piscina_str != "N/A": sources["piscina"] = "html-grid"

    m2_total_detail = _safe_m2_field(_lookup(details, _DETAIL_M2_TOTAL), "m2_total_detail")
    if m2_total_detail != "N/A":
        m2_total_str = m2_total_detail
        sources["m2_total"] = "html-grid"
        
    m2_util_cand = _safe_m2_field(_lookup(details, _DETAIL_M2_UTIL),  "m2_util")
    if m2_util_cand != "N/A":
        m2_util_str = m2_util_cand
        sources["m2_util"] = "html-grid"

    # â”€â”€ 5. Location & More Fallbacks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    map_match = re.search(r'(?:q|center)=(-?\d+\.\d+),(-?\d+\.\d+)', html)
    if map_match:
        lat, lon = _safe_coords(map_match.group(1), map_match.group(2))
        if lat != "N/A": sources["location"] = "google-maps-q"
    if lat == "N/A" and "latitude" in data_layer:
        lat, lon = _safe_coords(data_layer["latitude"], data_layer["longitude"])
        if lat != "N/A": sources["location"] = "data-layer"

    adv_match = re.search(r"setTargeting\('advertiser',\s*\"(\w+)\"\)", html)
    if adv_match:
        seller_type = adv_match.group(1)
        sources["seller_type"] = "ads-targeting"

    images = re.findall(r'"contentUrl":\s*"([^"]+)"', html) or re.findall(r'class="d3-property-info__images--container".*?src="([^"]+)"', html, re.DOTALL)
    if images: sources["images"] = "html-images"

    # Seller formatting
    publicador = seller_name if seller_name != "N/A" else "N/A"
    company_name = "N/A"
    
    # --- EXTRACCIÃ“N DEL VENDEDOR (Ãrea de Formulario/Sidebar) ---
    # Prioridad absoluta: El nombre que aparece arriba del formulario de contacto
    # Esto captura nombres de corredoras como "Houm", "Fuenzalida", etc.
    contact_name_match = re.search(r'class="[^"]*contact_name[^"]*"[^>]*>(.*?)</', html, re.DOTALL)
    if contact_name_match:
        val = _clean(contact_name_match.group(1))
        if val and val != "N/A":
            publicador = val
            sources["seller_name"] = "html-contact-name"
    
    # Nueva bÃºsqueda: Clase especÃ­fica de compaÃ±Ã­a en el sidebar o advertiser-name
    company_match = re.search(r'class="[^"]*(?:d3-property-info__company-name|contact_address)[^"]*"[^>]*>(.*?)</', html, re.DOTALL)
    if company_match:
        c_val = _clean(company_match.group(1))
        # Filtrar strings que son direcciones/ubicaciones (empiezan con '- ' o son muy largas)
        if c_val and c_val != "N/A" and not c_val.strip().startswith("-") and len(c_val) < 60:
            company_name = c_val
            # Si el 'publicador' es genÃ©rico, usamos el nombre de la empresa
            if publicador == "N/A" or publicador.lower().startswith("agente"):
                publicador = c_val
                sources["seller_name"] = "html-sidebar-company"
                
    adv_name_match = re.search(r'class="[^"]*advertiser-name[^"]*"[^>]*>(.*?)</', html, re.DOTALL)
    if adv_name_match:
        val = _clean(adv_name_match.group(1))
        if val and val != "N/A":
            if publicador == "N/A" or "agente" in publicador.lower():
                publicador = val
                sources["seller_name"] = "html-advertiser-name"

    broker_brand = "N/A"
    seller_profile_id = "N/A"

    # Buscar logo de corredora
    contact_logo_match = re.search(r'class="[^"]*contact_logo[^"]*"[^>]*>\s*<img[^>]+alt="([^"]+)"', html, re.IGNORECASE)
    if contact_logo_match:
        c_val = _clean(contact_logo_match.group(1))
        generic_avatars = ["user-avatar", "avatar", "default-user"]
        if c_val and c_val != "N/A" and not any(g in c_val.lower() for g in generic_avatars):
            broker_brand = c_val
            company_name = c_val
            if publicador == "N/A" or "agente" in publicador.lower():
                publicador = c_val
            sources["seller_name"] = "html-contact-logo"
            sources["broker_brand"] = "html-contact-logo"

    # Buscar profile ID
    profile_id_match = re.search(r'href="[^"]*/user/profile/id/(\d+)[^"]*"', html, re.IGNORECASE)
    if profile_id_match:
        seller_profile_id = profile_id_match.group(1)
        sources["seller_profile_id"] = "html-contact-info-link"

    # Buscar el nombre de la empresa en el alt del avatar (Nuevo layout)
    avatar_match = re.search(r'class="[^"]*advertiser-avatar[^"]*"[^>]*>\s*<img[^>]+alt="([^"]+)"', html, re.IGNORECASE)
    if avatar_match:
        c_val = _clean(avatar_match.group(1))
        generic_avatars = ["user-avatar", "avatar", "default-user"]
        if c_val and c_val != "N/A" and not any(g in c_val.lower() for g in generic_avatars):
            company_name = c_val
            broker_brand = c_val
            if publicador == "N/A" or "agente" in publicador.lower():
                publicador = c_val
                sources["seller_name"] = "html-avatar-alt"
            sources["broker_brand"] = "html-avatar-alt"

    # Si publicador tiene el mismo formato que la compaÃ±Ã­a (Ej: "Skyline Propiedades")
    if is_likely_broker(publicador, descripcion_corta, company_name):
        if company_name == "N/A": 
            if any(kw in publicador.lower() for kw in _BROKER_KEYWORDS):
                company_name = publicador
        # Si es corredor, su nombre es el de su empresa o "Agente de X"
        sources["seller_name"] = "seller-formatting-broker"

    if publicador.startswith("Agente "):
        company_name = publicador[7:].strip() or company_name
        # Mantenemos el publicador para que el usuario vea quiÃ©n es, pero sigue siendo broker
        sources["seller_name"] = "seller-formatting-agent"
    elif publicador.startswith("Propietario "):
        publicador = publicador[12:].strip() or "N/A"
        sources["seller_name"] = "seller-formatting-owner"

    if publicador == "N/A":
        if data_layer.get("seller_name"):
            publicador = data_layer["seller_name"]
            sources["seller_name"] = "data-layer"

    # â”€â”€ 5b. DETECCIÃ“N PRO DE YAPO â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # SeÃ±al 1: Badge explÃ­cito "Profesional" (ÃšNICA fuente vÃ¡lida de seller_is_pro)
    seller_is_pro = bool(re.search(r'title="Profesional"', html, re.IGNORECASE))

    # SeÃ±al 2: El nombre del vendedor tiene hipervÃ­nculo de perfil pÃºblico.
    # CORRECCIÃ“N BUG: el <a> PUEDE ser el propio elemento con class="contact_name"
    # (ej: <a class="contact_name" href="/user/profile/id/...">Correa propiedades</a>)
    # o puede estar DENTRO de un contenedor. Cubrimos ambos casos.
    if not seller_is_pro:
        # Caso A: el propio <a> tiene class="contact_name" y apunta a un perfil
        contact_name_link = re.search(
            r'<a[^>]+class="[^"]*contact_name[^"]*"[^>]+href="[^"]*\/user\/profile',
            html, re.IGNORECASE
        )
        # Caso B: un contenedor con clase relevante contiene un <a href> interno
        inner_link = re.search(
            r'class="[^"]*(?:advertiser-name|d3-property-info__advertiser)[^"]*"[^>]*>\s*<a\s+href',
            html, re.IGNORECASE
        )
        if contact_name_link or inner_link:
            seller_is_pro = True
            sources["seller_is_pro"] = "html-contact-name-link"

    # NOTA: seller_profile_id NO implica seller_is_pro.
    # La auditorÃ­a HTML demostrÃ³ que /user/profile/id/ aparece en el 100% de los avisos
    # de Yapo, incluyendo dueÃ±os directos. Derivar seller_is_pro desde profile_id
    # genera falsos positivos masivos. ELIMINADO.

    if seller_is_pro and "seller_is_pro" not in sources:
        sources["seller_is_pro"] = "html-pro-badge"

    # â”€â”€ 6. QUALITY SCORING â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    critical_fields = [(price_str!="N/A", 0.3), (m2_total_str!="N/A", 0.25), (dormitorios_str!="N/A", 0.15), (banos_str!="N/A", 0.15), (lat!="N/A", 0.15)]
    quality_score = sum(w for p, w in critical_fields if p)

    if title == "N/A" and price_str == "N/A" and dormitorios_str == "N/A":
        if "PÃ¡gina no encontrada" in html or "Oops!" in html: return None
        if quality_score < 0.15 and lat == "N/A": return None

    return {
        "source": "fast_path",
        "_metadata": {"sources": sources, "quality_score": round(quality_score, 2)},
        "region": region, "comuna": comuna, "sector": "N/A", "lat": lat, "lon": lon,
        "m2_total_str": m2_total_str, "m2_util_str": m2_util_str, "gastos_comunes_str": gastos_comunes_str,
        "dormitorios_str": dormitorios_str, "banos_str": banos_str, "estacionamientos_str": estacionamientos_str,
        "title": title, "price": price_str, "publicador": publicador, "raw_desc": descripcion_corta,
        "tipo_propiedad": tipo_propiedad, "list_time": fecha_pub, "seller_id": "N/A",
        "seller_type": seller_type, "company_name": company_name, "images_url": images,
        "piscina_str": piscina_str, "direccion": direccion,
        "seller_is_pro": seller_is_pro,
        "broker_brand": broker_brand,
        "seller_profile_id": seller_profile_id
    }


def _get_ai_context(html: str) -> str:
    """Extrae una versiÃ³n estratÃ©gica del HTML para el Auditor AI.
    Mantiene scripts de metadatos (JSON-LD, __YAPO__) y bloques de datos iniciales."""
    # 1. Preservar JSON-LD
    json_ld = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
    
    # 2. Preservar bloques de datos de Yapo/Loopa y Estado Inicial
    pattern = r'(document\.__YAPO__\.adview_event_base\s*=\s*\{.*?\};|var\s+loopaData\s*=\s*\{.*?\};|window\.__INITIAL_STATE__\s*=\s*\{.*?\};)'
    metadata_scripts = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
    
    # 3. Preservar URLs de Mapas (donde Yapo tiene lat/lon en q=LAT,LON)
    map_urls = re.findall(r'src="([^"]*map[^"]*q=-?\d+\.\d+,-?\d+\.\d+[^"]*)"', html, re.IGNORECASE)
    
    # 4. Limpiar el resto del HTML (Quitamos ruidos pesados de forma iterativa y segura)
    clean_html = html
    for tag in ['style', 'nav', 'footer', 'header', 'svg', 'noscript']:
        clean_html = re.sub(rf'<{tag}\b[^>]*>.*?</{tag}>', '', clean_html, flags=re.DOTALL|re.IGNORECASE)
    text_content = re.sub(r'<[^>]+>', ' ', clean_html)
    text_content = re.sub(r'\s+', ' ', text_content).strip()

    # Ensamblar contexto
    contextParts = []
    if json_ld: contextParts.append(f"JSON-LD: {' '.join(json_ld)}")
    if metadata_scripts: contextParts.append(f"METADATA_DATA_LAYERS: {' '.join(metadata_scripts)}")
    if map_urls: contextParts.append(f"MAP_URLS: {' '.join(map_urls)}")
    contextParts.append(f"PAGE_TEXT: {text_content[:8000]}")
    
    return "\n---\n".join(contextParts)[:15000]

async def _surgical_ai_audit(context: str, missing_fields: list[str], grok_client) -> dict:
    """Realiza una auditorÃ­a quirÃºrgica multi-campo usando Grok sobre el contexto estratÃ©gico."""
    fields_desc = ", ".join([f"'{f}'" for f in missing_fields])
    prompt = f"""AUDITOR ESTRATÃ‰GICO INMOBILIARIO.
Tu misiÃ³n es encontrar EXACTAMENTE estos datos faltantes: {fields_desc}.
Busca en el texto de la descripciÃ³n y en los bloques de METADATA/JSON-LD proporcionados.

ESTRATEGIA DE EXTRACCIÃ“N:
1. 'lat' y 'lon': BÃºscalas en objetos JSON o URLs de mapas (ej: q=-33.4,-70.5). Retorna NUMEROS (ej: -33.488).
2. 'm2_total': SÃ© muy agresivo. Busca "metros cuadrados", "m2", "mts2", "superficie", "sup.", "Ãºtil", "terreno".
   Incluso si estÃ¡ en palabras (ej: "setenta y cinco"), conviÃ©rtelo a nÃºmero (75).
3. 'gastos_comunes': Busca valores monetarios asociados a "mantenimiento", "GGCC", "gastos".
4. Si un dato no estÃ¡ presente en ninguna parte, retorna null para ese campo. No inventes.

CONTEXTO DE LA PROPIEDAD:
---
{context}
---
Retorna solo un JSON puro con los campos solicitados.
"""
    try:
        resp = await grok_client.chat.completions.create(
            model=Config.GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"}
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logging.error(f"Error en Auditor AI Multi-campo: {e}")
        return {}

async def extract_fast_path(url: str, client) -> tuple:
    """Intenta extraer datos usando el cliente curl_cffi persistente. Retorna (raw_data, size_bytes).
    Usa JSON-LD + HTML regex (yapo.cl ya no usa __NEXT_DATA__)."""
    try:
        resp = await client.get(url)
        size_bytes = len(resp.content)
        
        # DetecciÃ³n de bloqueo en el fast path (403, 429, o Reto de navegador)
        challenge_keywords = ["captcha", "access denied", "checking your browser", "please wait", "ray id"]
        if resp.status_code in [403, 429] or any(k in resp.text.lower() for k in challenge_keywords):
            raise ProxyBlockedError(f"Fast path bloqueado ({resp.status_code}/Challenge)")

        if resp.status_code != 200:
            return None, size_bytes

        # Forzar decodificaciÃ³n UTF-8 para evitar problemas de acentos (baÃ±os -> banos)
        html = resp.content.decode('utf-8', 'ignore')
        res = _parse_html_fast(html)
        if res:
            # OPTIMIZACIÃ“N: Guardamos HTML localmente, no en MongoDB
            html_path = save_html_locally(html, url)
            res["html_path"] = html_path
            res["html_dump"] = html # Lo mantenemos temporalmente para la IA en este thread
            return res, size_bytes
        return None, size_bytes
    except ProxyBlockedError as e:
        raise e
    except Exception as e:
        logging.debug(f"Fast path fallÃ³: {e}")
    return None, 0

async def block_resources(page, mode="standard"):
    async def handler(route):
        rt = route.request.resource_type
        if mode == "ultra":
            # Para __NEXT_DATA__, normalmt solo necesitamos el "document" inicial.
            # Bloqueamos scripts tambiÃ©n para mÃ¡ximo ahorro. Si falla, volveremos a modo standard.
            if rt not in ["document"]:
                await route.abort()
                return
        elif mode == "discovery":
            # Bloqueo ligero para listas: permitimos scripts y estilos para que el DOM se hidrate
            if rt in ["image", "media", "font", "track"]:
                await route.abort()
                return
        else:
            # Modo standard: bloqueamos todo lo pesado + trackers
            if rt in ["image", "media", "font", "track", "eventsource", "websocket"]:
                await route.abort()
                return
            
        # Filtro extra de trackers para todos los modos
        url = route.request.url.lower()
        if any(x in url for x in ["analytics", "facebook", "google", "pixel", "ads", "tracker"]):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", handler)

# ====================== EXTRACCIÃ“N (Solo Browser - Libera Proxy RÃ¡pido) ======================
async def extract_raw_data(page, url: str) -> dict:
    """Extrae datos crudos usando el browser Playwright.
    Navega a la URL, espera a que cargue el contenido principal y luego llama
    a _parse_html_fast() sobre el HTML renderizado â€” el mismo parser que el
    fast path httpx. De esta manera ambos paths comparten exactamente la misma
    lÃ³gica de extracciÃ³n y validaciÃ³n.
    NO llama a la IA. El proxy se libera al cerrar la pÃ¡gina despuÃ©s de esto."""

    # â”€â”€ NavegaciÃ³n â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        response = await page.goto(
            url,
            timeout=CONFIG["page_load_timeout"] * 1000,
            wait_until="commit"
        )
        if not response:
            raise Exception("Sin respuesta del servidor")
        
        title_text = await page.title()
        current_url = page.url.lower()
        
        # DetecciÃ³n de bloqueo REAL (no falsos positivos por script reCAPTCHA preventivo)
        is_blocked = False
        if "Access Denied" in title_text or "403 Forbidden" in title_text:
            is_blocked = True
        elif "captcha" in current_url or "/challenge" in current_url:
            is_blocked = True
        else:
            # Solo verificar si hay un captcha ACTIVO (iframe visible), no solo el script cargado
            captcha_visible = await page.query_selector('iframe[src*="recaptcha"][style*="visible"], .g-recaptcha, #captcha-container')
            if captcha_visible:
                is_blocked = True
        
        if is_blocked:
            raise ProxyBlockedError("Bloqueo detectado en Browser: Captcha/Access Denied")
    except ProxyBlockedError as e:
        raise e
    except Exception as e:
        if "ERR_TUNNEL_CONNECTION_FAILED" in str(e):
            raise Exception(f"Tunnel Error: {e}")
        raise e

    # Yapo carga el detalle con JS; esperamos cualquiera de estas seÃ±ales DE GOLPE.
    # Usamos un selector combinado para no esperar secuencialmente si uno falla.
    combined_selector = (
        'h2.d3-property-details__title, '
        '.d3-property-info__price, '
        'script[type="application/ld+json"], '
        'dt.d3-property-insight__attribute-title'
    )
    try:
        await page.wait_for_selector(combined_selector, timeout=8000)
    except:
        pass

    # PequeÃ±a pausa para que los insights terminen de renderizar
    await asyncio.sleep(0.5)

    # â”€â”€ Obtener HTML renderizado y parsear con el mismo motor que httpx â”€â”€â”€â”€â”€â”€
    html = await page.content()
    result = _parse_html_fast(html)

    if result:
        # OPTIMIZACIÃ“N: Guardamos HTML localmente, no en MongoDB
        html_path = save_html_locally(html, url)
        result["html_path"] = html_path
        result["html_dump"] = html # Mantenemos para IA
        # Log ultra-compacto
        logging.debug(f"ðŸŒ Data: d={result['dormitorios_str']} b={result['banos_str']} m2={result['m2_total_str']}")
        return result

    # â”€â”€ Fallback mÃ­nimo si el HTML del browser tampoco sirviÃ³ â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # (Caso extremo: pÃ¡gina muy dinÃ¡mica o bloqueada parcialmente)
    logging.warning(f"âš ï¸ Browser: _parse_html_fast no extrajo datos de {url[-40:]}")
    return {
        "source": "browser_fallback_empty",
        "html_dump": html if len(html) > 10 else None, # Si es casi vacÃ­o, que la IA ni lo intente
        "region": "N/A", "comuna": "N/A", "sector": "N/A",
        "lat": "N/A", "lon": "N/A",
        "m2_total_str": "N/A", "m2_util_str": "N/A",
        "gastos_comunes_str": "N/A",
        "dormitorios_str": "N/A", "banos_str": "N/A",
        "estacionamientos_str": "N/A",
        "title": "N/A", "price": "N/A",
        "publicador": "N/A", "raw_desc": "N/A",
        "tipo_propiedad": "N/A",
        "list_time": "N/A", "seller_id": "N/A", "seller_type": "N/A",
        "company_name": "N/A", "images_url": [], "piscina_str": "N/A",
        "direccion": "N/A",
        # Campos de clasificaciÃ³n â€” siempre incluidos para evitar KeyError en el worker
        "seller_is_pro": False,
        "broker_brand": "N/A",
        "seller_profile_id": "N/A",
    }


def _extract_operation_from_url(url: str) -> str:
    """Extrae tipo de operaciÃ³n (Venta/Arriendo) desde la URL de yapo.cl."""
    if not url:
        return "S/I"
    url_lower = url.lower()
    if "alquiler" in url_lower or "arriendo" in url_lower:
        return "Arriendo"
    if "venta" in url_lower:
        return "Venta"
    return "S/I"


# ====================== PROCESAMIENTO IA (Sin Browser/Proxy) ======================
async def process_with_ai(raw_data: dict, grok_client, uf_value: float = None, coll = None, url: str = "") -> dict:
    """Procesamiento avanzado con IA para normalizar y enriquecer datos."""
    title = raw_data["title"]
    raw_desc = raw_data["raw_desc"]
    publicador = raw_data["publicador"]
    price = raw_data["price"]
    region = raw_data["region"]
    comuna = raw_data["comuna"]
    company_name = raw_data.get("company_name", "N/A")

    def _resolve_precio_uf(price_text: str, precio_clp_val, uf_val):
        if precio_clp_val and uf_val:
            resolved = round(precio_clp_val / uf_val, 2)
            if resolved is not None:
                return resolved
        parsed_uf, parsed_clp = parse_price_components(price_text)
        if parsed_uf is not None and uf_val:
            return round(parsed_uf, 2)
        if parsed_clp and uf_val:
            return round(parsed_clp / uf_val, 2)
        return None

    content_hash = generate_content_hash(title, raw_desc)

    # deduplicaciÃ³n por hash
    duplicate = await coll.find_one({"details.content_hash": content_hash})
    if duplicate:
        return {"is_duplicate": True, "used_ai": False}

    # â”€â”€ 1. OptimizaciÃ³n: Filtros Locales y CachÃ© (Pre-IA) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Si ya tenemos precio, dormitorios y baÃ±os de forma nativa, podemos saltar Grok
    # para ahorrar tokens y tiempo, generando un resumen programÃ¡tico.
    has_critical = (
        raw_data["price"] != "N/A" and 
        raw_data["dormitorios_str"] != "N/A" and 
        raw_data["banos_str"] != "N/A"
    )
    
    seller_profile_id = raw_data.get("seller_profile_id", "N/A")
    seller_is_pro = raw_data.get("seller_is_pro", False)
    broker_brand = raw_data.get("broker_brand", "N/A")
    classification = classify_seller_state(
        publicador,
        raw_desc,
        company_name,
        seller_profile_id,
        seller_is_pro,
        broker_brand,
        raw_data.get("multi_publisher_count")
    )

    # Check CachÃ© primero
    s_key = normalize_text(f"{publicador} {company_name}").lower()
    cached = SELLER_CACHE.get(s_key)
    if cached:
        known_broker = cached["is_broker"]
        if known_broker:
            logging.debug(f"ðŸ›‘ [CACHE] Broker conocido: {publicador}")
            # Si ya sabemos que es broker, solo llamamos IA si faltan datos crÃ­ticos
            # Si NO faltan datos, devolvemos directo
            if has_critical:
                precio_clp = clean_num(price)
                precio_uf = _resolve_precio_uf(price, precio_clp, uf_value)
                return {
                    "is_duplicate": False,
                    "es_propietario_directo": False,
                    "confianza": 1.0,
                    "publicador": publicador,
                    "company_name": company_name,
                    "tipo_propiedad": raw_data["tipo_propiedad"],
                    "comuna": comuna, "region": region, "sector": "N/A",
                    "precio_clp": precio_clp, "precio_uf": precio_uf,
                    "m2_total": clean_num(raw_data["m2_total_str"]),
                    "dormitorios": clean_num(raw_data["dormitorios_str"]),
                    "banos": clean_num(raw_data["banos_str"]),
                    "descripcion": raw_desc,
                    "enlaces_fotos": raw_data.get("images_url", []),
                    "resumen_limpio": f"Broker detectado (Cache): {publicador}",
                    "content_hash": content_hash,
                    "used_ai": False
                }
        # Importante: el cache negativo no empuja a dueÃ±o; seguimos con la evaluaciÃ³n.

    # Check Blacklist Local (Sin IA)
    is_broker_local = is_likely_broker(publicador, raw_desc, company_name, seller_profile_id, seller_is_pro)
    if is_broker_local:
        # Guardar en cachÃ©
        SELLER_CACHE.set(s_key, {"is_broker": True})
        if has_critical:
            logging.info(f"ðŸš« Saltando IA: Broker detectado localmente ({publicador[:20]})")
            precio_clp = clean_num(price)
            precio_uf = _resolve_precio_uf(price, precio_clp, uf_value)
            return {
                "is_duplicate": False, 
                "es_propietario_directo": False, 
                "confianza": 1.0,
                "publicador": publicador,
                "company_name": company_name,
                "tipo_propiedad": raw_data["tipo_propiedad"], "comuna": comuna, "region": region,
                "precio_clp": precio_clp, "precio_uf": precio_uf,
                "m2_total": clean_num(raw_data["m2_total_str"]),
                "dormitorios": clean_num(raw_data["dormitorios_str"]), "banos": clean_num(raw_data["banos_str"]),
                "descripcion": raw_desc,
                "enlaces_fotos": raw_data.get("images_url", []),
                "resumen_limpio": f"Broker detectado por filtros locales: {publicador}",
                "content_hash": content_hash,
                "used_ai": False
            }

    if has_critical:
        logging.info(f"âš¡ Saltando IA: Datos tÃ©cnicos completos para {title[:30]}...")
        # Limpieza bÃ¡sica de precio para UF
        precio_clp = clean_num(price)
        precio_uf = _resolve_precio_uf(price, precio_clp, uf_value)
        if classification["classification_state"] == "CORREDOR_SEGURO":
            logging.info(f"ðŸš¨ Corredor seguro por semÃ¡ntica: {publicador} / {raw_data.get('company_name')}")
        elif classification["classification_state"] == "INCIERTO":
            logging.info(f"â“ Caso incierto por seguridad: {publicador} / {raw_data.get('company_name')}")

        return {
            "is_duplicate": False,
            "classification_state": classification["classification_state"],
            "es_propietario_directo": classification["es_propietario_directo"],
            "es_corredor": classification["es_corredor"],
            "es_incierto": classification["es_incierto"],
            "score_corredor": classification["score_corredor"],
            "score_dueno": classification["score_dueno"],
            "motivos_corredor": classification["motivos_corredor"],
            "motivos_dueno": classification["motivos_dueno"],
            "confianza": 1.0 if classification["classification_state"] == "CORREDOR_SEGURO" else (0.9 if classification["classification_state"] == "DUEÃ‘O_SEGURO" else 0.5),
            "publicador": publicador,
            "company_name": company_name,
            "tipo_propiedad": raw_data["tipo_propiedad"],
            "comuna": comuna,
            "region": region,
            "sector": "N/A",
            "precio_clp": precio_clp,
            "precio_uf": precio_uf,
            "m2_total": clean_num(raw_data["m2_total_str"]),
            "m2_util": clean_num(raw_data["m2_util_str"]),
            "dormitorios": clean_num(raw_data["dormitorios_str"]),
            "banos": clean_num(raw_data["banos_str"]),
            "estacionamientos": clean_num(raw_data["estacionamientos_str"]),
            "bodega": False,
            "piscina": "piscina" in (raw_data.get("piscina_str","") or "").lower() or "piscina" in raw_desc.lower(),
            "descripcion": raw_desc,
            "enlaces_fotos": raw_data.get("images_url", []),
            "resumen_limpio": f"Propiedad en {comuna}: {raw_data['tipo_propiedad']} con {raw_data['dormitorios_str']}D/{raw_data['banos_str']}B.",
            "content_hash": content_hash,
            "used_ai": False
        }

    # â”€â”€ 2. Llamada a IA (Fallback) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Si faltan datos crÃ­ticos, Grok los extrae de la descripciÃ³n.
    prompt = f"""Extrae datos de este aviso de Yapo.cl. Responde SOLO JSON puro:
{{
  "es_propietario_directo": true/false,
  "confianza": 0.XX,
  "tipo_propiedad": "Departamento/Casa/Oficina/Local Comercial/Bodega/Estacionamiento",
  "comuna": "Nombre Comuna",
  "region": "Nombre RegiÃ³n",
  "sector": "Barrio o sector especÃ­fico o null",
  "precio_clp": numero_entero_o_null,
  "precio_uf": numero_float_o_null,
  "m2_total": numero_float_o_null,
  "m2_util": numero_float_o_null,
  "dormitorios": numero_o_null,
  "banos": numero_o_null,
  "estacionamientos": numero_o_null,
  "bodega": true/false,
  "piscina": true/false,
  "resumen_limpio": "Resumen ejecutivo sin ruidos"
}}

IMPORTANTE: Si el vendedor es una empresa (Re/Max, Century 21, Propiedades, etc.) o si la descripcion menciona "comision", "honorarios" o servicios como "compra sin pie", "gestion de subsidio", "financiamiento", "es_propietario_directo" DEBE ser false.
Solo es true si tienes la certeza absoluta de que el vendedor es una persona natural particular que vende su propia casa. Si el aviso parece un producto de inversiÃ³n o un servicio de ventas masivas, marca false.
    T: {title} | P: {publicador} | Precio: {price}
    D: {raw_desc[:1800]} (Resumen descripciÃ³n)"""

    try:
        resp = await grok_client.chat.completions.create(
            model=Config.GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=500,
            response_format={"type": "json_object"}
        )
        extracted = json.loads(resp.choices[0].message.content)
    except Exception as e:
        logging.error(f"Error llamando a Grok: {e}")
        extracted = {}


    # ConsolidaciÃ³n de datos: Prioridad IA -> Scraper -> CÃ¡lculos
    def get_val(key, fallback_val=None):
        v = extracted.get(key)
        if v is not None and v != "N/A" and v != "": return v
        return raw_data.get(key, fallback_val)

    # LÃ³gica de precios: Si Grok extrajo precio limpio, lo usamos. Si no, parseamos el original.
    ai_clp = clean_num(extracted.get("precio_clp"))
    ai_uf = clean_float(extracted.get("precio_uf"))
    
    if ai_clp is None and ai_uf is None:
        p_uf, p_clp = parse_price_components(price) # Nuestra lÃ³gica manual robusta
    else:
        p_uf, p_clp = ai_uf, ai_clp

    # UF -> CLP conversion si falta uno
    if p_uf is None and p_clp and uf_value: p_uf = round(p_clp / uf_value, 2)
    elif p_clp is None and p_uf and uf_value: p_clp = int(p_uf * uf_value)

    m2_tot = clean_float(extracted.get("m2_total")) or clean_float(raw_data.get("m2_total_str"))
    p_uf_m2 = None
    if p_uf and m2_tot and m2_tot > 0:
        try:
            p_uf_m2 = round(clean_float(p_uf) / clean_float(m2_tot), 3)
        except: 
            pass

    # NormalizaciÃ³n de tipo_propiedad
    tipo_prop = extracted.get("tipo_propiedad") or raw_data.get("tipo_propiedad", "N/A")
    if tipo_prop.lower().endswith('s'): tipo_prop = tipo_prop[:-1]

    dias_en_portal = None
    fecha_pub = raw_data.get("list_time")
    if fecha_pub and fecha_pub != "N/A":
        try:
            if isinstance(fecha_pub, str):
                if fecha_pub.endswith('Z'):
                    fecha_pub = fecha_pub[:-1] + '+00:00'
                pub_dt = datetime.fromisoformat(fecha_pub)
                diff = datetime.now(timezone.utc) - pub_dt if pub_dt.tzinfo else datetime.now() - pub_dt
                dias_en_portal = diff.days
        except Exception:
            pass

    # â”€â”€ 4. STRATEGIC AI AUDITOR (Multi-field Surgical Pass) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Si faltan datos crÃ­ticos despuÃ©s de la extracciÃ³n inicial e IA, usamos el Auditor.
    meta = raw_data.get("_metadata", {})
    m2_tot_final = m2_tot
    lat_final = raw_data.get("lat")
    lon_final = raw_data.get("lon")
    gastos_final = clean_num(raw_data.get("gastos_comunes_str"))

    # Identificar quÃ© falta
    to_audit = []
    if not m2_tot_final: to_audit.append("m2_total")
    if not lat_final or lat_final == "N/A": to_audit.extend(["lat", "lon"])
    if not gastos_final: to_audit.append("gastos_comunes")

    if to_audit:
        html_dump = raw_data.get("html_dump", "")
        if html_dump:
            context = _get_ai_context(html_dump)
            audit_res = await _surgical_ai_audit(context, to_audit, grok_client)
            
            # Aplicar recuperaciones
            recovered = []
            if "m2_total" in audit_res and audit_res["m2_total"]:
                m2_tot_final = audit_res["m2_total"]
                recovered.append("m2")
            if "lat" in audit_res and audit_res["lat"]:
                lat_final = str(audit_res["lat"])
                recovered.append("lat")
            if "lon" in audit_res and audit_res["lon"]:
                lon_final = str(audit_res["lon"])
                recovered.append("lon")
            if "gastos_comunes" in audit_res and audit_res["gastos_comunes"]:
                gastos_final = clean_num(audit_res["gastos_comunes"])
                recovered.append("gastos")
            
            if recovered:
                meta["audit_recovered"] = recovered
                logging.info(f"âœ¨ [AUDITOR] Recuperado: {', '.join(recovered)}")

    # Merge de resultados y persistencia
    return {
        "portal": CONFIG["portal_name"],
        "comuna": (extracted.get("comuna") or comuna).strip(),
        "region": (extracted.get("region") or region).strip(),
        "sector": extracted.get("sector") or raw_data.get("sector"),
        "lat": lat_final,
        "lon": lon_final,
        "tipo_propiedad": tipo_prop,
        "tipo_operacion": _extract_operation_from_url(url),
        "titulo": title.strip()[:220],
        "precio": price,
        "precio_uf": p_uf,
        "precio_clp_raw": p_clp,
        "precio_uf_m2": p_uf_m2,
        "m2_total": m2_tot_final,
        "m2_util": extracted.get("m2_util") or clean_num(raw_data.get("m2_util_str")),
        "gastos_comunes": gastos_final, 
        "dormitorios": extracted.get("dormitorios") or clean_num(raw_data.get("dormitorios_str")),
        "banos": extracted.get("banos") or clean_num(raw_data.get("banos_str")),
        "estacionamientos": extracted.get("estacionamientos") or clean_num(raw_data.get("estacionamientos_str")),
        "bodega": extracted.get("bodega", False),
                "piscina": extracted.get("piscina", False),
        "descripcion": normalize_text(extracted.get("resumen_limpio", raw_desc), CONFIG["desc_max_chars"]),
        "classification_state": classification["classification_state"],
        "es_propietario_directo": classification["es_propietario_directo"],
        "es_corredor": classification["es_corredor"],
        "es_incierto": classification["es_incierto"],
        "score_corredor": classification["score_corredor"],
        "score_dueno": classification["score_dueno"],
        "motivos_corredor": classification["motivos_corredor"],
        "motivos_dueno": classification["motivos_dueno"],
        "confianza_propietario": 1.0 if classification["classification_state"] == "CORREDOR_SEGURO" else (0.9 if classification["classification_state"] == "DUEÃ‘O_SEGURO" else 0.5),
        "dias_en_portal": dias_en_portal,
        "fecha_publicacion": raw_data.get("list_time"),
        "seller_profile_id": seller_profile_id,
        "seller_is_pro": seller_is_pro,
        "publicador": publicador,
        "company_name": company_name,
        "broker_brand": raw_data.get("broker_brand", "N/A"),
        "enlaces_fotos": raw_data.get("images_url", []),
        "content_hash": "N/A", # Will be updated in pipeline
        "fecha_scraping": datetime.now(timezone.utc).isoformat(),
        "fecha_ultima_vista": datetime.now(timezone.utc).isoformat(),
        "metadata": meta,
        "used_ai": True,
        "html_version": 1,
        "parsed_version": 3
    }

async def extract_links_with_scroll(page, max_scrolls: int, delay_s: float) -> set:
    links = set()
    
    def _collect_hrefs(hrefs):
        for h in hrefs:
            if re.search(r'[/_]\d{7,11}(?:\?|$)', h) and not any(x in h for x in ["yapo.cl/comprar", "yapo.cl/vender"]):
                links.add(normalize_url(h))
    
    # Capturar links ANTES del primer scroll
    initial_hrefs = await page.evaluate('''() => {
        return Array.from(document.querySelectorAll('.d3-ads-grid--category-list a[href], a.d3-ad-tile__link')).map(a => a.href);
    }''')
    _collect_hrefs(initial_hrefs)
    
    for _ in range(max_scrolls):
        await page.mouse.wheel(0, 3000)
        await asyncio.sleep(delay_s + random.uniform(0.5, 1.0))
        new_hrefs = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('.d3-ads-grid--category-list a[href], a.d3-ad-tile__link')).map(a => a.href);
        }''')
        _collect_hrefs(new_hrefs)
    return links

async def build_yapo_publisher_activity(coll, raw_data: dict, url: str) -> dict:
    """Build bounded publisher history; missing dates never trigger a penalty."""
    profile_id = str(raw_data.get("seller_profile_id") or "").strip()
    publicador = str(raw_data.get("publicador") or "").strip()
    clauses = []
    if profile_id and profile_id not in {"N/A", "S/I"}:
        clauses.append({"details.seller_profile_id": profile_id})
    if publicador and publicador not in {"N/A", "S/I"}:
        clauses.append({"details.publicador": publicador})
    history = []
    if clauses:
        docs = await coll.find({"$or": clauses}, {
            "_id": 0, "url": 1, "fecha_captura": 1,
            "details.seller_profile_id": 1, "details.publicador": 1,
            "details.company_name": 1, "details.broker_brand": 1,
            "details.titulo": 1, "details.comuna": 1, "details.precio": 1,
            "details.fecha_scraping": 1, "details.fecha_publicacion": 1,
            "details.property_fingerprint": 1,
        }).limit(250).to_list(length=250)
        for doc in docs:
            details = doc.get("details", {}) or {}
            history.append({
                "seller_profile_id": details.get("seller_profile_id"),
                "publicador": details.get("publicador"),
                "company_name": details.get("company_name"),
                "broker_brand": details.get("broker_brand"),
                "title": details.get("titulo"), "comuna": details.get("comuna"),
                "price": details.get("precio"), "url": doc.get("url"),
                "property_fingerprint": details.get("property_fingerprint"),
                "processed_at": details.get("fecha_scraping") or doc.get("fecha_captura"),
                "fecha_publicacion": details.get("fecha_publicacion"),
            })
    current = {
        **raw_data, "url": url,
        "description": raw_data.get("raw_desc", ""),
        "processed_at": datetime.now(timezone.utc),
    }
    current["property_fingerprint"] = property_fingerprint(current)
    return compute_publisher_activity(current, history, window_days=90)


def enrich_yapo_details_with_owner_score(
    raw_data: dict, details: dict, publisher_activity: dict,
) -> dict:
    """Single production integration shared conceptually with the canary."""
    score_input = {
        **raw_data,
        "description": raw_data.get("raw_desc", ""),
        "publisher_activity": publisher_activity,
    }
    result = calculate_owner_score(score_input)
    state = propose_classification_state(result)
    original_signals = {
        "legacy_state": details.get("classification_state"),
        "legacy_score_corredor": details.get("score_corredor"),
        "legacy_score_dueno": details.get("score_dueno"),
        "legacy_motivos_corredor": details.get("motivos_corredor", []),
        "legacy_motivos_dueno": details.get("motivos_dueno", []),
    }
    score_input["classifier_original_signals"] = original_signals
    source_snapshot = build_source_signal_snapshot(score_input)
    details.update({
        "company_name": raw_data.get("company_name") or "",
        "broker_brand": raw_data.get("broker_brand") or "",
        "seller_type": raw_data.get("seller_type") or "",
        "seller_is_pro": bool(raw_data.get("seller_is_pro")),
        "publicador": raw_data.get("publicador") or "",
        "seller_profile_id": raw_data.get("seller_profile_id") or "",
        "publisher_activity": publisher_activity,
        "property_fingerprint": property_fingerprint(score_input),
        "source_signals": source_snapshot,
        "classifier_original_signals": original_signals,
        "owner_score": result.score,
        "owner_score_version": result.version,
        "owner_score_signals": list(result.signals),
        "owner_score_calculated_at": datetime.now(timezone.utc).isoformat(),
        "classification_state": state,
        "classification_confidence": 0.98 if state == "CORREDOR_SEGURO" else (0.95 if state == "DUEÑO_SEGURO" else 0.5),
        "classification_rule_version": "owner-state-v1-first-person",
        "es_propietario_directo": state == "DUEÑO_SEGURO",
        "es_corredor": state == "CORREDOR_SEGURO",
        "es_incierto": state == "INCIERTO",
    })
    is_owner = state == "DUE\u00d1O_SEGURO"
    details["classification_confidence"] = (
        0.98 if state == "CORREDOR_SEGURO" else (0.95 if is_owner else 0.5)
    )
    details["es_propietario_directo"] = is_owner
    return details


async def main():
    parser = argparse.ArgumentParser(description="Yapo Scraper INDUSTRIAL v5 - Persistent Queue + Stealth")
    parser.add_argument("--use-proxies", action="store_true")
    parser.add_argument("--concurrency", type=int, default=CONFIG["max_concurrency"])
    parser.add_argument("--max-pages", type=int, default=CONFIG["max_pages"])
    parser.add_argument("--force-discovery", action="store_true", help="Forzar bÃºsqueda de links")
    args = parser.parse_args()

    uf_value = await get_uf_value()
    logging.info(f"ðŸ’± UF actual: ${uf_value:,.0f} CLP")

    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]
    queue_coll = db["yapo_queue"]

    # Ãndices crÃ­ticos
    await coll.create_index("url", unique=True)
    await coll.create_index("details.content_hash")
    await queue_coll.create_index("url", unique=True)
    await queue_coll.create_index("status")

    # 1. ETAPA DE DESCUBRIMIENTO / FALLBACK (Discovery Layer)
    now = datetime.now(timezone.utc)
    urls_to_process = []

    async with async_playwright() as p_disc:
        disc_browser = await p_disc.chromium.launch(headless=True)
        disc_ctx = await disc_browser.new_context()
        disc_page = await disc_ctx.new_page()

        # --- Discovery: buscar propiedades nuevas ---
        logging.info("ðŸ” Iniciando Discovery Layer...")
        urls_to_process = await discover_new_properties(disc_page, db, base_url=CONFIG.get("base_url", ""))
        logging.info(f"ðŸ” Discovery: {len(urls_to_process)} nuevas propiedades encontradas")

        if not urls_to_process:
            # --- Fallback: re-scrapear propiedades antiguas/incompletas ---
            logging.info("â™»ï¸ Fallback activado: buscando propiedades para re-check...")
            urls_to_process = await get_properties_to_rescrape(db)
            if urls_to_process:
                logging.info(f"â™»ï¸ Fallback activado: {len(urls_to_process)} propiedades a re-scrapear")

        await disc_browser.close()

    # --- AJUSTE 3: ProtecciÃ³n extra en cola ---
    if not urls_to_process:
        logging.info("âš ï¸ No hay URLs para procesar. Finalizando.")
        client.close()
        return

    # Deduplicar
    urls_to_process = list(set(urls_to_process))
    # --- AJUSTE 4: Logging Ãºtil ---
    logging.info(f"ðŸ“¦ URLs encoladas: {len(urls_to_process)}")

    # --- INTEGRACIÃ“N SEGURA CON COLA (VersiÃ³n Safe) ---
    # Paso 1: Eliminar SOLO jobs pendientes con mÃ¡s de 1 hora de antigÃ¼edad
    one_hour_ago = now - timedelta(hours=1)
    del_res = await queue_coll.delete_many({
        "status": "pending",
        "created_at": {"$lt": one_hour_ago}
    })
    if del_res.deleted_count > 0:
        logging.info(f"ðŸ—‘ï¸ Limpieza: {del_res.deleted_count} jobs pendientes antiguos eliminados.")

    # Paso 2: Insertar nuevos jobs (ordered=False ignora duplicados si ya estÃ¡n en processing)
    docs = [{"url": url, "status": "pending", "retries": 0, "created_at": now} for url in urls_to_process]
    try:
        await queue_coll.insert_many(docs, ordered=False)
    except Exception:
        # BulkWriteError esperado si alguna URL ya existe con otro estado â€” se ignora
        pass


    # 2. EXTRACCIÃ“N
    proxy_api_url = os.getenv("PROXY_API_URL")
    proxies = await get_proxies_from_api(proxy_api_url) if proxy_api_url else [p.strip() for p in Config.PROXIES.split(",") if p.strip()]
    proxy_cycle = cycle(proxies) if proxies else None

    # 7. DASHBOARD DE CONTROL EN TIEMPO REAL
    stats = {
        "processed": 0, "new": 0, "duplicates": 0,
        "owners": 0, "brokers": 0, "uncertain": 0, "errors": 0, "skipped_ai": 0
    }
    total_pending = await queue_coll.count_documents({"status": "pending"})

    def print_dashboard():
        """Tablero de control industrial."""
        done, total = stats['processed'], total_pending
        pct = (done / total * 100) if total > 0 else 0
        mb = sum(PROXY_MB_USAGE.values())
        logging.info(f"ðŸ“Š [%d/%d %d%%] | +Nuevos:%d | ðŸ”„Dupes:%d | âŒFail:%d | ðŸ D/ðŸ¢C/â“: %d/%d/%d | ðŸ’°%.1fMB", done, total, pct, stats['new'], stats['duplicates'], stats['errors'], stats['owners'], stats['brokers'], stats['uncertain'], mb)
        return # Salir para que no ejecute el print de abajo por problemas de codificaciÃ³n
        print(f"\rðŸ“Š [{done}/{total_pending} {pct:.0f}%] | +Nuevos:{stats['new']} | ðŸ”„Dupes:{stats['duplicates']} | âŒErr:{stats['errors']} | ðŸ D/ðŸ¢C: {stats['owners']}/{stats['brokers']} | ï¿½{mb:.1f}MB", end="", flush=True)

    logging.info(f"ðŸš€ ETAPA 2: ExtracciÃ³n | Pendientes: {total_pending} | Concurrency: {args.concurrency}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        
        async def worker(worker_id):
            ua = UserAgent()
            is_rotator = len(proxies) == 1
            while True:
                # Polling atÃ³mico de la cola para obtener el proxy a usar en la sesiÃ³n
                # Prioridad: mÃ¡s recientes primero (created_at DESC), luego menos reintentos
                doc = await queue_coll.find_one_and_update(
                    {"status": "pending"},
                    {"$set": {"status": "processing", "worker": worker_id}},
                    sort=[("created_at", -1), ("retries", 1)],
                    return_document=True
                )
                if not doc: break

                url = doc["url"]
                
                # Obtener un proxy que no estÃ© en cooldown
                proxy = None
                if proxy_cycle:
                    if is_rotator:
                        # Para rotadores, no bloqueamos globalmente (la IP cambia igual)
                        proxy = next(proxy_cycle)
                    else:
                        # LÃ³gica estÃ¡ndar para lista de proxies estÃ¡ticos
                        for _ in range(len(proxies)):
                            p = next(proxy_cycle)
                            cooldown_until = BURNED_PROXIES.get(p)
                            if not cooldown_until or datetime.now() > cooldown_until:
                                proxy = p
                                break
                    
                    if not proxy:
                        logging.error("âŒ TODOS LOS PROXIES ESTÃN EN COOLDOWN. Esperando 15s...")
                        await asyncio.sleep(15)
                        await queue_coll.update_one({"url": url}, {"$set": {"status": "pending"}})
                        continue

                ctx_proxy = {"server": proxy} if proxy else None
                if ctx_proxy and Config.PROXY_USER:
                    ctx_proxy["username"], ctx_proxy["password"] = Config.PROXY_USER, Config.PROXY_PASS

                # Reducir ruido: log de sesiÃ³n a DEBUG
                logging.debug(f"ðŸŒ [W{worker_id}] SesiÃ³n batch({CONFIG['urls_per_session']}) | Proxy: {proxy or 'directo'}")
                
                # ConfiguraciÃ³n de cliente HTTP ligero persistente
                h_headers = {
                    "User-Agent": ua.random,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Upgrade-Insecure-Requests": "1",
                    "Referer": "https://www.yapo.cl/",
                }
                h_proxy = None
                if proxy:
                    h_proxy = proxy if proxy.startswith("http") else f"http://{proxy}"

                ctx = None
                try:
                    async with curl_requests.AsyncSession(headers=h_headers, proxies={"http": h_proxy, "https": h_proxy} if h_proxy else None, timeout=15.0, impersonate="chrome120") as h_client:
                        ctx = await browser.new_context(user_agent=ua.random, proxy=ctx_proxy)
                        for batch_i in range(CONFIG["urls_per_session"]):
                            if batch_i > 0:
                                # Prioridad: mÃ¡s recientes primero (created_at DESC), luego menos reintentos
                                doc = await queue_coll.find_one_and_update(
                                    {"status": "pending"},
                                    {"$set": {"status": "processing", "worker": worker_id}},
                                    sort=[("created_at", -1), ("retries", 1)],
                                    return_document=True
                                )
                            if not doc: break
                            url = doc["url"]

                            # === PRE-CHECK: Â¿Ya existe en DB? ===
                            exists = await coll.find_one({"url": url}, {"_id": 1})
                            if exists:
                                await queue_coll.update_one({"url": url}, {"$set": {"status": "completed"}})
                                stats["duplicates"] += 1
                                stats["processed"] += 1
                                print_dashboard()
                                continue

                            page = None
                            raw_data = None
                            try:
                                # 1.1 INTENTO FAST PATH (~40KB)
                                p_key = proxy or "directo"
                                PROXY_USAGE[p_key] += 1
                                
                                try:
                                    raw_data, size_bytes = await extract_fast_path(url, h_client)
                                    if not raw_data:
                                        logging.warning(f"âš ï¸ Fast Path sin datos (Posible 404 farsa). Verificando en Browser...")
                                    else:
                                        PROXY_MB_USAGE[p_key] += (size_bytes / (1024 * 1024))
                                except ProxyBlockedError:
                                    if is_rotator:
                                        wait_s = random.uniform(2, 5)
                                        logging.warning(f"ðŸ”¥ Bloqueo (Fast) -> Backoff {wait_s:.1f}s")
                                        await asyncio.sleep(wait_s)
                                    else:
                                        logging.warning(f"ðŸ”¥ Proxy en COOLDOWN (Fast Path): {p_key}. Rotando...")
                                        BURNED_PROXIES[p_key] = datetime.now() + timedelta(seconds=60)
                                    
                                    await queue_coll.update_one({"url": url}, {"$set": {"status": "pending"}})
                                    raise # Relanzar para salir del batch
                                
                                mb_url = size_bytes / (1024 * 1024)
                                # Log consolidado: URL + Proxy en una sola lÃ­nea
                                logging.info(f"ðŸ”— [W{worker_id}] {url} | {p_key} | {mb_url:.3f}MB")
                                
                                # Si el Fast Path falla o es incompleto â†’ Fallback
                                # RELAXED: Solo vamos a browser si faltan datos CRÃTICOS (Precio O Dormitorios).
                                # Si falta el tÃ­tulo, no importa (la IA lo saca de la descripciÃ³n).
                                if not raw_data or (raw_data.get("price") == "N/A" and raw_data.get("dormitorios") == "N/A"):
                                    if raw_data: logging.info(f"âš ï¸ Datos crÃ­ticos faltantes ({raw_data.get('price')}/{raw_data.get('dormitorios')}). Browser Fallback...")
                                    
                                    if not page:
                                        page = await ctx.new_page()
                                        session_bytes = [0]
                                        async def log_response(response):
                                            try:
                                                cl = response.headers.get("content-length")
                                                if cl: session_bytes[0] += int(cl)
                                                else:
                                                    b = await response.body()
                                                    session_bytes[0] += len(b)
                                            except: pass
                                        page.on("response", log_response)
                                        try: await stealth(page)
                                        except: pass
                                        await block_resources(page, mode="ultra")
                                    
                                    try:
                                        raw_data = await extract_raw_data(page, url)
                                    except ProxyBlockedError as e:
                                        if is_rotator:
                                            wait_s = random.uniform(4, 10)
                                            logging.warning(f"ðŸ”¥ Bloqueo (Browser) -> Backoff {wait_s:.1f}s")
                                            await asyncio.sleep(wait_s)
                                        else:
                                            logging.warning(f"ðŸ”¥ Proxy en COOLDOWN (Browser): {proxy}. Rotando...")
                                            BURNED_PROXIES[proxy] = datetime.now() + timedelta(seconds=60)
                                        
                                        await queue_coll.update_one({"url": url}, {"$set": {"status": "pending"}})
                                        raise e # Relanzamos para salir del loop de batch
                                    except Exception as e:
                                        bw_session_mb = session_bytes[0] / (1024 * 1024)
                                        PROXY_MB_USAGE[p_key] += bw_session_mb
                                        raise e
                                        
                                    if not raw_data:
                                        logging.warning(f"ðŸ’€ Anuncio verificado borrado en Yapo: {url[-25:]}")
                                        await coll.update_one({"url": url}, {"$set": {"status": "eliminado", "fecha_eliminacion": datetime.now(timezone.utc)}})
                                        await queue_coll.update_one({"url": url}, {"$set": {"status": "completed"}})
                                        stats["processed"] += 1
                                        PROXY_MB_USAGE[p_key] += (session_bytes[0] / (1024 * 1024))
                                        print_dashboard()
                                        continue

                                    if raw_data:
                                        raw_data["source"] = "browser"
                                        bw_session_mb = session_bytes[0] / (1024 * 1024)
                                        PROXY_MB_USAGE[p_key] += bw_session_mb
                                        logging.info(f"ðŸŒ [W{worker_id}] Browser BW: {bw_session_mb:.2f}MB | Acumulado: {PROXY_MB_USAGE[p_key]:.2f}MB")

                            except ProxyBlockedError:
                                # No contamos el bloqueo como error terminal, es solo un retry
                                break 

                            except Exception as e:
                                stats["errors"] += 1
                                stats["processed"] += 1
                                error_msg = str(e)[:100]
                                logging.error(f"âš ï¸ [W{worker_id}] Error en {url[-20:]}: {error_msg}")
                                ns = "failed" if doc.get("retries", 0) >= CONFIG["max_retries_per_url"] else "pending"
                                await queue_coll.update_one({"url": url}, {"$set": {"status": ns, "last_error": error_msg}, "$inc": {"retries": 1}})
                                if "Tunnel" in error_msg or "ERR_TUNNEL" in error_msg: break # Salir del batch si el tÃºnel fallÃ³
                                continue
                            finally:
                                if page:
                                    try: await page.close()
                                    except: pass
                                    page = None

                            # === FASE 2: PROCESAMIENTO IA (con Filtro Pre-IA) ===
                            try:
                                # --- FILTRO PRE-IA (INSERT HERE) ---
                                _publicador = raw_data.get("publicador", "N/A")
                                _desc = raw_data.get("raw_desc", "N/A")
                                _company = raw_data.get("company_name", "N/A")
                                _price = raw_data.get("price", "N/A")
                                _dorm = raw_data.get("dormitorios_str", "N/A")
                                _banos = raw_data.get("banos_str", "N/A")
                                _raw_available = (
                                    raw_data.get("title", "N/A") != "N/A" and
                                    _price != "N/A"
                                )

                                # PRIORIDAD 0: Badge Profesional de Yapo (seÃ±al HTML directa REAL)
                                # seller_is_pro = True SOLO si hay un badge visual 'Profesional' en el HTML.
                                # profile_id existe en el 100% de usuarios de Yapo (dueÃ±os y corredores).
                                # NO es indicador de corredor. Bug histÃ³rico: antes se forzaba is_pro=True aquÃ­.
                                _seller_is_pro = raw_data.get("seller_is_pro", False)  # Solo badge real
                                _profile_id = raw_data.get("seller_profile_id", "N/A")
                                _broker_brand = raw_data.get("broker_brand", "N/A")
                                _classification = classify_seller_state(
                                    _publicador, _desc, _company, _profile_id, _seller_is_pro, _broker_brand,
                                    raw_data.get("multi_publisher_count")
                                )

                                # PRIORIDAD 1: Broker detectado por keywords â†’ SIEMPRE saltar IA
                                if is_likely_broker(_publicador, _desc, _company, _profile_id, _seller_is_pro):
                                    logging.info(f"ðŸš« Broker detectado (pre-IA / keywords): {_publicador[:30]}")
                                    details = {
                                        "portal": CONFIG["portal_name"],
                                        "titulo": raw_data.get("title", "N/A"),
                                        "precio": _price,
                                        "comuna": raw_data.get("comuna", "N/A"),
                                        "region": raw_data.get("region", "N/A"),
                                        "sector": raw_data.get("sector", "N/A"),
                                        "lat": raw_data.get("lat", "N/A"),
                                        "lon": raw_data.get("lon", "N/A"),
                                        "tipo_propiedad": raw_data.get("tipo_propiedad", "N/A"),
                                        "tipo_operacion": _extract_operation_from_url(url),
                                        "m2_total": clean_num(raw_data.get("m2_total_str")),
                                        "dormitorios": clean_num(_dorm),
                                        "banos": clean_num(_banos),
                                        "classification_state": "CORREDOR_SEGURO",
                                        "es_propietario_directo": False,
                                        "es_corredor": True,
                                        "es_incierto": False,
                                        "score_corredor": _classification["score_corredor"],
                                        "score_dueno": _classification["score_dueno"],
                                        "motivos_corredor": _classification["motivos_corredor"],
                                        "motivos_dueno": _classification["motivos_dueno"],
                                        "confianza_propietario": 0.0,  # NUNCA alta confianza a broker
                                        "publicador": _publicador,
                                        "company_name": _company,
                                        "seller_profile_id": _profile_id,
                                        "seller_is_pro": _seller_is_pro,
                                        "broker_brand": raw_data.get("broker_brand", "N/A"),
                                        "fecha_scraping": datetime.now(timezone.utc).isoformat(),
                                        "used_ai": False,
                                        "is_duplicate": False,
                                    }

                                # PRIORIDAD 2: Datos completos â†’ saltar IA
                                elif _price != "N/A" and _dorm != "N/A" and _banos != "N/A" and _raw_available:
                                    logging.info(f"âš¡ Saltando IA (datos completos): {raw_data.get('title','')[:30]}")
                                    precio_clp = clean_num(_price)
                                    precio_uf = round(precio_clp / uf_value, 2) if (precio_clp and uf_value) else None
                                    is_brk = _classification["classification_state"] == "CORREDOR_SEGURO"
                                    details = {
                                        "portal": CONFIG["portal_name"],
                                        "titulo": raw_data.get("title", "N/A"),
                                        "precio": _price,
                                        "precio_clp_raw": precio_clp,
                                        "precio_uf": precio_uf,
                                        "comuna": raw_data.get("comuna", "N/A"),
                                        "region": raw_data.get("region", "N/A"),
                                        "sector": raw_data.get("sector", "N/A"),
                                        "lat": raw_data.get("lat", "N/A"),
                                        "lon": raw_data.get("lon", "N/A"),
                                        "tipo_propiedad": raw_data.get("tipo_propiedad", "N/A"),
                                        "tipo_operacion": _extract_operation_from_url(url),
                                        "m2_total": clean_num(raw_data.get("m2_total_str")),
                                        "m2_util": clean_num(raw_data.get("m2_util_str")),
                                        "dormitorios": clean_num(_dorm),
                                        "banos": clean_num(_banos),
                                        "estacionamientos": clean_num(raw_data.get("estacionamientos_str")),
                                        "classification_state": _classification["classification_state"] if not is_brk else "CORREDOR_SEGURO",
                                        "es_propietario_directo": _classification["classification_state"] == "DUEÃ‘O_SEGURO",
                                        "es_corredor": _classification["classification_state"] == "CORREDOR_SEGURO",
                                        "es_incierto": _classification["classification_state"] == "INCIERTO" or not (_classification["classification_state"] in ["DUEÃ‘O_SEGURO", "CORREDOR_SEGURO"]),
                                        "score_corredor": _classification["score_corredor"],
                                        "score_dueno": _classification["score_dueno"],
                                        "motivos_corredor": _classification["motivos_corredor"],
                                        "motivos_dueno": _classification["motivos_dueno"],
                                        "confianza_propietario": 1.0 if _classification["classification_state"] == "CORREDOR_SEGURO" else (0.9 if _classification["classification_state"] == "DUEÃ‘O_SEGURO" else 0.5),
                                        "publicador": _publicador,
                                        "company_name": _company,
                                        "seller_profile_id": _profile_id,
                                        "seller_is_pro": _seller_is_pro,
                                        "broker_brand": raw_data.get("broker_brand", "N/A"),
                                        "fecha_scraping": datetime.now(timezone.utc).isoformat(),
                                        "used_ai": False,
                                        "is_duplicate": False,
                                    }

                                # PRIORIDAD 3: Usar IA (caso base)
                                else:
                                    details = await process_with_ai(raw_data, grok_client, uf_value, coll, url)

                                if details:
                                    if details.get("is_duplicate"):
                                        stats["duplicates"] += 1
                                        await queue_coll.update_one({"url": url}, {"$set": {"status": "completed"}})
                                    else:
                                        sm = "âš¡" if raw_data.get("source") == "fast_path" else "ðŸŒ"
                                        logging.info(f"âœ¨ {sm} Ã‰xito ({raw_data.get('source')})")
                                        publisher_activity = await build_yapo_publisher_activity(coll, raw_data, url)
                                        details = enrich_yapo_details_with_owner_score(
                                            raw_data,
                                            details,
                                            publisher_activity,
                                        )
                                        await coll.update_one(
                                            {"url": url}, 
                                            {"$set": {
                                                "url": url,
                                                "details": details,
                                                "origen": "yapo.cl",
                                                "fecha_captura": datetime.now(timezone.utc),
                                                "source_signals": details.get("source_signals", {}),
                                                "company_name": details.get("company_name", ""),
                                                "broker_brand": details.get("broker_brand", ""),
                                                "seller_type": details.get("seller_type", ""),
                                                "seller_is_pro": details.get("seller_is_pro", False),
                                                "publicador_visible": details.get("publicador", ""),
                                                "publisher_activity": details.get("publisher_activity", {}),
                                                "classifier_original_signals": details.get("classifier_original_signals", {}),
                                                "owner_score": details.get("owner_score"),
                                                "owner_score_version": details.get("owner_score_version"),
                                                "owner_score_signals": details.get("owner_score_signals", []),
                                            }}, 
                                            upsert=True
                                        )
                                        stats["new"] += 1
                                        if not details.get("used_ai"): stats["skipped_ai"] += 1
                                        state = details.get("classification_state")
                                        if state == "DUEÃ‘O_SEGURO" or details.get("es_propietario_directo"):
                                            stats["owners"] += 1
                                        elif state == "CORREDOR_SEGURO" or details.get("es_corredor"):
                                            stats["brokers"] += 1
                                        else:
                                            stats["uncertain"] += 1
                                        await queue_coll.update_one({"url": url}, {"$set": {"status": "completed"}})
                                else:
                                    raise Exception("IA retornÃ³ None")
                            except Exception as e:
                                stats["errors"] += 1
                                error_msg = f"AI: {str(e)[:90]}"
                                logging.error(f"ðŸ¤– [W{worker_id}] {url[-20:]}: {error_msg}")
                                await queue_coll.update_one({"url": url}, {"$set": {"status": "pending", "last_error": error_msg}, "$inc": {"retries": 1}})
                            
                            stats["processed"] += 1
                            print_dashboard()
                            await asyncio.sleep(random.uniform(1, 2))
                except Exception as e:
                    logging.error(f"ðŸ”„ Rotando proxy o error de sesiÃ³n: {str(e)[:40]}...")
                    await asyncio.sleep(3)
                finally:
                    if ctx:
                        try: await ctx.close()
                        except: pass
                        ctx = None

        tasks = [worker(i) for i in range(args.concurrency)]
        await asyncio.gather(*tasks)
        await browser.close()

    # â”€â”€ POST-PROCESO: Reclasificar multi-publicadores â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Si un "dueÃ±o" tiene 5+ propiedades activas en la BD, es un corredor camuflado.
    logging.info("Iniciando post-proceso: detecciÃ³n de multi-publicadores...")
    try:
        from collections import defaultdict
        # Disabled permanently: lifetime publication counts caused false broker
        # classifications. Per-listing scoring above already evaluates temporal
        # activity with explainable thresholds and distinct-property fingerprints.
        pipeline = [
            {"$match": {"_id": {"$exists": False}}},
            {"$group": {
                "_id": {"pub": "$details.publicador", "prof": "$details.seller_profile_id"},
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"}
            }},
            {"$match": {"count": {"$gte": 5}}}
        ]
        multi_publishers = await db["yapo_propiedades"].aggregate(pipeline).to_list(length=None)
        reclasificados_multi = 0
        for group in multi_publishers:
            pub = group["_id"].get("pub", "N/A")
            prof = group["_id"].get("prof", "N/A")
            # Excluir nombres vacÃ­os o N/A
            if not pub or pub == "N/A":
                continue
            if prof in (None, "", "N/A"):
                continue
            for doc_id in group["ids"]:
                await db["yapo_propiedades"].update_one(
                    {"_id": doc_id},
                    {"$set": {
                        "details.es_propietario_directo": False,
                        "details.audit_fix": "multi_publisher_reclassified"
                    }}
                )
                reclasificados_multi += 1
        logging.info(f"Post-proceso completado: {reclasificados_multi} registros reclasificados por multi-publicador.")
    except Exception as e:
        logging.warning(f"Post-proceso multi-publicador fallÃ³: {e}")

    client.close()
    
    # Resumen final
    logging.info("â•" * 60)
    logging.info("ðŸŽ‰ SCRAPING FINALIZADO")
    logging.info(f"ðŸ“Š Procesados: {stats['processed']} | Nuevos: {stats['new']} | Duplicados: {stats['duplicates']}")
    logging.info(f"ðŸ  DueÃ±os: {stats['owners']} | ðŸ¢ Corredores: {stats['brokers']} | â“ Inciertos: {stats['uncertain']} | ðŸ¤– Sin IA: {stats['skipped_ai']}")
    logging.info(f"âŒ Errores: {stats['errors']}")
    logging.info("-" * 30)
    logging.info("ðŸ“ˆ RESUMEN DE CONSUMO DE PROXIES:")
    total_mb_final = 0
    for p, count in PROXY_USAGE.items():
        mb = PROXY_MB_USAGE.get(p, 0)
        total_mb_final += mb
        logging.info(f"   - {p}: {count} peticiones | {mb:.2f} MB")
    logging.info(f"ðŸ’° CONSUMO TOTAL: {total_mb_final:.2f} MB")
    logging.info("â•" * 60)

if __name__ == "__main__":
    asyncio.run(main())
