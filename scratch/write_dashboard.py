import json

html_content = """<!DOCTYPE html>
<html lang="es" data-theme="dark">

<head>
    <meta charset="UTF-8">
    <title>Procasa | Módulo de Convenios</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <style>
        :root {
            --bg-body: #0f172a;
            --bg-card: #1e293b;
            --bg-sidebar: #020617;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent: #6366f1;
            --accent-hover: #4f46e5;
            --danger-color: #ef4444;
            --success-color: #22c55e;
            --border-color: rgba(255, 255, 255, 0.05);
            --sidebar-width: 240px;
            --sidebar-width-collapsed: 80px;
            --input-bg: #334155;
            --input-border: #475569;
            --input-text: #fff;
        }

        [data-theme="light"] {
            --bg-body: #f1f5f9;
            --bg-card: #ffffff;
            --bg-sidebar: #0f172a;
            --text-primary: #1e293b;
            --text-secondary: #475569;
            --border-color: #e2e8f0;
            --input-bg: #ffffff;
            --input-border: #cbd5e1;
            --input-text: #1e293b;
        }

        body {
            background-color: var(--bg-body);
            color: var(--text-primary);
            font-family: 'Outfit', sans-serif;
            overflow-x: hidden;
            transition: background-color 0.3s, color 0.3s;
        }

        /* SIDEBAR (Resumen) */
        .sidebar {
            width: var(--sidebar-width-collapsed);
            height: 100vh;
            position: fixed;
            left: 0;
            top: 0;
            padding: 30px 0;
            border-right: 1px solid var(--border-color);
            z-index: 1050;
            transition: width 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            overflow: hidden;
            display: flex;
            flex-direction: column;
            white-space: nowrap;
            backdrop-filter: blur(20px);
        }

        .sidebar:hover { width: var(--sidebar-width); }
        
        .brand {
            font-size: 1.5rem;
            font-weight: 800;
            color: var(--text-primary);
            margin-bottom: 2.5rem;
            display: flex;
            align-items: center;
            height: 50px;
        }
        .brand i, .brand img {
            min-width: var(--sidebar-width-collapsed);
            text-align: center;
        }
        .brand span { margin-left: -10px; opacity: 0; transition: opacity 0.3s; }
        .sidebar:hover .brand span { opacity: 1; }

        .nav-link {
            color: var(--text-secondary);
            padding: 14px 0;
            display: flex;
            align-items: center;
            text-decoration: none;
            margin: 4px 12px;
            border-radius: 16px;
            transition: all 0.3s;
            height: 50px;
        }
        .nav-link i { min-width: calc(var(--sidebar-width-collapsed) - 24px); text-align: center; font-size: 1.25rem; }
        .nav-link span { opacity: 0; transform: translateX(-10px); transition: all 0.3s; font-weight: 600; }
        .sidebar:hover .nav-link span { opacity: 1; transform: translateX(0); }
        .nav-link:hover { background: rgba(255, 255, 255, 0.05); color: var(--text-primary); }
        .nav-link.active { background: rgba(99, 102, 241, 0.1); color: var(--accent) !important; }

        .main-content {
            margin-left: var(--sidebar-width-collapsed);
            padding: 30px;
            transition: margin-left 0.3s;
        }

        .card-custom {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }

        .section-title {
            font-size: 1rem;
            font-weight: 700;
            color: var(--text-secondary);
            margin-bottom: 15px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 10px;
        }

        .form-control, .form-select {
            background-color: var(--input-bg);
            border-color: var(--input-border);
            color: var(--input-text);
            font-size: 0.9rem;
        }
        .form-control:focus, .form-select:focus {
            background-color: var(--input-bg);
            border-color: var(--accent);
            color: var(--input-text);
            box-shadow: 0 0 0 0.25rem rgba(99, 102, 241, 0.25);
        }

        /* Tabla Estilo Captacion */
        .table-container {
            background: var(--bg-card);
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.05);
            border: 1px solid var(--border-color);
        }

        .prop-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0 8px;
        }

        .prop-table th {
            color: var(--text-secondary);
            font-weight: 600;
            font-size: 0.8rem;
            text-transform: uppercase;
            padding: 10px 15px;
            border-bottom: none;
        }

        .prop-table tbody tr {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border-radius: 12px;
        }

        .prop-table tbody tr:hover {
            transform: translateY(-3px);
            box-shadow: 0 12px 28px -6px rgba(0, 0, 0, 0.3);
            z-index: 10;
            position: relative;
        }

        .prop-table tbody tr:hover td {
            background-color: rgba(99, 102, 241, 0.08);
        }

        .prop-table td {
            background: var(--bg-card);
            padding: 15px;
            vertical-align: middle;
            border-top: 1px solid var(--border-color);
            border-bottom: 1px solid var(--border-color);
            font-size: 0.9rem;
            box-shadow: inset 0 0 0 9999px rgba(255, 255, 255, 0.045);
        }

        .prop-table td:first-child {
            border-left: 1px solid var(--border-color);
            border-radius: 10px 0 0 10px;
        }

        .prop-table td:last-child {
            border-right: 1px solid var(--border-color);
            border-radius: 0 10px 10px 0;
        }

        .status-badge {
            padding: 5px 10px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 700;
        }
        .status-created { background: rgba(148, 163, 184, 0.2); color: #94a3b8; }
        .status-sent { background: rgba(59, 130, 246, 0.2); color: #3b82f6; }
        .status-opened { background: rgba(245, 158, 11, 0.2); color: #f59e0b; }
        .status-otp_requested { background: rgba(168, 85, 247, 0.2); color: #a855f7; }
        .status-otp_verified { background: rgba(14, 165, 233, 0.2); color: #0ea5e9; }
        .status-accepted { background: rgba(34, 197, 94, 0.2); color: #22c55e; }
        
        .pdf-preview-container {
            height: 100%;
            min-height: 70vh;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            background: #fff;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        #pdfPreviewIframe {
            width: 100%;
            height: 100%;
            border: none;
        }
        
        .required-star { color: var(--danger-color); }
    </style>
</head>

<body>
    <!-- Sidebar -->
    <nav class="sidebar" id="sidebar">
        <div class="brand">
            <img src="/static/logo.png" alt="PROCASA"
                style="max-height: 30px; width: auto; min-width: var(--sidebar-width-collapsed); object-fit: contain; padding: 0 15px;"
                onerror="this.style.display='none'"> <span>MENÚ</span>
        </div>
        <a href="/leads-dashboard" class="nav-link"><i class="fa-solid fa-chart-line"></i> <span>Dashboard</span></a>
        {% if user_role in ['supervisor', 'admin'] %}
        <a href="/manual-lead-entry" class="nav-link"><i class="fa-solid fa-user-plus"></i> <span>Ingreso Manual</span></a>
        {% endif %}
        <a href="/crm" class="nav-link"><i class="fa-solid fa-list-ul"></i> <span>Listado Leads</span></a>
        <a href="/captacion" class="nav-link"><i class="fa-solid fa-house-circle-check"></i> <span>Captaciones</span></a>
        <a href="/contracts/dashboard" class="nav-link active"><i class="fa-solid fa-file-contract"></i> <span>Convenios</span></a>
        
        <a href="javascript:void(0)" class="nav-link theme-toggle" style="margin-top:auto" onclick="toggleTheme()">
            <i class="fa-solid fa-sun" id="themeIcon"></i> <span>Tema</span>
        </a>
        <a href="/logout" class="nav-link logout" style="margin-top:5px">
            <i class="fa-solid fa-power-off"></i> <span>Cerrar Sesión</span>
        </a>
    </nav>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h4 class="m-0 fw-bold">Módulo de Convenios (Ley 19.799)</h4>
            <button class="btn btn-primary fw-bold" data-bs-toggle="modal" data-bs-target="#modalNewContract">
                <i class="fa-solid fa-plus me-2"></i> Nuevo Convenio
            </button>
        </div>

        <div class="table-container">
            <div class="section-title">Convenios Emitidos Recientemente</div>
            <div class="table-responsive">
                <table class="prop-table">
                    <thead>
                        <tr>
                            <th>Fecha</th>
                            <th>Código Interno</th>
                            <th>Cliente</th>
                            <th>Propiedad / Rol</th>
                            <th>Estado</th>
                            <th>Acciones</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for c in contracts %}
                        <tr>
                            <td>{{ c.created_at.strftime('%d/%m/%Y %H:%M') if c.created_at else '---' }}</td>
                            <td class="font-monospace text-accent">{{ c.property_code or c.contract_code[:8] }}</td>
                            <td>
                                <b>{{ c.client_data.nombre }}</b><br>
                                <small class="text-secondary">{{ c.client_data.rut }} | {{ c.client_data.email }}</small>
                            </td>
                            <td>
                                {{ c.property_data.direccion }}<br>
                                <small class="text-secondary">{{ c.property_data.tipo }} | Rol: {{ c.property_data.rol }}</small>
                            </td>
                            <td>
                                <span class="status-badge status-{{ c.status }}">
                                    {{ c.status|replace('_', ' ')|title }}
                                </span>
                            </td>
                            <td>
                                {% if c.status == 'created' %}
                                <a href="/contracts/api/download/{{ c.contract_code }}" target="_blank" class="btn btn-sm btn-secondary" title="Ver PDF Generado">
                                    <i class="fa-solid fa-file-pdf"></i>
                                </a>
                                <button class="btn btn-sm btn-success" onclick="sendContract('{{ c.contract_code }}')" title="Enviar por WhatsApp">
                                    <i class="fa-brands fa-whatsapp"></i>
                                </button>
                                {% endif %}
                                {% if c.status == 'accepted' %}
                                <a href="/contracts/verify/{{ c.contract_code }}" target="_blank" class="btn btn-sm btn-info" title="Verificar Evidencia">
                                    <i class="fa-solid fa-shield-halved"></i>
                                </a>
                                {% endif %}
                                <button class="btn btn-sm btn-danger ms-2" onclick="deleteContract('{{ c.contract_code }}')" title="Eliminar Contrato">
                                    <i class="fa-solid fa-trash"></i>
                                </button>
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="6" class="text-center text-secondary py-4">No hay convenios generados aún.</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Modal Nuevo Contrato -->
    <div class="modal fade" id="modalNewContract" tabindex="-1">
        <div class="modal-dialog modal-xl">
            <div class="modal-content" style="background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-color);">
                <div class="modal-header border-bottom-0">
                    <h5 class="modal-title fw-bold"><i class="fa-solid fa-file-signature text-accent me-2"></i> Generar Nuevo Convenio de Corretaje</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="row">
                        <!-- Formulario -->
                        <div class="col-lg-6">
                            <form id="formNewContract">
                                <div class="row mb-3">
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Nombre Cliente <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_nombre" required onblur="capitalizeName(this); triggerPreview();">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">RUT Cliente <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_rut" placeholder="12345678-9" required oninput="formatRut(this); triggerPreview();">
                                    </div>
                                </div>
                                <div class="row mb-3">
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Teléfono <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="phone" placeholder="+56912345678" required onblur="formatPhone(this); triggerPreview();">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Email <span class="required-star">*</span></label>
                                        <input type="email" class="form-control" name="email" required onblur="triggerPreview()">
                                    </div>
                                </div>
                                
                                <hr style="border-color: var(--border-color);">
                                
                                <div class="row mb-3">
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Tipo de Operación <span class="required-star">*</span></label>
                                        <select class="form-select" name="tipo" onchange="triggerPreview()">
                                            <option value="Venta">Autorización de Venta</option>
                                            <option value="Arriendo">Autorización de Arriendo</option>
                                            <option value="Arriendo y Administración">Autorización de Arriendo y Administración</option>
                                        </select>
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Vigencia (Días) <span class="required-star">*</span></label>
                                        <select class="form-select" name="vigencia" onchange="triggerPreview()">
                                            <option value="30">30 Días</option>
                                            <option value="60">60 Días</option>
                                            <option value="90" selected>90 Días</option>
                                            <option value="120">120 Días</option>
                                            <option value="180">180 Días</option>
                                        </select>
                                    </div>
                                </div>
                                <div class="row mb-3">
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Dirección Propiedad <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="propiedad_direccion" placeholder="Calle, Número" required oninput="triggerPreview()">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Comuna <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="comuna" placeholder="Ej: Santiago" required oninput="triggerPreview()">
                                    </div>
                                </div>
                                
                                <div class="row mb-3">
                                    <div class="col-md-4">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Rol <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="rol" placeholder="Ej: 1234-5" required oninput="triggerPreview()">
                                    </div>
                                    <div class="col-md-4">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Precio <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="precio" placeholder="Ej: 5.000 UF" required oninput="triggerPreview()">
                                    </div>
                                    <div class="col-md-4">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Comisión <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="comision" placeholder="Ej: 2% o 50%" required oninput="triggerPreview()">
                                    </div>
                                </div>
                                
                                <div class="row mb-3">
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Cód Propiedad (Convecta)</label>
                                        <input type="text" class="form-control" name="property_code" placeholder="Ej: 15482" oninput="triggerPreview()">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Origen</label>
                                        <select class="form-select" name="origen">
                                            <option value="Captación Interna">Captación Interna</option>
                                            <option value="Captación Externa">Captación Externa</option>
                                        </select>
                                    </div>
                                </div>
                            </form>
                        </div>
                        
                        <!-- Vista Previa PDF -->
                        <div class="col-lg-6">
                            <div class="pdf-preview-container">
                                <iframe id="pdfPreviewIframe" src="about:blank"></iframe>
                            </div>
                            <div class="text-center mt-2 text-secondary small">
                                <i class="fa-solid fa-eye"></i> Vista Previa en Tiempo Real
                            </div>
                        </div>
                    </div>
                </div>
                <div class="modal-footer border-top-0">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cerrar</button>
                    <button type="button" id="btnGenerateContract" class="btn btn-primary fw-bold" onclick="generateContract()" disabled>
                        Generar y Preparar
                    </button>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function toggleTheme() {
            const html = document.documentElement;
            const current = html.getAttribute('data-theme');
            const next = current === 'dark' ? 'light' : 'dark';
            html.setAttribute('data-theme', next);
            localStorage.setItem('theme', next);
        }

        if(localStorage.getItem('theme') === 'light') toggleTheme();

        // --- SMART INPUTS ---
        function capitalizeName(input) {
            let val = input.value.toLowerCase();
            input.value = val.replace(/(^|\s)\S/g, l => l.toUpperCase());
        }

        function formatRut(input) {
            let rut = input.value.replace(/[^0-9kK]/g, '').toUpperCase();
            if (rut.length > 1) {
                rut = rut.slice(0, -1) + '-' + rut.slice(-1);
            }
            if (rut.length > 5) {
                rut = rut.slice(0, -5) + '.' + rut.slice(-5);
            }
            if (rut.length > 9) {
                rut = rut.slice(0, -9) + '.' + rut.slice(-9);
            }
            input.value = rut;
        }

        function formatPhone(input) {
            let val = input.value.replace(/[^0-9+]/g, '');
            // Extraer 8 ultimos
            let nums = val.replace(/[^0-9]/g, '');
            if(nums.length >= 8) {
                let last8 = nums.slice(-8);
                // Asumimos Chile por defecto si no tiene codigo internacional
                if(!val.startsWith('+') || val.startsWith('+56')) {
                    input.value = '+569' + last8;
                } else {
                    input.value = val;
                }
            }
        }

        // --- PREVIEW LOGIC ---
        let previewTimeout = null;
        let objectUrl = null;

        function checkFormValidity() {
            const form = document.getElementById('formNewContract');
            const btn = document.getElementById('btnGenerateContract');
            btn.disabled = !form.checkValidity();
        }

        function triggerPreview() {
            checkFormValidity();
            
            if(previewTimeout) clearTimeout(previewTimeout);
            previewTimeout = setTimeout(async () => {
                const form = document.getElementById('formNewContract');
                // Al menos tener un nombre para mostrar algo
                if(form.cliente_nombre.value.trim().length < 2) return;
                
                const data = {
                    cliente_nombre: form.cliente_nombre.value,
                    cliente_rut: form.cliente_rut.value,
                    phone: form.phone.value,
                    email: form.email.value,
                    tipo: form.tipo.value,
                    propiedad_direccion: form.propiedad_direccion.value,
                    comuna: form.comuna.value,
                    vigencia: form.vigencia.value,
                    rol: form.rol.value,
                    precio: form.precio.value,
                    comision: form.comision.value,
                    property_code: form.property_code.value,
                    origen: form.origen.value
                };

                try {
                    const res = await fetch('/contracts/api/preview', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(data)
                    });
                    if(res.ok) {
                        const blob = await res.blob();
                        if(objectUrl) URL.revokeObjectURL(objectUrl);
                        objectUrl = URL.createObjectURL(blob);
                        document.getElementById('pdfPreviewIframe').src = objectUrl + "#toolbar=0&navpanes=0&scrollbar=0";
                    }
                } catch(e) {
                    console.error("Preview error", e);
                }
            }, 800); // 800ms debounce
        }

        // --- SUBMIT ---
        async function generateContract() {
            const form = document.getElementById('formNewContract');
            if(!form.checkValidity()) {
                form.reportValidity();
                return;
            }

            const btn = document.getElementById('btnGenerateContract');
            btn.disabled = true;
            btn.innerText = "Procesando...";

            const data = {
                cliente_nombre: form.cliente_nombre.value,
                cliente_rut: form.cliente_rut.value,
                phone: form.phone.value,
                email: form.email.value,
                tipo: form.tipo.value,
                propiedad_direccion: form.propiedad_direccion.value,
                comuna: form.comuna.value,
                vigencia: form.vigencia.value,
                rol: form.rol.value,
                precio: form.precio.value,
                comision: form.comision.value,
                property_code: form.property_code.value,
                origen: form.origen.value
            };

            try {
                const res = await fetch('/contracts/api/create', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                const result = await res.json();
                if(res.ok) {
                    window.location.reload();
                } else {
                    alert('Error: ' + result.detail);
                    btn.disabled = false;
                    btn.innerText = "Generar y Preparar";
                }
            } catch(e) {
                alert('Error al generar contrato.');
                btn.disabled = false;
                btn.innerText = "Generar y Preparar";
            }
        }

        async function sendContract(code) {
            if(!confirm('¿Enviar enlace seguro de firma por WhatsApp al cliente?')) return;
            
            try {
                const res = await fetch(`/contracts/api/${code}/send`, { method: 'POST' });
                const result = await res.json();
                if(res.ok) {
                    alert('¡Enviado exitosamente!');
                    window.location.reload();
                } else {
                    alert('Error: ' + result.detail);
                }
            } catch(e) {
                alert('Error al enviar contrato.');
            }
        }
        
        async function deleteContract(code) {
            if(!confirm('¿Estás seguro de eliminar este contrato de la base de datos? Esto no se puede deshacer.')) return;
            try {
                const res = await fetch(`/contracts/api/delete/${code}`, { method: 'DELETE' });
                if(res.ok) {
                    window.location.reload();
                } else {
                    alert('Error eliminando contrato.');
                }
            } catch(e) {
                alert('Error de conexión.');
            }
        }
    </script>
</body>
</html>
"""

with open("c:/Users/pgall/Desktop/Python/ChatBot_v4_Grok/templates/contract_dashboard.html", "w", encoding="utf-8") as f:
    f.write(html_content)
