import os
import re

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

# Replace triggerPreview
new_trigger_preview = """function triggerPreview() {
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
                    property_code: form.property_code?.value || '',
                    property_comuna: form.property_comuna?.value || '',
                    property_region: form.property_region?.value || '',
                    origen: form.origen?.value || ''
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
                        mobileBtn.href = url;
                        mobileBtn.classList.remove('d-none');
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
        }"""

text = re.sub(r'function triggerPreview\(\) \{.*?\},\s*600\);\s*\}', new_trigger_preview, text, flags=re.DOTALL)


# Replace generateContract
new_generate = """function generateContract() {
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
                executive: form.executive.value,
                cliente_nombre: form.cliente_nombre?.value || '',
                cliente_rut: form.cliente_rut?.value || '',
                phone: window.getPhoneData ? window.getPhoneData() : (document.getElementById('phoneInput')?.value || ''),
                email: form.email?.value || '',
                cliente_direccion: form.cliente_direccion?.value || '',
                cliente_comuna: form.cliente_comuna?.value || '',
                property_code: form.property_code?.value || '',
                property_comuna: form.property_comuna?.value || '',
                property_region: form.property_region?.value || '',
                origen: form.origen?.value || ''
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
        }"""

text = re.sub(r'function generateContract\(\) \{.*?catch.*?\}\s*\);\s*\}', new_generate, text, flags=re.DOTALL)

# Fix Sidebar Link active state
text = text.replace('<a href="/contracts/dashboard" class="nav-link active">', '<a href="/contracts/dashboard" class="nav-link">')
text = text.replace('<a href="/visitas/dashboard" class="nav-link">', '<a href="/visitas/dashboard" class="nav-link active">')

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)

# Now loop through all templates and wrap Ordenes de Visita link in auth block
import glob
templates = glob.glob(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\*.html')

target_link = '<a href="/visitas/dashboard" class="nav-link"><i class="fa-solid fa-map-location-dot"></i> <span>Órdenes de Visita</span></a>'
active_link = '<a href="/visitas/dashboard" class="nav-link active"><i class="fa-solid fa-map-location-dot"></i> <span>Órdenes de Visita</span></a>'

wrapped_link = '{% if user_role in [\'supervisor\', \'admin\'] %}\n        ' + target_link + '\n        {% endif %}'
wrapped_active = '{% if user_role in [\'supervisor\', \'admin\'] %}\n        ' + active_link + '\n        {% endif %}'

for t in templates:
    with open(t, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if target_link in content or active_link in content:
        # Don't double wrap
        if '{% if user_role in [\'supervisor\', \'admin\'] %}\n        <a href="/visitas/dashboard"' not in content:
            content = content.replace(target_link, wrapped_link)
            content = content.replace(active_link, wrapped_active)
            with open(t, 'w', encoding='utf-8') as f:
                f.write(content)

print("Updates applied.")
