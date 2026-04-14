# chatbot/link_extractor.py → VERSIÓN CORREGIDA CON YAPO + FIX url_lower
import re
from typing import Tuple, Optional
from .storage import get_db
from .utils import safe_int_conversion
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
    Retorna: (encontrado_link, propiedad_encontrada, plataforma_origen, codigo_externo)
    """
    urls = re.findall(r'https?://[^\s]+', mensaje, re.IGNORECASE)
    db = get_db()
    coleccion = db[Config.COLLECTION_NAME]
    
def analizar_mensaje_para_link(mensaje: str) -> Tuple[bool, Optional[dict], str, Optional[str]]:
    """
    Analiza el mensaje buscando URLs o códigos y busca la propiedad en la DB.
    Retorna: (encontrado_link, propiedad_encontrada, plataforma_origen, codigo_externo)
    """
    urls = re.findall(r'https?://[^\s]+', mensaje, re.IGNORECASE)
    db = get_db()
    coleccion = db[Config.COLLECTION_NAME]
    
    for url in urls:
        # Limpieza básica
        url_clean = url.split("?")[0].split("#")[0].rstrip("/")
        url_lower = url_clean.lower()
        plataforma = "Otro Portal"
        
        # Identificar plataforma para el log
        if "toctoc.com" in url_lower: plataforma = "TocToc"
        elif "yapo.cl" in url_lower: plataforma = "Yapo"
        elif "portalinmobiliario.com" in url_lower: plataforma = "PortalInmobiliario"
        elif "mercadolibre." in url_lower: plataforma = "MercadoLibre"
        elif "procasa.cl" in url_lower: plataforma = "Procasa"
        
        print(f"\n[INFO] BUSCANDO LINK EN {Config.COLLECTION_NAME} (Plataforma estimada: {plataforma})")
        
        # BUSQUEDA UNIVERSAL: Intentar machar el link directamente o por partes
        url_regex = {"$regex": re.escape(url_clean), "$options": "i"}
        
        query_or = [
            {"publicaciones.portal_inmobiliario.url_pi": url_regex},
            {"publicaciones.portal_inmobiliario.url_mercado_libre": url_regex},
            {"publicaciones.toctoc.url_toctoc": url_regex},
            {"publicaciones.yapo.url_yapo": url_regex},
            {"publicaciones.procasa.url_procasa": url_regex},
            {"toctoc.enlace": url_regex},
            {"url_yapo": url_regex}
        ]
        
        # REFUERZO: Extraer el ID del link (Yapo, ML, PI) y buscar solo por ese ID
        # Para Yapo: buscaremos los últimos dígitos
        cod_yapo = extraer_codigo_yapo(url_clean)
        if cod_yapo:
            query_or.append({"publicaciones.yapo.codigo_yapo": cod_yapo})
            query_or.append({"codigo_yapo": cod_yapo})
            # A veces el link guardado tiene el código al final
            query_or.append({"publicaciones.yapo.url_yapo": {"$regex": cod_yapo + "$"}})

        # Para Mercado Libre / Portal Inmobiliario
        codigo_ml = extraer_codigo_mercadolibre(url_clean)
        if codigo_ml:
            query_or.append({"publicaciones.portal_inmobiliario.codigo_pi": codigo_ml})
            query_or.append({"codigo_pi": codigo_ml})
            query_or.append({"codigo_mercadolibre": codigo_ml})
            
        # Para Toctoc (A veces el ID está en el link)
        if "toctoc.com" in url_lower:
            match_tt = re.search(r"/([a-f0-9]{32,})", url_lower)
            if match_tt:
                tt_id = match_tt.group(1)
                query_or.append({"publicaciones.toctoc.url_toctoc": {"$regex": tt_id}})
                query_or.append({"toctoc.enlace": {"$regex": tt_id}})

        propiedad = coleccion.find_one({"$or": query_or})
        
        # BUSQUEDA DE RESPALDO: Si no hay match directo, buscar por CUALQUIER código numérico dentro del link
        if not propiedad:
            codigos_en_url = re.findall(r"(\d{4,10})", url_clean)
            if codigos_en_url:
                print(f"[RE-BUSQUEDA] Buscando por códigos detectados en URL: {codigos_en_url}")
                for potential_code in codigos_en_url:
                    prop_extra = coleccion.find_one({
                        "$or": [
                            {"codigo": potential_code},
                            {"codigo": safe_int_conversion(potential_code)},
                            {"codigo_internacional": potential_code},
                            {"publicaciones.codigo_internacional": potential_code}
                        ]
                    })
                    if prop_extra:
                        propiedad = prop_extra
                        print(f"[EXITO] PROPIEDAD ENCONTRADA por código embebido en link! Cod: {propiedad.get('codigo')}")
                        break

        if propiedad:
            print(f"[EXITO] PROPIEDAD ENCONTRADA por Link! Codigo Procasa: {propiedad.get('codigo')}")
            return True, propiedad, plataforma, url_clean
        else:
            print(f"[FALLO] NO se encontró propiedad con el link '{url_clean}'")
            # Retornamos el código detectado aunque no esté en DB para trazabilidad
            ext_code = codigo_ml or cod_yapo or url_clean
            return True, None, plataforma, ext_code

    return False, None, "", None
