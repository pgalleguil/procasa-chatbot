import sys
import os

# Añadir directorio raíz al path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from api_captacion import ensure_leads_indexes
import logging

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    print("[*] Ejecutando mantenimiento de base de datos...")
    try:
        ensure_leads_indexes()
        print("[OK] Índices creados/verificados con éxito.")
    except Exception as e:
        print(f"[ERROR] Error al crear índices: {e}")
