import os
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
from collections import defaultdict

from curl_cffi import requests as curl_requests
from fake_useragent import UserAgent
from playwright.async_api import async_playwright
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

from config import Config

from playwright_stealth import Stealth

async def apply_stealth(page):
    """Aplica técnicas de sigilo al navegador."""
    await Stealth().apply_stealth_async(page)

# ====================== CONFIG ======================
CONFIG = {
    "portal_name": "yapo.cl",
    "max_pages": 500,             # Fallback de seguridad (auto-stop inteligente frena antes)
    "max_scrolls": 4,
    "scroll_delay": 0.9,
    "max_concurrency": 4,
    "page_load_timeout": 30,      # 30s — si no carga, proxy lento → rotar
    "networkidle_timeout": 10,
    "html_max_chars": 16000,
    "desc_max_chars": 8000,
    "discovery_force_on": True,   # Siempre busca links nuevos al encender el scraper
    "search_urls": [
        "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=valparaiso-vina-del-mar,valparaiso-valparaiso&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,bienes-raices-venta-de-propiedades-casas|f_price.80000000-|f_currency.CLP",
        #"https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades?regionslug=region-metropolitana-la-florida,region-metropolitana-macul&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,bienes-raices-venta-de-propiedades-casas|f_price.80000000-|f_currency.CLP"
    ],
    "max_retries_per_url": 3,
    "hours_to_recheck": 12,
    "urls_per_session": 30,
    "max_empty_pages_streak": 2,  # Detiene la búsqueda si pasan X páginas seguidas sin encontrar links nuevos
    "max_queue_size": 1000,       # Tamaño máximo de cola para activar control de backlog
    "priority_processing": True,  # Activa el sistema de Fresh-First
}

# ====================== GLOBAL STATE ======================
PROXY_USAGE = defaultdict(int)
PROXY_MB_USAGE = defaultdict(float)
BURNED_PROXIES = {} # proxy -> cooldown_until (datetime)
SUCCESSFUL_NO_PROXY = set() # cache de URLs exitosas sin proxy
MAX_MB_PER_PROXY = 50.0 # Límite de ancho de banda por proxy

# Excepciones personalizadas para manejo de proxies
class ProxyBlockedError(Exception): pass
class CaptchaError(Exception): pass

grok_client = AsyncOpenAI(
    api_key=Config.XAI_API_KEY,
    base_url=Config.GROK_BASE_URL
)

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
# Silenciar logs ruidosos de librerías
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
                logging.info(f"💱 UF (vía Gael): ${val:,.2f}")
                return val
            # Tercer intento - mindicador
            r = await client.get("https://mindicador.cl/api/uf")
            if r.status_code == 200:
                val = float(r.json()['serie'][0]['valor'])
                return val
    except: pass
    logging.warning("⚠️ Sin API de UF. Usando $39,800 CLP.")
    return 39800.0

async def get_proxies_from_api(api_url: str) -> list:
    """Obtiene una lista de proxies desde una URL API."""
    try:
        async with curl_requests.AsyncSession(timeout=10.0, impersonate="chrome120") as client:
            resp = await client.get(api_url)
            if resp.status_code == 200:
                # Asumimos que la API devuelve proxies separados por línea o en JSON
                text = resp.text.strip()
                if text.startswith("[") or text.startswith("{"):
                    # Si es JSON, intentamos parsearlo
                    data = resp.json()
                    if isinstance(data, list): return data
                    if isinstance(data, dict) and "proxies" in data: return data["proxies"]
                return [p.strip() for p in text.splitlines() if p.strip()]
    except Exception as e:
        logging.error(f"❌ Error obteniendo proxies de API: {e}")
    return []

def parse_price_components(price_str: str):
    if not price_str or price_str == "N/A":
        return None, None
    s = price_str.upper()
    
    # Extraer UF primero (es más específico)
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
    """Limpia un string de número (quita puntos, comas, símbolos) y retorna int o None."""
    if s is None or s == "N/A": return None
    if isinstance(s, (int, float)): return int(s)
    
    # Remover símbolos de moneda y espacios
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
    """Limpia un string de número preservando decimales y eliminando símbolos."""
    if s is None or s == "N/A": return None
    if isinstance(s, (int, float)): return float(s)
    
    # Conservar solo dígitos y separadores
    val = re.sub(r'[^\d.,]', '', str(s))
    if not val: return None

    try:
        if ',' in val and '.' in val:
            if val.rfind('.') > val.rfind(','): return float(val.replace(',', ''))
            else: return float(val.replace('.', '').replace(',', '.'))
        
        sep = ',' if ',' in val else '.' if '.' in val else None
        if not sep: return float(val)
        
        parts = val.split(sep)
        # Lógica chilena: si hay múltiples puntos o el último grupo es de 3, es separador de miles
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
    """Hash robusto: normaliza texto y elimina ruidos (precios, teléfonos) para evitar duplicados."""
    # 1. Combinar y bajar a minúsculas
    text = f"{title} {description}".lower()
    # 2. Quitar acentos (NFKD)
    text = normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII')
    # 3. Eliminar ruidos comunes que los dueños cambian frecuentemente:
    # - Precios ($ o UF seguido de números)
    # - Teléfonos (+56, 9 xxx, etc.)
    # - Superficies (m2)
    text = re.sub(r'(clp|\$|uf|m2)\s*[\d\.,]+', '', text) 
    text = re.sub(r'(\+?56\s?9?|9)\s?\d{7,8}', '', text)  
    # 4. Quedarse solo con lo alfanumérico core
    text = re.sub(r'[^a-z0-9]', '', text)
    # 5. Tomar una muestra significativa (ej: 800 chars) para el MD5
    return hashlib.md5(text[:800].encode('utf-8')).hexdigest()

def calculate_priority(fecha_descubrimiento: datetime, broker_score_val: float = None, is_new: bool = False) -> float:
    """Calcula el score de prioridad (Part 13)."""
    now = datetime.now(timezone.utc)
    # Recency
    diff = now - fecha_descubrimiento
    hours = diff.total_seconds() / 3600
    
    if hours < 24: recency = 1.0
    elif hours < 72: recency = 0.7
    elif hours < 168: recency = 0.4
    else: recency = 0.1
    
    # Broker influence
    broker_adj = 0.0
    if broker_score_val is not None:
        if broker_score_val < 0.2: broker_adj = 0.5
        elif broker_score_val <= 0.5: broker_adj = 0.2
        else: broker_adj = -1.0
    
    # Penalties (Duplicates handled separately in discovery/worker)
    priority = recency + broker_adj
    if is_new: priority += 0.2 # Bonus for explicitly being discovered in this run
    
    return round(priority, 2)

def normalize_url(href: str) -> str:
    full = urljoin("https://www.yapo.cl", href)
    p = urlparse(full)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))

# ── Mapas de etiquetas Normalizadas (sin acento) ──────────────────────────
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

