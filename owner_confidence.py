"""
Helper functions for owner confidence display.
Independent module with no circular imports.
"""


def build_owner_confidence(classification, state):
    """Retorna el texto a mostrar en la columna Confianza dueño."""
    sem = classification.get("semantic_check", {}) or {}
    sem_status = sem.get("status") if isinstance(sem, dict) else None
    conf = classification.get("confidence")
    if state == "DUEÑO_SEGURO" and sem_status == "SKIPPED_EXPLICIT_OWNER":
        return "Dueño explícito"
    if state == "DUEÑO_SEGURO" and sem_status == "VALID" and conf is not None:
        try:
            pct = int(float(conf) * 100)
            return f"{pct}%"
        except (ValueError, TypeError):
            pass
    return "\u2014"


def build_owner_confidence_sort(classification, state):
    """Valor para ordenar: menor = mayor prioridad.
    0=Dueño explícito, 1-99=porcentaje invertido, 999=INCIERTO/sin confianza."""
    sem = classification.get("semantic_check", {}) or {}
    sem_status = sem.get("status") if isinstance(sem, dict) else None
    conf = classification.get("confidence")
    if state == "DUEÑO_SEGURO" and sem_status == "SKIPPED_EXPLICIT_OWNER":
        return 0
    if state == "DUEÑO_SEGURO" and sem_status == "VALID" and conf is not None:
        try:
            return 100 - int(float(conf) * 100)
        except (ValueError, TypeError):
            pass
    return 999


def build_owner_confidence_type(classification, state):
    """Tipo de confianza: explicit, percentage, none."""
    sem = classification.get("semantic_check", {}) or {}
    sem_status = sem.get("status") if isinstance(sem, dict) else None
    conf = classification.get("confidence")
    if state == "DUEÑO_SEGURO" and sem_status == "SKIPPED_EXPLICIT_OWNER":
        return "explicit"
    if state == "DUEÑO_SEGURO" and sem_status == "VALID" and conf is not None:
        try:
            float(conf)
            return "percentage"
        except (ValueError, TypeError):
            pass
    return "none"
