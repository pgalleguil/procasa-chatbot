"""Deterministic CRM KPI-card URLs."""

from __future__ import annotations

from urllib.parse import urlencode


STATE_CARDS = ("NEW", "GRUPO_GESTION", "GRUPO_VISITA", "GRUPO_CERRADO")


def build_crm_card_urls(query_params) -> dict[str, str]:
    current = dict(query_params)
    current.pop("page", None)
    if current.get("temperatura") == "Todos":
        current.pop("temperatura", None)

    def url(changes=None, remove=()):
        params = dict(current)
        for key in remove:
            params.pop(key, None)
        if changes:
            params.update(changes)
        params["page"] = "1"
        return "/crm?" + urlencode(params)

    urls = {"total": url(remove=("temperatura", "estado"))}
    for temperature, key in (("HOT", "hot"), ("COLD", "cold")):
        if current.get("temperatura") == temperature:
            urls[key] = url(remove=("temperatura", "estado"))
        else:
            urls[key] = url({"temperatura": temperature}, remove=("estado",))

    unassigned_active = (
        not current.get("temperatura")
        and current.get("estado") == "UNASSIGNED"
    )
    urls["unassigned"] = url(
        remove=("temperatura", "estado") if unassigned_active else ("temperatura",),
        changes=None if unassigned_active else {"estado": "UNASSIGNED"},
    )

    for state in STATE_CARDS:
        key = state.lower()
        if current.get("estado") == state:
            urls[key] = url(remove=("estado",))
        else:
            urls[key] = url({"estado": state})
        for temperature in ("HOT", "COLD"):
            urls[f"{key}_{temperature.lower()}"] = url(
                {"temperatura": temperature, "estado": state}
            )
    return urls


def build_crm_filter_urls(query_params) -> dict[str, str]:
    """URLs para quitar un filtro sin perder el resto del contexto del listado."""
    current = dict(query_params)
    current.pop("page", None)
    if current.get("temperatura") == "Todos":
        current.pop("temperatura", None)

    def without(*keys):
        params = dict(current)
        for key in keys:
            params.pop(key, None)
        params["page"] = "1"
        return "/crm?" + urlencode(params)

    return {
        "temperature": without("temperatura"),
        "state": without("estado"),
        "executive": without("ejecutivo"),
        "search": without("busqueda"),
        "order": without("orden"),
        "clear": "/crm",
    }
