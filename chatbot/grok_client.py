# chatbot/grok_client.py → VERSIÓN 100% LIMPIA Y SIN NADA RARO
from openai import OpenAI
from config import Config

client = OpenAI(
    api_key=Config.XAI_API_KEY,
    base_url=Config.GROK_BASE_URL
)

MAX_TOKENS = {
    "propietario": 600,
    "prospecto": 400
}

def generar_respuesta(messages: list, tipo: str = "prospecto") -> str:
    try:
        print(f"[GROK] Enviando {len(messages)} mensajes al modelo...")
        response = client.chat.completions.create(
            model=Config.GROK_MODEL or "grok-4-1-fast-non-reasoning",
            messages=messages,
            temperature=Config.GROK_TEMPERATURE,
            max_tokens=MAX_TOKENS.get(tipo, 400),
            timeout=30
        )
        contenido = response.choices[0].message.content.strip()
        print(f"[GROK] Respuesta recibida correctamente")
        return contenido
    except Exception as e:
        print(f"[ERROR GROK] Fallo en la API: {e}")
        return "Lo siento, tengo un problema técnico en este momento. En un segundo vuelvo a estar disponible."