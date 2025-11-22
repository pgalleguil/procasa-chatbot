# constants.py
INITIAL_TEMPLATE_LEAD = """
Hola {cliente}, soy asistente inmobiliaria de PROCASA Jorge Pablo Caro Propiedades. 😊 

Recordamos que hace poco mostraste interés en {prop_desc} y contactaste a uno de nuestros ejecutivos. ¿Pudiste coordinar y visitar alguna opción que te gustara? ¿Qué te pareció la experiencia?

Si sigues en la búsqueda de tu hogar ideal, me encantaría saber qué estás priorizando ahora: ¿dormitorios, comuna, presupuesto? ¡Estoy aquí para mostrarte opciones que se ajusten perfecto a lo que buscas!

Cuéntame un poco más para reconectarte con lo mejor de nuestra cartera.

Responde STOP para no recibir más mensajes.
"""

INITIAL_TEMPLATE_PROPIETARIO = """
Hola {{NOMBRE}} 👋, soy asistente inmobiliaria de PROCASA Jorge Pablo Caro Propiedades.
Breve actualización: El mercado está presionado por factores que debemos considerar para la venta de tu propiedad. Te resumo la foto actual:

📉 Sobre-Stock: Hay 108.000 viviendas disponibles (nivel histórico) y la velocidad de venta supera los 30 meses (CChC).
🏦 Freno Bancario: Las tasas siguen en el rango 4,5%–4,8%. Sumado a una desocupación del 8–9%, los bancos están pidiendo más pie y aprobando menos créditos.
📢 Dato Clave (Nuevo Ciclo):
Un posible cambio político/económico traerá inversionistas, pero también hará que salgan muchos más vendedores al mercado.
Mi recomendación: Posicionar tu propiedad como "oportunidad" AHORA (antes de que aumente la competencia) mediante un ajuste técnico.

¿Cómo prefieres avanzar? (Respóndeme con el número):

⿡ 1. Ajustar precio (7%)
⿢ 2. Mantener precio — Aceptando un tiempo de venta más largo.
⿣ 3. Propiedad no disponible

Quedo atento a tu número de respuesta para gestionar de inmediato.
"""

TIPO_CONTACTO_LEAD = "lead"
TIPO_CONTACTO_PROPIETARIO = "propietario"

# Regex exactamente como los tenías
STOP_KEYWORDS = r'\b(stop|no interesa|no molesten|no contacten|no insistir|denunciar|bloquear|spam|déjame en paz|cállate|no más|ignora|unfollow|silencio|para ya|acoso|molesto)\b'
FOUND_KEYWORDS = r'\b(encontre|ya compre|ya arriendo|en proceso|otro corredor|ya atendido|otro ejecutivo|tengo casa|cerrado el trato|ya firmé|otro agente|con competencia|ya encontré|listo con eso)\b'
WAITING_KEYWORDS = r'\b(espero|mejore|tasas bajen|sin empleo|desempleado|crisis|esperando momento|tiempos duros|mejor economía|cuando baje el dólar|pausa temporal|despido reciente|situación difícil|esperaré)\b'
CONTACT_ADVISOR_KEYWORDS = r'\b(quiero hablar|contactar asesor|hablar humano|llamar|escalar|asesor personal|con un experto|llámame ya|chat con persona|transfiere a humano|quiero llamar|habla conmigo|conecta con agente|contecte una persona)\b'
CLOSURE_KEYWORDS = r'\b(gracias|de nada|ok|okay|adiós|bye|saludos|perfecto|entendido|listo|genial|bueno|vale)\b'
FOLLOWUP_KEYWORDS = r'\b(aun no|todavía no|no me contactan|esperando|delay|frustrado|:(|molesto por|reclama|prioriza|urgente|no llegó)\b'

