import os
import sys
from chatbot.storage import get_db

db = get_db()
pdf_dir = r"C:/Users/pgall/Desktop/Tasaciones"

# Load ALL active properties in Sucre portfolio
props = list(db.propiedades_accionables.find({
    "oficina": "PROCASA SUCRE",
    "precio_publicado_uf": {"$gt": 0}
}))

print(f"Total active properties loaded: {len(props)}")

# Enriched code
pdf_codes = set()
if os.path.exists(pdf_dir):
    for f in os.listdir(pdf_dir):
        if f.endswith(".pdf"):
            pdf_codes.add(f.replace(".pdf", "").strip())

# Joins
by_code = {str(x["codigo_propiedad"]): x for x in props}

for u in db.universo_cartera.find({"codigo": {"$in": list(by_code.keys())}}):
    c = str(u["codigo"])
    if c in by_code:
        by_code[c]["email_propietario"] = (u.get("email_propietario") or "").strip()
        by_code[c]["ejecutivo"] = (u.get("ejecutivo") or "").strip()

# Enriched tasaciones
for t in db.tasaciones.find({"codigo_propiedad": {"$in": list(by_code.keys())}}):
    c = str(t["codigo_propiedad"]).strip()
    if c in by_code:
        tas = t.get("tasacion_online") or {}
        vcom = tas.get("valor_comercial") or {}
        varr = tas.get("arriendo_estimado") or {}
        vmm = tas.get("valor_minimo_maximo") or {}
        val_com_uf = vcom.get("uf")
        val_arr_uf = varr.get("uf")
        min_uf = vmm.get("precio_minimo_uf")
        max_uf = vmm.get("precio_maximo_uf")
        
        if min_uf not in (None, "", 0, "0"):
            by_code[c]["tasacion_comercial_min_uf"] = float(min_uf)
        if max_uf not in (None, "", 0, "0"):
            by_code[c]["tasacion_comercial_max_uf"] = float(max_uf)
        if val_com_uf not in (None, "", 0, "0"):
            if by_code[c].get("tasacion_comercial_min_uf") in (None, "", 0, "0"):
                by_code[c]["tasacion_comercial_min_uf"] = float(val_com_uf)
            if by_code[c].get("tasacion_comercial_max_uf") in (None, "", 0, "0"):
                by_code[c]["tasacion_comercial_max_uf"] = float(val_com_uf)
        if val_arr_uf not in (None, "", 0, "0"):
            by_code[c]["tasacion_arriendo_uf"] = float(val_arr_uf)

def calculate_brecha(p):
    precio = float(p.get("precio_publicado_uf") or 0)
    operacion = str(p.get("operacion") or "").strip().lower()
    tas_arriendo = p.get("tasacion_arriendo_uf")
    if operacion == "arriendo" and tas_arriendo and tas_arriendo > 0 and precio > 0:
        return ((precio - tas_arriendo) / tas_arriendo) * 100.0
        
    tas_min = p.get("tasacion_comercial_min_uf")
    tas_max = p.get("tasacion_comercial_max_uf")
    tas_ref = None
    if tas_min is not None and tas_max is not None:
        tas_ref = (tas_min + tas_max) / 2.0
    elif tas_min is not None:
        tas_ref = tas_min
    elif tas_max is not None:
        tas_ref = tas_max
        
    if not tas_ref or tas_ref <= 0 or precio <= 0:
        return None
    return ((precio - tas_ref) / tas_ref) * 100.0

total_props = len(props)
con_email = 0
sin_email = 0
ruta_tasacion = 0
ruta_comunal = 0

email_by_action = {}
no_email_by_action = {}

for c, p in by_code.items():
    email = (p.get("email_propietario") or "").strip()
    action = p.get("accion_recomendada", "unknown")
    
    if email:
        con_email += 1
        email_by_action[action] = email_by_action.get(action, 0) + 1
        
        # New proposed routing logic:
        # A property goes to Ruta de Tasación Individual only if:
        # 1. We have a physical PDF on disk: f"{c}.pdf" in pdf_codes
        # 2. AND we have positive appraisal gap: brecha > 0
        brecha = calculate_brecha(p)
        has_pdf = c in pdf_codes
        
        if has_pdf and brecha is not None and brecha > 0:
            ruta_tasacion += 1
        else:
            ruta_comunal += 1
    else:
        sin_email += 1
        no_email_by_action[action] = no_email_by_action.get(action, 0) + 1

print("\n--- SIMULATION RESULTS ---")
print(f"Total Properties Loaded:        {total_props}")
print(f"Properties with Email:          {con_email}")
print(f"Properties without Email:       {sin_email}")
print("-" * 40)
print("Routing of properties with email:")
print(f"  -> Ruta de Tasación Individual: {ruta_tasacion}")
print(f"  -> Ruta de Inteligencia Comunal: {ruta_comunal}")
print("-" * 40)
print("Emails by action:")
for act, cnt in email_by_action.items():
    print(f"  - {act}: {cnt} with email")
print("\nNo-emails by action:")
for act, cnt in no_email_by_action.items():
    print(f"  - {act}: {cnt} without email")
