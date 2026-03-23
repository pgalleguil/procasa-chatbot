import json
import time
from pymongo import MongoClient
from openai import OpenAI
from config import Config
from chatbot.prompts import PROMPT_CLASIFICACION_BI
from api_leads_intelligence import calculate_score # Importamos tu nueva lógica de score

# Configuración
client = OpenAI(api_key=Config.XAI_API_KEY, base_url=Config.GROK_BASE_URL)
mongo_client = MongoClient(Config.MONGO_URI)
db = mongo_client[Config.DB_NAME]
coleccion = db["leads"]

def ejecutar_todo():
    # ---- PASO 1: LIMPIEZA TOTAL ----
    print("🧹 PASO 1: Borrando clasificaciones anteriores y reseteando scores...")
    resultado_limpieza = coleccion.update_many(
        {}, 
        {
            "$unset": {
                "bi_procesado": "", 
                "bi_analytics_global": "",
                "score": "" 
            }
        }
    )
    print(f"✅ Se limpiaron {resultado_limpieza.modified_count} documentos.")

    # ---- PASO 2: REPROCESAMIENTO CON NUEVO PROMPT ----
    query = {"bi_procesado": {"$exists": False}}
    leads = list(coleccion.find(query))
    
    print(f"🚀 PASO 2: Analizando {len(leads)} leads con Ventana de 40 mensajes...")

    for doc in leads:
        phone = doc.get("phone", "S/N")
        messages = doc.get("messages", [])
        
        if not messages:
            continue

        # Usamos una ventana de 40 mensajes para detectar el "Abandono Inicial" o "Visitas" antiguas
        ultimos_40 = messages[-40:]
        historial_texto = ""
        for m in ultimos_40:
            role = "Bot" if m.get("role") == "assistant" else "Cliente"
            historial_texto += f"{role}: {m.get('content')}\n"

        try:
            print(f"📦 Analizando: {phone}...")
            
            response = client.chat.completions.create(
                model=Config.GROK_MODEL or "grok-2-1212",
                messages=[
                    {"role": "system", "content": PROMPT_CLASIFICACION_BI},
                    {"role": "user", "content": f"Historial:\n{historial_texto}"}
                ],
                temperature=0.1,
                response_format={ "type": "json_object" }
            )

            res_content = response.choices[0].message.content.strip()
            if res_content.startswith("```json"):
                res_content = res_content[7:-3].strip()

            bi_data = json.loads(res_content)

            # ---- PASO 3: CALCULAR NUEVO SCORE ----
            # Usamos la función que actualizamos en api_leads_intelligence.py
            nuevo_score = calculate_score(doc.get("prospecto", {}), bi_data)

            # Guardamos todo
            coleccion.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "bi_analytics_global": bi_data,
                        "bi_procesado": True,
                        "score": nuevo_score,
                        "ultima_actualizacion_bi": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                }
            )
            print(f"✅ {phone} -> {bi_data.get('RESULTADO_CHAT')} | Score: {nuevo_score}")
            
            # Delay para respetar límites de API
            time.sleep(0.5)

        except Exception as e:
            print(f"❌ Error en lead {phone}: {e}")

    print("\n✨ ¡Reprocesamiento completado con éxito!")

if __name__ == "__main__":
    ejecutar_todo()