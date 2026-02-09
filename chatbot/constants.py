# chatbot/constants.py
from enum import Enum

class PipelineStage(str, Enum):
    NEW = "NEW"
    CONTACTED = "CONTACTED"     # El BOT solo puede llegar hasta aquí
    INTERESTED = "INTERESTED"   # Implicit interest detected (Legacy/Suggestion)
    VISIT_SCHEDULED = "VISIT_SCHEDULED" # Requiere confirmación humana
    VISIT_DONE = "VISIT_DONE"
    OFFER = "OFFER"
    NEGOTIATION = "NEGOTIATION"
    CLOSED_WON = "CLOSED_WON"
    CLOSED_LOST = "CLOSED_LOST"

class LeadIntent(str, Enum):
    """Representa el deseo o intención del cliente detectado por la IA"""
    ASK_VISIT = "ASK_VISIT"    # Quiero agendar
    ASK_PRICE = "ASK_PRICE"    # ¿Cuánto vale?
    ASK_INFO = "ASK_INFO"      # Dame más detalles
    GIVE_OFFER = "GIVE_OFFER"  # Quiero hacer una oferta
    COMPLAINT = "COMPLAINT"    # Reclamo
    UNSUBSCRIBE = "UNSUBSCRIBE" # No molestar más
    OTHER = "OTHER"

class LeadSource(str, Enum):
    WHATSAPP = "WHATSAPP"
    PORTAL_YAPO = "PORTAL_YAPO"
    PORTAL_MELI = "PORTAL_MELI"
    PORTAL_TOCTOC = "PORTAL_TOCTOC"
    MANUAL = "MANUAL"
    WEB = "WEB"

class InteractionType(str, Enum):
    BOT_MSG = "BOT_MSG"
    USER_MSG = "USER_MSG"
    HUMAN_NOTE = "HUMAN_NOTE"
    STATUS_CHANGE = "STATUS_CHANGE"
    ASSIGNMENT = "ASSIGNMENT"
    ALERT = "ALERT"

class InteractionResult(str, Enum):
    ANSWERED = "ANSWERED"
    NO_ANSWER = "NO_ANSWER"
    VISIT_AGREED = "VISIT_AGREED"
    NOT_INTERESTED = "NOT_INTERESTED"
    
# Mapping for legacy compatibility or frontend display
STAGE_LABELS = {
    PipelineStage.NEW: "Sin Atender",
    PipelineStage.CONTACTED: "Contactado",
    PipelineStage.INTERESTED: "Interesado",
    PipelineStage.VISIT_SCHEDULED: "Visita Agendada",
    PipelineStage.VISIT_DONE: "Visita Realizada",
    PipelineStage.OFFER: "Oferta",
    PipelineStage.NEGOTIATION: "Negociación",
    PipelineStage.CLOSED_WON: "Cerrado Ganado",
    PipelineStage.CLOSED_LOST: "Cerrado Perdido"
}

# Soft transition validation (Allowed NEXT steps)
# This is a guide, not a hard blocking constraint for now.
ALLOWED_TRANSITIONS = {
    PipelineStage.NEW: [PipelineStage.CONTACTED, PipelineStage.CLOSED_LOST],
    PipelineStage.CONTACTED: [PipelineStage.INTERESTED, PipelineStage.VISIT_SCHEDULED, PipelineStage.CLOSED_LOST],
    PipelineStage.INTERESTED: [PipelineStage.VISIT_SCHEDULED, PipelineStage.OFFER, PipelineStage.CLOSED_LOST],
    PipelineStage.VISIT_SCHEDULED: [PipelineStage.VISIT_DONE, PipelineStage.CLOSED_LOST],
    PipelineStage.VISIT_DONE: [PipelineStage.OFFER, PipelineStage.INTERESTED, PipelineStage.CLOSED_LOST],
    PipelineStage.OFFER: [PipelineStage.NEGOTIATION, PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST],
    PipelineStage.NEGOTIATION: [PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST, PipelineStage.OFFER],
    PipelineStage.CLOSED_WON: [], # Terminal
    PipelineStage.CLOSED_LOST: [PipelineStage.CONTACTED] # Re-activation
}
