"""Regression coverage for the CRM lead-detail authorization import and route."""

from __future__ import annotations

import mongomock
from bson import ObjectId
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient


def _lead(lead_id: ObjectId, *, assigned_name: str = "Ana Agente") -> dict:
    return {
        "_id": lead_id,
        "phone": "56912345678",
        "nombre": "Lead de regresión",
        "ejecutivo_asignado": assigned_name,
        "prospecto": {"ejecutivo": assigned_name, "owner_name": "Propietario"},
    }


def _detail(lead: dict) -> dict:
    return {
        "_id": lead["_id"],
        "phone": lead["phone"],
        "nombre": lead["nombre"],
        "ejecutivo_asignado": lead["ejecutivo_asignado"],
        "historial": [],
        "datos_propiedad": {},
    }


def test_crm_lead_access_module_exports_exact_webhook_contract():
    import chatbot.crm_lead_access as access

    assert callable(access.resolve_crm_lead_access_context)
    assert callable(access.safe_access_error)
    assert callable(access.sanitize_lead_for_access)
    assert isinstance(access.LOCKED_LEAD_MESSAGE, str)


def test_webhook_imports_with_crm_detail_access_module():
    import webhook

    assert webhook.view_crm_detail_by_id


def test_crm_list_and_detail_access_matrix(monkeypatch):
    import webhook

    db = mongomock.MongoClient()["crm_regression"]
    legacy_id = ObjectId()
    reassigned_id = ObjectId()
    legacy_lead = _lead(legacy_id)
    reassigned_lead = _lead(reassigned_id)
    reassigned_lead.update({
        "assigned_by": "sla_reassignment",
        "reassigned_by_sla": True,
        "lifecycle": {"current_assignment_cycle_id": "cycle-current"},
    })
    db["leads"].insert_many([legacy_lead, reassigned_lead])
    # Deliberately store the cycle lead_id as a string while the lead _id is an
    # ObjectId. This is the production identity boundary being protected.
    db["crm_assignment_cycles"].insert_one({
        "assignment_cycle_id": "cycle-current",
        "lead_id": str(reassigned_id),
        "assigned_to_user_id": "owner-id",
        "assigned_to_display_name": "Ana Agente",
        "cycle_status": "active",
        "unassigned_at": None,
        "automatic_reassignment_number": 1,
    })

    current_user = {"doc": {"_id": "owner-id", "nombre": "Ana Agente", "rol": "agente"}}

    async def fake_current_user_doc(request):
        return current_user["doc"]

    async def fake_render_crm_list(request, **kwargs):
        return HTMLResponse("<main>CRM list</main>")

    class FakeTemplates:
        def TemplateResponse(self, request, template_name, context):
            return HTMLResponse("<main>Detalle de Lead</main>")

    monkeypatch.setattr(webhook, "get_current_user_doc", fake_current_user_doc)
    monkeypatch.setattr(webhook, "get_lead_detail_data", lambda phone, lead_doc=None: _detail(lead_doc))
    monkeypatch.setattr(webhook, "_render_crm_list", fake_render_crm_list)
    monkeypatch.setattr(webhook, "templates", FakeTemplates())
    monkeypatch.setattr(webhook.Config, "CRM_SLA_SECURITY_LAYER_ENABLED", True)
    monkeypatch.setattr("chatbot.storage.get_db", lambda: db)

    client = TestClient(webhook.app, raise_server_exceptions=False)

    assert client.get("/crm").status_code == 200

    # The assigned executive can open both legacy and canonical records.
    assert client.get(f"/crm/lead-id/{legacy_id}").status_code == 200
    assert client.get(f"/crm/lead-id/{reassigned_id}").status_code == 200

    # Supervisor access is retained for the canonical/reassigned record.
    current_user["doc"] = {"_id": "supervisor-id", "nombre": "Supervisor", "rol": "supervisor"}
    assert client.get(f"/crm/lead-id/{reassigned_id}").status_code == 200

    # A different executive is rejected by the canonical lock, not a 500.
    current_user["doc"] = {"_id": "other-id", "nombre": "Otra Ejecutiva", "rol": "agente"}
    wrong_owner = client.get(f"/crm/lead-id/{reassigned_id}")
    assert wrong_owner.status_code == 409
    assert "Lead no disponible" in wrong_owner.text
    assert "ModuleNotFoundError" not in wrong_owner.text

    # Missing leads remain a controlled 404.
    missing = client.get(f"/crm/lead-id/{ObjectId()}")
    assert missing.status_code == 404
    assert missing.status_code != 500
