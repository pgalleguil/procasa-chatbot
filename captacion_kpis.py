"""Reglas compartidas para agrupar las respuestas de captación."""

VISIBLE_CLASSIFICATION_STATES = ("DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO")

# `gestion.estado` es la respuesta guardada desde el detalle. Los documentos que
# todavía no han sido trabajados pueden venir sin el campo en cargas antiguas.
AVAILABLE_STATES = ("NUEVO", "DETECTADO", None, "")
MANAGEMENT_STATES = (
    "Por contactar",
    "En gestión",
    "Contacto exitoso",
    "Sin respuesta",
    "Reunión agendada",
    "GESTION",
    "INTENTO DE CONTACTO",
    "INTERESADO EN TASACIÓN",
    "TASACIÓN ENVIADA",
)
# Universo utilizado exclusivamente por las cuatro tarjetas superiores. Una
# propiedad "Por contactar" sigue perteneciendo a la cartera, pero todavía no
# representa una gestión comercial realizada.
KPI_MANAGEMENT_STATES = tuple(state for state in MANAGEMENT_STATES if state != "Por contactar")
KPI_PENDING_STATES = tuple(dict.fromkeys(AVAILABLE_STATES + ("Por contactar",)))
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
KPI_WORKED_STATES = KPI_MANAGEMENT_STATES + CAPTURED_STATES + DISCARDED_STATES


def build_kpi_queries(base_query):
    """Construye consultas mutuamente excluyentes desde la respuesta del equipo."""
    return {
        "available": {**base_query, "gestion.estado": {"$in": list(AVAILABLE_STATES)}},
        "management": {**base_query, "gestion.estado": {"$in": list(MANAGEMENT_STATES)}},
        "captured": {**base_query, "gestion.estado": {"$in": list(CAPTURED_STATES)}},
        "discarded": {**base_query, "gestion.estado": {"$in": list(DISCARDED_STATES)}},
    }
