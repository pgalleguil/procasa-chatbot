# chatbot/core.py → VERSIÓN ANALYTICS + FLUJO NATURAL
import logging
import re
from datetime import datetime

from config import Config
from .prompts import SYSTEM_PROMPT_PROSPECTO
from .classifier import es_propietario
from .storage import (
    guardar_mensaje, 
    obtener_conversacion, 
    get_db, 
    actualizar_prospecto, 
    obtener_prospecto,
    establecer_nombre_usuario
)
from .grok_client import generar_respuesta
from .link_extractor import analizar_mensaje_para_link
from .utils import extraer_rut, extraer_email, extraer_nombre_posible

logger = logging.getLogger(__name__)

# === FICHA SEGURA (Sin alucinaciones) ===
def formatear_ficha_tecnica(propiedad):
    """
    Ficha técnica OFICIAL. 
    NOTA: Si un dato no está aquí, el modelo NO debe inventarlo.
    """
    return f"""
    === FICHA TÉCNICA (CÓDIGO {propiedad.get('codigo')}) ===
    Operación: {propiedad.get('operacion', 'Venta')}
    Tipo: {propiedad.get('tipo', 'Propiedad')}
    Comuna: {propiedad.get('comuna', '')}
    Precio: {propiedad.get('precio_uf', 'N/D')} UF
    Gastos Comunes: ${int(propiedad.get('gastos_comunes', 0)):,} (aprox)
    
    Dormitorios: {propiedad.get('dormitorios', 'N/D')}
    Baños: {propiedad.get('banos', 'N/D')}
    M2 Útiles: {propiedad.get('m2_utiles', 'N/D')}
    Terraza: {propiedad.get('m2_terraza', '0')} m²
    Estacionamiento: {propiedad.get('estacionamientos', '0')}
    Bodega: {'Sí' if str(propiedad.get('bodega','')).lower() in ['sí','si','1'] else 'No'}
    
    Orientación: {propiedad.get('orientacion', 'No indicada')}
    Descripción: {propiedad.get('descripcion_clean', '')[:400]}...
    
    UBICACIÓN REFERENCIAL: Sector {propiedad.get('calle', '')}, {propiedad.get('comuna')}.
    (La dirección exacta se entrega SOLO en la Orden de Visita).
    """

def detectar_intencion(mensaje: str) -> str:
    m = mensaje.lower()
    if any(x in m for x in ["visita", "verla", "verlo", "agendar", "ir a ver", "conocer"]):
        return "agendar_visita"
    if any(x in m for x in ["precio", "valor", "cuanto vale", "uf", "gastos", "comunes"]):
        return "consulta_precio"
    if any(x in m for x in ["ubicacion", "direccion", "donde", "calle", "sector"]):
        return "consulta_ubicacion"
    if any(x in m for x in ["requisitos", "papeles", "documentos", "credito", "pie"]):
        return "consulta_financiera"
    return "consulta_general"

