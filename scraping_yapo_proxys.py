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

import httpx
from fake_useragent import UserAgent
from playwright.async_api import async_playwright
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

from config import Config

from playwright_stealth import stealth

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
    "desc_max_chars": 4000,
    "base_url": "https://www.yapo.cl/bienes-raices-alquiler?regionslug=region-metropolitana-nunoa,region-metropolitana-providencia,region-metropolitana-macul&q=withcat.bienes-raices-alquiler-apartamentos,bienes-raices-alquiler-comercios,bienes-raices-alquiler-casas",
    "max_retries_per_url": 3,
    "hours_to_recheck": 12,
    "urls_per_session": 30,
}

# ====================== GLOBAL STATE ======================
PROXY_USAGE = defaultdict(int)
PROXY_MB_USAGE = defaultdict(float)

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

# ====================== HELPERS ======================
async def get_uf_value() -> float:
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get("https://mindicador.cl/api/uf")
            return float(r.json()['serie'][0]['valor'])
    except Exception as e:
        logging.warning(f"Error UF: {e}. Fallback.")
        return 39485.65

async def get_proxies_from_api(api_url: str) -> list:
    """Obtiene una lista de proxies desde una URL API."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
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
        # En UF, si hay coma, suele ser decimal. Si hay punto y coma, el punto es miles.
        clean_uf = uf_match.group(1).replace('.', '') # Eliminar separador mil
        clean_uf = clean_uf.replace(',', '.') # Convertir decimal CLP a float Python
        try: uf_val = float(clean_uf)
        except: pass

    # Extraer CLP ($)
    clp_match = re.search(r'(?:CLP|\$)\s*([\d.,]+)', s)
    clp_val = None
    if clp_match:
        raw_num = clp_match.group(1)
        # Lógica robusta para CLP:
        # Si tiene un solo separador y es coma -> puede ser decimal (ej 600,5) o mil (ej 600,000)
        # Pero en Yapo el 99% de los precios son enteros. 
        # Si el número después del separador tiene 3 dígitos, es MIL.
        if ',' in raw_num and '.' not in raw_num:
            parts = raw_num.split(',')
            if len(parts[-1]) == 3: # ej 600,000
                raw_num = raw_num.replace(',', '')
            else: # ej 600,5
                raw_num = raw_num.replace(',', '.')
        elif '.' in raw_num and ',' not in raw_num:
            parts = raw_num.split('.')
            if len(parts[-1]) == 3: # ej 600.000
                raw_num = raw_num.replace('.', '')
            # Si no son 3 dígitos, dejamos el punto como decimal
        else:
            # Mixto: 1.200,50 o 1,200.50
            raw_num = raw_num.replace('.', '').replace(',', '')
            # (Aquí perdemos decimales en mixto, pero para CLP de arriendo no importa)
            
        try: clp_val = float(raw_num)
        except: pass

    return uf_val, clp_val

def normalize_text(text: str, max_chars: int = None) -> str:
    if not text or text == "N/A":
        return "N/A"
    text = normalize('NFKD', text.lower()).encode('ASCII', 'ignore').decode('ASCII')
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_chars] if max_chars else text

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

def normalize_url(href: str) -> str:
    full = urljoin("https://www.yapo.cl", href)
    p = urlparse(full)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))

async def extract_fast_path(url: str, client: httpx.AsyncClient) -> tuple:
    """Intenta extraer datos usando el cliente httpx persistente. Retorna (raw_data, size_bytes)."""
    try:
        resp = await client.get(url)
        size_bytes = len(resp.content)
        if resp.status_code != 200:
            return None, size_bytes
            
        # Buscar __NEXT_DATA__ en el HTML
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.DOTALL)
        if match:
            nd = json.loads(match.group(1))
            props = nd.get("props", {}).get("pageProps", {}).get("ad", {})
            if props:
                location = props.get("location", {})
                
                # Extraer campos con fallback entre nombres de keys comunes
                res = {
                    "source": "fast_path",
                    "region": location.get("regionName") or location.get("region_name") or "N/A",
                    "comuna": location.get("communeName") or location.get("commune_name") or location.get("cityName") or "N/A",
                    "sector": location.get("neighbourhood") or location.get("location_name") or "N/A",
                    "lat": location.get("latitude") or location.get("lat") or "N/A",
                    "lon": location.get("longitude") or location.get("lng") or location.get("lon") or "N/A",
                    "m2_total_str": str(props.get("size") or props.get("total_area") or "N/A"),
                    "m2_util_str": str(props.get("usefulSize") or props.get("useful_area") or "N/A"),
                    "gastos_comunes_str": str(props.get("maintenanceCost") or props.get("maintenance_cost") or "N/A"),
                    "dormitorios_str": str(props.get("rooms") or props.get("bedrooms") or "N/A"),
                    "banos_str": str(props.get("bathrooms") or "N/A"),
                    "estacionamientos_str": str(props.get("parkingSpaces") or props.get("parking") or "N/A"),
                    "title": props.get("title") or props.get("subject") or "N/A",
                    "price": f"{props.get('priceLabel', '')} {props.get('price', '')}".strip() or "N/A",
                    "publicador": props.get("sellerName") or props.get("contactName") or "N/A",
                    "raw_desc": props.get("description") or props.get("body") or "N/A",
                    "tipo_propiedad": props.get("category", {}).get("name") or "N/A",
                    "list_time": props.get("listTime") or props.get("list_time") or props.get("date") or "N/A",
                    "seller_id": str(props.get("userId") or props.get("user_id") or props.get("accountId") or props.get("account_id") or "N/A"),
                    "seller_type": props.get("sellerType") or props.get("seller_type") or props.get("accountType") or "N/A",
                    "company_name": props.get("companyName") or props.get("company_name") or props.get("agencyName") or "N/A",
                    "images_url": [img.get("url") if isinstance(img, dict) else img for img in props.get("images", [])] if isinstance(props.get("images", []), list) else [],
                }
                
                # Si campos críticos son N/A, mejor forzar fallback
                if res["title"] == "N/A" or res["price"] == "N/A":
                    return None, size_bytes
                    
                return res, size_bytes
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
    """Extrae datos crudos de la página y retorna inmediatamente.
    Intenta __NEXT_DATA__ primero (fast path). Fallback a selectores DOM.
    NO llama a la IA. El proxy se libera al cerrar la página después de esto."""
    # Navegación con detección de tunnel error o bloqueos
    try:
        response = await page.goto(url, timeout=CONFIG["page_load_timeout"] * 1000, wait_until="commit")
        if not response:
            raise Exception("Sin respuesta del servidor")
        if "Access Denied" in (await page.title()) or "captcha" in page.url:
            raise Exception("Bloqueo detectado: Captcha/Access Denied")
    except Exception as e:
        if "ERR_TUNNEL_CONNECTION_FAILED" in str(e):
            raise Exception(f"Tunnel Error: {e}")
        raise e

    # === FAST PATH: __NEXT_DATA__ ===
    try:
        next_data_raw = await page.evaluate('''() => {
            const el = document.getElementById("__NEXT_DATA__");
            return el ? el.textContent : null;
        }''')
        if next_data_raw:
            nd = json.loads(next_data_raw)
            props = nd.get("props", {}).get("pageProps", {}).get("ad", {})
            if props:
                logging.info(f"⚡ __NEXT_DATA__ detectado — fast path")
                location = props.get("location", {})
                return {
                    "region": location.get("regionName") or location.get("region_name") or "N/A",
                    "comuna": location.get("communeName") or location.get("commune_name") or location.get("cityName") or "N/A",
                    "sector": location.get("neighbourhood") or location.get("location_name") or "N/A",
                    "lat": location.get("latitude") or location.get("lat") or "N/A",
                    "lon": location.get("longitude") or location.get("lng") or location.get("lon") or "N/A",
                    "m2_total_str": str(props.get("size") or props.get("total_area") or "N/A"),
                    "m2_util_str": str(props.get("usefulSize") or props.get("useful_area") or "N/A"),
                    "gastos_comunes_str": str(props.get("maintenanceCost") or props.get("maintenance_cost") or "N/A"),
                    "dormitorios_str": str(props.get("rooms") or props.get("bedrooms") or "N/A"),
                    "banos_str": str(props.get("bathrooms") or "N/A"),
                    "estacionamientos_str": str(props.get("parkingSpaces") or props.get("parking") or "N/A"),
                    "title": props.get("title") or props.get("subject") or "N/A",
                    "price": f"{props.get('priceLabel', '')} {props.get('price', '')}".strip() or "N/A",
                    "publicador": props.get("sellerName") or props.get("contactName") or "N/A",
                    "raw_desc": props.get("description") or props.get("body") or "N/A",
                    "tipo_propiedad": props.get("category", {}).get("name") or "N/A",
                    "list_time": props.get("listTime") or props.get("list_time") or props.get("date") or "N/A",
                    "seller_id": str(props.get("userId") or props.get("user_id") or props.get("accountId") or props.get("account_id") or "N/A"),
                    "seller_type": props.get("sellerType") or props.get("seller_type") or props.get("accountType") or "N/A",
                    "company_name": props.get("companyName") or props.get("company_name") or props.get("agencyName") or "N/A",
                    "images_url": [img.get("url") if isinstance(img, dict) else img for img in props.get("images", [])] if isinstance(props.get("images", []), list) else [],
                }
    except Exception as e:
        logging.debug(f"__NEXT_DATA__ no disponible: {e}")

    # === SLOW PATH: Selectores DOM ===
    # Helper para extraer texto con timeout corto
    async def safe_text(selectors, timeout=4000):
        if isinstance(selectors, str): selectors = [selectors]
        for selector in selectors:
            try:
                text = await page.locator(selector).first.inner_text(timeout=timeout)
                if text and text != "N/A": return text
            except: continue
        return "N/A"

    # Esperar a que cargue la estructura básica
    try:
        await page.wait_for_selector('h2.d3-property-details__title, h1, .ad-title, .product-price', timeout=10000)
    except: pass

    # 1. Ubicación desde breadcrumbs
    breadcrumbs_elems = await page.query_selector_all('ol.breadcrumb li')
    region = comuna = tipo_propiedad = "N/A"
    if breadcrumbs_elems:
        try:
            # Los breadcrumbs de Yapo suelen ser: Home > Región > Comuna > Categoría
            # Pero pueden variar. Buscamos de atrás hacia adelante.
            # El último suele ser el título (o enlace al título), penúltimo la categoría.
            texts = []
            for item in breadcrumbs_elems:
                t = await item.inner_text()
                if t.strip(): texts.append(t.strip())
            
            if len(texts) >= 4:
                # [Home, Bienes Raíces, Arriendo, Región, Comuna, Categoría]
                region = texts[3] if len(texts) > 3 else "N/A"
                comuna = texts[4] if len(texts) > 4 else "N/A"
                if len(texts) > 5:
                    tipo_propiedad = texts[5]
        except: pass
    
    # 2. Datos técnicos (d3-)
    async def get_d3_attr(label):
        try:
            xpath = f"//dt[contains(text(), '{label}')]/following-sibling::dd"
            return await page.locator(xpath).first.inner_text(timeout=2000)
        except: return "N/A"

    async def get_d3_detail(label):
        try:
            xpath = f"//div[contains(@class, 'd3-property-details__detail-label') or contains(@class, 'col-6')][contains(., '{label}')]/following-sibling::div/strong | //div[contains(@class, 'd3-property-details__detail-label')][contains(., '{label}')]/p"
            return await page.locator(xpath).first.inner_text(timeout=2000)
        except: return "N/A"

    m2_total_str = await get_d3_detail('M² totales')
    if m2_total_str == "N/A": m2_total_str = await safe_text('.product-icons-icon:has-text("m²")')
    
    m2_util_str = await get_d3_detail('M² útiles')
    gastos_comunes_str = await get_d3_detail('mantenimiento')
    
    dormitorios_str = await get_d3_attr('Dormitorios')
    if dormitorios_str == "N/A": dormitorios_str = await safe_text('.product-icons-icon:has-text("dorm")')
    
    banos_str = await get_d3_attr('Baños')
    if banos_str == "N/A": banos_str = await safe_text('.product-icons-icon:has-text("baños")')
    
    estacionamientos_str = await get_d3_attr('Estacionamientos')
    if estacionamientos_str == "N/A": estacionamientos_str = await safe_text('.product-icons-icon:has-text("parks")')

    title = await safe_text(['h2.d3-property-details__title', 'h1', '.ad-title', '.product-name'])
    price = await safe_text(['.d3-property-info__price', '.price', '.ad-price', '.product-price'])
    publicador = await safe_text(['.contact_name', '.seller-info', '.seller-name', 'strong:has-text("Propietario")'])
    raw_desc = await safe_text(['.d3-property-about__text', '.description', '.ad-description', '.product-comments'])

    # Extraer tipo de propiedad desde los breadcrumbs o selectores
    tipo_propiedad = "N/A"
    try:
        if len(breadcrumbs_elems) >= 4:
            tipo_propiedad = await (await breadcrumbs_elems[3].query_selector('a')).inner_text()
    except: pass

    # Retornar todo lo extraído como dict crudo — sin IA, sin DB
    return {
        "region": region, "comuna": comuna, "sector": "N/A",
        "lat": "N/A", "lon": "N/A",
        "m2_total_str": m2_total_str, "m2_util_str": m2_util_str,
        "gastos_comunes_str": gastos_comunes_str,
        "dormitorios_str": dormitorios_str, "banos_str": banos_str,
        "estacionamientos_str": estacionamientos_str,
        "title": title, "price": price,
        "publicador": publicador, "raw_desc": raw_desc,
        "tipo_propiedad": tipo_propiedad,
        "list_time": "N/A", "seller_id": "N/A", "seller_type": "N/A",
        "company_name": "N/A", "images_url": [],
    }


# ====================== PROCESAMIENTO IA (Sin Browser/Proxy) ======================
async def process_with_ai(raw_data: dict, uf_value: float, coll) -> dict:
    """Procesa los datos crudos con deduplicación, filtro pre-AI y Grok.
    No requiere browser ni proxy — se ejecuta después de liberar el contexto."""
    title = raw_data["title"]
    raw_desc = raw_data["raw_desc"]
    publicador = raw_data["publicador"]
    price = raw_data["price"]
    region = raw_data["region"]
    comuna = raw_data["comuna"]

    content_hash = generate_content_hash(title, raw_desc)

    # Deduplicación por hash
    duplicate = await coll.find_one({"details.content_hash": content_hash})
    if duplicate:
        return {"is_duplicate": True}

    # Eliminamos el filtro pre-AI simplista para asegurar calidad total en cada aviso
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
T: {title} | P: {publicador} | Precio Original: {price}
D: {raw_desc[:1200]}"""

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

    # Procesamiento numérico en Python (Gratis)
    def clean_num(s):
        if not s or s == "N/A": return None
        nums = re.findall(r'\d+', s.replace('.', '').replace(',', ''))
        return int(nums[0]) if nums else None

    # Consolidación de datos: Prioridad IA -> Scraper -> Cálculos
    def get_val(key, fallback_val=None):
        v = extracted.get(key)
        if v is not None and v != "N/A" and v != "": return v
        return raw_data.get(key, fallback_val)

    # Lógica de precios: Si Grok extrajo precio limpio, lo usamos. Si no, parseamos el original.
    ai_clp = extracted.get("precio_clp")
    ai_uf = extracted.get("precio_uf")
    
    if not ai_clp and not ai_uf:
        p_uf, p_clp = parse_price_components(price) # Nuestra lógica manual robusta
    else:
        p_uf, p_clp = ai_uf, ai_clp

    # UF -> CLP conversion si falta uno
    if not p_uf and p_clp and uf_value: p_uf = round(p_clp / uf_value, 2)
    elif not p_clp and p_uf and uf_value: p_clp = int(p_uf * uf_value)

    m2_tot = extracted.get("m2_total") or clean_num(raw_data.get("m2_total_str"))
    p_uf_m2 = None
    if p_uf and m2_tot and m2_tot > 0:
        p_uf_m2 = round(p_uf / m2_tot, 3)

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

    return {
        "portal": CONFIG["portal_name"],
        "comuna": (extracted.get("comuna") or comuna).strip(),
        "region": (extracted.get("region") or region).strip(),
        "sector": extracted.get("sector") or raw_data.get("sector"),
        "lat": raw_data.get("lat"),
        "lon": raw_data.get("lon"),
        "tipo_propiedad": tipo_prop,
        "tipo_operacion": "Arriendo",
        "titulo": title.strip()[:220],
        "precio": price,
        "precio_uf": p_uf,
        "precio_clp_raw": p_clp,
        "precio_uf_m2": p_uf_m2,
        "m2_total": m2_tot,
        "m2_util": extracted.get("m2_util") or clean_num(raw_data.get("m2_util_str")),
        "gastos_comunes": clean_num(raw_data.get("gastos_comunes_str")), # Suele ser difícil para la IA, mejor manual
        "dormitorios": extracted.get("dormitorios") or clean_num(raw_data.get("dormitorios_str")),
        "banos": extracted.get("banos") or clean_num(raw_data.get("banos_str")),
        "estacionamientos": extracted.get("estacionamientos") or clean_num(raw_data.get("estacionamientos_str")),
        "bodega": extracted.get("bodega", False),
        "piscina": extracted.get("piscina", False),
        "descripcion": normalize_text(extracted.get("resumen_limpio", raw_desc), CONFIG["desc_max_chars"]),
        "es_propietario_directo": extracted.get("es_propietario_directo", False),
        "confianza_propietario": extracted.get("confianza", 0.5),
        "dias_en_portal": dias_en_portal,
        "fecha_publicacion": raw_data.get("list_time"),
        "vendedor_id": raw_data.get("seller_id"),
        "tipo_vendedor": raw_data.get("seller_type"),
        "nombre_ejecutivo": raw_data.get("publicador"),
        "nombre_corredora": raw_data.get("company_name"),
        "enlaces_fotos": raw_data.get("images_url", []),
        "content_hash": content_hash,
        "fecha_scraping": datetime.now(timezone.utc).isoformat(),
        "fecha_ultima_vista": datetime.now(timezone.utc).isoformat()
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
    await queue_coll.create_index("status")

    # 1. DESCUBRIMIENTO (Auto-stop inteligente: 3 páginas sin links nuevos = fin)
    links_pending = await queue_coll.count_documents({"status": "pending"})
    if links_pending == 0 or args.force_discovery:
        logging.info(f"🔍 ETAPA 1: Descubrimiento de enlaces (auto-stop inteligente)...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ctx = await browser.new_context(user_agent=ua)
            page = await ctx.new_page()
            try:
                await stealth(page)
            except: pass
            
            await block_resources(page, mode="discovery")
            
            page_num = 0
            empty_streak = 0
            MAX_EMPTY_PAGES = 3  # Auto-stop: 3 páginas consecutivas sin links nuevos = fin del sitio
            
            while page_num < args.max_pages:
                page_num += 1
                if page_num == 1:
                    p_url = CONFIG["base_url"]
                else:
                    parsed = urlparse(CONFIG["base_url"])
                    new_path = f"{parsed.path}.{page_num}"
                    p_url = urlunparse((parsed.scheme, parsed.netloc, new_path, "", parsed.query, ""))

                try:
                    logging.info(f"🔍 Navegando a página {page_num}: {p_url}")
                    await page.goto(p_url, timeout=40000, wait_until="domcontentloaded")
                    
                    try:
                        await page.wait_for_load_state('networkidle', timeout=10000)
                    except:
                        pass
                    
                    # Cerrar cookie consent / banners en la primera carga
                    if page_num == 1:
                        for sel in ['button:has-text("Aceptar")', 'button:has-text("Acepto")', 
                                    'button:has-text("Accept")', '[id*="cookie"] button',
                                    '[class*="cookie"] button', '[class*="consent"] button']:
                            try:
                                btn = await page.wait_for_selector(sel, timeout=2000)
                                if btn:
                                    await btn.click()
                                    logging.info(f"🍪 Cookie/banner cerrado")
                                    await asyncio.sleep(1)
                                    break
                            except:
                                continue
                    
                    # Esperar a que los listings aparezcan en el DOM
                    try:
                        await page.wait_for_selector('a[href*="/3"], a[href*="/2"], footer', timeout=8000)
                    except:
                        pass
                    
                    links = await extract_links_with_scroll(page, CONFIG["max_scrolls"], CONFIG["scroll_delay"])
                    
                    # Si la página no tiene NINGÚN link de propiedad → fin del sitio
                    if len(links) == 0:
                        logging.info(f"🛑 Página {page_num} sin links de propiedad → fin del sitio.")
                        break
                    
                    new_inserted = 0
                    for link in links:
                        result = await queue_coll.update_one(
                            {"url": link},
                            {"$setOnInsert": {"url": link, "status": "pending", "retries": 0, "fecha_descubrimiento": datetime.now(timezone.utc)}},
                            upsert=True
                        )
                        if result.upserted_id:
                            new_inserted += 1
                    
                    total_pending = await queue_coll.count_documents({"status": "pending"})
                    logging.info(f"📄 Pág {page_num} | {len(links)} links | +{new_inserted} nuevos | Cola: {total_pending}")
                    
                    # Auto-stop: si no hay links nuevos (todos ya conocidos)
                    if new_inserted == 0:
                        empty_streak += 1
                        logging.info(f"⚠️ Sin links nuevos: {empty_streak}/{MAX_EMPTY_PAGES} consecutivas")
                        if empty_streak >= MAX_EMPTY_PAGES:
                            logging.info(f"🛑 AUTO-STOP: {MAX_EMPTY_PAGES} páginas sin novedades. Todo descubierto.")
                            break
                    else:
                        empty_streak = 0
                    
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                except Exception as e:
                    logging.error(f"⚠️ Error discovery pág {page_num}: {e}")
            
            logging.info(f"📊 Descubrimiento finalizado en {page_num} páginas.")
            await browser.close()
    else:
        logging.info(f"⏭️ Saltando descubrimiento (ya hay {links_pending} links pendientes).")

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
        """Imprime una línea compacta con todas las métricas clave."""
        pct = (stats['processed'] / total_pending * 100) if total_pending > 0 else 0
        total_mb = sum(PROXY_MB_USAGE.values())
        logging.info(
            f" [{stats['processed']}/{total_pending} ({pct:.0f}%)] "
            f"✅ Nuevos:{stats['new']} | 🏠 Dueños:{stats['owners']} | 🏢 Corredores:{stats['brokers']} | "
            f"🔄 Dupes:{stats['duplicates']} | ❌ Err:{stats['errors']} | 📉 Consumo:{total_mb:.2f}MB"
        )

    logging.info(f"🚀 ETAPA 2: Extracción | Pendientes: {total_pending} | Concurrency: {args.concurrency}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        async def worker(worker_id):
            ua = UserAgent()
            while True:
                # Polling atómico de la cola para obtener el proxy a usar en la sesión
                doc = await queue_coll.find_one_and_update(
                    {"status": "pending"},
                    {"$set": {"status": "processing", "worker": worker_id}},
                    sort=[("retries", 1)],
                    return_document=True
                )
                if not doc: break

                url = doc["url"]
                proxy = next(proxy_cycle) if proxy_cycle else None
                ctx_proxy = {"server": proxy} if proxy else None
                if ctx_proxy and Config.PROXY_USER:
                    ctx_proxy["username"], ctx_proxy["password"] = Config.PROXY_USER, Config.PROXY_PASS

                logging.info(f"🌐 [W{worker_id}] Sesión batch({CONFIG['urls_per_session']}) | Proxy: {proxy or 'directo'}")
                
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
                    async with httpx.AsyncClient(headers=h_headers, proxy=h_proxy, timeout=15.0, follow_redirects=True) as h_client:
                        ctx = await browser.new_context(user_agent=ua.random, proxy=ctx_proxy)
                        
                        for batch_i in range(CONFIG["urls_per_session"]):
                            if batch_i > 0:
                                doc = await queue_coll.find_one_and_update(
                                    {"status": "pending"},
                                    {"$set": {"status": "processing", "worker": worker_id}},
                                    sort=[("retries", 1)],
                                    return_document=True
                                )
                            if not doc: break
                            url = doc["url"]

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
                                p_key = proxy or "directo"
                                PROXY_USAGE[p_key] += 1
                                
                                raw_data, size_bytes = await extract_fast_path(url, h_client)
                                PROXY_MB_USAGE[p_key] += (size_bytes / (1024 * 1024))
                                
                                mb_url = size_bytes / (1024 * 1024)
                                logging.info(f"🔗 URL: {url}")
                                logging.info(f"🌐 [W{worker_id}] Proxy: {p_key} | Consumo URL: {mb_url:.3f}MB | Acumulado Proxy: {PROXY_MB_USAGE[p_key]:.2f}MB")
                                
                                # Si el Fast Path falla o es incompleto → Fallback
                                if not raw_data or raw_data.get("title") == "N/A":
                                    if raw_data: logging.info(f"⚠️ Fast Path imcompleto. Fallback a Browser...")
                                    
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
                                    except Exception as e:
                                        bw_session_mb = session_bytes[0] / (1024 * 1024)
                                        PROXY_MB_USAGE[p_key] += bw_session_mb
                                        if page: await page.close()
                                        page = None
                                        raise e

                                    if raw_data:
                                        raw_data["source"] = "browser"
                                        bw_session_mb = session_bytes[0] / (1024 * 1024)
                                        PROXY_MB_USAGE[p_key] += bw_session_mb
                                        logging.info(f"🌐 [W{worker_id}] Browser BW: {bw_session_mb:.2f}MB | Acumulado: {PROXY_MB_USAGE[p_key]:.2f}MB")

                                if page:
                                    await page.close()
                                    page = None

                            except Exception as e:
                                stats["errors"] += 1
                                stats["processed"] += 1
                                error_msg = str(e)[:100]
                                logging.error(f"⚠️ [W{worker_id}] Error en {url[-20:]}: {error_msg}")
                                ns = "failed" if doc.get("retries", 0) >= CONFIG["max_retries_per_url"] else "pending"
                                await queue_coll.update_one({"url": url}, {"$set": {"status": ns, "last_error": error_msg}, "$inc": {"retries": 1}})
                                if page:
                                    try: await page.close()
                                    except: pass
                                if "Tunnel" in error_msg or "ERR_TUNNEL" in error_msg: raise e
                                continue

                            # === FASE 2: PROCESAMIENTO IA ===
                            try:
                                details = await process_with_ai(raw_data, uf_value, coll)
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
                        
                        if ctx: await ctx.close()
                except Exception as e:
                    logging.error(f"🔄 [W{worker_id}] Rotando proxy: {str(e)[:60]}")
                    if ctx:
                        try: await ctx.close()
                        except: pass
                    await asyncio.sleep(3)

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
