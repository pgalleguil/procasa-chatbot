import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from chatbot.crm_filters import build_crm_card_urls, build_crm_filter_urls
from chatbot import crm_service as crm_service_module
from chatbot.crm_permissions import (
    can_administer_leads,
    lead_is_assigned_to_user,
    payload_attempts_reassignment,
)
from chatbot.crm_service import CrmService
from chatbot.storage import COLLECTION_CONVERSATIONS


def _render_list(*, administrative: bool) -> str:
    request = SimpleNamespace(query_params={"temperatura": "HOT"})
    kpis = SimpleNamespace(
        total=1,
        scope_total=1,
        hot=1,
        cold=0,
        sin_asignar=0,
        sin_asignar_global=0,
        nuevo=1,
        gestion=0,
        visita=0,
        cerrado=0,
        managed=0,
        managed_percent=0.0,
        hot_percent=100.0,
        cold_percent=0.0,
        sin_asignar_percent=0.0,
        nuevo_percent=100.0,
        gestion_percent=0.0,
        visita_percent=0.0,
        cerrado_percent=0.0,
        nuevo_hot=1,
        nuevo_cold=0,
        gestion_hot=0,
        gestion_cold=0,
        visita_hot=0,
        visita_cold=0,
        cerrado_hot=0,
        cerrado_cold=0,
    )
    lead = SimpleNamespace(
        phone="56911111111",
        nombre="Cliente Prueba",
        lead_temperature_effective="HOT",
        sla_status="pending",
        sla_label="En plazo",
        cold_age_label="Asignado hoy",
        whatsapp_display="+56 9 1111 1111",
        codigo_propiedad="1234",
        url_propiedad="https://www.procasa.cl/1234",
        estado_badge="Sin Atender",
        ultima_accion_titulo="Sin gestión registrada",
        tiempo_relativo="Ahora",
        ultima_accion_nota="",
        ejecutivo_nombre="Mariela Arriagada",
        fecha_asignacion_relativa="Hoy",
    )
    return Environment(loader=FileSystemLoader("templates"), autoescape=True).get_template(
        "crm_leads_list.html"
    ).render(
        partial=True,
        request=request,
        leads=[lead],
        kpis=kpis,
        user_role="admin" if administrative else "agente",
        user_name="Administrador" if administrative else "Mariela Arriagada",
        can_administer_leads=administrative,
        executives=["Mariela Arriagada", "Erika Garrido"] if administrative else [],
        current_ejecutivo="Todos" if administrative else "Mariela Arriagada",
        current_temperatura="HOT",
        crm_version=1,
        card_urls=build_crm_card_urls(request.query_params),
        filter_urls=build_crm_filter_urls(request.query_params),
        pagination_base_url="/crm?temperatura=HOT&",
        pagination={
            "total_count": 1,
            "current_page": 1,
            "total_pages": 1,
            "has_prev": False,
            "has_more": False,
        },
    )


@pytest.mark.parametrize("role", ["admin", "supervisor", "jefatura"])
def test_crm_administrative_roles_can_use_real_list_actions(role):
    assert can_administer_leads(role)


@pytest.mark.parametrize("role", ["agente", "ejecutivo", "jefe", "", None])
def test_crm_regular_executives_cannot_administer_leads(role):
    assert not can_administer_leads(role)


def test_crm_ownership_accepts_legacy_shortened_assignment_but_not_another_agent():
    lead = {"ejecutivo_asignado": "Raquel Cheneaux"}
    assert lead_is_assigned_to_user(lead, {"nombre": "Raquel Cheneaux Valz"})
    assert not lead_is_assigned_to_user(lead, {"nombre": "Mariela Arriagada"})


def test_manual_payload_cannot_hide_reassignment_fields_in_nested_data():
    assert payload_attempts_reassignment({"ejecutivo_asignado": "Otra Persona"})
    assert payload_attempts_reassignment({"details_json": {"prospecto.ejecutivo": "Otra"}})
    assert not payload_attempts_reassignment({"estado": "gestion", "notas": "Seguimiento"})


