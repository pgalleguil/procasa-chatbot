import requests
import json
import time

# Configuración
BASE_URL = "http://localhost:8000"  # Cambia esto si usas otro puerto
SERVER_URL = f"{BASE_URL}/webhook"

def simulate_message(phone, text, from_me=False):
    """
    Simula un mensaje llegando al webhook.
    """
    payload = {
        "id": "SIM_" + str(int(time.time())),
        "messages": [
            {
                "key": {
                    "remoteJid": f"{phone}@s.whatsapp.net",
                    "fromMe": from_me,
                    "id": "SIM_MSG_" + str(int(time.time())),
                    "cleanedSenderPn": phone
                },
                "pushName": "Simulador",
                "message": {
                    "conversation": text
                },
                "messageTimestamp": int(time.time())
            }
        ]
    }
    
    print(f"\n[SIM] Enviando mensaje de {phone}: {text}")
    try:
        response = requests.post(SERVER_URL, json=payload, timeout=10)
        print(f"[SIM] Respuesta Servidor: {response.status_code}")
        print(f"[SIM] Contenido: {response.text}")
    except Exception as e:
        print(f"[SIM] ERROR conectando al servidor local: {e}")
        print(f"[TIP] Asegúrate de tener el bot corriendo localmente con: uvicorn webhook:app --port 10000")

if __name__ == "__main__":
    print("--- SIMULADOR DE WEBHOOK LOCAL ---")
    
    # Ejemplo 1: Cliente interesado
    #simulate_message("56983219804", "Hola, me interesa la propiedad 67872")
    
    # Esperamos un poco para no saturar si hay logs
    #time.sleep(2)
    
    # Ejemplo 2: Cliente enviando link
    simulate_message("56983219804","""Hola, tengo algunas preguntas sobre Estacionamiento en Venta en kennedy/luis carrera. https://inmueble.mercadolibre.cl/MLC-3539636180-estacionamiento-kennedyluis-carrera-_JM

agendar visista, mi nombre es pia pascal, 16209335-5, zzz@gmail.com""")

    print("\n--- Simulación completada ---")
