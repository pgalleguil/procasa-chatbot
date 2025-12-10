# chatbot/core.py → VERSIÓN OFICIAL FINAL PROFESIONAL – 10 DIC 2025
import logging
import re
from datetime import datetime

from config import Config
from .prompts import (
    SYSTEM_PROMPT_PROPIETARIO,
    SYSTEM_PROMPT_PROSPECTO
)
from .classifier import es_propietario
from .storage import guardar_mensaje, obtener_conversacion, get_db
from .grok_client import generar_respuesta
from .link_extractor import analizar_mensaje_para_link

logger = logging.getLogger(__name__)

# === HELPER: FICHA TÉCNICA COMPLETA (TODOS LOS CAMPOS) ===
def formatear_ficha_tecnica(propiedad):
    """
    Genera la ficha técnica con TODOS los campos para evitar alucinaciones.
    """
    return f"""
=== FICHA TÉCNICA OFICIAL (SOLO USA ESTOS DATOS) ===
Código Procasa: {propiedad.get('codigo', 'N/D')}
Tipo: {propiedad.get('tipo', 'Departamento').title()}
Operación: {propiedad.get('operacion', 'Venta').title()}
Comuna: {propiedad.get('comuna', 'Santiago').title()}
Precio: {propiedad.get('precio_uf', 'N/D')} UF | ${int(propiedad.get('precio_clp', 0)):,}
Metros útiles: {propiedad.get('m2_utiles', 'N/D')} m²
Metros totales: {propiedad.get('m2_totales', 'N/D')} m²
Terraza: {propiedad.get('m2_terraza', '0')} m²
Dormitorios: {propiedad.get('dormitorios', 'N/D')}
Baños: {propiedad.get('banos', 'N/D')}
Estacionamientos: {propiedad.get('estacionamientos', '0')}
Bodega: {'Sí' if str(propiedad.get('bodega','')).lower() in ['sí','si','1'] else 'No'}
Gastos comunes: ${int(propiedad.get('gastos_comunes', 0)):,}
Orientación: {propiedad.get('orientacion', 'No especificada')}
Calefacción: {propiedad.get('calefaccion', 'No especificada')}
Piscina: {propiedad.get('piscina', 'No')}
Quincho: {'Sí' if str(propiedad.get('quincho','')).lower() in ['sí','si','1'] else 'No'}
Jardín: {'Sí' if str(propiedad.get('jardin','')).lower() in ['sí','si'] else 'No'}
Gimnasio: {'Sí' if str(propiedad.get('gimnasio','')).lower() in ['sí','si','1'] else 'No'}
Lavandería: {'Sí' if str(propiedad.get('lavanderia_edificio','')).lower() in ['sí','si','1'] else 'No'}
Sala multiuso: {'Sí' if str(propiedad.get('sala_multiuso','')).lower() in ['sí','si','1'] else 'No'}
Mascotas permitidas: {'Sí' if str(propiedad.get('adecuado_mascotas','')).lower() in ['si','sí'] else 'No'}
Seguridad: {propiedad.get('seguridad', 'No especificada')}
Ubicación Referencial: {propiedad.get('nombre_calle', '')} (NO DAR NÚMERO EXACTO)
Amenities: {propiedad.get('amenities_text', 'muy bien conectado')[:200]}...
Descripción: {propiedad.get('descripcion_clean', '')[:300]}...
"""