def test_regular_executive_has_no_three_dot_menu_or_list_mutations():
    rendered = _render_list(administrative=False)
    assert 'class="row-actions"' not in rendered
    assert "fa-ellipsis-vertical" not in rendered
    assert "Reasignar lead" not in rendered
    assert "Marcar como duplicado" not in rendered
    assert ">Archivar<" not in rendered
    assert "Cambiar estado" not in rendered
    assert 'tabindex="0" role="link"' in rendered
    assert 'href="tel:56911111111"' in rendered


def test_administrator_menu_is_absent_until_a_complete_action_set_is_available():
    rendered = _render_list(administrative=True)
    assert 'class="row-actions"' not in rendered
    assert "fa-ellipsis-vertical" not in rendered
    assert "col-row-actions" not in rendered
    assert "Reasignar lead" not in rendered
    assert "Marcar como duplicado" not in rendered
    assert ">Archivar<" not in rendered
    assert "Ver auditoría" not in rendered
    assert 'tabindex="0" role="link"' in rendered


def test_backend_routes_enforce_admin_and_ownership_permissions():
    source = Path("webhook.py").read_text(encoding="utf-8")
    update_block = source[source.index('async def api_crm_update_lead'):source.index('@app.post("/api/crm/admin/reassign")')]
    reassign_block = source[source.index('async def api_crm_admin_reassign'):source.index('@app.post("/api/crm/admin/mark-duplicate")')]
    duplicate_block = source[source.index('async def api_crm_admin_mark_duplicate'):source.index('@app.post("/api/crm/admin/archive")')]
    archive_block = source[source.index('async def api_crm_admin_archive'):source.index('@app.post("/api/crm/notes")')]

    assert "await _get_authorized_crm_lead(request, phone)" in update_block
    assert "payload_attempts_reassignment(data)" in update_block
    assert "status_code=403" in update_block
    assert "administrative=True" in reassign_block
    assert "CrmService.assign_executive" in reassign_block
    assert "administrative=True" in duplicate_block
    assert "CrmService.mark_duplicate" in duplicate_block
    assert "administrative=True" in archive_block
    assert "CrmService.archive_lead" in archive_block


def test_row_navigation_ignores_internal_links_and_supports_keyboard():
    template = Path("templates/crm_leads_list.html").read_text(encoding="utf-8")
    assert "rowEventComesFromInteractiveElement" in template
    assert "a, button, input, select, textarea, summary, details" in template
    assert "event.target !== row" in template
    assert "['Enter', ' '].includes(event.key)" in template
    assert "window.location.assign(row.dataset.leadUrl)" in template
    assert "onclick=\"window.location.href='/crm/lead/" not in template


def test_archived_leads_are_excluded_from_the_active_list_universe():
    source = Path("api_crm.py").read_text(encoding="utf-8")
    assert "not can_administer_leads(user_role)" in source
    assert '{"stage": {"$ne": "ARCHIVED"}}' in source
    assert '{"pipeline_stage": {"$ne": "ARCHIVED"}}' in source
    assert '{"archived_at": {"$exists": False}}' in source


