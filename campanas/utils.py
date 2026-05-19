# campanas/utils.py

ACCIONES_MAP = {
    "aceptar_rebaja": {
        "estado": "ajuste_autorizado",
        "titulo": "Autorizacion recibida",
        "color": "#10b981",
        "mensaje": "Recibimos tu aprobacion para aplicar la rebaja sugerida. Nuestro equipo ejecutara el ajuste y te confirmara la actualizacion."
    },
    "contactar_ejecutivo": {
        "estado": "pendiente_llamada",
        "titulo": "Solicitud recibida",
        "color": "#3b82f6",
        "mensaje": "Perfecto. Un ejecutivo de Procasa te contactara para revisar la propuesta de precio contigo."
    },
    "mantener_precio": {
        "estado": "precio_mantenido",
        "titulo": "Precio mantenido",
        "color": "#f59e0b",
        "mensaje": "Registramos tu decision de mantener el precio actual. Seguiremos monitoreando el mercado para apoyarte."
    },
    "no_disponible": {
        "estado": "no_disponible",
        "titulo": "No disponible",
        "color": "#ef4444",
        "mensaje": "Perfecto, marcamos tu propiedad como no disponible. Si en el futuro tienes otra para vender o arrendar, aqui estaremos."
    },
    "unsubscribe": {
        "estado": "suscripcion_anulada",
        "titulo": "Suscripcion anulada",
        "color": "#6b7280",
        "mensaje": "Hemos procesado tu solicitud y quedaste desinscrito de nuestras comunicaciones."
    },
    # Compatibilidad legacy
    "ajuste_7": {
        "estado": "ajuste_autorizado",
        "titulo": "Autorizacion recibida",
        "color": "#10b981",
        "mensaje": "Ya realizamos la actualizacion del precio de tu propiedad en Procasa."
    },
    "llamada": {
        "estado": "pendiente_llamada",
        "titulo": "Solicitud recibida",
        "color": "#3b82f6",
        "mensaje": "Perfecto, derivamos tu solicitud para que un ejecutivo de Procasa se ponga en contacto contigo."
    },
    "mantener": {
        "estado": "precio_mantenido",
        "titulo": "Precio mantenido",
        "color": "#f59e0b",
        "mensaje": "Perfecto, dejamos el precio de tu propiedad tal como esta."
    }
}


def normalize_accion(accion: str) -> str:
    mapping = {
        "ajuste_7": "aceptar_rebaja",
        "llamada": "contactar_ejecutivo",
        "mantener": "mantener_precio",
    }
    return mapping.get((accion or "").strip().lower(), (accion or "").strip().lower())


def get_accion_config(accion: str):
    a = normalize_accion(accion)
    return ACCIONES_MAP.get(a, ACCIONES_MAP["mantener_precio"])