def process_user_message(phone: str, message: str) -> str:
    original_message = message
    message_lower = message.strip().lower()

    # 1. Guardar mensaje usuario
    guardar_mensaje(phone, "user", original_message)

    # === 2. ESCALADO DE URGENCIA ===
    palabras_alarma = ["hablar con persona", "ejecutivo real", "humano", "reclamo", "estafa", "gerente"]
    if any(p in message_lower for p in palabras_alarma):
        respuesta = "Entiendo. He notificado a un supervisor. Un ejecutivo humano te contactará a la brevedad."
        guardar_mensaje(phone, "assistant", respuesta, {"escalado_urgente": True})
        return respuesta

    # === 3. FLUJO PROPIETARIO ===
    es_prop, _ = es_propietario(phone)
    if es_prop:
        respuesta = generar_respuesta([
            {"role": "system", "content": "Eres asistente Procasa para propietarios. Sé breve y directo."},
            *obtener_conversacion(phone)[-8:],
            {"role": "user", "content": original_message}
        ], "propietario")
        guardar_mensaje(phone, "assistant", respuesta)
        return respuesta

    # === 4. PROCESAMIENTO INTELIGENTE PROSPECTO ===
    prospecto_actual = obtener_prospecto(phone)
    historial = obtener_conversacion(phone)
    
    # A) Detectar Origen (Link) y Código
    propiedad = None
    es_link, temp_prop, origen_str = analizar_mensaje_para_link(original_message)
    
    nuevo_origen = None
    codigo_detectado = None

    if es_link and temp_prop:
        propiedad = temp_prop
        nuevo_origen = origen_str
        codigo_detectado = str(propiedad.get("codigo"))
    
    # B) Si no es link, buscar código explícito en texto
    if not propiedad:
        match_cod = re.search(r"\b(\d{4,6})\b", original_message)
        if match_cod:
            cod_str = match_cod.group(1)
            propiedad = get_db()[Config.COLLECTION_NAME].find_one({
                "$or": [{"codigo": cod_str}, {"codigo": int(cod_str)}]
            })
            if propiedad:
                codigo_detectado = str(propiedad.get("codigo"))
                if not prospecto_actual.get("origen"):
                    nuevo_origen = "Chat/Codigo_Directo"

    # C) Actualizar Analytics en Mongo (Prospecto)
    updates = {
        "ultimo_mensaje": datetime.utcnow().isoformat(),
        "intencion_actual": detectar_intencion(original_message)
    }
    
    if codigo_detectado:
        updates["codigo_procasa"] = codigo_detectado
        # Datos estáticos solo para snapshot
        if propiedad:
            updates["snapshot_precio"] = propiedad.get("precio_uf")
            updates["snapshot_comuna"] = propiedad.get("comuna")
            updates["snapshot_tipo"] = propiedad.get("tipo")

    if nuevo_origen:
        updates["origen"] = nuevo_origen # Ej: "MercadoLibre MLC..."

    # D) Extracción de Datos Personales (Siempre activa y silenciosa)
    rut = extraer_rut(original_message)
    email = extraer_email(original_message)
    nombre = extraer_nombre_posible(original_message)

    if rut: updates["rut"] = rut
    if email: updates["email"] = email
    if nombre: 
        updates["nombre"] = nombre
        establecer_nombre_usuario(phone, nombre)

    actualizar_prospecto(phone, updates)
    
    # Recargar prospecto actualizado
    prospecto_actual = obtener_prospecto(phone)
    
    # Estado del lead
    tiene_datos_clave = bool(prospecto_actual.get("rut") or prospecto_actual.get("email"))
    estado_lead = "caliente" if tiene_datos_clave else "tibio"
    actualizar_prospecto(phone, {"estado": estado_lead})

    # === 5. GENERACIÓN DE RESPUESTA ===

    # CASO A: PRIMERA DETECCIÓN DE PROPIEDAD
    if propiedad and not any("presentacion_propiedad" in m.get("metadata", {}).get("tipo", "") for m in historial[-5:]):
        ficha = formatear_ficha_tecnica(propiedad)
        prompt = f"""
        Eres ejecutiva de Procasa. Tono cercano pero profesional.
        Acabas de encontrar la ficha del código {propiedad.get('codigo')}.

        DATOS:
        {ficha}

        TU OBJETIVO:
        1. Confirma que la encontraste: "{propiedad.get('tipo')} en {propiedad.get('comuna')}."
        2. Menciona el precio y 1 característica destacada.
        3. Pregunta abierta: "¿Te gustaría coordinar una visita o tienes alguna duda?"
        """
        respuesta = generar_respuesta([{"role": "system", "content": prompt}], "prospecto")
        guardar_mensaje(phone, "assistant", respuesta, {"tipo": "presentacion_propiedad"})
        return respuesta

    # CASO B: CLIENTE QUIERE VISITAR (INTENCIÓN DE VISITA)
    if updates["intencion_actual"] == "agendar_visita":
        # ¿Tenemos datos de contacto?
        faltantes = []
        if not prospecto_actual.get("nombre"): faltantes.append("nombre")
        if not prospecto_actual.get("rut"): faltantes.append("RUT")
        if not prospecto_actual.get("email"): faltantes.append("email")

        system_prompt = f"""
        El cliente quiere visitar la propiedad.
        TU OBJETIVO: Conseguir sus datos para ESCALAR a un humano.
        NO intentes agendar fecha y hora exacta. Eso lo hace el humano.

        Instrucciones:
        1. Dile que es una excelente opción.
        2. Explica: "Un ejecutivo especialista te contactará para coordinar la visita."
        3. Pide amablemente los datos faltantes ({', '.join(faltantes)}) diciendo que son para generar la **Orden de Visita con Firma Electrónica** que llegará a su correo.
        4. Si ya tienes todo, solo confirma que lo llamarán.
        """
        
        # Si ya tenemos todo
        if not faltantes:
            respuesta = f"¡Perfecto, {prospecto_actual.get('nombre', '')}! Ya tengo tus datos registrados.\n\nHe escalado tu solicitud. Un asesor inmobiliario te llamará en breve para coordinar el horario exacto y enviarte la orden de visita electrónica a tu correo {prospecto_actual.get('email')}.\n\n¡Gracias!"
            actualizar_prospecto(phone, {"estado": "listo_para_cierre"})
        else:
            respuesta = generar_respuesta([
                {"role": "system", "content": system_prompt},
                *historial[-5:],
                {"role": "user", "content": original_message}
            ], "prospecto")

        guardar_mensaje(phone, "assistant", respuesta, {"tipo": "gestion_visita"})
        return respuesta

    # CASO C: PREGUNTAS TÉCNICAS (USANDO FICHA O ESCALANDO)
    codigo_activo = prospecto_actual.get("codigo_procasa")
    if codigo_activo:
        # Recuperar ficha si no está en memoria
        if not propiedad:
            propiedad = get_db()[Config.COLLECTION_NAME].find_one({"codigo": {"$in": [codigo_activo, int(codigo_activo)]}})
        
        ficha = formatear_ficha_tecnica(propiedad) if propiedad else "Ficha no disponible."
        
        system_prompt = f"""
        Eres asistente Procasa.
        El cliente pregunta sobre la propiedad {codigo_activo}.
        
        INFORMACIÓN OFICIAL:
        {ficha}

        REGLAS ESTRICTAS:
        1. Si el dato está en la ficha, respóndelo amablemente.
        2. Si el dato NO está (ej: gastos comunes exactos si dice aprox, año construcción, contribuciones), DI LA VERDAD: "Ese dato específico no aparece en mi ficha, pero le pediré al ejecutivo que te lo averigüe."
        3. NUNCA INVENTES INFORMACIÓN.
        4. Al final, pregunta suavemente: "¿Te interesa coordinar una visita?"
        """
        respuesta = generar_respuesta([
            {"role": "system", "content": system_prompt},
            *historial[-10:],
            {"role": "user", "content": original_message}
        ], "prospecto")
        
        guardar_mensaje(phone, "assistant", respuesta)
        return respuesta

    # CASO D: GENÉRICO (Sin código)
    respuesta = generar_respuesta([
        {"role": "system", "content": "Eres asistente Procasa. El cliente no ha dado código. Pídelo amablemente o pregunta si busca comprar o arrendar."},
        *historial[-5:],
        {"role": "user", "content": original_message}
    ], "prospecto")
    
    guardar_mensaje(phone, "assistant", respuesta)
    return respuesta