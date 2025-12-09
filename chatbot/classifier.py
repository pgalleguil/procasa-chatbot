# chatbot/classifier.py
import logging
from config import Config
from .utils import limpiar_telefono

logger = logging.getLogger(__name__)

def es_propietario(phone: str) -> tuple[bool, str]:
    """
    Retorna (es_propietario: bool, nombre_encontrado: str o None)
    Busca en colección universo_obelix → campo movil_propietario
    """
    from pymongo import MongoClient
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]

    limpio = limpiar_telefono(phone)
    if not limpio:
        return False, None

    variantes = [
        limpio,
        "56" + limpio,
        "+56" + limpio,
        "0" + limpio,
    ]
    if len(limpio) == 9:
        variantes.append(limpio[1:])  # por si guardaron sin el 9 inicial

    resultado = db[Config.COLLECTION_NAME].find_one(
        {"movil_propietario": {"$in": variantes}},
        {"nombre_propietario": 1, "apellido_paterno_propietario": 1}
    )

    if resultado:
        nombre = f"{resultado.get('nombre_propietario', '')} {resultado.get('apellido_paterno_propietario', '')}".strip()
        nombre = nombre or "Cliente"
        logger.info(f"PROPIETARIO detectado: {phone} → {nombre}")
        return True, nombre

    logger.info(f"PROSPECTO detectado: {phone}")
    return False, None