import re
from pathlib import Path

BASE_DIR = Path(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')

# 1. Update HTML layout (visita_dashboard.html)
html_path = BASE_DIR / 'templates' / 'visita_dashboard.html'
with open(html_path, 'r', encoding='utf-8') as f:
    html = f.read()

# Make all inputs col-lg-6 col-md-6 instead of col-12
html = re.sub(r'<div class="col-12">', '<div class="col-lg-6 col-md-6">', html)

# Fix Comuna structure by adding position-relative
comuna_old = '''<div class="col-lg-6 col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Comuna <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_comuna" id="comunaInput"'''
comuna_new = '''<div class="col-lg-6 col-md-6 position-relative">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Comuna <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_comuna" id="comunaInput"'''
html = html.replace(comuna_old, comuna_new)

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html)

# 2. Update api_visitas.py
api_path = BASE_DIR / 'api_visitas.py'
with open(api_path, 'r', encoding='utf-8') as f:
    api_text = f.read()

# I will write a simple helper that is called in preview and create
fetch_logic = '''
async def _enrich_with_property_data(data: dict) -> dict:
    prop_code = data.get("property_code", "").strip()
    if prop_code:
        try:
            from chatbot.storage import get_async_db
            adb = get_async_db()
            prop_data = await adb["universo_cartera"].find_one({"codigo": prop_code})
            if prop_data:
                data["property_comuna"] = prop_data.get("comuna", "")
                data["property_region"] = prop_data.get("region", "")
                data["property_tipo"] = prop_data.get("tipo", "")
                data["precio"] = prop_data.get("precio", "")
                data["operacion"] = prop_data.get("operacion", "")
        except Exception as e:
            pass
    return data

@router.post("/api/preview")'''

api_text = api_text.replace('@router.post("/api/preview")', fetch_logic)

# Call the helper in preview
preview_old = '''data = _normalize_visita_fields(await request.json())
        pdf_bytes = await _run_blocking(PDFGenerator.generate_original_contract, data)'''
preview_new = '''data = _normalize_visita_fields(await request.json())
        data = await _enrich_with_property_data(data)
        pdf_bytes = await _run_blocking(PDFGenerator.generate_original_contract, data)'''
api_text = api_text.replace(preview_old, preview_new)

# Call the helper in create
create_old = '''data = _normalize_visita_fields(await request.json())
        from chatbot.storage import get_async_db'''
create_new = '''data = _normalize_visita_fields(await request.json())
        data = await _enrich_with_property_data(data)
        from chatbot.storage import get_async_db'''
api_text = api_text.replace(create_old, create_new)

# Add operacion to doc in create
doc_old = '''"tipo": data.get("property_tipo", ""),
                "precio": data.get("precio", "")'''
doc_new = '''"tipo": data.get("property_tipo", ""),
                "precio": data.get("precio", ""),
                "operacion": data.get("operacion", "")'''
api_text = api_text.replace(doc_old, doc_new)

with open(api_path, 'w', encoding='utf-8') as f:
    f.write(api_text)

print("HTML and API updated")
