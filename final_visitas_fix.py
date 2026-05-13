import re

# 1. Fix pdf_generator_visitas.py
with open(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\services\pdf_generator_visitas.py', 'r', encoding='utf-8') as f:
    pdf_text = f.read()

# Replace c.contract_code = doc.contract_code with c.visita_code = getattr(doc, 'visita_code', '')
pdf_text = pdf_text.replace('c.contract_code = doc.contract_code', "c.visita_code = getattr(doc, 'visita_code', '')")
with open(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\services\pdf_generator_visitas.py', 'w', encoding='utf-8') as f:
    f.write(pdf_text)


# 2. Fix visita_dashboard.html script block
with open(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html', 'r', encoding='utf-8') as f:
    html = f.read()

# We need to remove all functions and logic related to 'ciudadFirma', 'tipo', 'vigencia', 'rol', etc.
html = re.sub(r'let activeCiudadFirmaIndex = -1;', '', html)
html = re.sub(r'buildCiudadFirmaDropdown\(comunasDeChile\);', '', html)

# We can just define a clean JS block and replace the whole script
script_start = html.find('<script>')
script_end = html.find('</script>', script_start) + len('</script>')

new_script = '''<script>
        const comunasDeChile = [
            'Arica', 'Camarones', 'Putre', 'General Lagos', 'Iquique', 'Alto Hospicio', 'Pozo Almonte', 'Camiña', 'Colchane', 'Huara', 'Pica', 'Antofagasta', 'Mejillones', 'Sierra Gorda', 'Taltal', 'Calama', 'Ollagüe', 'San Pedro de Atacama', 'Tocopilla', 'María Elena', 'Copiapó', 'Caldera', 'Tierra Amarilla', 'Chañaral', 'Diego de Almagro', 'Vallenar', 'Alto del Carmen', 'Freirina', 'Huasco', 'La Serena', 'Coquimbo', 'Andacollo', 'La Higuera', 'Paihuano', 'Vicuña', 'Illapel', 'Canela', 'Los Vilos', 'Salamanca', 'Ovalle', 'Combarbalá', 'Monte Patria', 'Punitaqui', 'Río Hurtado', 'Valparaíso', 'Casablanca', 'Concón', 'Juan Fernández', 'Puchuncaví', 'Quintero', 'Viña del Mar', 'Isla de Pascua', 'Los Andes', 'Calle Larga', 'Rinconada', 'San Esteban', 'La Ligua', 'Cabildo', 'Papudo', 'Petorca', 'Zapallar', 'Quillota', 'Calera', 'Hijuelas', 'La Cruz', 'Nogales', 'San Antonio', 'Algarrobo', 'Cartagena', 'El Quisco', 'El Tabo', 'Santo Domingo', 'San Felipe', 'Catemu', 'Llaillay', 'Panquehue', 'Putaendo', 'Santa María', 'Quilpué', 'Limache', 'Olmué', 'Villa Alemana', 'Rancagua', 'Codegua', 'Coinco', 'Coltauco', 'Doñihue', 'Graneros', 'Las Cabras', 'Machalí', 'Malloa', 'Mostazal', 'Olivar', 'Peumo', 'Pichidegua', 'Quinta de Tilcoco', 'Rengo', 'Requínoa', 'San Vicente', 'Pichilemu', 'La Estrella', 'Litueche', 'Marchihue', 'Navidad', 'Paredones', 'San Fernando', 'Chépica', 'Chimbarongo', 'Lolol', 'Nancagua', 'Palmilla', 'Peralillo', 'Placilla', 'Pumanque', 'Santa Cruz', 'Talca', 'Constitución', 'Curepto', 'Empedrado', 'Maule', 'Pelarco', 'Pencahue', 'Río Claro', 'San Clemente', 'San Rafael', 'Cauquenes', 'Chanco', 'Pelluhue', 'Curicó', 'Hualañé', 'Licantén', 'Molina', 'Rauco', 'Romeral', 'Sagrada Familia', 'Teno', 'Vichuquén', 'Linares', 'Colbún', 'Longaví', 'Parral', 'Retiro', 'San Javier', 'Villa Alegre', 'Yerbas Buenas', 'Cobquecura', 'Coelemu', 'Ninhue', 'Portezuelo', 'Quirihue', 'Ránquil', 'Treguaco', 'Bulnes', 'Chillán Viejo', 'Chillán', 'El Carmen', 'Pemuco', 'Pinto', 'Quillón', 'San Ignacio', 'Yungay', 'Coihueco', 'Ñiquén', 'San Carlos', 'San Fabián', 'San Nicolás', 'Concepción', 'Coronel', 'Chiguayante', 'Florida', 'Hualqui', 'Lota', 'Penco', 'San Pedro de la Paz', 'Santa Juana', 'Talcahuano', 'Tomé', 'Hualpén', 'Lebu', 'Arauco', 'Cañete', 'Contulmo', 'Curanilahue', 'Los Álamos', 'Tirúa', 'Los Ángeles', 'Antuco', 'Cabrero', 'Laja', 'Mulchén', 'Nacimiento', 'Negrete', 'Quilaco', 'Quilleco', 'San Rosendo', 'Santa Bárbara', 'Tucapel', 'Yumbel', 'Alto Biobío', 'Temuco', 'Carahue', 'Cunco', 'Curarrehue', 'Freire', 'Galvarino', 'Gorbea', 'Lautaro', 'Loncoche', 'Melipeuco', 'Nueva Imperial', 'Padre las Casas', 'Perquenco', 'Pitrufquén', 'Pucón', 'Saavedra', 'Teodoro Schmidt', 'Toltén', 'Vilcún', 'Villarrica', 'Cholchol', 'Angol', 'Collipulli', 'Curacautín', 'Ercilla', 'Lonquimay', 'Los Sauces', 'Lumaco', 'Purén', 'Renaico', 'Traiguén', 'Victoria', 'Valdivia', 'Corral', 'Lanco', 'Los Lagos', 'Máfil', 'San José de la Mariquina', 'Paillaco', 'Panguipulli', 'La Unión', 'Futrono', 'Lago Ranco', 'Río Bueno', 'Puerto Montt', 'Calbuco', 'Cochamó', 'Fresia', 'Frutillar', 'Los Muermos', 'Llanquihue', 'Maullín', 'Puerto Varas', 'Castro', 'Ancud', 'Chonchi', 'Curaco de Vélez', 'Dalcahue', 'Puqueldón', 'Queilén', 'Quellón', 'Quemchi', 'Quinchao', 'Osorno', 'Puerto Octay', 'Purranque', 'Puyehue', 'Río Negro', 'San Juan de la Costa', 'San Pablo', 'Chaitén', 'Futaleufú', 'Hualaihué', 'Palena', 'Coyhaique', 'Lago Verde', 'Aysén', 'Cisnes', 'Guaitecas', 'Cochrane', 'O\\'Higgins', 'Tortel', 'Chile Chico', 'Río Ibáñez', 'Punta Arenas', 'Laguna Blanca', 'Río Verde', 'San Gregorio', 'Cabo de Hornos', 'Antártica', 'Porvenir', 'Primavera', 'Timaukel', 'Natales', 'Torres del Paine', 'Cerrillos', 'Cerro Navia', 'Conchalí', 'El Bosque', 'Estación Central', 'Huechuraba', 'Independencia', 'La Cisterna', 'La Florida', 'La Granja', 'La Pintana', 'La Reina', 'Las Condes', 'Lo Barnechea', 'Lo Espejo', 'Lo Prado', 'Macul', 'Maipú', 'Ñuñoa', 'Pedro Aguirre Cerda', 'Peñalolén', 'Providencia', 'Pudahuel', 'Quilicura', 'Quinta Normal', 'Recoleta', 'Renca', 'San Joaquín', 'San Miguel', 'San Ramón', 'Santiago', 'Vitacura', 'Puente Alto', 'Pirque', 'San José de Maipo', 'Colina', 'Lampa', 'Tiltil', 'San Bernardo', 'Buin', 'Calera de Tango', 'Paine', 'Melipilla', 'Alhué', 'Curacaví', 'María Pinto', 'San Pedro', 'Talagante', 'El Monte', 'Isla de Maipo', 'Padre Hurtado', 'Peñaflor'
        ];

        let activeComunaIndex = -1;
        let hasInteracted = false;

        document.addEventListener("DOMContentLoaded", function () {
            buildComunaDropdown(comunasDeChile);
        });

        function buildComunaDropdown(list) {
            const dropdown = document.getElementById('comunaDropdown');
            if (!dropdown) return;
            dropdown.innerHTML = '';
            if (list.length === 0) {
                dropdown.classList.add('d-none');
                return;
            }
            list.forEach((comuna, index) => {
                const div = document.createElement('div');
                div.className = 'autocomplete-item';
                div.textContent = comuna;
                div.onclick = function () {
                    const inp = document.getElementById('comunaInput');
                    inp.value = comuna;
                    dropdown.classList.add('d-none');
                    validateField(inp);
                    triggerPreview();
                };
                dropdown.appendChild(div);
            });
            activeComunaIndex = -1;
            updateComunaActive();
        }

        function filterComunas() {
            const val = document.getElementById('comunaInput').value.toLowerCase();
            const filtered = comunasDeChile.filter(c => c.toLowerCase().includes(val));
            buildComunaDropdown(filtered);
            const dropdown = document.getElementById('comunaDropdown');
            if (filtered.length > 0) dropdown.classList.remove('d-none');
            else dropdown.classList.add('d-none');
        }

        function showComunas() {
            filterComunas();
        }

        function validateComunaList(input) {
            const val = input.value.trim();
            if (val && !comunasDeChile.includes(val)) {
                const match = comunasDeChile.find(c => c.toLowerCase() === val.toLowerCase());
                if (match) {
                    input.value = match;
                }
            }
            validateField(input);
            document.getElementById('comunaDropdown').classList.add('d-none');
        }

        function updateComunaActive() {
            const items = document.querySelectorAll('#comunaDropdown .autocomplete-item');
            items.forEach((item, index) => {
                if (index === activeComunaIndex) item.classList.add('active');
                else item.classList.remove('active');
            });
        }

        const comunaInput = document.getElementById('comunaInput');
        if (comunaInput) {
            comunaInput.addEventListener('keydown', function(e) {
                const items = document.querySelectorAll('#comunaDropdown .autocomplete-item');
                if (e.key === 'ArrowDown') {
                    activeComunaIndex++;
                    if (activeComunaIndex >= items.length) activeComunaIndex = 0;
                    updateComunaActive();
                    e.preventDefault();
                } else if (e.key === 'ArrowUp') {
                    activeComunaIndex--;
                    if (activeComunaIndex < 0) activeComunaIndex = items.length - 1;
                    updateComunaActive();
                    e.preventDefault();
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    if (activeComunaIndex > -1 && items.length > 0) {
                        items[activeComunaIndex].click();
                    }
                } else if (e.key === 'Escape') {
                    document.getElementById('comunaDropdown').classList.add('d-none');
                }
            });
        }

        // Utilidades de formato
        function capitalizeName(input) {
            input.value = input.value.replace(/\w\S*/g, function(txt){
                return txt.charAt(0).toUpperCase() + txt.substr(1).toLowerCase();
            });
        }
        function toTitleCase(str) {
            return str.replace(/\w\S*/g, function(txt){
                return txt.charAt(0).toUpperCase() + txt.substr(1).toLowerCase();
            });
        }
        function formatRut(input) {
            let value = input.value.replace(/[^0-9kK]/g, '').toUpperCase();
            if (value.length > 1) {
                value = value.slice(0, -1) + '-' + value.slice(-1);
            }
            input.value = value;
        }
        function validateRut(rut) {
            if (!/^[0-9]+-[0-9K]$/i.test(rut)) return false;
            let parts = rut.split('-');
            let number = parts[0];
            let dv = parts[1].toUpperCase();
            let sum = 0;
            let multiplier = 2;
            for (let i = number.length - 1; i >= 0; i--) {
                sum += parseInt(number.charAt(i)) * multiplier;
                multiplier = multiplier === 7 ? 2 : multiplier + 1;
            }
            let expectedDv = 11 - (sum % 11);
            expectedDv = expectedDv === 11 ? '0' : expectedDv === 10 ? 'K' : expectedDv.toString();
            return dv === expectedDv;
        }

        function validateEmail(email) {
            const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
            return re.test(email);
        }

        function validateField(input) {
            let isValid = true;
            if (input.required && !input.value.trim()) {
                isValid = false;
            } else if (input.name === 'cliente_rut' && input.value) {
                isValid = validateRut(input.value);
            } else if (input.name === 'email' && input.value) {
                isValid = validateEmail(input.value);
            }

            if (isValid) {
                input.classList.remove('is-invalid');
                input.classList.add('is-valid');
            } else {
                input.classList.remove('is-valid');
                input.classList.add('is-invalid');
            }
            return isValid;
        }

        let previewTimeout;
        
        function checkFormValidity() {
            const form = document.getElementById('formNewContract');
            if (!form) return;
            const elements = Array.from(form.elements).filter(el => el.tagName === 'INPUT' || el.tagName === 'SELECT');
            let isValid = true;
            elements.forEach(el => {
                if (el.required && !el.value.trim()) isValid = false;
                if (el.classList.contains('is-invalid')) isValid = false;
            });
            const phoneInputObj = window.intlTelInputGlobals ? window.intlTelInputGlobals.getInstance(document.getElementById('phoneInput')) : null;
            if (phoneInputObj && !phoneInputObj.isValidNumber()) isValid = false;
            
            document.getElementById('btnGenerateContract').disabled = !isValid;
        }

        function triggerPreview() {
            checkFormValidity();
            const overlay = document.getElementById('pdfLoadingOverlay');
            if (overlay) overlay.classList.add('active');

            if (previewTimeout) clearTimeout(previewTimeout);
            previewTimeout = setTimeout(async () => {
                const form = document.getElementById('formNewContract');
                if (!form) return;

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
                    if (overlay) overlay.classList.remove('active');
                }
            }, 600);
        }

        function generateContract() {
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
        }

        // Listeners globales
        document.addEventListener('DOMContentLoaded', () => {
            const modal = document.getElementById('modalNewContract');
            if (modal) {
                modal.addEventListener('shown.bs.modal', function () {
                    if(!hasInteracted) triggerPreview();
                });
            }
            
            // Inicializar intlTelInput
            const phoneInputField = document.querySelector("#phoneInput");
            if (phoneInputField && window.intlTelInput) {
                const phoneInput = window.intlTelInput(phoneInputField, {
                    initialCountry: "cl",
                    preferredCountries: ["cl", "ar", "pe", "co", "ve", "br"],
                    utilsScript: "https://cdnjs.cloudflare.com/ajax/libs/intl-tel-input/19.2.14/js/utils.js",
                    separateDialCode: true,
                    formatOnDisplay: true,
                    nationalMode: true
                });

                window.getPhoneData = function() {
                    return phoneInput.getNumber();
                };

                phoneInputField.addEventListener('input', function() {
                    const msg = document.getElementById('phoneErrorMsg');
                    if (phoneInput.isValidNumber()) {
                        phoneInputField.classList.remove('is-invalid');
                        phoneInputField.classList.add('is-valid');
                        if (msg) msg.style.display = 'none';
                    } else {
                        phoneInputField.classList.remove('is-valid');
                        phoneInputField.classList.add('is-invalid');
                        if (msg) msg.style.display = 'block';
                    }
                    triggerPreview();
                });
            }
        });

        async function deleteVisita(code, btn) {
            if(!confirm('¿Estás seguro de que deseas eliminar este documento?')) return;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            btn.disabled = true;
            try {
                const res = await fetch(`/visitas/api/${code}/delete`, {method: 'DELETE'});
                const data = await res.json();
                if(data.status === 'success') {
                    window.location.reload();
                } else {
                    alert('Error: ' + data.message);
                    btn.innerHTML = '<i class="fa-solid fa-trash-can"></i>';
                    btn.disabled = false;
                }
            } catch(e) {
                alert('Error de red');
                btn.innerHTML = '<i class="fa-solid fa-trash-can"></i>';
                btn.disabled = false;
            }
        }
        
        async function sendVisita(code, btn, force=false) {
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            btn.disabled = true;
            try {
                const res = await fetch(`/visitas/api/${code}/send`, {method: 'POST'});
                const data = await res.json();
                if(data.status === 'success') {
                    window.location.reload();
                } else {
                    alert('Error: ' + data.message);
                    btn.innerHTML = '<i class="fa-brands fa-whatsapp"></i>';
                    btn.disabled = false;
                }
            } catch(e) {
                alert('Error de red');
                btn.innerHTML = '<i class="fa-brands fa-whatsapp"></i>';
                btn.disabled = false;
            }
        }

        function editVisita(data) {
            alert("La edición aún no está habilitada para Órdenes de Visita.");
        }

    </script>'''

html = html[:script_start] + new_script + html[script_end:]

with open(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html', 'w', encoding='utf-8') as f:
    f.write(html)

print('Successfully cleaned up script and fixed PDF backend!')