# Mensajes de respuesta exactamente iguales
RESPONSES = {
    "stop": "Entiendo, gracias por tu tiempo. Si cambias de opinión, estamos aquí para ayudarte.",
    "found": "¡Genial! Me alegra que hayas encontrado lo que buscabas. Si en el futuro necesitas más ayuda con propiedades, no dudes en contactarnos. ¡Éxito en tu nuevo hogar!",
    "waiting": "Entiendo perfectamente, momentos como estos requieren paciencia. Mientras tanto, si quieres, puedo enviarte actualizaciones mensuales sobre tendencias del mercado o propiedades que bajen de precio. Cuando estés listo, solo dime '¡Empecemos!' y te ayudo a encontrar lo ideal. ¿Te parece?",
    "advisor": "¡Por supuesto! Te conecto inmediatamente con uno de nuestros asesores inmobiliarios de Procasa. Te contactarán en breve para ayudarte personalmente. 😊",
    "followup_advisor": "Lo siento por el delay en el contacto desde mi escalado anterior, voy a RE-PRIORIZAR tu caso con máxima urgencia. Un asesor te llama en los próximos minutos. ¡Gracias por tu paciencia! 😊",
    "closure": "¡De nada! Si necesitas algo más, solo avísame. ¡Que tengas un gran día! 😊",
    "continue_first": "¡Hola! Gracias por responder. Me alegra que hayas tomado el tiempo. Cuéntame, ¿qué te pareció la experiencia con las opciones que revisamos? ¿Hay algo específico que estés buscando ahora?",
    "continue_more": "Para darte el mejor feedback, ¿puedes contarme más sobre tu experiencia o qué priorizas en una propiedad ideal?",
    "propietario_placeholder": "Hola {cliente}, gracias por responder sobre tu propiedad con Procasa (código: {codigo}). Estamos revisando tu mensaje y te contactaremos pronto con más detalles. 😊"
}

# ===================================================================
# RESPUESTAS PROPIETARIOS – VERSIÓN FINAL MASIVA 2025 (cero intervención humana)
# ===================================================================
RESPONSES_PROPIETARIO = {
    "autoriza_baja": "¡Perfecto {primer_nombre}! ✅ Recibimos tu autorización para ajustar el precio y vender mucho más rápido.\n\n"
                     "Todo quedó registrado automáticamente.\n"
                     "En máximo 72 hrs verás tu propiedad con el nuevo valor publicado + campaña full activa en portales y redes.\n\n"
                     "¡Gracias por confiar! Esto es lo que más resultados está dando ahora mismo. 🔥",

    "mantiene": "Entendido {primer_nombre}, decides mantener el precio por ahora.\n\n"
                "Te quedas en seguimiento automático: cada 30 días te haremos llegar un informe mensual.",

    "pausa": "Recibido fuerte y claro {primer_nombre} 🙌\n\n"
             "Tu propiedad queda pausada y no recibirás más mensajes automáticos.\n"
             "Si cambias de idea, solo escribe \"reactivar\" o \"volver\" y la ponemos de nuevo en venta al instante.\n"
             "¡Quedamos a disposición!",

    "default_caliente": "¡Gracias por responder {primer_nombre}! 😊\n\n"
                        "Entendemos que estás evaluando la venta de tu propiedad código {codigo}.\n"
                        "Quedó registrado tu interés y seguimos trabajando para posicionarla lo mejor posible.\n"
                        "Si necesitas algo puntual, un ejecutivo te contactará en las próximas horas."
}

# ===================================================================
# NUEVAS RESPUESTAS INTELIGENTES PARA PROPIETARIOS (2025)
# ===================================================================
RESPONSES_PROPIETARIO.update({
    "rechaza_baja": "Entendido {primer_nombre}, gracias por tu sinceridad 😊\n\n"
                    "Respeto completamente tu valoración de la propiedad. "
                    "El mercado está muy cambiante ahora mismo, pero cuando quieras "
                    "te envío un informe actualizado con las últimas ventas reales "
                    "en tu zona (sin compromiso alguno).\n\n"
                    "Solo dime 'infórmame' y te lo mando al tiro.\n"
                    "Quedamos a disposición cuando tú decidas. ¡Abrazo!",

    "rechazo_agresivo": "Lamento mucho que te haya molestado el contacto {primer_nombre} 🙌\n\n"
                        "Entiendo perfectamente y ya no recibirás más mensajes automáticos.\n"
                        "Si en el futuro cambias de idea, solo escribe 'reactivar' y volvemos al instante.\n"
})