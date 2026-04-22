<!DOCTYPE html>
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

        .table-container {
            background: var(--bg-card);
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.05);
            border: 1px solid var(--border-color);
        }

        /* Filter Bar */
        .filter-bar {
            display: flex;
            gap: 12px;
            margin-bottom: 20px;
            flex-wrap: wrap;
            align-items: center;
        }
        .filter-bar .search-box {
            flex: 1;
            min-width: 200px;
            position: relative;
        }
        .filter-bar .search-box i {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-secondary);
        }
        .filter-bar .search-box input {
            width: 100%;
            padding: 10px 14px 10px 40px;
            background: var(--input-bg);
            border: 1px solid var(--input-border);
            border-radius: 10px;
            color: var(--input-text);
        }
        .filter-bar select {
            padding: 10px 14px;
            background: var(--input-bg);
            border: 1px solid var(--input-border);
            border-radius: 10px;
            color: var(--input-text);
            min-width: 160px;
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

        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(125, 125, 125, 0.3); border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(125, 125, 125, 0.5); }
        
        .required-star { color: var(--danger-color); }
        
        .is-invalid {
            border-color: var(--danger-color) !important;
            background-image: none !important;
        }
        .is-valid {
            border-color: var(--success-color) !important;
            background-image: none !important;
        }
        .validation-message {
            color: var(--danger-color);
            font-size: 0.8rem;
            margin-top: 4px;
            display: none;
        }
        .is-invalid + .validation-message {
            display: block;
        }
        
        #formErrorAlert {
            display: none;
        }
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
            <div class="filter-bar">
                <div class="search-box">
                    <i class="fa-solid fa-magnifying-glass"></i>
                    <input type="text" id="searchInput" placeholder="Buscar por cliente, comuna o código..." onkeyup="filterTable()">
                </div>
                <select id="statusFilter" onchange="filterTable()">
                    <option value="ALL">Cualquier estado</option>
                    <option value="created">Creado (Sin Enviar)</option>
                    <option value="sent">Enviado</option>
                    <option value="opened">Visto por Cliente</option>
                    <option value="accepted">Firmado</option>
                </select>
                <select id="sortFilter" onchange="sortTable()">
                    <option value="desc">Más Recientes Primero</option>
                    <option value="asc">Más Antiguos Primero</option>
                </select>
            </div>
            
            <div class="table-responsive">
                <table class="prop-table" id="contractsTable">
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
                    <tbody id="contractsBody">
                        {% for c in contracts %}
                        <tr class="contract-row" data-status="{{ c.status }}" data-search="{{ c.client_data.nombre|lower }} {{ c.property_data.comuna|lower }} {{ c.property_code|lower }} {{ c.contract_code|lower }}" data-timestamp="{{ c.created_at.timestamp() if c.created_at else 0 }}">
                            <td>{{ c.created_at.strftime('%d/%m/%Y %H:%M') if c.created_at else '---' }}</td>
                            <td class="font-monospace text-accent fw-bold">{{ c.property_code or c.contract_code[:8] }}</td>
                            <td>
                                <b>{{ c.client_data.nombre }}</b><br>
                                <small class="text-secondary">{{ c.client_data.rut }} | {{ c.client_data.email }}</small>
                            </td>
                            <td>
                                {{ c.property_data.direccion }}, {{ c.property_data.comuna }}<br>
                                <small class="text-secondary">{{ c.property_data.tipo }} | Rol: {{ c.property_data.rol }}</small>
                            </td>
                            <td>
                                <span class="status-badge status-{{ c.status }}">
                                    {{ c.status|replace('_', ' ')|title }}
                                </span>
                            </td>
                            <td>
                                {% if c.status == 'created' or c.status == 'sent' or c.status == 'opened' or c.status == 'otp_requested' or c.status == 'otp_verified' %}
                                <a href="/contracts/api/download/{{ c.contract_code }}" target="_blank" class="btn btn-sm btn-secondary" title="Ver PDF Generado">
                                    <i class="fa-solid fa-file-pdf"></i>
                                </a>
                                <button class="btn btn-sm btn-success" onclick="sendContract('{{ c.contract_code }}')" title="Enviar por WhatsApp">
                                    <i class="fa-brands fa-whatsapp"></i>
                                </button>
                                {% endif %}
                                {% if c.status == 'accepted' %}
                                <a href="/contracts/api/download_signed/{{ c.contract_code }}" target="_blank" class="btn btn-sm btn-success" title="Descargar Firmado">
                                    <i class="fa-solid fa-download"></i>
                                </a>
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
                        <tr id="emptyRow">
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
                        <div class="col-lg-5">
                            <div class="alert alert-danger" id="formErrorAlert">
                                <i class="fa-solid fa-triangle-exclamation me-2"></i> Faltan campos obligatorios o hay errores de formato. Por favor revisa los campos en rojo.
                            </div>
                            <form id="formNewContract">
                                <div class="row mb-3">
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Nombre Cliente <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_nombre" onblur="capitalizeName(this); validateField(this); triggerPreview();">
                                        <div class="validation-message">Ingresa el nombre completo</div>
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">RUT Cliente <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="cliente_rut" placeholder="12345678-9" oninput="formatRut(this);" onblur="validateField(this); triggerPreview();">
                                        <div class="validation-message">Ingresa un RUT válido</div>
                                    </div>
                                </div>
                                <div class="row mb-3">
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Teléfono <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="phone" placeholder="+56912345678" onblur="formatPhone(this); validateField(this); triggerPreview();">
                                        <div class="validation-message">Debe ser un teléfono válido (+569...)</div>
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Email <span class="required-star">*</span></label>
                                        <input type="email" class="form-control" name="email" placeholder="ejemplo@correo.cl" onblur="validateField(this); triggerPreview();">
                                        <div class="validation-message">Ingresa un correo electrónico válido (con @)</div>
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
                                        <input type="text" class="form-control" name="propiedad_direccion" placeholder="Calle, Número" onblur="validateField(this); triggerPreview();">
                                        <div class="validation-message">Ingresa la dirección</div>
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Comuna <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="comuna" placeholder="Ej: Santiago" onblur="validateField(this); triggerPreview();">
                                        <div class="validation-message">Ingresa la comuna</div>
                                    </div>
                                </div>
                                
                                <div class="row mb-3">
                                    <div class="col-md-4">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Rol <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="rol" placeholder="Ej: 1234-5" onblur="validateField(this); triggerPreview();">
                                        <div class="validation-message">Ingresa el Rol</div>
                                    </div>
                                    <div class="col-md-4">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Precio <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="precio" placeholder="Ej: 5.000 UF" onblur="validateField(this); triggerPreview();">
                                        <div class="validation-message">Ingresa precio con su unidad (ej: UF, $)</div>
                                    </div>
                                    <div class="col-md-4">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Comisión <span class="required-star">*</span></label>
                                        <input type="text" class="form-control" name="comision" placeholder="Ej: 2% o 50%" onblur="validateField(this); triggerPreview();">
                                        <div class="validation-message">Ingresa la comisión</div>
                                    </div>
                                </div>
                                
                                <div class="row mb-3">
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Cód Propiedad (Convecta)</label>
                                        <input type="text" class="form-control" name="property_code" placeholder="Ej: 15482" onblur="triggerPreview()">
                                    </div>
                                    <div class="col-md-6">
                                        <label class="form-label text-secondary small text-uppercase fw-bold">Origen</label>
                                        <select class="form-select" name="origen" onchange="triggerPreview()">
                                            <option value="Captación Interna">Captación Interna</option>
                                            <option value="Captación Externa">Captación Externa</option>
                                        </select>
                                    </div>
                                </div>
                            </form>
                        </div>
                        
                        <!-- Vista Previa PDF -->
                        <div class="col-lg-7">
                            <div class="pdf-preview-container" id="pdfPreviewWrapper">
                                <div id="pdfLoading" style="display:none; position:absolute; z-index:10; background:rgba(255,255,255,0.8); width:100%; height:100%; display:flex; align-items:center; justify-content:center; flex-direction:column;">
                                    <div class="spinner-border text-primary" role="status"></div>
                                    <span class="mt-2 text-dark fw-bold">Actualizando Documento...</span>
                                </div>
                                <iframe id="pdfPreviewIframe" src="about:blank"></iframe>
                            </div>
                            <div class="text-center mt-2 text-secondary small">
                                <i class="fa-solid fa-eye"></i> Vista Previa (se actualiza al cambiar de campo)
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

        // --- FILTERS & SORTING ---
        function filterTable() {
            const search = document.getElementById('searchInput').value.toLowerCase();
            const status = document.getElementById('statusFilter').value;
            const rows = document.querySelectorAll('.contract-row');
            
            rows.forEach(row => {
                const text = row.getAttribute('data-search');
                const rowStatus = row.getAttribute('data-status');
                
                const matchesSearch = text.includes(search);
                const matchesStatus = status === 'ALL' || status === rowStatus;
                
                if (matchesSearch && matchesStatus) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            });
        }
        
        function sortTable() {
            const sortOrder = document.getElementById('sortFilter').value;
            const tbody = document.getElementById('contractsBody');
            const rows = Array.from(document.querySelectorAll('.contract-row'));
            
            rows.sort((a, b) => {
                const timeA = parseFloat(a.getAttribute('data-timestamp'));
                const timeB = parseFloat(b.getAttribute('data-timestamp'));
                if(sortOrder === 'desc') return timeB - timeA;
                return timeA - timeB;
            });
            
            rows.forEach(row => tbody.appendChild(row));
        }

        // --- SMART INPUTS & VALIDATION ---
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
            let nums = val.replace(/[^0-9]/g, '');
            if(nums.length >= 8) {
                let last8 = nums.slice(-8);
                if(!val.startsWith('+') || val.startsWith('+56')) {
                    input.value = '+569' + last8;
                } else {
                    input.value = val;
                }
            }
        }
        
        function validateField(input) {
            let isValid = true;
            let val = input.value.trim();
            
            if(val === '') {
                isValid = false;
            } else if(input.type === 'email') {
                const re = /^[\w-\.]+@([\w-]+\.)+[\w-]{2,4}$/;
                isValid = re.test(val);
            } else if(input.name === 'precio') {
                const low = val.toLowerCase();
                isValid = low.includes('uf') || low.includes('$') || low.includes('pesos');
            } else if(input.name === 'phone') {
                isValid = val.length >= 11 && val.startsWith('+');
            }
            
            if(isValid) {
                input.classList.remove('is-invalid');
                input.classList.add('is-valid');
            } else {
                input.classList.remove('is-valid');
                input.classList.add('is-invalid');
            }
            
            checkFormValidity();
            return isValid;
        }

        let objectUrl = null;

        function checkFormValidity() {
            const form = document.getElementById('formNewContract');
            const btn = document.getElementById('btnGenerateContract');
            const alertBox = document.getElementById('formErrorAlert');
            
            const inputs = form.querySelectorAll('input[required], input:not([name="property_code"]):not([name="origen"])');
            let allValid = true;
            let anyInvalid = false;
            
            inputs.forEach(inp => {
                if(inp.classList.contains('is-invalid') || inp.value.trim() === '') {
                    allValid = false;
                    if(inp.classList.contains('is-invalid')) anyInvalid = true;
                }
            });
            
            btn.disabled = !allValid;
            alertBox.style.display = anyInvalid ? 'block' : 'none';
            return allValid;
        }

        async function triggerPreview() {
            const form = document.getElementById('formNewContract');
            if(form.cliente_nombre.value.trim().length < 2) return;
            
            document.getElementById('pdfLoading').style.display = 'flex';
            
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
            } finally {
                document.getElementById('pdfLoading').style.display = 'none';
            }
        }

        // --- SUBMIT ---
        async function generateContract() {
            if(!checkFormValidity()) return;

            const form = document.getElementById('formNewContract');
            const btn = document.getElementById('btnGenerateContract');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> Procesando...';

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
            if(!confirm('¿Estás seguro de mover este contrato a la papelera? Esta acción cancelará cualquier firma pendiente.')) return;
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
