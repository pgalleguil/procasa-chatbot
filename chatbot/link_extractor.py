# chatbot/link_extractor.py → VERSIÓN CON BÚSQUEDA PRIORIZADA POR PLATAFORMA + codigo_procasa
import re
from typing import Tuple, Optional
from .storage import get_db
from .utils import safe_int_conversion
from config import Config

URL_RE = re.compile(r'https?://[^\s<>\]\)"]+', re.IGNORECASE)


def detectar_plataforma(url: str) -> str:
    url_lower = (url or "").lower()
    if "toctoc.com" in url_lower:
        return "TocToc"
    if "yapo.cl" in url_lower:
        return "Yapo"
    if "portalinmobiliario.com" in url_lower:
        return "PortalInmobiliario"
    if "mercadolibre." in url_lower:
        return "MercadoLibre"
    if "procasa.cl" in url_lower:
        return "Procasa"
    return "Otro Portal"


def normalizar_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip().rstrip("/")
    url = re.sub(r"^https?://(www\.)?", "", url, flags=re.IGNORECASE)
    return url.lower()


def construir_patron_url(url: str) -> dict:
    """
    Construye un patrón flexible para matchear la URL aunque cambie http/https o www.
    """
    url_norm = normalizar_url(url)
    return {"$regex": rf"^(https?://)?(www\.)?{re.escape(url_norm)}/?$", "$options": "i"}

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



