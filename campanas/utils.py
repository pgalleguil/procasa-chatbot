# campanas/utils.py
from datetime import datetime

ACCIONES_MAP = {
    "ajuste_7": {
        "estado": "ajuste_autorizado",
        "titulo": "¡Autorización recibida!",
        "color": "#10b981",
        "mensaje": "Ya realizamos la actualización del precio de tu propiedad en Procasa.\n\nEl nuevo valor se verá reflejado en los portales inmobiliarios dentro de aproximadamente 72 horas.\n\nQuedaremos atentos"
    },
    "llamada": {
        "estado": "pendiente_llamada",
        "titulo": "¡Solicitud recibida!",
        "color": "#3b82f6",
        "mensaje": "Perfecto, derivamos tu solicitud para que un ejecutivo de Procasa se ponga en contacto contigo.\n\nTe llamaremos dentro de las próximas 24-48 horas.\n\n¡Gracias por confiar en nosotros!"
    },
    "mantener": {
        "estado": "precio_mantenido",
        "titulo": "Precio mantenido",
        "color": "#f59e0b",
        "mensaje": "Perfecto, dejamos el precio de tu propiedad tal como está.\n\nSeguiremos monitoreando el mercado para avisarte si cambia la situación.\n\nQuedamos a tu disposición."
    },
    "no_disponible": {
        "estado": "no_disponible",
        "titulo": "Entendido",
        "color": "#ef4444",
        "mensaje": "Perfecto, marcamos tu propiedad como no disponible.\n\nSi en el futuro tienes otra para vender o arrendar, aquí estaremos.\n\n¡Gracias por tu confianza!"
    },
    "unsubscribe": {
        "estado": "suscripcion_anulada",
        "titulo": "Suscripción anulada",
        "color": "#6b7280",
        "mensaje": "Hemos procesado tu solicitud y quedaste desinscrito de nuestras comunicaciones.\n\nSi deseas volver a recibir novedades, solo avísanos.\n\n¡Gracias por haber sido parte de Procasa!"
    }
}

def get_accion_config(accion: str):
    return ACCIONES_MAP.get(accion, ACCIONES_MAP["mantener"])