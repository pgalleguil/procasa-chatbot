# chatbot/utils.py
import re
from typing import Optional

# ==========================================
# 1. LIMPIEZA DE TELÉFONO (FUNCIÓN FALTANTE)
# ==========================================
def limpiar_telefono(phone: str) -> str:
    """Limpia el número de teléfono para su uso en la base de datos."""
    if not phone: return ""
    # Quita cualquier cosa que no sea dígito
    num = re.sub(r"[^\d]", "", phone)
    # Intenta quitar el código de país '56' o '0' si están al inicio
    if num.startswith("56"): num = num[2:]
    if num.startswith("0"): num = num[1:]
    return num

# ==========================================
# 2. CONVERSIÓN SEGURA DE NÚMEROS (Corrección del error 'invalid literal for int')
# ==========================================
def safe_int_conversion(valor) -> int:
    """Convierte strings con formato '100,000' o '100.000' a enteros limpios."""
    if not valor:
        return 0
    
    # Convertimos a string y quitamos comas y puntos
    s_val = str(valor).replace(",", "").replace(".", "").strip()
    
    try:
        # Intentamos convertir lo que queda a entero
        return int(s_val)
    except ValueError:
        try:
            # Si falla, intentamos float y luego int (por si hay decimales raros)
            return int(float(s_val))
        except ValueError:
            return 0

# ==========================================
# 3. EXTRACCIÓN DE DATOS PERSONALES
# ==========================================
def extraer_rut(texto: str) -> Optional[str]:
    # Busca formato 12.345.678-9 o 12345678-9
    match = re.search(r'\b(\d{1,2}\.?\d{3}\.?\d{3}-?[\dkK])\b', texto)
    if match:
        rut_raw = match.group(1).replace(".", "").replace("-", "").upper()
        if len(rut_raw) > 1:
            return f"{rut_raw[:-1]}-{rut_raw[-1]}"
    return None

def extraer_email(texto: str) -> Optional[str]:
    match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', texto)
    return match.group(0).lower() if match else None

def extraer_nombre_posible(texto: str) -> Optional[str]:
    """
    Intenta extraer un nombre propio limpiando palabras comunes.
    """
    texto_limpio = re.sub(r'[\d\W_]+', ' ', texto).strip()
    
    # Lista ampliada de palabras y frases que NO son nombres
    palabras_ignoradas = [
        "Hola", "Gracias", "Si", "Sí", "No", "Quiero", "Busco", "Necesito", 
        "El", "La", "Los", "Un", "Una", "Dato", "Correo", "Rut", "Nombre", 
        "Visitar", "Ver", "Agendar", "Por", "Favor", "Saludos",
        "Interesa", "Interesado", "Interesada", "Gustaría", "Podría",
        "Me", "Te", "Le", "Se", "Lo", "La", "Al", "Del",
        "Fin", "Semana", "Mañana", "Tarde", "Noche", "Lunes", "Martes", 
        "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo", "Ok", "Claro", 
        "Perfecto", "Excelente", "Casa", "Depto"
    ]
    
    tokens = [p.strip(".,!?").title() for p in texto_limpio.split()]
    nombres_candidatos = []

    for t in tokens:
        if len(t) < 2 or any(char.isdigit() for char in t): 
            continue
        
        if t not in palabras_ignoradas:
            nombres_candidatos.append(t)

    # Heurística simple: si tenemos 2 a 4 palabras que parecen nombres, las devolvemos.
    if 1 < len(nombres_candidatos) <= 4 and all(t[0].isupper() for t in nombres_candidatos):
        return " ".join(nombres_candidatos)
        
    return None

def extraer_nombre_explicito(texto: str) -> Optional[str]:
    """
    Extrae nombre SOLO si el usuario lo entrega explícitamente.
    Ej: 
    - "me llamo Pedro"
    - "mi nombre es Juan Pérez"
    - "soy María"
    """
    texto = texto.strip()

    patrones = [
        r"me llamo\s+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,40})",
        r"mi nombre es\s+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,40})",
        r"soy\s+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,40})"
    ]

    for patron in patrones:
        match = re.search(patron, texto, re.IGNORECASE)
        if match:
            nombre = match.group(1).strip().title()
            # Evitamos frases largas raras
            if len(nombre.split()) <= 4:
                return nombre

    return None
