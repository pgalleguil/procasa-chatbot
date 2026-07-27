from chatbot.crm_permissions import can_administer_leads, lead_is_assigned_to_user

def lead(): return {"ejecutivo_asignado": "Susana Ensignia"}
def test_assigned_executive_has_backend_ownership():
    assert lead_is_assigned_to_user(lead(), {"nombre": "Susana Ensignia", "rol": "agente"})
def test_other_executive_has_no_full_phone_access():
    assert not lead_is_assigned_to_user(lead(), {"nombre": "Erika Garrido", "rol": "agente"})
def test_authorized_admin_has_full_phone_access():
    assert can_administer_leads("admin") and can_administer_leads("supervisor")
def test_anonymous_has_no_ownership():
    assert not lead_is_assigned_to_user(lead(), None)