def process_user_message(phone: str, message: str) -> str:
    original_message = message
    message_lower = message.strip().lower()

    guardar_mensaje(phone, "user", original_message)

    # === 1. BOTÓN DE PÁNICO ===
    palabras_alarma = ["hablar con persona", "ejecutivo real", "humano", "reclamo", "estafa", "mentira", "gerente"]
    if any(p in message_lower for p in palabras_alarma):
        respuesta = "Entiendo perfectamente. He marcado esta conversación como prioritaria para que un ejecutivo humano te llame personalmente a la brevedad posible."
        guardar_mensaje(phone, "assistant", respuesta, {"escalado_urgente": True})
        return respuesta

    # === 2. FLUJO PROPIETARIO ===
    es_prop, _ = es_propietario(phone)
    if es_prop:
        respuesta = generar_respuesta([
            {"role": "system", "content": "Eres asistente premium de Procasa. Habla directo y claro."},
            *obtener_conversacion(phone)[-30:],
            {"role": "user", "content": original_message}
        ], "propietario")
        guardar_mensaje(phone, "assistant", respuesta)
        return respuesta

    # === 3. ANÁLISIS DE CONTEXTO ===
    historial = obtener_conversacion(phone)
    historial_texto = " ".join(m["content"].lower() for m in historial)
    
    ya_presentamos = any("procasa" in m["content"].lower() and ("uf" in m["content"].lower() or "dorm" in m["content"].lower())
                         for m in historial if m["role"] == "assistant")
    
    ya_preguntamos_financiamiento = any("crédito" in m["content"].lower() or "contado" in m["content"].lower() for m in historial if m["role"] == "assistant")
    ya_preguntamos_horarios = any(p in historial_texto for p in ["horario", "disponibilidad", "días", "cuándo"])
    
    # -- DETECCIÓN INTELIGENTE DE VISITA --
    preguntas_horario_visita = ["cuando se puede", "cuándo se puede", "que horario", "qué horario", "disponibilidad para ver", "horario de visita"]
    esta_preguntando_horario_visita = any(p in message_lower for p in preguntas_horario_visita)

    palabras_visita = ["visita", "verla", "interesa", "agendar", "quiero ver", "me interesa"]
    quiere_visitar = any(p in message_lower for p in palabras_visita) or esta_preguntando_horario_visita

    # === 4. BUSCAR PROPIEDAD (LINK O CÓDIGO) ===
    propiedad = None
    es_link = False
    codigo_match = None

    if not ya_presentamos:
        # A) Link
        es_link, temp, _ = analizar_mensaje_para_link(original_message)
        if es_link and temp:
            propiedad = temp
        
        # B) Código Procasa
        if not propiedad:
            codigo_match = re.search(r"\b(\d{4,6})\b", original_message)
            if codigo_match:
                codigo_str = codigo_match.group(1)
                logger.info(f"[CÓDIGO PROCASA] Detectado: {codigo_str}")

                db = get_db()
                coleccion = db[Config.COLLECTION_NAME]

                propiedad = coleccion.find_one({
                    "$or": [
                        {"codigo": codigo_str},
                        {"codigo": int(codigo_str)}
                    ]
                })

                if propiedad:
                    logger.info(f"[ÉXITO] Propiedad encontrada por código Procasa {codigo_str}")
                else:
                    logger.warning(f"[FALLO] Código Procasa {codigo_str} NO encontrado")

    # === 5. PRIMERA PRESENTACIÓN ===
    if propiedad and not ya_presentamos:
        ficha = formatear_ficha_tecnica(propiedad)
        prompt = f"""
Eres ejecutiva Procasa.
PROHIBIDO USAR "HOLA" SI NO ES NECESARIO.
NUNCA DES DIRECCIÓN EXACTA.

{ficha}

Instrucciones:
1. Confirma: "Tengo la ficha del código {propiedad.get('codigo')}: {propiedad.get('tipo')} en {propiedad.get('comuna')}."
2. Menciona 2 cosas clave.
3. Pregunta: "¿Te gustaría coordinar una visita?"
"""
        respuesta = generar_respuesta([
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Presenta la propiedad"}
        ], "prospecto")

        guardar_mensaje(phone, "assistant", respuesta, {
            "tipo": "propiedad_presentada_segura",
            "codigo_procasa": propiedad.get("codigo"),
            "propiedad_data": propiedad
        })
        return respuesta

    # === 6. RESPUESTAS POSTERIORES (ANTI-ALUCINACIÓN TOTAL) ===
    if ya_presentamos:
        ultimo = next((m for m in reversed(historial) if "propiedad_data" in m), None)
        
        # --- A) FILTRO COMERCIAL ---
        if quiere_visitar and not ya_preguntamos_financiamiento and not ya_preguntamos_horarios:
            respuesta = "¡Genial! Para coordinar la visita: ¿Esta compra sería con **crédito hipotecario** o **al contado**?"
            guardar_mensaje(phone, "assistant", respuesta, {"pregunto_financiamiento": True})
            return respuesta

        # --- B) PREGUNTA HORARIOS ---
        es_respuesta_financiamiento = any(x in message_lower for x in ["credito", "crédito", "hipotecario", "banco", "contado", "efectivo", "preaprobado"])
        
        if (es_respuesta_financiamiento or quiere_visitar) and not ya_preguntamos_horarios:
             respuesta = "Perfecto. Respecto a los horarios, estos dependen de la disponibilidad del dueño.\n\nPor favor indícame: **¿Qué días y horas te acomodan a ti?** Así el ejecutivo coordina el calce exacto."
             guardar_mensaje(phone, "assistant", respuesta, {"pregunto_horarios": True})
             return respuesta

        # --- C) PREGUNTAS TÉCNICAS O HORARIOS ---
        if ultimo and "propiedad_data" in ultimo:
            propiedad = ultimo["propiedad_data"]

            if any(pal in message_lower for pal in ["cuando", "cuándo", "horario", "día", "visitar", "verla", "verlo", "agendar", "puedo ver"]):
                respuesta_fija = "Perfecto. Los horarios dependen 100% de la disponibilidad del dueño.\n\nPara no perder tiempo, ¿me indicas **qué días y horarios te acomodan a ti** esta semana o la próxima? Así el ejecutivo te confirma el bloque exacto en minutos."
                guardar_mensaje(phone, "assistant", respuesta_fija, {"pregunto_horarios": True})
                return respuesta_fija

            ficha = formatear_ficha_tecnica(propiedad)
            system_prompt = f"""
Eres ejecutiva de Procasa.
TU REGLA DE ORO: Solo hablas de lo que ves en la FICHA TÉCNICA de abajo.

{ficha}

REGLAS ESTRICTAS:
1. NO SALUDES repetitivamente.
2. HORARIOS PROHIBIDOS: responde exactamente: "Los horarios los coordina el dueño. ¿Qué días/horarios te sirven a ti?"
3. DIRECCIÓN: "Se entrega al confirmar la visita".
4. DATOS N/D: "Lo consulto con el ejecutivo".

Responde corto y preciso.
"""
            respuesta = generar_respuesta([
                {"role": "system", "content": system_prompt},
                *historial[-10:],
                {"role": "user", "content": original_message}
            ], "prospecto")
            guardar_mensaje(phone, "assistant", respuesta)
            return respuesta

    # === 7. CONFIRMACIÓN HORARIOS (CIERRE) ===
    if ya_preguntamos_horarios and any(d in message_lower for d in ["lunes","martes","miércoles","jueves","viernes","sábado","domingo","mañana","tarde"]):
        respuesta = "¡Listo! Ya registré sus horarios. Un ejecutivo te contactará pronto para confirmar la dirección exacta y el bloque definitivo.\n\n¡Gracias por confiar en Procasa!"
        guardar_mensaje(phone, "assistant", respuesta, {"escalado": True, "estado": "visita_pendiente"})
        return respuesta

    # === 8.5 CAPTURA PROACTIVA DE DATOS (3 ESCENARIOS MÁXIMA CONVERSIÓN) - CORREGIDO ===
    # Definimos la variable ANTES de usarla
    es_respuesta_financiamiento = any(x in message_lower for x in ["credito", "crédito", "hipotecario", "banco", "contado", "efectivo", "preaprobado"])

    if ya_preguntamos_horarios or (quiere_visitar and not ya_preguntamos_financiamiento) or es_respuesta_financiamiento:
        ya_pedimos_datos = any("nombre completo" in m["content"].lower() or "correo" in m["content"].lower() for m in historial if m["role"] == "assistant")
        if not ya_pedimos_datos:
            codigo_interes = None
            for msg in reversed(historial):
                if msg.get("codigo_procasa"):
                    codigo_interes = msg["codigo_procasa"]
                    break
            if codigo_interes:
                respuesta_datos = (
                    "¡Genial, ya estamos muy cerca de cerrar tu visita!\n\n"
                    "Para que el ejecutivo te llame con todo listo, me ayudás si me dejás (100% opcional):\n\n"
                    "• Nombre completo\n"
                    "• Correo electrónico\n"
                    "• RUT (si lo tenés)\n\n"
                    f"Así asocio todo al código **{codigo_interes}**. ¡Cuando quieras me lo pasas!"
                )
                guardar_mensaje(phone, "assistant", respuesta_datos, {"solicitando_datos_lead": True})
                return respuesta_datos

    # === 8.7 DETECCIÓN Y GUARDADO DE DATOS + ESTRUCTURA PROFESIONAL EN RAÍZ ===
    email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', original_message)
    rut_match = re.search(r'\b(\d{1,2}\.?\d{3}\.?\d{3}-?[\dkK])\b', original_message)
    nombre_detectado = None
    if any(t in message_lower for t in ["me llamo", "mi nombre", "soy ", "nombre es"]):
        resto = original_message.lower()
        for t in ["me llamo", "mi nombre es", "mi nombre", "soy", "nombre es"]:
            if t in resto:
                resto = resto.split(t, 1)[-1]
        posible = resto.strip(" .,!:@\n").split("\n")[0].split()
        if 2 <= len(posible) <= 5:
            nombre_detectado = " ".join(posible).strip(". ,!").title()

    if email_match or rut_match or nombre_detectado:
        update_fields = {"datos_capturados": True}
        if email_match:
            update_fields["email"] = email_match.group(0).lower()
        if rut_match:
            update_fields["rut"] = re.sub(r'\.', '', rut_match.group(1)).upper()
        if nombre_detectado:
            update_fields["nombre"] = nombre_detectado

        db = get_db()
        db[Config.COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {"$set": update_fields},
            upsert=False
        )

        nombre_saludo = update_fields.get("nombre", "").split()[0] if update_fields.get("nombre") else ""
        respuesta = f"¡Gracias{nombre_saludo and ' ' + nombre_saludo + ' ' or ' '} Ya tengo tus datos! El ejecutivo te llama en minutos con la orden de visita lista."
        guardar_mensaje(phone, "assistant", respuesta, {"lead_datos_ok": True, "escalado": True})
        return respuesta

    # === 9. ACTUALIZACIÓN PROFESIONAL DEL DOCUMENTO RAÍZ (LA CLAVE) ===
    update_root = {"ultima_actividad": datetime.utcnow().isoformat() + "Z"}
    if ya_presentamos:
        if ultimo and "propiedad_data" in ultimo:
            p = ultimo["propiedad_data"]
            update_root.update({
                "codigo_actual": p.get("codigo"),
                "tipo_actual": p.get("tipo"),
                "comuna_actual": p.get("comuna"),
                "precio_uf_actual": p.get("precio_uf"),
                "estado": "visita_pendiente" if ya_preguntamos_horarios else "interesado",
                "intencion_detectada": "visita" if quiere_visitar else "consulta",
                "$addToSet": {"codigos_vistos": p.get("codigo")}
            })

    if update_root:
        db = get_db()
        db[Config.COLLECTION_CONVERSATIONS].update_one(
            {"phone": phone},
            {
                "$set": update_root,
                "$setOnInsert": {"created_at": datetime.utcnow().isoformat() + "Z"}
            },
            upsert=True
        )

    # === 10. MODO CONVERSACIONAL POR DEFECTO ===
    prompt_ayuda = """
Eres asistente Procasa.
El cliente NO ha enviado link ni código.
Ayúdalo amablemente, pero explícale que necesitas el **Link** o el **Código (5 dígitos)** para dar detalles.
No seas robótica. Conversa brevemente sobre lo que busca.
"""
    respuesta = generar_respuesta([
        {"role": "system", "content": prompt_ayuda},
        *historial[-20:],
        {"role": "user", "content": original_message}
    ], "prospecto")
    
    guardar_mensaje(phone, "assistant", respuesta)
    return respuesta