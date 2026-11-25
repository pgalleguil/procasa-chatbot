"""Conservative copy for commercial follow-up attempts. No model, no I/O.

Only use after the independent eligibility policy and verified context checks.
Never infer a property feature, financing status, availability or appointment.
"""
from .executive_silence_policy import _plain

def followup_copy(*, scenario: str, attempt: int, last_agent: str = "") -> str:
    if scenario not in {"INITIAL_NO_REPLY", "DIALOGUE_INTERRUPTED"}:
        raise ValueError("unknown_scenario")
    if type(attempt) is not int or attempt not in (1, 2, 3):
        raise ValueError("invalid_attempt")
    prefix = "Hola, seguimiento automático de PROCASA. "
    if attempt == 3:
        return prefix + ("¿Sigue necesitando ayuda en su búsqueda inmobiliaria? "
                         "Si ya encontró propiedad, no es necesario responder. "
                         "No insistiremos con más seguimientos automáticos.")
    if attempt == 2:
        return prefix + ("¿Continúa buscando propiedad o cambió lo que necesita? "
                         "Si le acomoda, podemos retomar su consulta o revisar alternativas.")
    if scenario == "INITIAL_NO_REPLY":
        return prefix + ("¿Pudo revisar la propiedad por la que consultó? "
                         "Si tiene dudas sobre sus características o necesita más información, "
                         "quedamos disponibles para ayudarle.")
    context = _plain(last_agent)
    if "presupuesto" in context:
        return prefix + ("Para retomar su búsqueda, ¿nos podría indicar el presupuesto "
                         "que le acomoda? Así podremos orientar mejor las alternativas.")
    if any(word in context for word in ("alternativas", "opciones", "revisar la propiedad",
                                       "parecieron las caracteristicas")):
        return prefix + ("¿Pudo revisar lo que le compartimos? Si algo no se ajusta a "
                         "lo que necesita, cuéntenos y podremos orientar mejor su búsqueda.")
    return prefix + ("Retomamos la conversación sobre su consulta inmobiliaria. "
                     "¿Le quedó alguna duda o necesita ayuda para avanzar?")