# --- LÓGICA DE DETECCIÓN DE CORREDORES ---
_BROKER_KEYWORDS = {
    # --- Grandes Franquicias y Cadenas ---
    "remax", "re/max", "re max", "re-max", "century 21", "c21", "engel", "völkers", "volkers", 
    "keller williams", "kw chile", "coldwell banker", "sothebys", "realty", 
    "betterhomes", "property partners", "zillow", "houm", "isbast", "buydepa",

    # --- Corredoras Tradicionales (Chile) ---
    "fuenzalida", "ahumada", "valdivieso", "larrain", "mclean", "prevost", 
    "quinteros", "skyline", "one propiedades", "maca", "habitab", "grupocasa", 
    "buscapro", "copro", "toctoc", "procasa", "mateo sánchez", "marcos sánchez",
    "pablo cassini", "fajre", "besnier", "uribe", "soza", "morandé", "dante",
    "matias ruffat", "vivaqui", "golden", "infofit", "p&g", "pgr", "pro urbe",
    "urzúa", "jaime masmela", "pizarro propiedades", "carreño", "puelma",
    "assetplan", "nexxos", "hyc", "h y c", "arrendo", "arriendo plus",
    "mueve chile", "plusrent", "rentahouse", "findep", "arrendaplus", "alucerto",

    # --- Inmobiliarias / Constructoras ---
    "socovesa", "almagro", "aconcagua", "ingevec", "imagina", "rvc", "salfacorp", 
    "sinergia", "ebco", "euroinmobiliaria", "manquehue", "moller", "siena", 
    "paz corp", "besalco", "su ksa", "fundamenta", "activa", "armas", "iman",
    "ictinos", "desa", "claro vicuña", "valmar", "enaco", "pocuro", "indesa",

    # --- Consultoría e Inversión ---
    "colliers", "jll", "cushman", "wakefield", "gps property", "fitzroy", 
    "asset", "capital", "management", "investment", "inversión", "inversiones",
    "renta", "patrimonio", "valoriza", "tasaciones", "gestión", "proyectos",

    # --- Identificadores Legales ---
    "cia ltda", "compañia limitada", "sociedad", "spa", "s.a.", "eirl", "asociados", 
    "group", "partners", "consulting", "holding", "legal", "estudio", "propiedades",
    "inmobiliaria", "corredora", "corretaje", "broker", "real estate",
    "ejecutivo", "asesor", "habitacional", "comercializadora", "bienes raices",
    "corredor de propiedades", "gestora", "admon", "administración",

    # --- Servicios y Ganchos Comerciales (No Dueños) ---
    "compra sin pie", "sin pie", "con subsidio", "ds1", "ds19", "gestionamos", 
    "aprobacion", "pie en cuotas", "financiamiento", "con o sin pie", 
    "oportunidad inversionista", "rentabilidad", "plusvalia", "sala de ventas",
    "piloto", "vende inmobiliaria", "arrienda inmobiliaria", "agendar visita",
    "metraje aproximado", "gastos comunes aprox", "comisión más iva", "sii",
    "gestion de credito", "credito hipotecario", "evaluacion", "pre-aprobado"
}

# Abreviaciones peligrosas que necesitan límites de palabra (\b) para evitar falsos positivos
_BROKER_ABREVIATIONS = {"sa", "spa", "kw", "c21", "id", "p&g", "pgr", "m2", "sii", "esa", "val"}

# --- OPTIMIZACIÓN DE COSTOS ---
# Caché en memoria para no clasificar a la misma corredora múltiples veces
SELLER_CACHE = {} # { "nombre_normalizado": { "es_broker": bool, "timestamp": float } }

def broker_score(seller_name: str, description: str, company_name: str = "N/A", seller_type: str = "N/A") -> float:
    """Calcula un score (0.0 a 1.0) para determinar si es corredor basándose en nombre, empresa, descripción y tipo."""
    score = 0.0
    s_name = normalize_text(seller_name).lower()
    c_name = normalize_text(company_name).lower()
    full_text = normalize_text(f"{seller_name} {company_name} {description}").lower()
    formatted_desc = normalize_text(description).lower()
    
    for kw in _BROKER_KEYWORDS:
        if len(kw) <= 5 or kw in _BROKER_ABREVIATIONS:
            if re.search(rf'\b{re.escape(kw)}\b', f"{s_name} {c_name}"):
                score += 0.8
        else:
            if kw in s_name or kw in c_name:
                score += 0.8
                
    if re.search(r'\by\s+cia\b|\bltda\b|\bs\.a\b|\bspa\b|\beirl\b|real estate|properties', s_name + " " + c_name):
        score += 0.7
        
    commercial_terms = [
        "comision", "honorarios", "corretaje", "orden de visita", 
        "corredor de propiedades", "gestion de arriendo", "exclusividad",
        "gastos comunes aprox", "metraje aproximado", "agendar visita", "plusvalia",
        "sin pie", "compra sin pie", "subsidio", "gestionamos", "hipotecario", "financiamiento",
        "inversión", "inversionista", "rentabilidad", "aprobacion", "excelente oportunidad", "agenda tu visita"
    ]
    for term in commercial_terms:
        if term in formatted_desc:
            score += 0.35
            
    s_type = normalize_text(seller_type).lower()
    if s_type == "agente" or "inmobiliar" in s_type or "corredor" in s_type:
        score += 0.5
    elif s_type == "propietario" or s_type == "dueño":
        score -= 0.5
        
    generic_names = ["agente", "vendedor"]
    if any(gn == s_name for gn in generic_names):
        context_clues = ["inmobiliario", "inmobiliaria", "propiedades", "oficina", "visita", "suf"]
        for cc in context_clues:
            if cc in formatted_desc:
                score += 0.4
                
    return max(0.0, min(1.0, score))

def is_likely_broker(seller_name: str, description: str, company_name: str = "N/A") -> bool:
    """Detecta si un vendedor es realmente un corredor basándose en nombre y descripción."""
    # 1. Normalizar textos
    s_name = normalize_text(seller_name).lower()
    c_name = normalize_text(company_name).lower()
    full_text = normalize_text(f"{seller_name} {company_name} {description}").lower()
    
    # 2. Verificar palabras clave (Usando Regex \b para TODAS las palabras cortas o sospechosas)
    for kw in _BROKER_KEYWORDS:
        # Si la palabra es corta o propensa a falsos positivos (como apellidos que son partes de otras palabras)
        if len(kw) <= 5 or kw in _BROKER_ABREVIATIONS:
            if re.search(rf'\b{re.escape(kw)}\b', f"{s_name} {c_name}"):
                return True
        else:
            if kw in s_name or kw in c_name:
                return True
            
    # 3. Patrones semánticos de empresas
    if re.search(r'\by\s+cia\b|\bltda\b|\bs\.a\b|\bspa\b|\beirl\b', s_name):
        return True
        
    # 4. Análisis de la descripción completa (Términos críticos - con límites de palabra para evitar errores)
    broker_terms = [
        "comision", "honorarios", "corretaje", "orden de visita", 
        "corredor de propiedades", "gestion de arriendo", "exclusividad",
        "gastos comunes aprox", "metraje aproximado", "agendar visita", "plusvalia",
        "sin pie", "compra sin pie", "subsidio", "gestionamos", "hipotecario", "financiamiento"
    ]
    formatted_desc = normalize_text(description).lower()
    for term in broker_terms:
        if term in formatted_desc:
            return True
            
    # 5. Handle Yapo's default anonymous name "Agente" / "Vendedor"
    # Only flag as broker if the description has some minimal professional context
    generic_names = ["agente", "vendedor"]
    if any(gn == s_name for gn in generic_names):
        # We need more proof from the description
        context_clues = ["inmobiliario", "inmobiliaria", "propiedades", "oficina", "visita", "suf"]
        for cc in context_clues:
            if cc in formatted_desc:
                return True
                
    return False

def save_html_locally(html_content: str, url: str) -> str:
    """Guarda el HTML en una carpeta local y retorna la ruta relativa."""
    if not html_content:
        return None
        
    folder = os.path.join(os.path.dirname(__file__), "html_dumps")
    if not os.path.exists(folder):
        os.makedirs(folder)
        
    # Generar un nombre de archivo único basado en la URL
    filename = hashlib.md5(url.encode()).hexdigest() + ".html"
    filepath = os.path.join(folder, filename)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    return f"html_dumps/{filename}"


def _safe_int_field(val: str, field_name: str, min_v: int = 0, max_v: int = 50) -> str:
    """Valida que val sea un entero razonable para el campo dado. Si falla → 'N/A'.
    Evita contaminar dormitorios con precios, m2, etc."""
    if not val or val == "N/A":
        return "N/A"
    nums = re.findall(r'\d+', val.replace('.', '').replace(',', ''))
    if not nums:
        return "N/A"
    n = int(nums[0])
    if n < min_v or n > max_v:
        logging.debug(f"_safe_int_field: '{field_name}' valor {n} fuera de rango [{min_v},{max_v}] — descartado")
        return "N/A"
    return str(n)


def _safe_m2_field(val: str, field_name: str) -> str:
    """Valida que val sea un m² razonable (1–9999). Si falla → 'N/A'."""
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
        logging.debug(f"_safe_m2_field: '{field_name}' valor {n} fuera de rango — descartado")
        return "N/A"
    return str(n)  # Retornamos solo el número limpio como string


def _safe_coords(lat_s: str, lon_s: str) -> tuple:
    """Valida que lat/lon correspondan a Chile continental/Patagonia.
    Chile: lat ∈ [-55.9, -17.5], lon ∈ [-75.7, -66.0]
    Si las coords están fuera → retorna ('N/A', 'N/A')."""
    try:
        lat = float(lat_s)
        lon = float(lon_s)
        if -55.9 <= lat <= -17.5 and -75.7 <= lon <= -66.0:
            return str(lat), str(lon)
        logging.debug(f"_safe_coords: coords ({lat}, {lon}) fuera de Chile — descartadas")
    except (ValueError, TypeError):
        pass
    return "N/A", "N/A"


