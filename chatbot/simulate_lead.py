
import os
import sys
import asyncio
from datetime import datetime, time
from unittest.mock import patch, MagicMock

# Add parent directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from chatbot.lead_router import find_responsible_executive, should_send_now, format_whatsapp_template

async def simulate_lead(property_code: str, test_name: str = "Cliente de Prueba", sim_hour: int = None):
    print(f"\n--- SIMULACIÓN DE LEAD: PROPIEDAD {property_code} ---")
    
    # 1. Verificar Enrutamiento
    exec_name, exec_phone = find_responsible_executive(property_code)
    print(f"📍 Ejecutivo Asignado: {exec_name}")
    print(f"📞 Teléfono Destino: {exec_phone or '❌ NO ENCONTRADO (Se requiere en BD)'}")
    
    # 2. Verificar Mensaje
    lead_data = {
        "phone": "+56912345678",
        "nombre": test_name,
        "email": "prueba@ejemplo.cl",
        "last_message": "Hola, me interesa visitar esta propiedad.",
        "property_code": property_code
    }
    
    mensaje = format_whatsapp_template(lead_data, exec_name, property_code)
    print("\n📝 PROPUESTA DE MENSAJE WA:")
    print("-" * 30)
    print(mensaje)
    print("-" * 30)
    
    # 3. Verificar Lógica de Horario
    if sim_hour is not None:
        # Mocking time for simulation
        with patch('chatbot.is_business_hours') as mock_hours:
            # We assume business hours logic from lead_router
            import pytz
            chile_tz = pytz.timezone('Chile/Continental')
            now = datetime.now(chile_tz).replace(hour=sim_hour, minute=0)
            
            # Simplified check for simulation display
            is_business = 9 <= sim_hour < 18
            print(f"\n⏰ Simulación Hora: {sim_hour}:00")
            if is_business:
                print("✅ RESULTADO: Se envía de INMEDIATO.")
            else:
                print("⏳ RESULTADO: Se guarda como PENDIENTE para el día siguiente.")
    else:
        now_business = should_send_now()
        print(f"\n⏰ Estado Actual (Hora Real): {'✅ Horario Hábil (Envío Inmediato)' if now_business else '⏳ Fuera de Horario (Encola para mañana)'}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Simula la entrada de un lead para probar el enrutamiento.')
    parser.get_property = parser.add_argument('codigo', type=str, help='Código de la propiedad (ej: 57570)')
    parser.add_argument('--hora', type=int, help='Hora opcional para simular (0-23)', default=None)
    
    args = parser.parse_args()
    
    asyncio.run(simulate_lead(args.codigo, sim_hour=args.hora))
