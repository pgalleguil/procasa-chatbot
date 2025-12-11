# chatbot/utils.py
import re

def limpiar_telefono(phone: str) -> str:
    if not phone: return ""
    num = re.sub(r"[^\d]", "", phone)
    if num.startswith("56"): num = num[2:]
    if num.startswith("0"): num = num[1:]
    return num

def extraer_rut(texto: str) -> str | None:
    # Busca formato 12345678-9 o 12.345.678-k
    match = re.search(r'\b(\d{1,2}\.?\d{3}\.?\d{3}-?[\dkK])\b', texto)
    if match:
        rut_raw = match.group(1).replace(".", "").replace("-", "").upper()
        if len(rut_raw) > 1:
            return f"{rut_raw[:-1]}-{rut_raw[-1]}"
    return None

def extraer_email(texto: str) -> str | None:
    match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', texto)
    return match.group(0).lower() if match else None

def extraer_nombre_posible(texto: str) -> str | None:
    """
    Intenta extraer un nombre propio limpiando prefijos comunes, incluyendo repeticiones.
    """
    # 1. Limpieza agresiva de prefijos (incluyendo repeticiones como "es es")
    # Usa \b para límites de palabra y un match no-greedy para evitar problemas
    prefixes = r'(?i)\b(hola|buenas|mi nombre es|me llamo|soy|el nombre es|es|mi|nombre)\b\s*'
    texto_limpio = re.sub(prefixes, '', texto).strip()

    # 2. Buscar palabras con mayúscula inicial (Title Case)
    palabras_ignoradas = ["Hola", "Gracias", "Si", "No", "Quiero", "Busco", "Necesito", "El", "La", "Los", "Un", "Una", "Dato", "Correo", "Rut", "Nombre"]
    
    tokens = [p.strip(".,!?") for p in texto_limpio.split() if p and p[0].isupper()]
    
    nombres_reales = [t for t in tokens if t.title() not in palabras_ignoradas]

    if 1 <= len(nombres_reales) <= 4:
        return " ".join(nombres_reales)
    
    return None