def _normalize_key(s: str) -> str:
    """Normaliza una etiqueta de HTML: minusculas, sin acentos (NFKD) y sin simbolos extra."""
    if not s: return ""
    s = s.lower().strip()
    # Eliminar acentos y diacríticos
    s = "".join(c for c in normalize('NFKD', s) if not re.match(r'[\u0300-\u036f]', c))
    # Limpiar simbolos m2 -> m2
    s = s.replace("²", "2").replace("m2", "m2")
    # Eliminar parentesis y su contenido: "Área construida (m²)" -> "area construida"
    s = re.sub(r'\(.*?\)', '', s).strip()
    return s

def _lookup(d: dict, keys_set: set, default: str = "N/A") -> str:
    """Busca en el dict `d` usando etiquetas ya normalizadas."""
    for k, v in d.items():
        if _normalize_key(k) in keys_set:
            return v
    return default


def _parse_yapo_date(date_str: str) -> str:
    """Convierte fechas de Yapo (Hoy, Ayer, 27 feb) a formato ISO o string leíble."""
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
                # Si estamos en Enero y el aviso es de Diciembre, es del año pasado
                if month > now.month:
                    year -= 1
                return f"{year}-{month:02d}-{day:02d}"
    except:
        pass
    return date_str


_TIPO_PROPIEDAD_URL_MAP = {
    "apartamentos": "Departamento",
    "departamento": "Departamento",
    "casas": "Casa",
    "casa": "Casa",
    "oficinas": "Oficina",
    "oficina": "Oficina",
    "locales-comerciales": "Local Comercial",
    "local-comercial": "Local Comercial",
    "parcelas": "Parcela",
    "parcela": "Parcela",
    "terrenos": "Terreno",
    "terreno": "Terreno",
    "estacionamientos": "Estacionamiento",
    "estacionamiento": "Estacionamiento",
    "bodegas": "Bodega",
    "bodega": "Bodega",
    "propiedades-industrial": "Propiedad Industrial",
    "penthouse": "Penthouse",
    "loft": "Loft",
}

def _extract_tipo_propiedad_from_url(url: str) -> str:
    if not url:
        return "N/A"
    url_lower = url.lower()
    for keyword, tp in _TIPO_PROPIEDAD_URL_MAP.items():
        if keyword in url_lower:
            return tp
    return "N/A"

