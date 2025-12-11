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
    Intenta extraer un nombre propio limpiando RUTs, emails y prefijos.
    """
    # 1. Eliminar RUTs y Emails del texto para que no se confundan con nombres
    texto_limpio = re.sub(r'\b\d{1,2}\.?\d{3}\.?\d{3}-?[\dkK]\b', '', texto) # Quitar RUTs
    texto_limpio = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '', texto_limpio) # Quitar Emails
    
    # 2. Limpieza de prefijos comunes
    prefixes = r'(?i)\b(hola|buenas|mi nombre es|me llamo|soy|el nombre es|es|mi|nombre)\b\s*'
    texto_limpio = re.sub(prefixes, '', texto_limpio).strip()

    # 3. Lista negra de palabras comunes
    palabras_ignoradas = [
        "Hola", "Gracias", "Si", "No", "Quiero", "Busco", "Necesito", 
        "El", "La", "Los", "Un", "Una", "Dato", "Correo", "Rut", "Nombre", 
        "Visit", "Visitar", "Ver", "Agendar", "Por", "Favor", "Saludos",
        "Fin", "Semana", "Mañana", "Tarde", "Noche", "Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"
    ]
    
    tokens = [p.strip(".,!?") for p in texto_limpio.split()]
    nombres_candidatos = []

    for t in tokens:
        if len(t) < 2 or any(char.isdigit() for char in t): 
            continue
        
        t_cap = t.title() 
        if t_cap not in palabras_ignoradas:
            nombres_candidatos.append(t_cap)

    # Solo retornamos si parece un nombre válido (1 a 4 palabras)
    if 1 <= len(nombres_candidatos) <= 4:
        return " ".join(nombres_candidatos)
    
    return None