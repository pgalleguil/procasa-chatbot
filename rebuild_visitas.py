import re

# 1. Copy contract_dashboard.html content
with open(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\contract_dashboard.html', 'r', encoding='utf-8') as f:
    text = f.read()

# 2. General text replacements
text = text.replace('Módulo de Convenios', 'Módulo de Órdenes de Visita')
text = text.replace('Convenios Emitidos', 'Órdenes de Visita Emitidas')
text = text.replace('Generar Nuevo Convenio', 'Generar Nueva Orden de Visita')
text = text.replace('Ver Convenio', 'Ver Orden de Visita')
text = text.replace('/contracts/dashboard', '/visitas/dashboard')

# 3. Variable replacements (Jinja & JS)
text = text.replace('contractCode', 'visitaCode')
text = text.replace('contract.contract_code', 'visita.visita_code')
text = text.replace('"visita_code": visita.visita_code', '"visita_code": visita.visita_code')
text = text.replace('for contract in contracts', 'for visita in visitas')
text = text.replace('contract.status', 'visita.status')
text = text.replace('contract.created_at', 'visita.created_at')
text = text.replace('contract.executive_display', 'visita.executive_display')
text = text.replace('contract.property_data', 'visita.property_data')
text = text.replace('contract.client_data', 'visita.client_data')
text = text.replace('contract.phone', 'visita.phone')
text = text.replace('contract.origen', 'visita.origen')
text = text.replace('contract.edit_data', 'visita.edit_data')
text = text.replace('contracts | length', 'visitas | length')
text = text.replace('contract.property_code', 'visita.property_code')

# Fix table row data attrs
text = text.replace('data-contract-code="{{ visita.visita_code }}"', 'data-visita-code="{{ visita.visita_code }}"')
text = text.replace('data-contract-status="{{ visita.status }}"', 'data-visita-status="{{ visita.status }}"')

# API endpoints
text = text.replace('/contracts/api/', '/visitas/api/')
text = text.replace('/contracts/verify/', '/visitas/verify/')

# 4. Form replacement
form_start = text.find('<form id="formNewContract"')
form_end = text.find('</form>', form_start) + len('</form>')

new_form = '''<form id="formNewContract" class="needs-validation" novalidate>
                                <div id="formErrors" class="alert alert-danger d-none mb-3 py-2 text-sm fw-bold"></div>
                                <input type="hidden" name="executive" value="{{ user_username }}">
                                <div class="row g-3 mb-3">
                                    <div class="col-12">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Nombre del Cliente <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_nombre" placeholder="Ej: Juan Pérez" required onblur="capitalizeName(this); validateField(this); triggerPreview();">
                                        <div class="invalid-feedback">Ingresa el nombre completo.</div>
                                    </div>
                                    <div class="col-12">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">RUT Cliente <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_rut" placeholder="12345678-9" required oninput="formatRut(this)" onblur="validateField(this); triggerPreview();">
                                        <div class="invalid-feedback">Ingresa un RUT válido.</div>
                                    </div>
                                    <div class="col-12">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Domicilio <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_direccion" placeholder="Calle, Número" required onblur="this.value=toTitleCase(this.value); validateField(this); triggerPreview();">
                                        <div class="invalid-feedback">Ingresa el domicilio.</div>
                                    </div>
                                    <div class="col-12">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Comuna <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_comuna" id="comunaInput" placeholder="Ej: Santiago" required autocomplete="off" oninput="filterComunas(); triggerPreview();" onfocus="showComunas()" onblur="setTimeout(() => validateComunaList(this), 200);">
                                        <div id="comunaDropdown" class="autocomplete-dropdown custom-scrollbar d-none"></div>
                                        <div class="invalid-feedback">Ingresa la comuna.</div>
                                    </div>
                                    <div class="col-12">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Teléfono <span class="required-star">*</span></label>
                                        <div class="w-100">
                                            <input type="tel" class="form-control w-100" id="phoneInput" name="phone_full" placeholder="912345678" required style="width: 100%;">
                                        </div>
                                        <div class="invalid-feedback" id="phoneErrorMsg">Ingresa un teléfono válido.</div>
                                    </div>
                                    <div class="col-12">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Email</label>
                                        <input type="email" class="form-control" name="email" oninput="this.value=this.value.toLowerCase()" onblur="this.value=this.value.trim().toLowerCase(); validateField(this); triggerPreview();">
                                        <div class="invalid-feedback">Ingresa un correo electrónico válido (con @).</div>
                                    </div>
                                    <div class="col-12">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Código de Propiedad <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="property_code" placeholder="Ej: 15482" required oninput="this.value=this.value.replace(/[^0-9]/g,''); triggerPreview();" onblur="validateField(this); triggerPreview();">
                                        <div class="invalid-feedback">Ingresa el código.</div>
                                    </div>
                                </div>
                            </form>'''

text = text[:form_start] + new_form + text[form_end:]

# 5. JS Replacement
new_trigger_preview = '''function triggerPreview() {
            checkFormValidity();

            const overlay = document.getElementById('pdfLoadingOverlay');
            overlay.classList.add('active');

            if (previewTimeout) clearTimeout(previewTimeout);
            previewTimeout = setTimeout(async () => {
                const form = document.getElementById('formNewContract');

                const data = {
                    cliente_nombre: form.cliente_nombre?.value || '',
                    cliente_rut: form.cliente_rut?.value || '',
                    phone: window.getPhoneData ? window.getPhoneData() : (document.getElementById('phoneInput')?.value || ''),
                    email: form.email?.value || '',
                    cliente_direccion: form.cliente_direccion?.value || '',
                    cliente_comuna: form.cliente_comuna?.value || '',
                    property_code: form.property_code?.value || ''
                };

                try {
                    const response = await fetch('/visitas/api/preview', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    
                    if (response.ok) {
                        const blob = await response.blob();
                        const url = URL.createObjectURL(blob);
                        document.getElementById('pdfPreviewIframe').src = url + "#toolbar=0&view=FitH";
                        const mobileBtn = document.getElementById('mobilePdfOpenBtn');
                        if (mobileBtn) {
                            mobileBtn.href = url;
                            mobileBtn.classList.remove('d-none');
                        }
                    } else {
                        console.error('Error generating preview');
                        document.getElementById('pdfPreviewIframe').src = 'about:blank';
                    }
                } catch (error) {
                    console.error('Preview error:', error);
                } finally {
                    overlay.classList.remove('active');
                }
            }, 600);
        }'''

text = re.sub(r'function triggerPreview\(\) \{.*?\},\s*600\);\s*\}', new_trigger_preview, text, flags=re.DOTALL)

new_generate = '''function generateContract() {
            const form = document.getElementById('formNewContract');
            const btn = document.getElementById('btnGenerateContract');
            
            Array.from(form.elements).forEach(el => {
                if (el.tagName === 'SELECT' || el.tagName === 'INPUT') validateField(el);
            });
            checkFormValidity();
            if (btn.disabled) return;

            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Generando...';
            btn.disabled = true;

            const payload = {
                executive: form.executive ? form.executive.value : '',
                cliente_nombre: form.cliente_nombre?.value || '',
                cliente_rut: form.cliente_rut?.value || '',
                phone: window.getPhoneData ? window.getPhoneData() : (document.getElementById('phoneInput')?.value || ''),
                email: form.email?.value || '',
                cliente_direccion: form.cliente_direccion?.value || '',
                cliente_comuna: form.cliente_comuna?.value || '',
                property_code: form.property_code?.value || ''
            };

            fetch('/visitas/api/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(response => response.json())
              .then(data => {
                  if (data.status === 'success') {
                      window.location.reload();
                  } else {
                      alert('Error: ' + data.message);
                      btn.innerHTML = '<i class="fa-solid fa-file-signature me-2"></i>Crear Documento';
                      btn.disabled = false;
                  }
              })
              .catch(err => {
                  alert('Error de conexión');
                  btn.innerHTML = '<i class="fa-solid fa-file-signature me-2"></i>Crear Documento';
                  btn.disabled = false;
              });
        }'''

text = re.sub(r'function generateContract\(\) \{.*?catch.*?\}\s*\);\s*\}', new_generate, text, flags=re.DOTALL)

# Also rename JS helpers
text = text.replace('deleteContract', 'deleteVisita')
text = text.replace('editContract', 'editVisita')
text = text.replace('sendContract', 'sendVisita')

# Sidebar fix: Add logic around visistas links
# First revert the active class from /contracts/dashboard
text = text.replace('<a href="/contracts/dashboard" class="nav-link active">', '<a href="/contracts/dashboard" class="nav-link">')

# Then replace the Visitas link with an active wrapped version
target_link = '<a href="/visitas/dashboard" class="nav-link"><i class="fa-solid fa-map-location-dot"></i> <span>Órdenes de Visita</span></a>'
wrapped_active = """{% if user_role in ['supervisor', 'admin'] %}
        <a href="/visitas/dashboard" class="nav-link active"><i class="fa-solid fa-map-location-dot"></i> <span>Órdenes de Visita</span></a>
        {% endif %}"""

text = text.replace(target_link, wrapped_active)

with open(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html', 'w', encoding='utf-8') as f:
    f.write(text)

print('Successfully restored and fixed visita_dashboard.html')