def _parse_html_fast(html: str, url: str = "") -> dict | None:
    """Extrae datos de un HTML de detalle yapo.cl usando JSON-LD + regex (sin JS).
    Versión INDUSTRIAL: Incluye 'The Observer' (Source Tracking) y 'Quality Scoring'."""

    if not html or "</body>" not in html.lower():
        logging.warning("⚠️ _parse_html_fast: HTML truncado o inválido (sin </body>). Ignorando.")
        return None

    def _clean(s: str) -> str:
        if not s:
            return "N/A"
        s = re.sub(r'<[^>]+>', '', s).strip()
        s = re.sub(r'\s+', ' ', s)
        return s if s and "Pregunta al anunciante" not in s else "N/A"

    # ── THE OBSERVER: Registro de fuentes y calidad ──────────────────────────
    sources = {}  # {field: source_name}
    
    # Inicialización de campos
    title = price_str = region = comuna = direccion = "N/A"
    seller_name = descripcion_corta = og_title = "N/A"
    dormitorios_str = banos_str = estacionamientos_str = "N/A"
    m2_total_str = m2_util_str = gastos_comunes_str = "N/A"
    piscina_str = seller_type = fecha_pub = tipo_propiedad = "N/A"
    lat = lon = "N/A"
    images = []

    # ── 1. Data Layers (Frecuentes en Browser / Loopa) ───────────────────────
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

    # ── 2. JSON-LD: precio, seller, título, dirección ────────────────────────
    jsonld_data = {}
    for jm in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            # Limpiar caracteres de control (< 0x20 excepto tab) que Yapo mete en strings
            jm_clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\n\r]', '', jm)
            jd = json.loads(jm_clean)
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
            try:
                uf_val = float(price_raw)
            except:
                uf_val = 0
            price_str = f"UF {price_raw}"
            sources["price"] = "json-ld-uf"
            # Sanity check: UF < 1 es sospechoso (probablemente Yapo tiene mal el dato)
            if uf_val > 0 and uf_val < 1.0:
                sources["price"] = "json-ld-uf-suspect"
                # No marcamos como inválido, dejamos que los fallbacks HTML intenten
                # encontrar un mejor precio (og:title, HTML, etc.)
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

    # ── 2b. Fallbacks de Meta Tags (Open Graph) ──────────────────────────────
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

    # Fallback agresivo: Buscar la descripción completa en el cuerpo del HTML (evita truncado JSON-LD)
    # Buscamos múltiples contenedores posibles (Yapo cambia las clases según el tipo de aviso)
    desc_containers = [
        r'class="[^"]*product-comments[^"]*"[^>]*>(.*?)</(?:div|p|span)>',
        r'class="[^"]*d3-property-info__description-text[^"]*"[^>]*>(.*?)</(?:p|div)>',
        r'class="[^"]*d3-property-about__text[^"]*"[^>]*>(.*?)</(?:p|div|span)>',
        r'id="[^"]*description[^"]*"[^>]*>(.*?)</(?:p|div|span)>',
        r'class="[^"]*d3-property-description[^"]*"[^>]*>(.*?)</(?:div|p|span)>',
        r'itemprop="description"[^>]*>(.*?)</(?:div|p|span)>',
        r'class="[^"]*property-description[^"]*"[^>]*>(.*?)</(?:div|p|span)>',
    ]
    for pattern in desc_containers:
        full_desc_match = re.search(pattern, html, re.DOTALL)
        if full_desc_match:
            full_text = _clean(full_desc_match.group(1))
            if len(full_text) > len(descripcion_corta) or descripcion_corta == "N/A":
                descripcion_corta = full_text
                sources["descripcion"] = f"html-body-full-{pattern[:15]}"
                break

    # ── 2c. Fallbacks de HTML Puro (H1, selectores CSS) ──────────────────────
    if title == "N/A":
        h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        if h1_match:
            title = _clean(h1_match.group(1))
            sources["title"] = "h1-html"
    
    need_price_fallback = (price_str == "N/A" or sources.get("price") == "json-ld-uf-suspect")
    if need_price_fallback:
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

    # ── 2d. Breadcrumbs: región, comuna, tipo propiedad ──────────────────────
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
    if tipo_propiedad == "N/A" and url:
        u_tp = _extract_tipo_propiedad_from_url(url)
        if u_tp != "N/A":
            tipo_propiedad = u_tp
            sources["tipo_propiedad"] = "url"

    # ── 3. d3-property-insight: Dormitorios, Baños, m² ───────────────────────
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

    # 3b. NUEVO LAYOUT: product-icons (Icons metrics) ─────────────────────
    # <div class="product-icons-icon"> ... 1 baños</div>
    for icon_html in re.findall(r'<div[^>]*class="[^"]*product-icons-icon[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL):
        # Limpiar tags para que el SVG no interfiera con los números
        cleaned_icon = _clean(icon_html)
        normalized_icon = _normalize_key(cleaned_icon)
        
        # Dormitorios
        if any(kw in normalized_icon for kw in ["dorm", "habitacion"]):
            val = _safe_int_field(cleaned_icon, "dorm_icon")
            if val != "N/A" and dormitorios_str == "N/A":
                dormitorios_str = val
                sources["dormitorios"] = "product-icons"
        # Baños
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

    # ── 4. Grid de Atributos: d3-property-details / features ────────────────
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

    # ── 5. Location & More Fallbacks ─────────────────────────────────────────
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

    images = re.findall(r'"contentUrl":\s*"([^"]+)"', html)
    if not images:
        images = re.findall(r'class="d3-property-info__images--container"[^>]*>(.*?)</div>', html, re.DOTALL)
        if images:
            images = re.findall(r'(?:src|data-src)="([^"]+)"', images[0])
            if not images:
                images = re.findall(r'(?:src|data-src)="([^"]+)"', html)
                images = [u for u in images if "photos.encuentra24.com" in u and "t_or_fh" in u]
    if images: sources["images"] = "html-images"

    # Seller formatting
    publicador = seller_name if seller_name != "N/A" else "N/A"
    company_name = "N/A"
    
    # --- EXTRACCIÓN DEL VENDEDOR (Área de Formulario/Sidebar) ---
    # Prioridad absoluta: El nombre que aparece arriba del formulario de contacto
    # Esto captura nombres de corredoras como "Houm", "Fuenzalida", etc.
    contact_name_match = re.search(r'class="[^"]*contact_name[^"]*"[^>]*>(.*?)</', html, re.DOTALL)
    if contact_name_match:
        val = _clean(contact_name_match.group(1))
        if val and val != "N/A":
            publicador = val
            sources["seller_name"] = "html-contact-name"
    
    # Nueva búsqueda: Clase específica de compañía en el sidebar o advertiser-name
    company_match = re.search(r'class="[^"]*(?:d3-property-info__company-name|contact_address)[^"]*"[^>]*>(.*?)</', html, re.DOTALL)
    if company_match:
        c_val = _clean(company_match.group(1))
        # Filtrar strings que son direcciones/ubicaciones (empiezan con '- ' o son muy largas)
        if c_val and c_val != "N/A" and not c_val.strip().startswith("-") and len(c_val) < 60:
            company_name = c_val
            # Si el 'publicador' es genérico, usamos el nombre de la empresa
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

    # Buscar el nombre de la empresa en el alt del avatar (Nuevo layout)
    avatar_match = re.search(r'class="[^"]*advertiser-avatar[^"]*"[^>]*>\s*<img[^>]+alt="([^"]+)"', html, re.IGNORECASE)
    if avatar_match:
        c_val = _clean(avatar_match.group(1))
        if c_val and c_val != "N/A" and "user-avatar" not in c_val.lower():
            company_name = c_val
            if publicador == "N/A" or "agente" in publicador.lower():
                publicador = c_val
                sources["seller_name"] = "html-avatar-alt"

    # Si publicador tiene el mismo formato que la compañía (Ej: "Skyline Propiedades")
    if is_likely_broker(publicador, descripcion_corta, company_name):
        if company_name == "N/A": 
            company_name = publicador
        # Si es corredor, su nombre es el de su empresa o "Agente de X"
        sources["seller_name"] = "seller-formatting-broker"

    if publicador.startswith("Agente "):
        company_name = publicador[7:].strip() or company_name
        # Mantenemos el publicador para que el usuario vea quién es, pero sigue siendo broker
        sources["seller_name"] = "seller-formatting-agent"
    elif publicador.startswith("Propietario "):
        publicador = publicador[12:].strip() or "N/A"
        sources["seller_name"] = "seller-formatting-owner"

    if publicador == "N/A":
        if data_layer.get("seller_name"):
            publicador = data_layer["seller_name"]
            sources["seller_name"] = "data-layer"

    # ── 6. QUALITY SCORING ───────────────────────────────────────────────────
    critical_fields = [(price_str!="N/A", 0.3), (m2_total_str!="N/A", 0.25), (dormitorios_str!="N/A", 0.15), (banos_str!="N/A", 0.15), (lat!="N/A", 0.15)]
    quality_score = sum(w for p, w in critical_fields if p)

    if title == "N/A" and price_str == "N/A" and dormitorios_str == "N/A":
        if "Página no encontrada" in html or "Oops!" in html: return None
        if quality_score < 0.15 and lat == "N/A": return None

    has_pro_badge = bool(re.search(r'(_pro\.png|pro[-_]badge|icon[-_]pro)', html, re.IGNORECASE))

    return {
        "source": "fast_path",
        "_metadata": {"sources": sources, "quality_score": round(quality_score, 2), "has_pro_badge": has_pro_badge},
        "region": region, "comuna": comuna, "sector": "N/A", "lat": lat, "lon": lon,
        "m2_total_str": m2_total_str, "m2_util_str": m2_util_str, "gastos_comunes_str": gastos_comunes_str,
        "dormitorios_str": dormitorios_str, "banos_str": banos_str, "estacionamientos_str": estacionamientos_str,
        "title": title, "price": price_str, "publicador": publicador, "raw_desc": descripcion_corta,
        "tipo_propiedad": tipo_propiedad, "list_time": fecha_pub, "seller_id": "N/A",
        "seller_type": seller_type, "company_name": company_name, "images_url": images,
        "piscina_str": piscina_str, "direccion": direccion
    }


def _get_ai_context(html: str) -> str:
    """Extrae una versión estratégica del HTML para el Auditor AI.
    Mantiene scripts de metadatos (JSON-LD, __YAPO__) y bloques de datos iniciales."""
    # 1. Preservar JSON-LD
    json_ld = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
    
    # 2. Preservar bloques de datos de Yapo/Loopa y Estado Inicial
    pattern = r'(document\.__YAPO__\.adview_event_base\s*=\s*\{.*?\};|var\s+loopaData\s*=\s*\{.*?\};|window\.__INITIAL_STATE__\s*=\s*\{.*?\};)'
    metadata_scripts = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
    
    # 3. Preservar URLs de Mapas (donde Yapo tiene lat/lon en q=LAT,LON)
    map_urls = re.findall(r'src="([^"]*map[^"]*q=-?\d+\.\d+,-?\d+\.\d+[^"]*)"', html, re.IGNORECASE)
    
    # 4. Limpiar el resto del HTML (Quitamos ruidos pesados pero preservamos iframes de mapa)
    clean_html = re.sub(r'<(style|nav|footer|header|svg|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL|re.IGNORECASE)
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
    """Realiza una auditoría quirúrgica multi-campo usando Grok sobre el contexto estratégico."""
    fields_desc = ", ".join([f"'{f}'" for f in missing_fields])
    prompt = f"""AUDITOR ESTRATÉGICO INMOBILIARIO.
Tu misión es encontrar EXACTAMENTE estos datos faltantes: {fields_desc}.
Busca en el texto de la descripción y en los bloques de METADATA/JSON-LD proporcionados.

ESTRATEGIA DE EXTRACCIÓN:
1. 'lat' y 'lon': Búscalas en objetos JSON o URLs de mapas (ej: q=-33.4,-70.5). Retorna NUMEROS (ej: -33.488).
2. 'm2_total': Sé muy agresivo. Busca "metros cuadrados", "m2", "mts2", "superficie", "sup.", "útil", "terreno".
   Incluso si está en palabras (ej: "setenta y cinco"), conviértelo a número (75).
3. 'gastos_comunes': Busca valores monetarios asociados a "mantenimiento", "GGCC", "gastos".
4. Si un dato no está presente en ninguna parte, retorna null para ese campo. No inventes.

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
        
        # Detección de bloqueo en el fast path (403, 429, o Reto de navegador)
        challenge_keywords = ["captcha", "access denied", "checking your browser", "please wait", "ray id"]
        if resp.status_code in [403, 429] or any(k in resp.text.lower() for k in challenge_keywords):
            raise ProxyBlockedError(f"Fast path bloqueado ({resp.status_code}/Challenge)")

        if resp.status_code != 200:
            return None, size_bytes

        # Forzar decodificación UTF-8 para evitar problemas de acentos (baños -> banos)
        html = resp.content.decode('utf-8', 'ignore')
        res = _parse_html_fast(html)
        if res:
            # OPTIMIZACIÓN: Guardamos HTML localmente, no en MongoDB
            html_path = save_html_locally(html, url)
            res["html_path"] = html_path
            res["html_dump"] = html # Lo mantenemos temporalmente para la IA en este thread
            return res, size_bytes
        return None, size_bytes
    except ProxyBlockedError as e:
        raise e
    except Exception as e:
        logging.debug(f"Fast path falló: {e}")
    return None, 0

async def block_resources(page, mode="standard"):
    async def handler(route):
        rt = route.request.resource_type
        if mode == "ultra":
            # Para __NEXT_DATA__, normalmt solo necesitamos el "document" inicial.
            # Bloqueamos scripts también para máximo ahorro. Si falla, volveremos a modo standard.
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

# ====================== EXTRACCIÓN (Solo Browser - Libera Proxy Rápido) ======================
async def extract_raw_data(page, url: str) -> dict:
    """Extrae datos crudos usando el browser Playwright.
    Navega a la URL, espera a que cargue el contenido principal y luego llama
    a _parse_html_fast() sobre el HTML renderizado — el mismo parser que el
    fast path httpx. De esta manera ambos paths comparten exactamente la misma
    lógica de extracción y validación.
    NO llama a la IA. El proxy se libera al cerrar la página después de esto."""

    # ── Navegación ────────────────────────────────────────────────────────────
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
        
        # Detección de bloqueo REAL (no falsos positivos por script reCAPTCHA preventivo)
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

    # Yapo carga el detalle con JS; esperamos cualquiera de estas señales DE GOLPE.
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

    # Pequeña pausa para que los insights terminen de renderizar
    await asyncio.sleep(0.5)

    # ── Obtener HTML renderizado y parsear con el mismo motor que httpx ──────
    html = await page.content()
    result = _parse_html_fast(html)

    if result:
        # OPTIMIZACIÓN: Guardamos HTML localmente, no en MongoDB
        html_path = save_html_locally(html, url)
        result["html_path"] = html_path
        result["html_dump"] = html # Mantenemos para IA
        # Log ultra-compacto
        logging.debug(f"🌐 Data: d={result['dormitorios_str']} b={result['banos_str']} m2={result['m2_total_str']}")
        return result

    # ── Fallback mínimo si el HTML del browser tampoco sirvió ───────────────
    # (Caso extremo: página muy dinámica o bloqueada parcialmente)
    logging.warning(f"⚠️ Browser: _parse_html_fast no extrajo datos de {url[-40:]}")
    return {
        "source": "browser_fallback_empty",
        "html_dump": html if len(html) > 10 else None, # Si es casi vacío, que la IA ni lo intente
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
    }


def _extract_operation_from_url(url: str) -> str:
    """Extrae tipo de operación (Venta/Arriendo) desde la URL de yapo.cl."""
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

    content_hash = generate_content_hash(title, raw_desc)

    # deduplicación por hash
    duplicate = await coll.find_one({"details.content_hash": content_hash})
    if duplicate:
        return {"is_duplicate": True, "used_ai": False}

    # ── 1. Optimización: Filtros Locales y Caché (Pre-IA) ────────────────────
    # Si ya tenemos precio, dormitorios y baños de forma nativa, podemos saltar Grok
    # para ahorrar tokens y tiempo, generando un resumen programático.
    has_critical = (
        raw_data["price"] != "N/A" and 
        raw_data["dormitorios_str"] != "N/A" and 
        raw_data["banos_str"] != "N/A"
    )
    
    # Check Caché primero
    s_key = normalize_text(f"{publicador} {company_name}").lower()
    if s_key not in SELLER_CACHE:
        SELLER_CACHE[s_key] = {"is_broker": False, "count": 0, "ts": datetime.now().timestamp()}
    
    SELLER_CACHE[s_key]["count"] += 1

    has_pro_badge = raw_data.get("_metadata", {}).get("has_pro_badge", False)

    # Check Blacklist Local (Sin IA)
    seller_type = str(raw_data.get("seller_type", ""))
    score = broker_score(publicador, raw_desc, company_name, seller_type)
    
    if has_pro_badge:
        score = 1.0
        SELLER_CACHE[s_key]["is_broker"] = True
        
    if SELLER_CACHE[s_key]["count"] > 3:
        SELLER_CACHE[s_key]["is_broker"] = True
        score = max(score, 0.8) # Forzar broker

    # If >= 0.75 or already known broker -> ALWAYS SKIP listing (don't hit AI)
    if score >= 0.75 or SELLER_CACHE[s_key]["is_broker"]:
        SELLER_CACHE[s_key]["is_broker"] = True
        logging.info(f"🚫 [SKIP] Broker/PRO Dominante (Score: {score:.2f}, Veces: {SELLER_CACHE[s_key]['count']}): {publicador[:20]}")
        precio_clp = clean_num(price)
        precio_uf = round(precio_clp / uf_value, 2) if (precio_clp and uf_value) else None
        return {
            "is_duplicate": False, 
            "es_propietario_directo": False, 
            "confianza": 1.0,
            "publicador": publicador,
            "company_name": company_name,
            "tipo_propiedad": raw_data.get("tipo_propiedad", "N/A"), "comuna": comuna, "region": region,
            "precio_clp": precio_clp, "precio_uf": precio_uf,
            "m2_total": clean_num(raw_data.get("m2_total_str")),
            "dormitorios": clean_num(raw_data.get("dormitorios_str")), "banos": clean_num(raw_data.get("banos_str")),
            "resumen_limpio": f"Broker detectado (Score: {score:.2f}, Veces: {SELLER_CACHE[s_key]['count']}): {publicador}",
            "content_hash": content_hash,
            "used_ai": False
        }

    is_low_priority = (0.5 <= score < 0.75)
    is_owner_local = (score < 0.5)
    is_confident_classification = is_owner_local

    if has_critical and is_confident_classification:
        logging.info(f"⚡ Saltando IA: Datos completos y clasificación firme (Score: {score:.2f}) para {title[:30]}...")
        precio_clp = clean_num(price)
        precio_uf = round(precio_clp / uf_value, 2) if (precio_clp and uf_value) else None
        
        is_direct = is_owner_local
        confianza_prop = 1.0 if is_broker_local else 0.95
        
        return {
            "is_duplicate": False,
            "es_propietario_directo": is_direct,
            "confianza": confianza_prop,
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
            "resumen_limpio": f"Propiedad en {comuna}: {raw_data['tipo_propiedad']} con {raw_data['dormitorios_str']}D/{raw_data['banos_str']}B.",
            "content_hash": content_hash,
            "used_ai": False
        }

    # ── 2. Llamada a IA (Fallback) ───────────────────────────────────────────
    # Si faltan datos críticos, Grok los extrae de la descripción.
    prompt = f"""Extrae datos de este aviso de Yapo.cl. Responde SOLO JSON puro:
{{
  "es_propietario_directo": true/false,
  "confianza": 0.XX,
  "tipo_propiedad": "Departamento/Casa/Oficina/Local Comercial/Bodega/Estacionamiento",
  "comuna": "Nombre Comuna",
  "region": "Nombre Región",
  "sector": "Barrio o sector específico o null",
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

IMPORTANTE: Si el vendedor es una empresa (Re/Max, Century 21, Propiedades, etc.) o el nombre es generico como "Agente" o "Vendedor", o si la descripcion menciona "comision", "honorarios" o servicios como "compra sin pie", "gestion de subsidio", "financiamiento", "es_propietario_directo" DEBE ser false.
Solo es true si tienes la certeza absoluta de que el vendedor es una persona natural particular que vende su propia casa. Si el aviso parece un producto de inversión o un servicio de ventas masivas, marca false.
    T: {title} | P: {publicador} | Precio: {price}
    D: {raw_desc[:1800]} (Resumen descripción)"""

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


    # Consolidación de datos: Prioridad IA -> Scraper -> Cálculos
    def get_val(key, fallback_val=None):
        v = extracted.get(key)
        if v is not None and v != "N/A" and v != "": return v
        return raw_data.get(key, fallback_val)

    # Lógica de precios: Si Grok extrajo precio limpio, lo usamos. Si no, parseamos el original.
    ai_clp = clean_num(extracted.get("precio_clp"))
    ai_uf = clean_float(extracted.get("precio_uf"))
    
    if ai_clp is None and ai_uf is None:
        p_uf, p_clp = parse_price_components(price) # Nuestra lógica manual robusta
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

    # Normalización de tipo_propiedad
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

    # ── 4. STRATEGIC AI AUDITOR (Multi-field Surgical Pass) ─────────────────
    # Si faltan datos críticos después de la extracción inicial e IA, usamos el Auditor.
    meta = raw_data.get("_metadata", {})
    m2_tot_final = m2_tot
    lat_final = raw_data.get("lat")
    lon_final = raw_data.get("lon")
    gastos_final = clean_num(raw_data.get("gastos_comunes_str"))

    # Identificar qué falta
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
                logging.info(f"✨ [AUDITOR] Recuperado: {', '.join(recovered)}")

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
        "es_propietario_directo": False if (
            str(raw_data.get("seller_type", "")).lower() == "agente" or 
            is_likely_broker(publicador, raw_desc, raw_data.get("company_name", "N/A")) or
            is_likely_broker(publicador, raw_desc, extracted.get("nombre_corredora", "N/A"))
        ) else extracted.get("es_propietario_directo", False),
        "confianza_propietario": 1.0 if (
            str(raw_data.get("seller_type", "")).lower() == "agente" or 
            is_likely_broker(publicador, raw_desc, raw_data.get("company_name", "N/A"))
        ) else extracted.get("confianza", 0.5),
        "dias_en_portal": dias_en_portal,
        "fecha_publicacion": raw_data.get("list_time"),
        "vendedor_id": raw_data.get("seller_id"),
        "tipo_vendedor": raw_data.get("seller_type"),
        "nombre_ejecutivo": raw_data.get("publicador"),
        "nombre_corredora": raw_data.get("company_name"),
        "enlaces_fotos": raw_data.get("images_url", []),
        "content_hash": "N/A", # Will be updated in pipeline
        "fecha_scraping": datetime.now(timezone.utc).isoformat(),
        "fecha_ultima_vista": datetime.now(timezone.utc).isoformat(),
        "metadata": meta,
        "used_ai": True
    }

async def extract_links_with_scroll(page, max_scrolls: int, delay_s: float) -> set:
    links = set()
    
    def _collect_hrefs(hrefs):
        for href in hrefs:
            if re.search(r'[/_]\d{7,11}(?:\?|$)', href):
                if any(x in href for x in ["yapo.cl/comprar", "yapo.cl/vender", "ayuda.yapo.cl"]):
                    continue
                links.add(normalize_url(href))
    
    # Capturar links ANTES del primer scroll
    hrefs = await page.evaluate('''() => {
        return Array.from(document.querySelectorAll('a[href]')).map(a => a.href);
    }''')
    _collect_hrefs(hrefs)
    
    for _ in range(max_scrolls):
        await page.mouse.wheel(0, 3000)
        await asyncio.sleep(delay_s + random.uniform(0.5, 1.0))
        hrefs = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('a[href]')).map(a => a.href);
        }''')
        _collect_hrefs(hrefs)
    return links

async def main():
    parser = argparse.ArgumentParser(description="Yapo Scraper INDUSTRIAL v5 - Persistent Queue + Stealth")
    parser.add_argument("--use-proxies", action="store_true")
    parser.add_argument("--concurrency", type=int, default=CONFIG["max_concurrency"])
    parser.add_argument("--max-pages", type=int, default=CONFIG["max_pages"])
    parser.add_argument("--force-discovery", action="store_true", help="Forzar búsqueda de links")
    args = parser.parse_args()

    uf_value = await get_uf_value()
    logging.info(f"💱 UF actual: ${uf_value:,.0f} CLP")

    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]
    queue_coll = db["yapo_queue"]

    # Índices críticos
    await coll.create_index("url", unique=True)
    await coll.create_index("details.content_hash")
    await queue_coll.create_index("url", unique=True)
    await queue_coll.create_index([("status", 1), ("priority", -1), ("retries", 1), ("fecha_descubrimiento", -1)])
    await queue_coll.create_index("priority")
    await queue_coll.create_index("status")

    # === MIGRACIÓN DE COLA (HEALING) ===
    # Si hay registros antiguos sin el campo 'priority', los inicializamos ahora
    missing_prio = await queue_coll.count_documents({"priority": {"$exists": False}})
    if missing_prio > 0:
        logging.info(f"🔧 [MIGRATION] Inicializando prioridad para {missing_prio} items antiguos...")
        # Procesar en bloques para no saturar
        cursor = queue_coll.find({"priority": {"$exists": False}}).limit(5000)
        async for doc in cursor:
            f_desc = doc.get("fecha_descubrimiento", datetime.now(timezone.utc))
            if isinstance(f_desc, str):
                try: f_desc = datetime.fromisoformat(f_desc.replace('Z', '+00:00'))
                except: f_desc = datetime.now(timezone.utc)
            
            prio = calculate_priority(f_desc, is_new=False)
            await queue_coll.update_one({"_id": doc["_id"]}, {"$set": {"priority": prio}})
        logging.info("✅ [MIGRATION] Priorización completada.")

    # 1. DESCUBRIMIENTO (Iteración sobre lista de búsqueda)
    links_pending = await queue_coll.count_documents({"status": "pending"})
    
    # Decidir si ejecutar descubrimiento
    should_discover = CONFIG.get("discovery_force_on", True) or links_pending == 0 or args.force_discovery
    
    if should_discover:
        logging.info(f"🔍 ETAPA 1: Descubrimiento de enlaces activos...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ctx = await browser.new_context(user_agent=ua)
            page = await ctx.new_page()
            try: await stealth(page)
            except: pass
            
            await block_resources(page, mode="discovery")
            
            # Obtener lista de URLs a procesar
            search_targets = CONFIG.get("search_urls", [])
            if not search_targets and CONFIG.get("base_url"):
                search_targets = [CONFIG["base_url"]]
            
            for base_url in search_targets:
                logging.info(f"📍 Iniciando descubrimiento en: {base_url[:60]}...")
                page_num = 0
                empty_streak = 0
                MAX_EMPTY_PAGES = CONFIG.get("max_empty_pages_streak", 3)
                
                while page_num < args.max_pages:
                    page_num += 1
                    if page_num == 1:
                        p_url = base_url
                    else:
                        parsed = urlparse(base_url)
                        new_path = f"{parsed.path}.{page_num}"
                        p_url = urlunparse((parsed.scheme, parsed.netloc, new_path, "", parsed.query, ""))

                    try:
                        logging.info(f"📊 Navegando pág {page_num} de zona actual...")
                        await page.goto(p_url, timeout=40000, wait_until="domcontentloaded")
                        
                        try: await page.wait_for_load_state('networkidle', timeout=6000)
                        except: pass
                        
                        if page_num == 1:
                            # Cerrar consentimientos
                            for sel in ['button:has-text("Aceptar")', 'button:has-text("Acepto")', '[id*="cookie"] button']:
                                try:
                                    btn = await page.wait_for_selector(sel, timeout=1500)
                                    if btn: await btn.click(); await asyncio.sleep(1); break
                                except: continue
                        
                        try: await page.wait_for_selector('a[href*="/3"], a[href*="/2"]', timeout=5000)
                        except: pass
                        
                        links = await extract_links_with_scroll(page, CONFIG["max_scrolls"], CONFIG["scroll_delay"])
                        
                        if len(links) == 0:
                            logging.info(f"ℹ️ Fin de zona alcanzado (pág {page_num})")
                            break
                        
                        new_inserted = 0
                        for link in links:
                            # PART 16 — DUPLICATE HARD FILTER (48h)
                            processed_recently = await coll.find_one({
                                "url": link,
                                "fecha_scraping": {"$gte": (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()}
                            })
                            if processed_recently:
                                logging.debug(f"⏭️ [SKIPPED DUPLICATE] Procesado recientemente: {link[-25:]}")
                                continue

                            init_priority = calculate_priority(datetime.now(timezone.utc), is_new=True)
                            result = await queue_coll.update_one(
                                {"url": link},
                                {"$setOnInsert": {
                                    "url": link, 
                                    "status": "pending", 
                                    "retries": 0, 
                                    "priority": init_priority,
                                    "fecha_descubrimiento": datetime.now(timezone.utc)
                                }},
                                upsert=True
                            )
                            if result.upserted_id: new_inserted += 1
                        
                        logging.info(f"✅ Pág {page_num} | {len(links)} links | +{new_inserted} nuevos")
                        
                        if new_inserted == 0:
                            empty_streak += 1
                            if empty_streak >= MAX_EMPTY_PAGES:
                                logging.info(f"⏭️ Saltando zona: {MAX_EMPTY_PAGES} páginas sin novedades.")
                                break
                        else:
                            empty_streak = 0
                        
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                    except Exception as e:
                        logging.error(f"⚠️ Error discovery pág {page_num}: {e}")
                        break # Siguiente zona si esta falla
            
            await browser.close()
    else:
        logging.info(f"⏭️ Descubrimiento omitido (Cola activa: {links_pending} links).")

    # 2. EXTRACCIÓN
    proxy_api_url = os.getenv("PROXY_API_URL")
    proxies = await get_proxies_from_api(proxy_api_url) if proxy_api_url else [p.strip() for p in Config.PROXIES.split(",") if p.strip()]
    proxy_cycle = cycle(proxies) if proxies else None

    # 7. DASHBOARD DE CONTROL EN TIEMPO REAL
    stats = {
        "processed": 0, "new": 0, "duplicates": 0,
        "owners": 0, "brokers": 0, "errors": 0, "skipped_ai": 0
    }
    total_pending = await queue_coll.count_documents({"status": "pending"})

    def print_dashboard():
        """Tablero de control industrial."""
        done, total = stats['processed'], total_pending
        pct = (done / total * 100) if total > 0 else 0
        mb = sum(PROXY_MB_USAGE.values())
        logging.info(f"📊 [%d/%d %d%%] | +Nuevos:%d | 🔄Dupes:%d | ❌Fail:%d | 🏠D/🏢C: %d/%d | 💰%.1fMB", done, total, pct, stats['new'], stats['duplicates'], stats['errors'], stats['owners'], stats['brokers'], mb)
        return # Salir para que no ejecute el print de abajo por problemas de codificación
        print(f"\r📊 [{done}/{total_pending} {pct:.0f}%] | +Nuevos:{stats['new']} | 🔄Dupes:{stats['duplicates']} | ❌Err:{stats['errors']} | 🏠D/🏢C: {stats['owners']}/{stats['brokers']} | �{mb:.1f}MB", end="", flush=True)

    logging.info(f"🚀 ETAPA 2: Extracción | Pendientes: {total_pending} | Concurrency: {args.concurrency}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        
        async def worker(worker_id):
            ua = UserAgent()
            is_rotator = len(proxies) == 1
            while True:
                # PART 15 — BACKLOG CONTROL
                # Si hay demasiados pendientes, solo procesamos los top (prioridad alta)
                pending_count = await queue_coll.count_documents({"status": "pending"})
                query = {"status": "pending"}
                
                if pending_count > CONFIG["max_queue_size"]:
                    logging.warning(f"⚠️ [BACKLOG CONTROL] Cola saturada ({pending_count}). Procesando solo los mejores candidatos.")
                    # Dejamos que el SORT elija los mejores, pero limitaremos la sesión del worker.

                # Polling atómico de la cola con prioridad Fresh-First
                doc = await queue_coll.find_one_and_update(
                    query,
                    {"$set": {"status": "processing", "worker": worker_id}},
                    sort=[("priority", -1), ("retries", 1), ("fecha_descubrimiento", -1)],
                    return_document=True
                )
                if not doc: break

                # Limite de seguridad para Backlog (Part 15)
                # Si la cola era gigante, detenemos este worker tras procesar una porción razonable
                # para no quedar atrapados en basura vieja infinitamente.
                processed_this_session = stats.get("session_processed", 0)
                if pending_count > CONFIG["max_queue_size"] and processed_this_session > (500 // args.concurrency):
                    logging.info(f"🛑 [BACKLOG LIMIT] Alcanzado límite de 500 items del top. Ignorando el resto temporalmente.")
                    break
                stats["session_processed"] = processed_this_session + 1

                url = doc["url"]
                
                # Log de prioridad
                prio_val = doc.get("priority", 0)
                if prio_val >= 1.0:
                    logging.info(f"🚀 [HIGH PRIORITY (NEW)] Procesando: {url[-30:]} (Prio: {prio_val})")
                else:
                    logging.info(f"🔄 [NORMAL PRIORITY] Procesando: {url[-30:]} (Prio: {prio_val})")
                
                # Obtener un proxy que no esté en cooldown
                proxy = None
                if proxy_cycle:
                    if is_rotator:
                        # Para rotadores, no bloqueamos globalmente (la IP cambia igual)
                        proxy = next(proxy_cycle)
                    else:
                        # Lógica estándar para lista de proxies estáticos
                        for _ in range(len(proxies)):
                            p = next(proxy_cycle)
                            cooldown_until = BURNED_PROXIES.get(p)
                            if not cooldown_until or datetime.now() > cooldown_until:
                                if PROXY_MB_USAGE[p] < MAX_MB_PER_PROXY:
                                    proxy = p
                                    break
                    
                    if not proxy:
                        logging.error("❌ TODOS LOS PROXIES ESTÁN EN COOLDOWN. Esperando 15s...")
                        await asyncio.sleep(15)
                        await queue_coll.update_one({"url": url}, {"$set": {"status": "pending"}})
                        continue

                ctx_proxy = {"server": proxy} if proxy else None
                if ctx_proxy and Config.PROXY_USER:
                    ctx_proxy["username"], ctx_proxy["password"] = Config.PROXY_USER, Config.PROXY_PASS

                # Reducir ruido: log de sesión a DEBUG
                logging.debug(f"🌐 [W{worker_id}] Sesión batch({CONFIG['urls_per_session']}) | Proxy: {proxy or 'directo'}")
                
                # PART 14 — FRESH-FIRST EXECUTION
                # Verificación de "New" (< 24h) para PAUSE backlog si es necesario
                # (Ya manejado por el QUERY de find_one_and_update arriba)
                
                # Configuración de cliente HTTP ligero persistente
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
                    # Usamos dos clientes: uno directo y uno con proxy
                    async with curl_requests.AsyncSession(headers=h_headers, timeout=15.0, impersonate="chrome120") as h_client_direct:
                     async with curl_requests.AsyncSession(headers=h_headers, proxies={"http": h_proxy, "https": h_proxy} if h_proxy else None, timeout=15.0, impersonate="chrome120") as h_client_proxy:
                        ctx = await browser.new_context(user_agent=ua.random, proxy=ctx_proxy)
                        for batch_i in range(CONFIG["urls_per_session"]):
                            if batch_i > 0:
                                doc = await queue_coll.find_one_and_update(
                                    {"status": "pending"},
                                    {"$set": {"status": "processing", "worker": worker_id}},
                                    sort=[("retries", 1), ("fecha_descubrimiento", -1)],
                                    return_document=True
                                )
                            if not doc: break
                            url = doc["url"]

                            # PART 16 — DUPLICATE HARD FILTER (Re-check en worker)
                            processed_recently = await coll.find_one({
                                "url": url,
                                "fecha_scraping": {"$gte": (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()}
                            })
                            if processed_recently:
                                logging.info(f"⏭️ [SKIPPED DUPLICATE] URL procesada en últimas 48h: {url[-25:]}")
                                await queue_coll.update_one({"url": url}, {"$set": {"status": "completed"}})
                                stats["duplicates"] += 1
                                stats["processed"] += 1
                                continue

                            # === PRE-CHECK: ¿Ya existe en DB? ===
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
                                # START PROXY OPTIMIZATION: Try WITHOUT proxy first
                                p_key = "directo"
                                PROXY_USAGE["directo"] += 1
                                raw_data = None
                                size_bytes = 0
                                
                                try:
                                    raw_data, size_bytes = await extract_fast_path(url, h_client_direct)
                                    if not raw_data:
                                        logging.warning(f"⚠️ Fast Path directo sin datos. Verificando en Browser...")
                                    else:
                                        PROXY_MB_USAGE["directo"] += (size_bytes / (1024 * 1024))
                                        SUCCESSFUL_NO_PROXY.add(url)
                                except ProxyBlockedError:
                                    logging.warning(f"🔥 Bloqueo directo. Cambiando a Proxy para {url}...")
                                    # Fallback to Proxy
                                    if not proxy and proxies:
                                        for prx in proxies:
                                            if (not BURNED_PROXIES.get(prx) or datetime.now() > BURNED_PROXIES[prx]) and PROXY_MB_USAGE[prx] < MAX_MB_PER_PROXY:
                                                proxy = prx
                                                break
                                    
                                    p_key = proxy
                                    if p_key:
                                        PROXY_USAGE[p_key] += 1
                                        try:
                                            # Using the proxy client
                                            raw_data, size_bytes = await extract_fast_path(url, h_client_proxy)
                                            if raw_data:
                                                PROXY_MB_USAGE[p_key] += (size_bytes / (1024 * 1024))
                                        except ProxyBlockedError:
                                            if is_rotator:
                                                wait_s = random.uniform(2, 5)
                                                logging.warning(f"🔥 Bloqueo Proxy -> Backoff {wait_s:.1f}s")
                                                await asyncio.sleep(wait_s)
                                            else:
                                                logging.warning(f"🔥 Proxy en COOLDOWN: {p_key}. Rotando...")
                                                BURNED_PROXIES[p_key] = datetime.now() + timedelta(seconds=60)
                                            await queue_coll.update_one({"url": url}, {"$set": {"status": "pending"}})
                                            raise
                                    else:
                                        logging.warning("No hay proxies disponibles para el fallback.")
                                        await queue_coll.update_one({"url": url}, {"$set": {"status": "pending"}})
                                        raise ProxyBlockedError("Direct failed and no proxies available")
                                
                                mb_url = size_bytes / (1024 * 1024)
                                logging.info(f"🔗 [W{worker_id}] {url} | {p_key[:15]}... | {mb_url:.3f}MB")
                                
                                # Si el Fast Path falla o es incompleto → Fallback
                                # RELAXED: Solo vamos a browser si faltan datos CRÍTICOS (Precio O Dormitorios).
                                # Si falta el título, no importa (la IA lo saca de la descripción).
                                needs_browser = not raw_data or (raw_data.get("price") == "N/A" and raw_data.get("dormitorios") == "N/A")
                                needs_browser = not raw_data or (raw_data.get("price") == "N/A" and raw_data.get("dormitorios") == "N/A")
                                
                                if needs_browser:
                                    used_pct = (stats.get("playwright_used", 0) / max(1, stats["processed"]))
                                    is_high_value = True
                                    if raw_data:
                                        ts_score = broker_score(raw_data.get("publicador", ""), raw_data.get("raw_desc", ""), raw_data.get("company_name", ""), raw_data.get("seller_type", ""))
                                        has_contact = bool(re.search(r'(\+?56\s?9|\b\d{8}\b|@[\w\.-]+)', raw_data.get("raw_desc", "")))
                                        if ts_score >= 0.2 or has_contact or raw_data.get("_metadata", {}).get("has_pro_badge", False):
                                            is_high_value = False

                                    if raw_data and not is_high_value:
                                        logging.info(f"⏭️ Browser Skipped: No es candidato owner premium (Score>=0.2 o tiene contacto).")
                                        needs_browser = False
                                    elif used_pct >= 0.10:
                                        logging.warning(f"📉 Browser limit reached ({used_pct*100:.1f}%), límite excedido. Skip browser preventivo. Continua sin usar Browser.")
                                        needs_browser = False
                                        if not raw_data:
                                            raw_data = {
                                                "source": "dummy_limit_fallback",
                                                "title": "N/A", "price": "N/A", "dormitorios_str": "N/A", "banos_str": "N/A",
                                                "publicador": "N/A", "raw_desc": "N/A", "region": "url_fallback", "comuna": "N/A", "tipo_propiedad": "N/A",
                                                "m2_total_str": "N/A", "m2_util_str": "N/A", "gastos_comunes_str": "N/A",
                                                "estacionamientos_str": "N/A", "piscina_str": "N/A", "lat": "N/A", "lon": "N/A"
                                            }
                                    else:
                                        stats["playwright_used"] = stats.get("playwright_used", 0) + 1

                                if needs_browser:
                                    if raw_data: logging.info(f"⚠️ Datos críticos faltantes ({raw_data.get('price')}/{raw_data.get('dormitorios')}). Browser Fallback...")
                                    
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
                                            logging.warning(f"🔥 Bloqueo (Browser) -> Backoff {wait_s:.1f}s")
                                            await asyncio.sleep(wait_s)
                                        else:
                                            logging.warning(f"🔥 Proxy en COOLDOWN (Browser): {proxy}. Rotando...")
                                            BURNED_PROXIES[proxy] = datetime.now() + timedelta(seconds=60)
                                        
                                        await queue_coll.update_one({"url": url}, {"$set": {"status": "pending"}})
                                        raise e # Relanzamos para salir del loop de batch
                                    except Exception as e:
                                        bw_session_mb = session_bytes[0] / (1024 * 1024)
                                        PROXY_MB_USAGE[p_key] += bw_session_mb
                                        raise e
                                        
                                    if not raw_data:
                                        logging.warning(f"💀 Anuncio verificado borrado en Yapo: {url[-25:]}")
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
                                        logging.info(f"🌐 [W{worker_id}] Browser BW: {bw_session_mb:.2f}MB | Acumulado: {PROXY_MB_USAGE[p_key]:.2f}MB")

                            except ProxyBlockedError:
                                # No contamos el bloqueo como error terminal, es solo un retry
                                break 

                            except Exception as e:
                                stats["errors"] += 1
                                stats["processed"] += 1
                                error_msg = str(e)[:100]
                                logging.error(f"⚠️ [W{worker_id}] Error en {url[-20:]}: {error_msg}")
                                ns = "failed" if doc.get("retries", 0) >= CONFIG["max_retries_per_url"] else "pending"
                                await queue_coll.update_one({"url": url}, {"$set": {"status": ns, "last_error": error_msg}, "$inc": {"retries": 1}})
                                if "Tunnel" in error_msg or "ERR_TUNNEL" in error_msg: break # Salir del batch si el túnel falló
                                continue
                            finally:
                                if page:
                                    try: await page.close()
                                    except: pass
                                    page = None

                            # === FASE 2: PROCESAMIENTO IA ===
                            try:
                                details = await process_with_ai(raw_data, grok_client, uf_value, coll, url)
                                if details:
                                    if details.get("is_duplicate"):
                                        stats["duplicates"] += 1
                                        await queue_coll.update_one({"url": url}, {"$set": {"status": "completed"}})
                                    else:
                                        sm = "⚡" if raw_data.get("source") == "fast_path" else "🌐"
                                        logging.info(f"✨ {sm} Éxito ({raw_data.get('source')})")
                                        await coll.update_one(
                                            {"url": url}, 
                                            {"$set": {"url": url, "details": details, "origen": "yapo.cl", "fecha_captura": datetime.now(timezone.utc)}}, 
                                            upsert=True
                                        )
                                        stats["new"] += 1
                                        if not details.get("used_ai"): stats["skipped_ai"] += 1
                                        if details.get("es_propietario_directo"): stats["owners"] += 1
                                        else: stats["brokers"] += 1
                                        await queue_coll.update_one({"url": url}, {"$set": {"status": "completed"}})
                                else:
                                    raise Exception("IA retornó None")
                            except Exception as e:
                                stats["errors"] += 1
                                error_msg = f"AI: {str(e)[:90]}"
                                logging.error(f"🤖 [W{worker_id}] {url[-20:]}: {error_msg}")
                                await queue_coll.update_one({"url": url}, {"$set": {"status": "pending", "last_error": error_msg}, "$inc": {"retries": 1}})
                            
                            stats["processed"] += 1
                            print_dashboard()
                            await asyncio.sleep(random.uniform(1, 2))
                except Exception as e:
                    logging.error(f"🔄 Rotando proxy o error de sesión: {str(e)[:40]}...")
                    await asyncio.sleep(3)
                finally:
                    if ctx:
                        try: await ctx.close()
                        except: pass
                        ctx = None

        tasks = [worker(i) for i in range(args.concurrency)]
        await asyncio.gather(*tasks)
        await browser.close()

    client.close()
    
    # Resumen final
    logging.info("═" * 60)
    logging.info("🎉 SCRAPING FINALIZADO")
    logging.info(f"📊 Procesados: {stats['processed']} | Nuevos: {stats['new']} | Duplicados: {stats['duplicates']}")
    logging.info(f"🏠 Dueños: {stats['owners']} | 🏢 Corredores: {stats['brokers']} | 🤖 Sin IA: {stats['skipped_ai']}")
    logging.info(f"❌ Errores: {stats['errors']}")
    logging.info("-" * 30)
    logging.info("📈 RESUMEN DE CONSUMO DE PROXIES:")
    total_mb_final = 0
    for p, count in PROXY_USAGE.items():
        mb = PROXY_MB_USAGE.get(p, 0)
        total_mb_final += mb
        logging.info(f"   - {p}: {count} peticiones | {mb:.2f} MB")
    logging.info(f"💰 CONSUMO TOTAL: {total_mb_final:.2f} MB")
    logging.info("═" * 60)

if __name__ == "__main__":
    asyncio.run(main())
