# chatbot/link_extractor.py → VERSIÓN CORREGIDA CON YAPO + FIX url_lower
import re
from typing import Tuple, Optional
from .storage import get_db
from config import Config

def extraer_codigo_mercadolibre(url: str) -> Optional[str]:
    # Normalizar para encontrar el código MLC
    match = re.search(r"MLC[-_]?(\d+)", url, re.IGNORECASE)
    if match:
        codigo = f"MLC{match.group(1)}"
        print(f"[EXTRACCION] Codigo MLC detectado -> {codigo}")
        return codigo
    return None

def extraer_codigo_yapo(url: str) -> Optional[str]:
    """
    Extrae el código numérico al final de una URL de Yapo.cl
    Ejemplo: .../28546597 → "28546597"
    """
    match = re.search(r"/(\d{8,12})$", url)
    if match:
        codigo = match.group(1)
        print(f"[EXTRACCION] Codigo Yapo detectado -> {codigo}")
        return codigo
    return None

def analizar_mensaje_para_link(mensaje: str) -> Tuple[bool, Optional[dict], str, Optional[str]]:
    """
    Retorna: (encontrado_link, propiedad_encontrada, plataforma_origen, codigo_externo)
    """
    urls = re.findall(r'https?://[^\s]+', mensaje, re.IGNORECASE)
    db = get_db()
    coleccion = db[Config.COLLECTION_NAME]
    
    for url in urls:
        # Limpieza básica (quitar query params)
        url_clean = url.split("?")[0].split("#")[0].rstrip("/")
        url_lower = url_clean.lower()

        # === 1. TOC TOC ===
        if "toctoc.com" in url_lower:
            plataforma_origen = "TocToc"
            print(f"\n[INFO] BUSCANDO EN {Config.COLLECTION_NAME} (Plataforma: {plataforma_origen})")
            # Búsqueda por enlace en el campo específico solicitado
            propiedad = coleccion.find_one({
                "$or": [
                    {"toctoc.enlace": {"$regex": re.escape(url_clean), "$options": "i"}},
                    {"toctoc.enlace": {"$regex": re.escape(url), "$options": "i"}}
                ]
            })
            
            if propiedad:
                print(f"[EXITO] PROPIEDAD ENCONTRADA en TocToc! Codigo Procasa: {propiedad.get('codigo')}")
                return True, propiedad, plataforma_origen, url_clean
            else:
                print(f"[FALLO] NO se encontró propiedad con toctoc.enlace para '{url_clean}'")
                return True, None, plataforma_origen, url_clean

        # === 2. YAPO.CL ===
        if "yapo.cl" in url_lower:
            plataforma_origen = "Yapo"
            codigo_yapo = extraer_codigo_yapo(url_clean)
            if codigo_yapo:
                print(f"\n[INFO] BUSCANDO EN {Config.COLLECTION_NAME} (Plataforma: {plataforma_origen})")
                propiedad = coleccion.find_one({"codigo_yapo": codigo_yapo})
                if propiedad:
                    return True, propiedad, plataforma_origen, codigo_yapo
                return True, None, plataforma_origen, codigo_yapo

        # === 3. MERCADO LIBRE / PORTAL INMOBILIARIO ===
        codigo_ml = extraer_codigo_mercadolibre(url_clean)
        if codigo_ml:
            if "portalinmobiliario.com" in url_lower:
                plataforma_origen = "PortalInmobiliario"
            elif "mercadolibre." in url_lower:
                plataforma_origen = "MercadoLibre"
            else:
                plataforma_origen = "Portal (MLC)"
            
            print(f"\n[INFO] BUSCANDO EN {Config.COLLECTION_NAME} (Plataforma: {plataforma_origen})")
            # Búsqueda multi-campo exhaustiva según requerimiento
            query = {
                "$or": [
                    {"codigo_pi": codigo_ml},
                    {"codigo_pi": codigo_ml.replace("MLC", "")},
                    {"publicaciones.portal_inmobiliario.codigo_pi": codigo_ml},
                    {"publicaciones.portal_inmobiliario.url_pi": {"$regex": re.escape(url_clean), "$options": "i"}},
                    # Compatibilidad con campos antiguos si existieran
                    {"codigo_mercadolibre": codigo_ml},
                    {"codigo_mercadolibre": codigo_ml.replace("MLC", "")}
                ]
            }
            propiedad = coleccion.find_one(query)
            
            if propiedad:
                print(f"[EXITO] PROPIEDAD ENCONTRADA! Desde: {plataforma_origen} | Codigo Procasa: {propiedad.get('codigo')}")
                return True, propiedad, plataforma_origen, codigo_ml
            else:
                print(f"[FALLO] NO se encontró propiedad con '{codigo_ml}' (universo_cartera)")
                return True, None, plataforma_origen, codigo_ml

    return False, None, "", None