# chatbot/link_extractor.py → VERSIÓN CORREGIDA CON YAPO + FIX url_lower
import re
from typing import Tuple, Optional
from .storage import get_db
from config import Config

def extraer_codigo_mercadolibre(url: str) -> Optional[str]:
    url = url.upper().replace("_", "-")
    match = re.search(r"MLC[-_]?(\d+)", url)
    if match:
        codigo = f"MLC{match.group(1)}"
        print(f"[EXTRACCIÓN] Código MLC detectado → Código extraído: {codigo}")
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
        print(f"[EXTRACCIÓN] Código Yapo detectado → Código extraído: {codigo}")
        return codigo
    return None

def analizar_mensaje_para_link(mensaje: str) -> Tuple[bool, Optional[dict], str, Optional[str]]:
    """
    Retorna: (encontrado_link, propiedad_encontrada, plataforma_origen, codigo_externo)
    plataforma_origen: ej. "Yapo", "MercadoLibre", "PortalInmobiliario"
    codigo_externo: ej. "28546597" o "MLC1234567890"
    """
    urls = re.findall(r'https?://[^\s]+', mensaje, re.IGNORECASE)
    
    for url in urls:
        url = url.split("?")[0].split("#")[0].rstrip("/")
        url_lower = url.lower()  # ← DEFINIDO AQUÍ PARA TODO EL LOOP

        # === YAPO.CL (primero) ===
        if "yapo.cl" in url_lower:
            plataforma_origen = "Yapo"
            codigo_yapo = extraer_codigo_yapo(url)

            if codigo_yapo:
                print(f"\n[INFO] BUSCANDO EN universo_obelix")
                print(f"[INFO] Campo usado → codigo_yapo")
                print(f"[INFO] Valor buscado → '{codigo_yapo}'")

                db = get_db()
                coleccion = db[Config.COLLECTION_NAME]

                propiedad = coleccion.find_one({"codigo_yapo": codigo_yapo})

                if propiedad:
                    print(f"[ÉXITO] ¡PROPIEDAD ENCONTRADA en Yapo! Código Procasa: {propiedad.get('codigo')}")
                    return True, propiedad, plataforma_origen, codigo_yapo
                else:
                    print(f"[FALLO] NO se encontró propiedad con codigo_yapo = '{codigo_yapo}'")
                    # Debug rápido
                    cursor = coleccion.find({"codigo_yapo": {"$exists": True}}).limit(3)
                    print(f"[DEBUG] Algunos codigo_yapo existentes: {[doc.get('codigo_yapo') for doc in cursor]}")

                    return True, None, plataforma_origen, codigo_yapo
            else:
                # Es yapo.cl pero no tiene código válido → continuar con otros checks
                continue

        # === MERCADO LIBRE / PORTAL INMOBILIARIO ===
        codigo_ml = extraer_codigo_mercadolibre(url)

        if codigo_ml:
            # Determinación de plataforma (ya no redefine url_lower)
            if "portalinmobiliario.com" in url_lower:
                plataforma_origen = "PortalInmobiliario"
            elif "mercadolibre.cl" in url_lower or "mercadolibre.com" in url_lower:
                plataforma_origen = "MercadoLibre"
            else:
                plataforma_origen = "Otro Portal (MLC code)"
            
            print(f"\n[INFO] BUSCANDO EN universo_obelix")
            print(f"[INFO] Campo usado → codigo_mercadolibre")
            print(f"[INFO] Valor buscado → '{codigo_ml}'")

            db = get_db()
            coleccion = db[Config.COLLECTION_NAME]

            propiedad = coleccion.find_one({"codigo_mercadolibre": codigo_ml})

            if propiedad:
                print(f"[ÉXITO] ¡PROPIEDAD ENCONTRADA! Desde: {plataforma_origen}")
                print(f"[ÉXITO] Código Procasa: {propiedad.get('codigo')}")
                print(f"[ÉXITO] Comuna: {propiedad.get('comuna')}")
                return True, propiedad, plataforma_origen, codigo_ml
            else:
                print(f"[FALLO] NO se encontró con codigo_mercadolibre = '{codigo_ml}'")
                cursor = coleccion.find({"codigo_mercadolibre": {"$exists": True}}).limit(5)
                print(f"[DEBUG] Últimos 5 codigo_mercadolibre guardados:")
                for doc in cursor:
                    valor = doc.get("codigo_mercadolibre")
                    print(f"   → '{valor}' (tipo: {type(valor)})")

                return True, None, plataforma_origen, codigo_ml

    # Ningún enlace reconocido
    return False, None, "", None