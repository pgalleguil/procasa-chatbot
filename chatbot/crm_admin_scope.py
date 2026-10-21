"""Fail-closed office authorization for supervisor assignment controls."""

from __future__ import annotations

from collections.abc import Mapping
import unicodedata
from typing import Any

from .crm_permissions import can_supervise_crm_assignments


_OFFICE_KEYS = frozenset({
    "office_id", "oficina_id", "office_ids", "oficina_ids",
    "offices", "oficinas", "office", "oficina", "office_name",
    "oficina_nombre", "crm_office_scope", "crm_supervisor_office_scope",
    "assignment_office_scope",
})
_NESTED_KEYS = ("scope", "permisos", "permissions", "crm", "assignment",
                "propiedad", "datos_propiedad", "prospecto", "property",
                "property_data", "oficina_data", "estado")
_VALUE_KEYS = ("id", "_id", "name", "nombre", "codigo", "office_id",
               "oficina_id", "office_name", "oficina_nombre")


def _token(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.casefold().split())


def _flatten(value: Any) -> set[str]:
    if value is None or value is False or value == "":
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        result: set[str] = set()
        for item in value:
            result.update(_flatten(item))
        return result
    if isinstance(value, Mapping):
        result: set[str] = set()
        for key in _VALUE_KEYS:
            if key in value:
                result.update(_flatten(value.get(key)))
        return result
    value_token = _token(value)
    return {value_token} if value_token else set()


def office_values(document: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(document, Mapping):
        return set()
    values: set[str] = set()
    for key in _OFFICE_KEYS:
        if key in document:
            values.update(_flatten(document.get(key)))
    for key in _NESTED_KEYS:
        nested = document.get(key)
        if isinstance(nested, Mapping):
            values.update(office_values(nested))
        elif isinstance(nested, (list, tuple)):
            for item in nested:
                if isinstance(item, Mapping):
                    values.update(office_values(item))
    return values


def _explicit_all_offices(user: Mapping[str, Any]) -> bool:
    if any(user.get(key) is True for key in ("all_offices", "todas_las_oficinas", "crm_all_offices")):
        return True
    for key in ("crm_assignment_scope", "assignment_scope", "office_scope_mode"):
        if _token(user.get(key)) in {"all", "all_offices", "global", "todas", "todas_las_oficinas"}:
            return True
    for key in ("crm_office_scope", "crm_supervisor_office_scope", "scope"):
        value = user.get(key)
        if isinstance(value, Mapping) and any(value.get(flag) is True for flag in ("all", "all_offices", "todas_las_oficinas")):
            return True
    return False


def _property_office_values(db: Any, lead: Mapping[str, Any], session: Any = None) -> set[str]:
    direct = office_values(lead)
    if direct:
        return direct
    prospect = lead.get("prospecto") if isinstance(lead.get("prospecto"), Mapping) else {}
    property_data = lead.get("datos_propiedad") if isinstance(lead.get("datos_propiedad"), Mapping) else {}
    codes = []
    for source in (lead, prospect, property_data):
        for key in ("property_code", "codigo_propiedad", "codigo"):
            value = str(source.get(key) or "").strip()
            if value and value not in codes:
                codes.append(value)
    if not codes:
        return set()
    for collection_name in ("universo_cartera_prop360", "propiedades", "properties"):
        try:
            collection = db[collection_name]
        except Exception:
            continue
        for code in codes:
            for field in ("codigo", "codigo_propiedad", "property_code"):
                try:
                    query = {field: code}
                    if session is None:
                        prop = collection.find_one(query)
                    else:
                        prop = collection.find_one(query, session=session)
                except Exception:
                    prop = None
                if prop:
                    found = office_values(prop)
                    if found:
                        return found
    return set()


def _target_property_office_values(
    db: Any, lead: Mapping[str, Any], target: Mapping[str, Any], session: Any = None,
) -> set[str]:
    """Infer one target's office only when the canonical listing names them as its agent."""
    prospect = lead.get("prospecto") if isinstance(lead.get("prospecto"), Mapping) else {}
    property_data = lead.get("datos_propiedad") if isinstance(lead.get("datos_propiedad"), Mapping) else {}
    codes = []
    for source in (lead, prospect, property_data):
        for key in ("property_code", "codigo_propiedad", "codigo"):
            value = str(source.get(key) or "").strip()
            if value and value not in codes:
                codes.append(value)
    target_name = _token(target.get("nombre"))
    if not codes or not target_name:
        return set()
    for collection_name in ("universo_cartera_prop360", "propiedades", "properties"):
        try:
            collection = db[collection_name]
        except Exception:
            continue
        for code in codes:
            for field in ("codigo", "codigo_propiedad", "property_code"):
                try:
                    query = {field: code}
                    prop = collection.find_one(query) if session is None else collection.find_one(query, session=session)
                except Exception:
                    prop = None
                if not prop:
                    continue
                estado = prop.get("estado") if isinstance(prop.get("estado"), Mapping) else {}
                property_agent = _token(
                    estado.get("ejecutivo") or prop.get("ejecutivo")
                    or prop.get("assigned_to_display_name")
                )
                if property_agent == target_name:
                    return office_values(prop)
    return set()


def office_access_error(db: Any, supervisor: Mapping[str, Any], lead: Mapping[str, Any],
                        target: Mapping[str, Any] | None = None, session: Any = None) -> str | None:
    """Return a stable denial code unless supervisor, lead and target share office scope."""
    if not can_supervise_crm_assignments(supervisor.get("rol")):
        return "supervisor_only"
    lead_offices = _property_office_values(db, lead, session=session)
    if not lead_offices:
        return "lead_office_unresolved"
    supervisor_offices = office_values(supervisor)
    if not supervisor_offices and not _explicit_all_offices(supervisor):
        return "supervisor_office_scope_missing"
    if not _explicit_all_offices(supervisor) and not supervisor_offices.intersection(lead_offices):
        return "office_scope_forbidden"
    if target is not None:
        target_offices = office_values(target)
        if not target_offices:
            target_offices = _target_property_office_values(db, lead, target, session=session)
        if not target_offices or not target_offices.intersection(lead_offices):
            return "target_office_ineligible"
    return None
