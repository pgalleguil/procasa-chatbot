import sys
import os
sys.path.append(os.getcwd())
import logging
from chatbot.lead_router import find_responsible_executive
from chatbot.storage import get_db

# Setup basic logging to see outcomes
logging.basicConfig(level=logging.INFO)

def verify_fix():
    print("Verificando solución de UnboundLocalError y Alerta...")
    
    # 1. Test con código inexistente
    dummy_code = "9999999999999"
    print(f"\nProbando con código inexistente: {dummy_code}")
    try:
        # Esto antes causaba UnboundLocalError: region
        result = find_responsible_executive(property_code=dummy_code)
        print(f"  [PASSED] La función no crasheó. Retornó: {result[0]}")
    except Exception as e:
        print(f"  [FAILED] La función crasheó: {e}")

    # 2. Verificar si se creó la notificación pendiente de alerta
    db = get_db()
    notif = db["pending_notifications"].find_one({"lead_data.lead_type": "MISSING_PROPERTY_ALERT", "lead_data.property_code": dummy_code})
    if notif:
        print(f"  [PASSED] Alerta de propiedad faltante creada en DB para {notif['lead_data']['target_name']}.")
    else:
        print(f"  [FAILED] No se encontró la notificación de alerta en la base de datos.")

    # 3. Limpiar el test
    db["pending_notifications"].delete_many({"lead_data.property_code": dummy_code})

if __name__ == "__main__":
    verify_fix()
