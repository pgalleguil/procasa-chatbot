# chatbot/utils.py
import re

def limpiar_telefono(phone: str) -> str:
    """Normaliza cualquier formato de teléfono chileno a solo números sin +56"""
    if not phone:
        return ""
    # Quita +, espacios, guiones, paréntesis
    num = re.sub(r"[^\d]", "", phone)
    # Si empieza con 56, quita el 56
    if num.startswith("56"):
        num = num[2:]
    # Si empieza con 0, quita el 0
    if num.startswith("0"):
        num = num[1:]
    # Ahora debería ser 9xxxxxxx o 8xxxxxxx
    return num

def formatear_telefono_limpio_para_busqueda(phone: str) -> str:
    """Devuelve versiones posibles para buscar en Mongo (flexible)"""
    limpio = limpiar_telefono(phone)
    if not limpio:
        return ""
    variantes = [
        limpio,
        "56" + limpio,
        "+56" + limpio,
        "0" + limpio,
    ]
    if len(limpio) == 9:
        variantes.append(limpio[1:])  # por si guardaron sin el 9
    return variantes