# constants.py
INITIAL_TEMPLATE_LEAD = """
Hola {cliente}, soy asistente inmobiliaria de PROCASA Jorge Pablo Caro Propiedades. 😊 

Recordamos que hace poco mostraste interés en {prop_desc} y contactaste a uno de nuestros ejecutivos. ¿Pudiste coordinar y visitar alguna opción que te gustara? ¿Qué te pareció la experiencia?

Si sigues en la búsqueda de tu hogar ideal, me encantaría saber qué estás priorizando ahora: ¿dormitorios, comuna, presupuesto? ¡Estoy aquí para mostrarte opciones que se ajusten perfecto a lo que buscas!

Cuéntame un poco más para reconectarte con lo mejor de nuestra cartera.

Responde STOP para no recibir más mensajes.
"""

INITIAL_TEMPLATE_PROPIETARIO = """
Hola {{NOMBRE}} 👋, soy asistente inmobiliaria de PROCASA.
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
# OPCIÓN 1: La más importante. Debe ser una celebración.
    "autoriza_baja": "¡Excelente decisión, {primer_nombre}! 👏\n\n"
                     "Créeme que es la estrategia correcta para movernos rápido en este mercado.\n"
                     "Ya dejé programada la actualización. En máximo 72 hrs verás tu propiedad destacada con el nuevo valor en los portales.\n\n"
                     "¡Vamos con todo a buscar ese cierre! 🔥",

# OPCIÓN 2: Validación + Advertencia suave (sin ser pesados)
    "mantiene": "Entendido, {primer_nombre}. Respetamos tu decisión al 100%. 👍\n\n"
                "Mantendremos el precio actual. Ten en cuenta que, al haber mucha oferta, quizás el flujo de visitas sea más lento, pero seguiremos gestionando con la misma energía de siempre.\n\n"
                "Cualquier cambio que quieras hacer a futuro, solo avísame.",

# OPCIÓN 3: Cierre limpio
    "pausa": "Recibido, {primer_nombre}. 🙌\n\n"
             "Dejamos la propiedad en 'Pausa' desde este momento para que no te lleguen más notificaciones.\n"
             "Cuando sientas que es buen momento para retomar, solo escríbenos 'Reactivar' y volvemos a la carga.\n\n"
             "¡Gracias por la confianza hasta ahora!",

# FALLBACK / CALIENTE: Cuando dicen algo que no es 1, 2 o 3
    "default_caliente": "Gracias por responder, {primer_nombre}. 😊\n\n"
                        "Entiendo tu punto sobre la propiedad {codigo}. Como es un tema importante, le he pedido a uno de nuestros ejecutivos senior que revise tu caso y te contacte personalmente para verlo en detalle.\n"
                        "¡Hablamos pronto!"
}

# ===================================================================
# NUEVAS RESPUESTAS INTELIGENTES PARA PROPIETARIOS (2025)
# ===================================================================
RESPONSES_PROPIETARIO.update({
# RECHAZA LA BAJA (Argumentativo): Educación ante todo
    "rechaza_baja": "Te entiendo perfectamente, {primer_nombre}. Es difícil ajustar el valor cuando uno sabe lo que vale su propiedad. 🏠\n\n"
                    "Por ahora sigamos como estamos. Si en unas semanas ves que el mercado sigue lento, podemos volver a evaluarlo sin compromiso.\n"
                    "¡Seguimos trabajando para ti!",

# RECHAZO MOLESTO: Empatía total para evitar denuncias de spam
    "rechazo_agresivo": "Lamento mucho si el mensaje fue inoportuno, {primer_nombre}. 🙏\n\n"
                        "No era nuestra intención molestar. Ya eliminé tu número de nuestra lista de difusión automática para que no recibas más alertas de este tipo.\n"
                        "Quedamos a tu disposición solo si tú nos contactas. Que tengas buena tarde.",
})