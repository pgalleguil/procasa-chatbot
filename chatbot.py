# chatbot.py - Versión limpia y corta (128 líneas reales)

import os
import re
import requests
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from config import Config
from pymongo import MongoClient

# === Importamos lo que movimos ===
from constants import (
    INITIAL_TEMPLATE_LEAD, INITIAL_TEMPLATE_PROPIETARIO,
    TIPO_CONTACTO_LEAD, TIPO_CONTACTO_PROPIETARIO,
    STOP_KEYWORDS, FOUND_KEYWORDS, WAITING_KEYWORDS,
    CONTACT_ADVISOR_KEYWORDS, CLOSURE_KEYWORDS, FOLLOWUP_KEYWORDS,
    RESPONSES
)
from prompts import INTENT_PROMPT
from handlers import (
    handle_stop, handle_found, handle_waiting,
    handle_advisor,
    handle_continue, handle_propietario_respuesta
)

# === Config y Mongo ===
config = Config()
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]
contactos_collection = db["contactos"]

SIM_PHONE_LEAD = config.TEST_PHONE or "+5699999999992"
SIM_PHONE_PROPIETARIO = os.getenv("TEST_PHONE_DUENO", "+5698888888881")

def call_grok(prompt: str, model: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None) -> Optional[str]:
    if not config.XAI_API_KEY:
        print("[ERROR] No XAI_API_KEY en .env.")
        return None
    
    model = model or config.GROK_MODEL
    headers = {
        "Authorization": f"Bearer {config.XAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # ← AQUÍ LA MAGIA: si no especificas max_tokens, usa 800 para humanización, sino el default
    if max_tokens is None:
        max_tokens = 800 if "humanizar" in prompt or len(prompt) > 500 else 50
    
    data = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    
    try:
        resp = requests.post(f"{config.GROK_BASE_URL}/chat/completions", headers=headers, json=data, timeout=15)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[GROK] Respuesta ({len(content.split())} palabras): {content[:100]}...")
        return content
    except Exception as e:
        print(f"[GROK ERROR] {e}")
        return None

# === Detectar intención (igual que antes, pero usando constants y prompts) ===
def detect_intent(user_msg: str, history: List[Dict[str, Any]]) -> str:
    # Construcción segura del resumen del historial
    recent_msgs = history[-8:] if history else []
    history_summary = ""
    for msg in recent_msgs:
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            content = str(msg.get("content", ""))[:60]
            meta_action = msg.get("metadata", {}).get("action", "")
            history_summary += f"{role}: {content} (action:{meta_action}) | "
    
    last_action = "normal"
    has_prior_escalado = False
    if history:
        last_meta = history[-1].get("metadata", {}) if isinstance(history[-1], dict) else {}
        last_action = last_meta.get("action", "normal")
        has_prior_escalado = any(
            "escalado" in m.get("metadata", {}).get("action", "") 
            for m in recent_msgs if isinstance(m, dict)
        )
        if any("cerrado" in m.get("metadata", {}).get("action", "") for m in recent_msgs if isinstance(m, dict)):
            last_action = "cerrado"

    prompt = INTENT_PROMPT.format(
        history_summary=history_summary or "Sin historial",
        last_action=last_action,
        has_prior_escalado="sí" if has_prior_escalado else "no",
        user_msg=user_msg
    )
    
    grok_response = call_grok(prompt, temperature=0.0)
    valid_intents = ["stop", "found", "waiting", "advisor", "closure", "followup_advisor", "continue", "not_looking"]
    
    if grok_response:
        cleaned = grok_response.strip().lower()
        if cleaned in valid_intents:
            print(f"[GROK] Intent: {cleaned}")
            if cleaned == "not_looking":
                return "stop"  # Tratamos como stop definitivo
            return cleaned
    
    # === FALLBACK REGEX (mejorado) ===
    lower = user_msg.lower()
    if re.search(STOP_KEYWORDS, lower) or "no estoy buscando" in lower or "no busco" in lower or "equivocad" in lower:
        return "stop"
    if re.search(FOUND_KEYWORDS, lower):
        return "found"
    if re.search(WAITING_KEYWORDS, lower):
        return "waiting"
    if re.search(CONTACT_ADVISOR_KEYWORDS, lower):
        return "advisor"
    if has_prior_escalado and re.search(FOLLOWUP_KEYWORDS, lower):
        return "followup_advisor"
    if re.search(CLOSURE_KEYWORDS, lower) and last_action != "normal":
        return "closure"
    
    return "continue"

# === DB utils (pequeñas, se quedan aquí) ===
# === DB utils – VERSIÓN SEGURA ===
def load_history(telefono: str) -> List[Dict[str, Any]]:
    contacto = contactos_collection.find_one({"telefono": telefono})
    if not contacto:
        return []
    
    messages = contacto.get("messages", [])
    if not isinstance(messages, list):
        print(f"[WARNING] Historial corrupto para {telefono}, reiniciando.")
        return []
    
    # Filtrar mensajes válidos
    valid_messages = []
    for msg in messages:
        if isinstance(msg, dict) and "role" in msg and "content" in msg:
            valid_messages.append(msg)
        else:
            print(f"[WARNING] Mensaje inválido ignorado: {msg}")
    
    print(f"[LOG] Historial cargado: {len(valid_messages)} msgs válidos para {telefono}")
    return valid_messages

def save_message(telefono: str, role: str, content: str, metadata: Dict[str, Any] = None):
    msg = {
        "role": role, "content": content,
        "timestamp": datetime.now(timezone.utc),
        "metadata": metadata or {}
    }
    contactos_collection.update_one(
        {"telefono": telefono},
        {"$push": {"messages": msg}},
        upsert=True
    )

def get_contacto_by_telefono(telefono: str) -> Optional[Dict[str, Any]]:
    contacto = contactos_collection.find_one({"telefono": telefono})
    if contacto:
        print(f"[DB] Contacto encontrado: {telefono} (tipo: {contacto.get('tipo', 'desconocido')})")
        return contacto
    return None

def deactivate_contacto(telefono: str):
    contactos_collection.update_one(
        {"telefono": telefono},
        {"$set": {"activo": False, "fecha_desactivacion": datetime.now(timezone.utc)}}
    )

# === Procesar mensaje (igual que antes) ===
def process_user_message(phone: str, user_msg: str):
    # Reactivar contacto vetado si vuelve a escribir
    contacto = get_contacto_by_telefono(phone)
    if contacto and contacto.get("activo") is False:
        print(f"[REVIVIDO] Contacto {phone} reactivado por nueva interacción")
        contactos_collection.update_one(
            {"telefono": phone},
            {"$set": {"activo": True, "fecha_reactivacion": datetime.now(timezone.utc)}}
        )

    history = load_history(phone)
    contacto = get_contacto_by_telefono(phone)
    tipo_contacto = contacto.get("tipo", TIPO_CONTACTO_LEAD) if contacto else TIPO_CONTACTO_LEAD

    if tipo_contacto == TIPO_CONTACTO_PROPIETARIO:
        #return handle_propietario_placeholder(phone, user_msg, history, contacto, contactos_collection, RESPONSES)
        return handle_propietario_respuesta(phone, user_msg, contacto, contactos_collection)

    intent = detect_intent(user_msg, history)
    print(f"[LOG] Intent detectada: {intent}")

    if intent == "stop":
        return handle_stop(phone, user_msg, tipo_contacto, contactos_collection, RESPONSES, deactivate_contacto)
    elif intent == "found":
        return handle_found(phone, user_msg, tipo_contacto, contactos_collection, RESPONSES)
    elif intent == "waiting":
        return handle_waiting(phone, user_msg, tipo_contacto, contactos_collection, RESPONSES)
    elif intent == "advisor":
        return handle_advisor(phone, user_msg, history, tipo_contacto, contactos_collection, RESPONSES)
    #elif intent == "followup_advisor":
    #    return handle_followup_advisor(phone, user_msg, history, tipo_contacto, contactos_collection, RESPONSES)
    elif intent == "closure":
        # Tratamos "closure" como "stop" por ahora (es lo más seguro)
        return handle_stop(phone, user_msg, tipo_contacto, contactos_collection, RESPONSES, deactivate_contacto)
    else:
        return handle_continue(phone, user_msg, history, tipo_contacto, contactos_collection, RESPONSES)

# === Simulación (exactamente como la tenías) ===
def simulate_chat():
    print("[SIM] Elige tipo: 'lead' (default) o 'propietario'")
    tipo_input = input("Tipo: ").strip().lower() or "lead"
    if tipo_input == "propietario":
        phone = SIM_PHONE_PROPIETARIO
        contactos_collection.update_one(
            {"telefono": phone},
            {"$set": {"tipo": TIPO_CONTACTO_PROPIETARIO, "codigo": "57570", "nombre_propietario": "Carolina de los Angeles"}},
            upsert=True
        )
        print(f"[SIM] Mock propietario: {phone}")
    else:
        phone = SIM_PHONE_LEAD
        contactos_collection.update_one(
            {"telefono": phone},
            {"$set": {"tipo": TIPO_CONTACTO_LEAD}},
            upsert=True
        )
        print(f"[SIM] Mock lead: {phone}")

    print("\n[SIM] Escribe mensajes ('exit' para salir)\n")
    while True:
        user_input = input("Contacto: ").strip()
        if user_input.lower() == "exit":
            break
        if not user_input:
            continue
        bot_response = process_user_message(phone, user_input)
        print(f"\nBot: {bot_response}\n")

if __name__ == "__main__":
    simulate_chat()