def test_crm_route_and_lead_list_signature_stay_compatible():
    """Regression for the production /crm TypeError on an unsupported kwarg."""
    api_source = Path("api_crm.py").read_text(encoding="utf-8")
    webhook_source = Path("webhook.py").read_text(encoding="utf-8")
    api_tree = ast.parse(api_source)
    webhook_tree = ast.parse(webhook_source)

    api_function = next(
        node for node in api_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "get_crm_leads_list"
    )
    api_parameters = {
        arg.arg for arg in (*api_function.args.posonlyargs, *api_function.args.args,
                            *api_function.args.kwonlyargs)
    }
    assert "user_id" not in api_parameters

    render_function = next(
        node for node in webhook_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_render_crm_list"
    )
    list_calls = [
        node for node in ast.walk(render_function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_crm_leads_list"
    ]
    assert len(list_calls) == 1
    call_keywords = {keyword.arg for keyword in list_calls[0].keywords if keyword.arg}
    assert call_keywords <= api_parameters
    assert {"user_role", "user_name", "ejecutivo_filter", "temperatura_filter"} <= call_keywords

    # Keep this assertion tied to the runtime function, not only duplicated
    # source text, so a signature drift is caught before /crm is deployed.
    from api_crm import get_crm_leads_list
    assert call_keywords <= set(inspect.signature(get_crm_leads_list).parameters)


def test_crm_list_scope_and_filters_remain_after_contract_fix():
    source = Path("api_crm.py").read_text(encoding="utf-8")
    assert "if not can_administer_leads(user_role) and user_name:" in source
    assert 'elif ejecutivo_filter and ejecutivo_filter != "Todos":' in source
    assert 'if busqueda and busqueda.strip():' in source
    assert 'if property_code and property_code.strip():' in source
    assert '{"is_test": {"$ne": True}}' in source


def test_crm_sla_security_and_old_owner_lock_remain_enforced():
    source = Path("webhook.py").read_text(encoding="utf-8")
    assert "CRM_SLA_SECURITY_LAYER_ENABLED" in source
    assert "resolve_crm_lead_access_context" in source
    assert "LeadReassignedSlaLockedError" in Path("chatbot/crm_management.py").read_text(encoding="utf-8")
    assert "LEAD_REASSIGNED_SLA_LOCKED" in Path("chatbot/crm_management.py").read_text(encoding="utf-8")


class _FakeCollection:
    def __init__(self, docs=None):
        self.query = None
        self.update = None
        self.docs = docs or []

    def find_one(self, query, *args, **kwargs):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                return doc
        return None

    def update_one(self, query, update):
        self.query = query
        self.update = update
        return SimpleNamespace(modified_count=1)


def test_admin_reassignment_service_targets_the_resolved_lead_and_audits_actor(monkeypatch):
    collection = _FakeCollection()
    events = []
    monkeypatch.setattr(crm_service_module, "get_db", lambda: {COLLECTION_CONVERSATIONS: collection})
    monkeypatch.setattr(
        CrmService,
        "get_lead",
        staticmethod(lambda _phone: {"_id": "lead-1", "prospecto": {}}),
    )
    monkeypatch.setattr(
        "chatbot.lead_router.get_next_business_slot",
        lambda value: value,
    )
    monkeypatch.setattr(
        crm_service_module,
        "log_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert CrmService.assign_executive(
        "56911111111",
        "Erika Garrido",
        method="crm_list_admin",
        actor="Supervisora",
    )
    assert collection.query == {"_id": "lead-1"}
    assert collection.update["$set"]["ejecutivo_asignado"] == "Erika Garrido"
    assert events[0][0][2] == "Supervisora"
    assert events[0][0][3]["method"] == "crm_list_admin"


def test_admin_archive_service_is_non_destructive_and_keeps_audit_history(monkeypatch):
    collection = _FakeCollection()
    cycle_collection = _FakeCollection()
    events = []
    monkeypatch.setattr(crm_service_module, "get_db", lambda: {
        COLLECTION_CONVERSATIONS: collection,
        "crm_assignment_cycles": cycle_collection,
        "crm_management_results": _FakeCollection(),
        "crm_notifications_v1": _FakeCollection(),
        "crm_events": _FakeCollection(),
    })
    monkeypatch.setattr(
        CrmService,
        "get_lead",
        staticmethod(lambda _phone: {"_id": "lead-2", "stage": "CONTACTED"}),
    )
    monkeypatch.setattr(
        crm_service_module,
        "log_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert CrmService.archive_lead("56922222222", actor="Jefatura", reason="Duplicado")
    assert collection.query == {"_id": "lead-2"}
    assert "$unset" not in collection.update
    assert collection.update["$set"]["stage"] == "ARCHIVED"
    assert collection.update["$push"]["stage_history"]["from"] == "CONTACTED"
    assert events[0][0][2] == "Jefatura"


def test_admin_duplicate_service_preserves_record_and_records_duplicate_audit(monkeypatch):
    collection = _FakeCollection()
    events = []
    monkeypatch.setattr(crm_service_module, "get_db", lambda: {COLLECTION_CONVERSATIONS: collection})
    monkeypatch.setattr(
        CrmService,
        "get_lead",
        staticmethod(lambda _phone: {"_id": "lead-3", "stage": "NEW"}),
    )
    monkeypatch.setattr(
        crm_service_module,
        "log_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert CrmService.mark_duplicate("56933333333", actor="Supervisora")
    assert collection.query == {"_id": "lead-3"}
    assert "$unset" not in collection.update
    assert collection.update["$set"]["is_duplicate"] is True
    assert collection.update["$set"]["stage"] == "ARCHIVED"
    assert events[0][0][3]["action"] == "mark_duplicate"
