"""
Script único de regularización: corrige `details.tipo_operacion` en yapo_propiedades
basándose en el slug de la URL de cada propiedad.
Uso: python scripts/regularizar_operacion.py [--dry-run]
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient, UpdateOne
from config import Config
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def extract_operation_from_url(url: str) -> str:
    if not url:
        return "S/I"
    u = url.lower()
    if "alquiler" in u or "arriendo" in u:
        return "Arriendo"
    if "venta" in u:
        return "Venta"
    return "S/I"


def regularizar(dry_run: bool = True):
    client = MongoClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]

    # Buscar docs que necesitan corrección:
    # - Sin tipo_operacion
    # - Con "Arriendo" (potencialmente incorrecto)
    # - Con "S/I"
    query = {
        "$or": [
            {"details.tipo_operacion": {"$exists": False}},
            {"details.tipo_operacion": "Arriendo"},
            {"details.tipo_operacion": "S/I"},
        ]
    }

    total = coll.count_documents(query)
    logger.info(f"Documentos a revisar: {total}")

    if total == 0:
        logger.info("No hay documentos para regularizar.")
        client.close()
        return

    cursor = coll.find(query, {"url": 1, "details.tipo_operacion": 1})
    operaciones = []
    stats = {"corregidos": 0, "sin_url": 0, "ya_correctos": 0, "no_detectable": 0}

    for doc in cursor:
        url = doc.get("url", "")
        current_op = (doc.get("details") or {}).get("tipo_operacion", "")

        if not url:
            stats["sin_url"] += 1
            continue

        new_op = extract_operation_from_url(url)

        if new_op == "S/I":
            stats["no_detectable"] += 1
            continue

        if current_op == new_op:
            stats["ya_correctos"] += 1
            continue

        operaciones.append(
            UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {"details.tipo_operacion": new_op}}
            )
        )
        stats["corregidos"] += 1

    logger.info(f"Resumen: {stats}")

    if not operaciones:
        logger.info("Ninguna operación requiere cambio.")
        client.close()
        return

    logger.info(f"Documentos a actualizar: {len(operaciones)}")

    if dry_run:
        logger.info("MODO DRY-RUN: No se realizaron cambios.")
        # Mostrar algunos ejemplos
        for i, op in enumerate(operaciones[:5]):
            doc = coll.find_one({"_id": op._filter["_id"]}, {"url": 1, "details.tipo_operacion": 1})
            url = doc.get("url", "") if doc else ""
            current = (doc.get("details") or {}).get("tipo_operacion", "") if doc else ""
            new = op._doc["$set"]["details.tipo_operacion"]
            logger.info(f"  {current} -> {new}  |  {url[:80]}")
    else:
        result = coll.bulk_write(operaciones, ordered=False)
        logger.info(f"Actualizados: {result.modified_count} documentos.")

    client.close()


if __name__ == "__main__":
    dry_run = "--apply" not in sys.argv
    if dry_run:
        logger.info("Ejecutando en MODO DRY-RUN (sin cambios). Usa --apply para aplicar cambios reales.")
    else:
        logger.info("Ejecutando en MODO REAL (APLICANDO CAMBIOS).")

    regularizar(dry_run=dry_run)