def analizar_mensaje_para_link(mensaje: str, phone=None) -> Tuple[bool, Optional[dict], str, Optional[str]]:
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
        url_regex = construir_patron_url(url_clean)
        url_norm = normalizar_url(url_clean)
        
        # === PASO 1: Identificar plataforma ===
        plataforma = detectar_plataforma(url_clean)
        
        print(f"\n[INFO] Plataforma detectada: {plataforma} | Buscando en {Config.COLLECTION_NAME}")
        print(f"[LINK_DEBUG] URL recibida: {url_clean}")
        print(f"[LINK_DEBUG] URL normalizada: {url_norm}")
        
        propiedad = None
        codigo_externo = None
        debug_info = {
            "url_original": url,
            "url_clean": url_clean,
            "repr_url_clean": repr(url_clean),
            "len_url_clean": len(url_clean),
            "platform": plataforma,
        }

        # === PASO 2: RESOLUCIÓN DETERMINÍSTICA POR PLATAFORMA ===
        if plataforma == "Yapo":
            cod_yapo = extraer_codigo_yapo(url_clean)
            cod_ints = re.findall(r"\b(\d{9,10})\b", url_clean)
            prop = coleccion.find_one({"publicaciones.yapo.url_yapo": url_clean})
            debug_info["exact_match"] = bool(prop)
            debug_info["regex_match"] = False
            debug_info["urls_encontradas"] = []
            if not prop and cod_yapo:
                regex_q = {"publicaciones.yapo.url_yapo": {"$regex": re.escape(cod_yapo) + r"$", "$options": "i"}}
                prop = coleccion.find_one(regex_q)
                debug_info["regex_match"] = bool(prop)
                if prop:
                    debug_info["urls_encontradas"] = [prop.get("publicaciones", {}).get("yapo", {}).get("url_yapo") or prop.get("url_yapo")]
            candidatos = [
                {"publicaciones.yapo.url_yapo": url_clean},
                {"publicaciones.yapo.url_yapo": url_regex},
                {"url_yapo": url_regex},
                {"url_yapo": url_clean},
                {"publicaciones.yapo.codigo_yapo": cod_yapo} if cod_yapo else None,
                {"codigo_yapo": cod_yapo} if cod_yapo else None,
            ]
            for cod_int in cod_ints:
                candidatos.extend([
                    {"codigo_internacional": cod_int},
                    {"publicaciones.codigo_internacional": cod_int},
                {"publicaciones.yapo.codigo_internacional": cod_int},
                ])
            if not propiedad:
                for i, q in enumerate([c for c in candidatos if c], 1):
                    print(f"[LINK_DEBUG] Yapo query #{i}: {q}")
                    propiedad = coleccion.find_one(q)
                    if propiedad:
                        print(f"[LINK_DEBUG] Yapo match en query #{i} -> codigo={propiedad.get('codigo')}")
                        break
            codigo_externo = cod_yapo
            if phone:
                try:
                    from .storage import actualizar_prospecto
                    actualizar_prospecto(phone, {"debug_link": debug_info})
                except Exception:
                    pass

        elif plataforma == "Procasa":
            match_pc = re.search(r"/(\d+)$", url_clean)
            cod_path = match_pc.group(1) if match_pc else None
            candidatos = []
            if cod_path:
                candidatos.extend([
                    {"codigo": cod_path},
                    {"codigo": cod_path},
                    {"codigo": safe_int_conversion(cod_path)},
                    {"publicaciones.procasa.url_procasa": url_clean},
                    {"publicaciones.procasa.url_procasa": url_regex},
                ])
            for i, q in enumerate([c for c in candidatos if c], 1):
                print(f"[LINK_DEBUG] Procasa query #{i}: {q}")
                propiedad = coleccion.find_one(q)
                if propiedad:
                    print(f"[LINK_DEBUG] Procasa match en query #{i} -> codigo={propiedad.get('codigo')}")
                    break
            codigo_externo = cod_path

        elif plataforma == "MercadoLibre":
            codigo_ml = extraer_codigo_mercadolibre(url_clean)
            candidatos = [
                {"publicaciones.portal_inmobiliario.url_mercado_libre": url_clean},
                {"publicaciones.portal_inmobiliario.url_mercado_libre": url_regex},
                {"codigo_mercadolibre": codigo_ml} if codigo_ml else None,
                {"publicaciones.portal_inmobiliario.codigo_pi": codigo_ml} if codigo_ml else None,
                {"codigo_pi": codigo_ml} if codigo_ml else None,
            ]
            for i, q in enumerate([c for c in candidatos if c], 1):
                print(f"[LINK_DEBUG] ML query #{i}: {q}")
                propiedad = coleccion.find_one(q)
                if propiedad:
                    print(f"[LINK_DEBUG] ML match en query #{i} -> codigo={propiedad.get('codigo')}")
                    break
            codigo_externo = codigo_ml

        elif plataforma == "PortalInmobiliario":
            codigo_pi = extraer_codigo_mercadolibre(url_clean)
            candidatos = [
                {"publicaciones.portal_inmobiliario.url_pi": url_clean},
                {"publicaciones.portal_inmobiliario.url_pi": url_regex},
                {"publicaciones.portal_inmobiliario.codigo_pi": codigo_pi} if codigo_pi else None,
                {"codigo_pi": codigo_pi} if codigo_pi else None,
                {"codigo_mercadolibre": codigo_pi} if codigo_pi else None,
            ]
            for i, q in enumerate([c for c in candidatos if c], 1):
                print(f"[LINK_DEBUG] PI query #{i}: {q}")
                propiedad = coleccion.find_one(q)
                if propiedad:
                    print(f"[LINK_DEBUG] PI match en query #{i} -> codigo={propiedad.get('codigo')}")
                    break
            codigo_externo = codigo_pi

        elif plataforma == "TocToc":
            match_tt = re.search(r"/([a-f0-9]{32,})", url_lower)
            tt_id = match_tt.group(1) if match_tt else None
            candidatos = [
                {"publicaciones.toctoc.url_toctoc": url_clean},
                {"publicaciones.toctoc.url_toctoc": url_regex},
                {"toctoc.enlace": url_clean},
                {"toctoc.enlace": url_regex},
                {"publicaciones.toctoc.url_toctoc": {"$regex": tt_id}} if tt_id else None,
                {"toctoc.enlace": {"$regex": tt_id}} if tt_id else None,
            ]
            for i, q in enumerate([c for c in candidatos if c], 1):
                print(f"[LINK_DEBUG] TocToc query #{i}: {q}")
                propiedad = coleccion.find_one(q)
                if propiedad:
                    print(f"[LINK_DEBUG] TocToc match en query #{i} -> codigo={propiedad.get('codigo')}")
                    break
            codigo_externo = tt_id

        elif plataforma == "Otro Portal":
            # No hacemos inventos; solo dejamos trazabilidad del link.
            propiedad = None
            codigo_externo = None

        if propiedad:
            print(f"[EXITO] PROPIEDAD ENCONTRADA | Plataforma: {plataforma} | Código Procasa: {propiedad.get('codigo')}")
            return True, propiedad, plataforma, url_clean
        else:
            print(f"[FALLO] NO se encontró propiedad con el link '{url_clean[:80]}'")
            return True, None, plataforma, codigo_externo

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
        plataforma = detectar_plataforma(url_clean)

        item = {"url": url_clean, "plataforma": plataforma}
        ml = extraer_codigo_mercadolibre(url_clean)
        if ml:
            item["codigo_mercadolibre"] = ml
        yapo = extraer_codigo_yapo(url_clean)
        if yapo:
            item["codigo_yapo"] = yapo
        contexto.append(item)
    return contexto

