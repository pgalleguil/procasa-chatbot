# chatbot/link_extractor.py → VERSIÓN CON BÚSQUEDA PRIORIZADA POR PLATAFORMA + codigo_procasa
import re
from typing import Tuple, Optional
from .storage import get_db
from .utils import safe_int_conversion
from config import Config

URL_RE = re.compile(r'https?://[^\s<>\]\)"]+', re.IGNORECASE)

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

def extraer_codigo_internacional(mensaje: str) -> Optional[str]:
    """Extrae códigos de 9 dígitos (formato internacional)."""
    match = re.search(r"\b(\d{9,10})\b", mensaje)
    if match:
        codigo = match.group(1)
        print(f"[EXTRACCION] Codigo Internacional detectado -> {codigo}")
        return codigo
    return None



def analizar_mensaje_para_link(mensaje: str) -> Tuple[bool, Optional[dict], str, Optional[str]]:
    """
    Analiza el mensaje buscando URLs y busca la propiedad en la DB.
    Prioriza el campo de búsqueda según la plataforma detectada en la URL.
    Retorna: (encontrado_link, propiedad_encontrada, plataforma_origen, codigo_externo)
    """
    urls = URL_RE.findall(mensaje)
    db = get_db()
    coleccion = db[Config.COLLECTION_NAME]
    
    for url in urls:
        # Limpieza básica
        url_clean = url.split("?")[0].split("#")[0].rstrip("/")
        url_lower = url_clean.lower()
        url_regex = {"$regex": re.escape(url_clean), "$options": "i"}
        
        # === PASO 1: Identificar plataforma ===
        plataforma = "Otro Portal"
        if "toctoc.com" in url_lower:              plataforma = "TocToc"
        elif "yapo.cl" in url_lower:               plataforma = "Yapo"
        elif "portalinmobiliario.com" in url_lower: plataforma = "PortalInmobiliario"
        elif "mercadolibre." in url_lower:          plataforma = "MercadoLibre"
        elif "procasa.cl" in url_lower:             plataforma = "Procasa"
        
        print(f"\n[INFO] Plataforma detectada: {plataforma} | Buscando en {Config.COLLECTION_NAME}")
        
        propiedad = None
        codigo_ml = None
        cod_yapo = None
        
        # === PASO 2: Búsqueda PRIORIZADA por plataforma ===
        
        if plataforma == "MercadoLibre":
            # Primero extraer código MLC y buscar en campos específicos de ML
            codigo_ml = extraer_codigo_mercadolibre(url_clean)
            if codigo_ml:
                propiedad = coleccion.find_one({"$or": [
                    {"publicaciones.portal_inmobiliario.codigo_pi": codigo_ml},
                    {"codigo_pi": codigo_ml},
                    {"codigo_mercadolibre": codigo_ml},
                    {"publicaciones.portal_inmobiliario.url_mercado_libre": url_regex},
                ]})
                if propiedad:
                    print(f"[EXITO-ML] Encontrado por código MLC {codigo_ml}")
            if not propiedad:
                propiedad = coleccion.find_one({"publicaciones.portal_inmobiliario.url_mercado_libre": url_regex})

        elif plataforma == "PortalInmobiliario":
            # Buscar por URL y código PI
            codigo_ml = extraer_codigo_mercadolibre(url_clean)  # PI usa mismo formato MLC
            query_pi = [{"publicaciones.portal_inmobiliario.url_pi": url_regex}]
            if codigo_ml:
                query_pi += [
                    {"publicaciones.portal_inmobiliario.codigo_pi": codigo_ml},
                    {"codigo_pi": codigo_ml},
                    {"codigo_mercadolibre": codigo_ml},
                ]
            propiedad = coleccion.find_one({"$or": query_pi})
            if propiedad:
                print(f"[EXITO-PI] Encontrado por URL/código Portal Inmobiliario")

        elif plataforma == "Yapo":
            # Buscar por código Yapo (dígitos finales de URL)
            cod_yapo = extraer_codigo_yapo(url_clean)
            query_yapo = [{"publicaciones.yapo.url_yapo": url_regex}, {"url_yapo": url_regex}]
            if cod_yapo:
                query_yapo += [
                    {"publicaciones.yapo.codigo_yapo": cod_yapo},
                    {"codigo_yapo": cod_yapo},
                    {"publicaciones.yapo.url_yapo": {"$regex": cod_yapo + "$"}},
                ]
            propiedad = coleccion.find_one({"$or": query_yapo})
            if propiedad:
                print(f"[EXITO-YAPO] Encontrado por código Yapo {cod_yapo}")

        elif plataforma == "TocToc":
            query_tt = [{"publicaciones.toctoc.url_toctoc": url_regex}, {"toctoc.enlace": url_regex}]
            match_tt = re.search(r"/([a-f0-9]{32,})", url_lower)
            if match_tt:
                tt_id = match_tt.group(1)
                query_tt += [
                    {"publicaciones.toctoc.url_toctoc": {"$regex": tt_id}},
                    {"toctoc.enlace": {"$regex": tt_id}},
                ]
            propiedad = coleccion.find_one({"$or": query_tt})
            if propiedad:
                print(f"[EXITO-TOCTOC] Encontrado por URL TocToc")

        elif plataforma == "Procasa":
            # Link directo procasa.cl/CODIGO — extraer el código del path
            match_pc = re.search(r"/([\w-]+)$", url_clean)
            if match_pc:
                cod_path = match_pc.group(1)
                propiedad = coleccion.find_one({"$or": [
                    {"codigo": cod_path},
                    {"codigo": safe_int_conversion(cod_path)},
                ]})

        # === PASO 3: Fallback UNIVERSAL si no se encontró por plataforma ===
        if not propiedad:
            print(f"[FALLBACK] Búsqueda universal para {plataforma} | URL: {url_clean[:80]}")
            query_or = [
                {"publicaciones.portal_inmobiliario.url_pi": url_regex},
                {"publicaciones.portal_inmobiliario.url_mercado_libre": url_regex},
                {"publicaciones.toctoc.url_toctoc": url_regex},
                {"publicaciones.yapo.url_yapo": url_regex},
                {"publicaciones.procasa.url_procasa": url_regex},
                {"toctoc.enlace": url_regex},
                {"url_yapo": url_regex},
            ]
            if codigo_ml:
                query_or += [
                    {"publicaciones.portal_inmobiliario.codigo_pi": codigo_ml},
                    {"codigo_pi": codigo_ml},
                    {"codigo_mercadolibre": codigo_ml},
                ]
            if cod_yapo:
                query_or += [
                    {"publicaciones.yapo.codigo_yapo": cod_yapo},
                    {"codigo_yapo": cod_yapo},
                ]
            propiedad = coleccion.find_one({"$or": query_or})

        # === PASO 4: Fallback por códigos numéricos embebidos en la URL ===
        if not propiedad:
            codigos_en_url = re.findall(r"(\d{4,10})", url_clean)
            if codigos_en_url:
                print(f"[RE-BUSQUEDA] Por códigos numéricos en URL: {codigos_en_url}")
                for potential_code in codigos_en_url:
                    prop_extra = coleccion.find_one({"$or": [
                        {"codigo": potential_code},
                        {"codigo": safe_int_conversion(potential_code)},
                        {"codigo_procasa": potential_code},
                        {"codigo_procasa": safe_int_conversion(potential_code)},
                        {"codigo_internacional": potential_code},
                        {"publicaciones.codigo_internacional": potential_code},
                    ]})
                    if prop_extra:
                        propiedad = prop_extra
                        print(f"[EXITO] PROPIEDAD ENCONTRADA por código embebido! Cod: {propiedad.get('codigo')}")
                        break

        if propiedad:
            print(f"[EXITO] PROPIEDAD ENCONTRADA | Plataforma: {plataforma} | Código Procasa: {propiedad.get('codigo')}")
            return True, propiedad, plataforma, url_clean
        else:
            print(f"[FALLO] NO se encontró propiedad con el link '{url_clean[:80]}'")
            ext_code = codigo_ml or cod_yapo or url_clean
            return True, None, plataforma, ext_code

    return False, None, "", None


def extraer_contexto_urls(mensaje: str) -> list[dict]:
    """
    Devuelve un resumen liviano de URLs detectadas para inyectarlo al prompt.
    Sirve para que el modelo no dependa solo de inferir desde texto crudo.
    """
    urls = URL_RE.findall(mensaje)
    contexto = []
    for url in urls:
        url_clean = url.split("?")[0].split("#")[0].rstrip("/")
        url_lower = url_clean.lower()
        plataforma = "Otro Portal"
        if "toctoc.com" in url_lower:
            plataforma = "TocToc"
        elif "yapo.cl" in url_lower:
            plataforma = "Yapo"
        elif "portalinmobiliario.com" in url_lower:
            plataforma = "PortalInmobiliario"
        elif "mercadolibre." in url_lower:
            plataforma = "MercadoLibre"
        elif "procasa.cl" in url_lower:
            plataforma = "Procasa"

        item = {"url": url_clean, "plataforma": plataforma}
        ml = extraer_codigo_mercadolibre(url_clean)
        if ml:
            item["codigo_mercadolibre"] = ml
        yapo = extraer_codigo_yapo(url_clean)
        if yapo:
            item["codigo_yapo"] = yapo
        contexto.append(item)
    return contexto

