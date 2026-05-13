import re

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace endpoint strings
content = content.replace('/contracts/api/', '/visitas/api/')
content = content.replace('/contracts/api/statuses', '/visitas/api/statuses')

# Modal title
content = content.replace('Generar Nuevo Convenio de Corretaje', 'Generar Nueva Orden de Visita')

# We need to change the form structure.
# The original form has rows for client data, phone, email, etc.
# We want: Nombre, RUT, Domicilio, Comuna (cliente), Codigo, Comuna (prop), Region (prop), Telefono, Email (for WhatsApp and email delivery).

# We will replace the entire <form id="contract-form"> ... </form> with a custom form for Visitas.
new_form = """<form id="contract-form" class="space-y-5">
                            <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">NOMBRE DEL CLIENTE *</label>
                                    <input type="text" id="cliente_nombre" required placeholder="Ej: Juan Pérez"
                                           class="w-full bg-slate-800 border border-slate-700 rounded-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                </div>
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">RUT CLIENTE *</label>
                                    <input type="text" id="cliente_rut" required placeholder="12345678-9"
                                           class="w-full bg-slate-800 border border-slate-700 rounded-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                </div>
                            </div>
                            
                            <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">TELÉFONO *</label>
                                    <div class="flex">
                                        <div class="flex-shrink-0 bg-slate-700 border border-slate-600 rounded-l-md px-3 flex items-center">
                                            <span class="text-white text-sm">🇨🇱 +56</span>
                                        </div>
                                        <input type="tel" id="phone" required placeholder="912345678"
                                            class="w-full bg-slate-800 border border-l-0 border-slate-700 rounded-r-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                    </div>
                                </div>
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">EMAIL (Opcional)</label>
                                    <input type="email" id="email" placeholder="cliente@correo.com"
                                           class="w-full bg-slate-800 border border-slate-700 rounded-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                </div>
                            </div>
                            
                            <hr class="border-slate-700 my-4">
                            
                            <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">DOMICILIO CLIENTE *</label>
                                    <input type="text" id="cliente_direccion" required placeholder="Calle 123, Depto 4"
                                           class="w-full bg-slate-800 border border-slate-700 rounded-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                </div>
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">COMUNA CLIENTE *</label>
                                    <input type="text" id="cliente_comuna" required placeholder="Ej: Las Condes"
                                           class="w-full bg-slate-800 border border-slate-700 rounded-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                </div>
                            </div>

                            <hr class="border-slate-700 my-4">

                            <div class="grid grid-cols-1 md:grid-cols-3 gap-5">
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">CÓDIGO PROPIEDAD *</label>
                                    <input type="text" id="property_code" required placeholder="Ej: 15482"
                                           class="w-full bg-slate-800 border border-slate-700 rounded-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                </div>
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">COMUNA PROPIEDAD *</label>
                                    <input type="text" id="property_comuna" required placeholder="Ej: Vitacura"
                                           class="w-full bg-slate-800 border border-slate-700 rounded-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                </div>
                                <div>
                                    <label class="block text-xs font-bold text-gray-400 mb-1 tracking-wider uppercase">REGIÓN PROPIEDAD *</label>
                                    <input type="text" id="property_region" required placeholder="Ej: Metropolitana"
                                           class="w-full bg-slate-800 border border-slate-700 rounded-md py-2 px-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors">
                                </div>
                            </div>
                        </form>"""

content = re.sub(r'<form id="contract-form" class="space-y-5">.*?</form>', new_form, content, flags=re.DOTALL)

# Now, we need to update the Javascript that submits this form!
# It's at the bottom of the file in setupCreateModal
# Let's change the payload building.
js_payload = """
                    const payload = {
                        cliente_nombre: document.getElementById('cliente_nombre').value,
                        cliente_rut: document.getElementById('cliente_rut').value,
                        phone: '+569' + document.getElementById('phone').value.replace(/\\D/g, '').slice(-8),
                        email: document.getElementById('email').value,
                        cliente_direccion: document.getElementById('cliente_direccion').value,
                        cliente_comuna: document.getElementById('cliente_comuna').value,
                        property_code: document.getElementById('property_code').value,
                        property_comuna: document.getElementById('property_comuna').value,
                        property_region: document.getElementById('property_region').value,
                        // Defaults for those fields not in form
                        property_tipo: "",
                        precio: ""
                    };
"""
content = re.sub(r'const payload = \{.*?\};', lambda m: js_payload.strip(), content, flags=re.DOTALL)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Done fixing visita_dashboard.html!")
