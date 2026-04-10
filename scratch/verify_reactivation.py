import sys
import os
sys.path.append(os.getcwd())
import asyncio
from chatbot.core import process_user_message
from chatbot.storage import get_db, actualizar_prospecto
from chatbot.constants import PipelineStage

async def verify_reactivation():
    print("Verificando lógica de reactivación de leads archivados...")
    db = get_db()
    test_phone = "569test0001"
    
    # 1. Preparar lead como ARCHIVED
    print(f"  - Preparando lead {test_phone} como ARCHIVED...")
    db["leads"].update_one(
        {"phone": test_phone},
        {"$set": {
            "stage": "ARCHIVED",
            "prospecto.nombre": "Test Reactivacion",
            "created_at": "2024-01-01T00:00:00+00:00" # Muy antiguo
        }},
        upsert=True
    )
    
    # 2. Enviar mensaje del usuario
    print(f"  - Enviando mensaje del usuario...")
    # Mocking process_user_message (we don't need the actually response, just the state change)
    # We call it twice to ensure history and context are handled
    await process_user_message(test_phone, "Hola, me interesa una propiedad ahora.")
    
    # 3. Verificar estado
    lead = db["leads"].find_one({"phone": test_phone})
    stage = lead.get("stage")
    print(f"  - Estado actual del lead: {stage}")
    
    if stage == PipelineStage.NEW:
        print("  [PASSED] El lead fue reactivado a NEW.")
        # Verificar que el timestamp se actualizó (para evitar el auto-archivado de 90 días)
        from datetime import datetime
        from chatbot.constants import CHILE_TZ
        created_at = datetime.fromisoformat(lead.get("created_at"))
        now = datetime.now(CHILE_TZ)
        if (now - created_at).total_seconds() < 60:
            print("  [PASSED] El timestamp fue actualizado correctamente.")
        else:
            print(f"  [FAILED] El timestamp no se actualizó. Sigue siendo {created_at}")
    else:
        print(f"  [FAILED] El lead sigue en estado {stage}.")

    # 4. Limpiar test
    db["leads"].delete_one({"phone": test_phone})

if __name__ == "__main__":
    asyncio.run(verify_reactivation())
