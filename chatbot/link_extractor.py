# chatbot/link_extractor.py → VERSIÓN CON LOGS ULTRA DETALLADOS (para ver exactamente qué busca)
import re
from typing import Tuple, Optional
from .storage import get_db
from config import Config

def extraer_codigo_mercadolibre(url: str) -> Optional[str]:
    url = url.upper().replace("_", "-")
    match = re.search(r"MLC[-_]?(\d+)", url)
    if match:
        codigo = f"MLC{match.group(1)}"
        print(f"[EXTRACCIÓN] Enlace Mercado Libre detectado → Código extraído: {codigo}")
        return codigo
    return None

def analizar_mensaje_para_link(mensaje: str) -> Tuple[bool, Optional[dict], str]:
    urls = re.findall(r'https?://[^\s]+', mensaje, re.IGNORECASE)
    
    for url in urls:
        url = url.split("?")[0].split("#")[0].rstrip("/")

        codigo_ml = extraer_codigo_mercadolibre(url)

        if codigo_ml:
            print(f"\n[INFO] BUSCANDO EN universo_obelix")
            print(f"[INFO] Campo usado → codigo_mercadolibre")
            print(f"[INFO] Valor buscado → '{codigo_ml}' (tipo: {type(codigo_ml)})")

            db = get_db()
            coleccion = db[Config.COLLECTION_NAME]

            # Búsqueda exacta
            propiedad = coleccion.find_one({"codigo_mercadolibre": codigo_ml})

            if propiedad:
                print(f"[ÉXITO] ¡PROPIEDAD ENCONTRADA!")
                print(f"[ÉXITO] Código Procasa: {propiedad.get('codigo')}")
                print(f"[ÉXITO] Comuna: {propiedad.get('comuna')}")
                return True, propiedad, f"MercadoLibre {codigo_ml}"
            else:
                print(f"[FALLO] NO se encontró con codigo_mercadolibre = '{codigo_ml}'")
                # Vamos a ver si existe en la colección pero con otro formato
                cursor = coleccion.find({"codigo_mercadolibre": {"$exists": True}}).limit(5)
                print(f"[DEBUG] Últimos 5 codigo_mercadolibre guardados en universo_obelix:")
                for doc in cursor:
                    valor = doc.get("codigo_mercadolibre")
                    print(f"   → '{valor}' (tipo: {type(valor)})")
                
                # Prueba adicional con regex por si hay espacios o diferencias
                prueba = coleccion.find_one({
                    "codigo_mercadolibre": {"$regex": codigo_ml.replace("MLC", ""), "$options": "i"}
                })
                if prueba:
                    print(f"[ALERTA] Encontrada con regex flexible → {prueba.get('codigo_mercadolibre')}")

            return True, None, f"MercadoLibre {codigo_ml}"

    return False, None, ""