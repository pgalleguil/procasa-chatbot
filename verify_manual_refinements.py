from chatbot.manual_entry import create_manual_lead, check_lead_duplicate
from chatbot.storage import get_db

def test_manual_entry_refinements():
    db = get_db()
    test_email = "test_manual_refine@example.com"
    test_prop = "REFINE_789"
    
    # Clean up previous tests
    db.leads.delete_many({"prospecto.email": test_email})
    
    print(f"1. Verificando duplicado inicial por EMAIL (debe ser False):")
    exists, exec_name = check_lead_duplicate(None, test_prop, test_email)
    print(f"   Resultado: {exists}, Ejecutivo: {exec_name}")
    
    print(f"\n2. Creando lead manual con SOLO EMAIL:")
    data = {
        "nombre": "Test Manual Refine",
        "email": test_email,
        "property_code": test_prop,
        "origen": "PortalInmobiliario"
    }
    result = create_manual_lead(data)
    print(f"   Resultado: {result.get('status')} - {result.get('message')}")
    
    print(f"\n3. Verificando duplicado con MISMA propiedad y EMAIL (debe ser True):")
    exists, exec_name = check_lead_duplicate(None, test_prop, test_email)
    print(f"   Resultado: {exists}, Ejecutivo: {exec_name}")
    
    print(f"\n4. Verificando que el documento tenga 'origen' y no 'channel':")
    lead = db.leads.find_one({"prospecto.email": test_email})
    print(f"   Campo 'origen': {lead.get('origen')}")
    print(f"   Campo 'channel': {lead.get('channel')}")
    print(f"   Campo 'prospecto.canal_origen': {lead.get('prospecto', {}).get('canal_origen')}")

if __name__ == "__main__":
    test_manual_entry_refinements()
