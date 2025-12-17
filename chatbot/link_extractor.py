# chatbot/link_extractor.py → VERSIÓN CON LOGS Y PLATAFORMA CORREGIDA
import re
from typing import Tuple, Optional
from .storage import get_db
from config import Config

def extraer_codigo_mercadolibre(url: str) -> Optional[str]:
    url = url.upper().replace("_", "-")
    match = re.search(r"MLC[-_]?(\d+)", url)
    if match:
        codigo = f"MLC{match.group(1)}"
        # CORRECCIÓN: Log más genérico ya que se usa para PI y ML
        print(f"[EXTRACCIÓN] Código MLC detectado → Código extraído: {codigo}")
        return codigo
    return None

def analizar_mensaje_para_link(mensaje: str) -> Tuple[bool, Optional[dict], str, Optional[str]]:
    """
    Retorna: (encontrado_link, propiedad_encontrada, plataforma_origen, codigo_externo)
    plataforma_origen: ej. "MercadoLibre" o "PortalInmobiliario"
    codigo_externo: ej. "MLC3185527254" o None
    """
    urls = re.findall(r'https?://[^\s]+', mensaje, re.IGNORECASE)
    
    for url in urls:
        url = url.split("?")[0].split("#")[0].rstrip("/")

        codigo_ml = extraer_codigo_mercadolibre(url)

        if codigo_ml:
            
            # --- LÓGICA DE DETERMINACIÓN DE PLATAFORMA CORREGIDA ---
            url_lower = url.lower()
            if "portalinmobiliario.com" in url_lower:
                plataforma_origen = "PortalInmobiliario" # <-- CORRECCIÓN APLICADA AQUÍ
            elif "mercadolibre.cl" in url_lower or "mercadolibre.com" in url_lower:
                plataforma_origen = "MercadoLibre"
            else:
                plataforma_origen = "Otro Portal (MLC code)"
            # ------------------------------------------------------
            
            print(f"\n[INFO] BUSCANDO EN universo_obelix")
            print(f"[INFO] Campo usado → codigo_mercadolibre")
            print(f"[INFO] Valor buscado → '{codigo_ml}' (tipo: {type(codigo_ml)})")

            db = get_db()
            coleccion = db[Config.COLLECTION_NAME]

            # Búsqueda exacta
            propiedad = coleccion.find_one({"codigo_mercadolibre": codigo_ml})

            if propiedad:
                # LOG CORREGIDO: Muestra la plataforma de origen
                print(f"[ÉXITO] ¡PROPIEDAD ENCONTRADA! Desde: {plataforma_origen}")
                print(f"[ÉXITO] Código Procasa: {propiedad.get('codigo')}")
                print(f"[ÉXITO] Comuna: {propiedad.get('comuna')}")
                
                # RETORNA LA PLATAFORMA CORRECTA
                return True, propiedad, plataforma_origen, codigo_ml 
            else:
                print(f"[FALLO] NO se encontró con codigo_mercadolibre = '{codigo_ml}'")
                # Debug adicional
                cursor = coleccion.find({"codigo_mercadolibre": {"$exists": True}}).limit(5)
                print(f"[DEBUG] Últimos 5 codigo_mercadolibre guardados en universo_obelix:")
                for doc in cursor:
                    valor = doc.get("codigo_mercadolibre")
                    print(f"   → '{valor}' (tipo: {type(valor)})")

                # Si no encontró propiedad, igual reportamos el link para trazabilidad con la plataforma_origen
                return True, None, plataforma_origen, codigo_ml

    return False, None, "", None