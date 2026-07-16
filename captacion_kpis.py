"""Reglas compartidas para agrupar las respuestas de captación."""

VISIBLE_CLASSIFICATION_STATES = ("DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO")

# `gestion.estado` es la respuesta guardada desde el detalle. Los documentos que
# todavía no han sido trabajados pueden venir sin el campo en cargas antiguas.
AVAILABLE_STATES = ("NUEVO", "DETECTADO", None, "")
MANAGEMENT_STATES = (
    "Por contactar",
    "Contacto exitoso",
    "Sin respuesta",
    "Reunión agendada",
    "GESTION",
    "INTENTO DE CONTACTO",
    "INTERESADO EN TASACIÓN",
    "TASACIÓN ENVIADA",
)
CAPTURED_STATES = ("Captado", "CAPTADO")
DISCARDED_STATES = (
    "Corredor",
    "Teléfono inválido",
    "Descartado",
    "Propiedad no disponible",
    "Publicación expirada",
    "No interesado",
    "DESCARTADO",
)


def build_kpi_queries(base_query):
    """Construye consultas mutuamente excluyentes desde la respuesta del equipo."""
    return {
        "available": {**base_query, "gestion.estado": {"$in": list(AVAILABLE_STATES)}},
        "management": {**base_query, "gestion.estado": {"$in": list(MANAGEMENT_STATES)}},
        "captured": {**base_query, "gestion.estado": {"$in": list(CAPTURED_STATES)}},
        "discarded": {**base_query, "gestion.estado": {"$in": list(DISCARDED_STATES)}},
    }
