/* ============================
   Analytics Dashboard — PROCASA
   Read-only Leads Dashboard JS
   ============================ */
let CURRENT_THEME = 'dark';
let FLATPICKR = null;
let SEARCH_TIMER = null;
let currentSort = { field: 'created_at', dir: 'desc' };

(function () {
    const saved = localStorage.getItem('procasa_theme') || 'dark';
    CURRENT_THEME = saved;
    document.documentElement.setAttribute('data-theme', saved);
    const icon = document.getElementById('themeIcon');
    if (icon) icon.className = saved === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
})();

document.addEventListener('DOMContentLoaded', () => {
    initDatePicker();
    loadFilters();
    loadAll();
});

function initDatePicker() {
    FLATPICKR = flatpickr('#dateRange', {
        mode: 'range',
        dateFormat: 'Y-m-d',
        locale: 'es',
        defaultDate: [thirtyDaysAgo(), today()],
        onChange: () => {
            document.getElementById('clearDate').style.display = FLATPICKR.selectedDates.length ? 'block' : 'none';
            applyFilters();
        }
    });
}

function getDateRange() {
    if (!FLATPICKR || !FLATPICKR.selectedDates || FLATPICKR.selectedDates.length < 2) return { start: '', end: '' };
    const fmt = d => d.toISOString().split('T')[0];
    return { start: fmt(FLATPICKR.selectedDates[0]), end: fmt(FLATPICKR.selectedDates[1]) };
}

function getFilters() {
    return {
        stage: getVal('filterStage'),
        temperature: getVal('filterTemperature'),
        source: getVal('filterSource'),
    };
}

function getVal(id) { const el = document.getElementById(id); return el ? el.value : ''; }
function thirtyDaysAgo() { const d = new Date(); d.setDate(d.getDate() - 30); return d; }
function today() { return new Date(); }

async function loadAll() {
    showLoading();
    const { start, end } = getDateRange();
    const exec = getVal('filterExecutive');
    const filters = getFilters();
    const universe = getVal('filterUniverse');

    await Promise.all([
        loadSummary(start, end, exec, filters),
        loadTrends(start, end),
        loadDistributions(start, end, exec, filters, universe),
        loadCoverage(exec, universe),
        loadTable(1, exec, filters, universe, start, end)
    ]);
    hideLoading();
}

function applyFilters() {
    document.getElementById('btn-clear').style.display = (getVal('filterStage') || getVal('filterTemperature') || getVal('filterSource') || getVal('filterExecutive')) ? 'block' : 'none';
    loadAll();
}

function clearAllFilters() {
    document.getElementById('filterExecutive').value = '';
    document.getElementById('filterStage').value = '';
    document.getElementById('filterTemperature').value = '';
    document.getElementById('filterSource').value = '';
    document.getElementById('filterUniverse').value = 'current_active';
    FLATPICKR.clear();
    applyFilters();
}

function clearDateFilter() {
    FLATPICKR.clear();
    applyFilters();
}

async function loadFilters() {
    try {
        const resp = await fetch('/api/analytics/leads/filters');
        const data = await resp.json();
        populateSelect('filterExecutive', data.executives || []);
        populateSelect('filterSource', data.sources || []);
        populateSelect('filterStage', data.stages || []);
        populateSelect('filterTemperature', data.temperatures || [], true);
    } catch (e) { console.error('Filters error:', e); }
}

function populateSelect(id, items, prependEmpty = true) {
    const el = document.getElementById(id);
    if (!el) return;
    const current = el.value;
    el.innerHTML = prependEmpty ? '<option value="">Todos</option>' : (items.length === 0 ? '<option value="">--</option>' : '');
    items.forEach(item => {
        const v = item.value || item._id || item.stage || item.executive || item;
        const lbl = item.label || item.value || item._id || item.stage || item.executive || item;
        const cnt = item.count !== undefined ? ` (${item.count})` : '';
        el.innerHTML += `<option value="${esc(v)}">${esc(lbl)}${cnt}</option>`;
    });
    el.value = current;
}

async function loadSummary(start, end, exec, filters) {
    try {
        const params = new URLSearchParams();
        if (start) params.set('period_start', start);
        if (end) params.set('period_end', end);
        if (exec) params.set('executive', exec);
        if (filters.stage) params.set('stage', filters.stage);
        if (filters.temperature) params.set('temperature', filters.temperature);
        if (filters.source) params.set('source', filters.source);
        const resp = await fetch('/api/analytics/leads/summary?' + params);
        const d = await resp.json();
        const s = d.stock || {};
        setKpi(0, d.flow?.received_in_period ?? '--', 'Periodo');
        setKpi(1, s.active_operational ?? '--', 'Actual');
        setKpi(2, s.assigned ?? '--', 'Actual');
        setKpi(3, s.unassigned ?? '--', 'Actual');
        setKpi(4, s.hot ?? '--', 'Actual');
        setKpi(5, (s.closed_won_current ?? '--') + '*', 'Actual');
    } catch (e) { console.error('Summary error:', e); }
}

function setKpi(idx, value, scope) {
    const cards = document.querySelectorAll('#kpi-cards .kpi-card');
    if (idx >= cards.length) return;
    const card = cards[idx];
    const scopeEl = card.querySelector('.kpi-scope');
    const valEl = card.querySelector('.kpi-value');
    if (scopeEl) scopeEl.textContent = scope;
    if (valEl) { valEl.textContent = value; valEl.classList.remove('skeleton'); }
}

async function loadTrends(start, end) {
    try {
        const params = new URLSearchParams();
        if (start) params.set('period_start', start);
        if (end) params.set('period_end', end);
        const resp = await fetch('/api/analytics/leads/trends?' + params);
        const d = await resp.json();
        const daily = d.daily || [];
        if (daily.length === 0) {
            document.getElementById('chart-trends').innerHTML = '<div class="empty-state"><i class="fa-solid fa-chart-line"></i><p>Sin datos en el periodo seleccionado</p></div>';
            return;
        }
        const dates = daily.map(r => r.date);
        const vals = daily.map(r => r.received);
        const isDark = CURRENT_THEME === 'dark';
        const layout = baseLayout(isDark);
        Plotly.newPlot('chart-trends', [{
            x: dates, y: vals, type: 'scatter', mode: 'lines+markers',
            line: { shape: 'spline', color: '#6366f1', width: 3 },
            fill: 'tozeroy', fillcolor: 'rgba(99,102,241,0.08)',
            marker: { size: 4 }
        }], { ...layout, showlegend: false, margin: { t: 10, b: 30, l: 40, r: 10 } },
        { responsive: true });
    } catch (e) { console.error('Trends error:', e); }
}

async function loadDistributions(start, end, exec, filters, universe) {
    try {
        const params = new URLSearchParams();
        if (start) params.set('period_start', start);
        if (end) params.set('period_end', end);
        if (exec) params.set('executive', exec);
        if (filters.stage) params.set('stage', filters.stage);
        if (filters.temperature) params.set('temperature', filters.temperature);
        if (filters.source) params.set('source', filters.source);
        if (universe) params.set('universe', universe);

        const resp = await fetch('/api/analytics/leads/distributions?' + params);
        const d = await resp.json();
        const isDark = CURRENT_THEME === 'dark';
        const layout = baseLayout(isDark);
        const barLayout = { ...layout, showlegend: false, margin: { t: 10, b: 30, l: 120, r: 10 }, yaxis: { ...layout.yaxis, automargin: true } };

        renderBar('chart-stages', d.by_stage || d.stages || [], barLayout, isDark);
        renderBar('chart-sources', d.sources || [], barLayout, isDark);
        renderBar('chart-communes', d.communes || [], barLayout, isDark);

    } catch (e) { console.error('Distributions error:', e); }
}

function renderBar(divId, data, layout, isDark) {
    const el = document.getElementById(divId);
    if (!el) return;
    if (!data || data.length === 0) {
        el.innerHTML = '<div class="empty-state"><i class="fa-solid fa-chart-simple"></i><p>Sin datos</p></div>';
        return;
    }
    const labels = data.map(r => r.value || r._id || r.stage || r.date || r.executive || '?');
    const values = data.map(r => r.count || 0);
    Plotly.newPlot(el, [{
        x: values, y: labels, type: 'bar', orientation: 'h',
        marker: { color: values.map(() => isDark ? '#6366f1' : '#4f46e5') },
        text: values.map(String), textposition: 'outside',
    }], layout, { responsive: true });
}

async function loadCoverage(exec, universe) {
    try {
        const params = new URLSearchParams();
        if (exec) params.set('executive', exec);
        if (universe) params.set('universe', universe);
        const resp = await fetch('/api/analytics/leads/coverage?' + params);
        const data = await resp.json();
        const grid = document.getElementById('quality-grid');
        if (!grid) return;
        grid.innerHTML = '';
        for (const [key, info] of Object.entries(data)) {
            const pct = info.coverage_pct || 0;
            const cls = pct >= 90 ? 'good' : pct >= 70 ? 'warn' : 'bad';
            grid.innerHTML += `<div class="quality-item">
                <div><span class="field-name">${key}</span>
                <div class="quality-bar"><div class="quality-bar-fill ${cls}" style="width:${pct}%"></div></div></div>
                <div class="field-value">${info.populated}/${info.total} (${pct}%)</div>
            </div>`;
        }
    } catch (e) { console.error('Coverage error:', e); }
}

async function loadTable(page, exec, filters, universe, start, end, sortField, sortDir) {
    const sf = sortField || currentSort.field;
    const sd = sortDir || currentSort.dir;
    try {
        const params = new URLSearchParams();
        params.set('page', page);
        params.set('limit', 50);
        params.set('sort_by', sf);
        params.set('sort_dir', sd);
        if (exec) params.set('executive', exec);
        if (filters.stage) params.set('stage', filters.stage);
        if (filters.temperature) params.set('temperature', filters.temperature);
        if (filters.source) params.set('source', filters.source);
        if (universe) params.set('universe', universe);
        if (start) params.set('period_start', start);
        if (end) params.set('period_end', end);
        const searchVal = document.getElementById('searchInput')?.value?.trim();
        if (searchVal && searchVal.length >= 2) params.set('search', searchVal.substring(0, 60));

        const resp = await fetch('/api/analytics/leads/table?' + params);
        const d = await resp.json();
        renderTable(d, page, sf, sd);
    } catch (e) { console.error('Table error:', e); }
}

function renderTable(data, page, sortField, sortDir) {
    const head = document.getElementById('table-head');
    const body = document.getElementById('table-body');
    const pagBar = document.getElementById('pagination-bar');
    if (!head || !body) return;

    const cols = [
        { key: 'nombre', label: 'Nombre' },
        { key: 'phone', label: 'Telefono' },
        { key: 'origen', label: 'Origen' },
        { key: 'etapa', label: 'Etapa' },
        { key: 'ejecutivo', label: 'Ejecutivo' },
        { key: 'temperatura', label: 'Temp' },
        { key: 'dias_desde_creacion', label: 'Dias' },
    ];

    head.innerHTML = '<tr>' + cols.map(c => {
        const icon = sortField === c.key ? `<i class="fa-solid fa-sort-${sortDir === 'asc' ? 'up' : 'down'}"></i>` : '<i class="fa-solid fa-sort"></i>';
        return `<th onclick="sortTable('${c.key}')">${c.label}${icon}</th>`;
    }).join('') + '</tr>';

    const items = data.items || [];
    if (items.length === 0) {
        body.innerHTML = '<tr><td colspan="7"><div class="empty-state"><i class="fa-solid fa-inbox"></i><p>Sin resultados</p></div></td></tr>';
        pagBar.innerHTML = '';
        return;
    }

    body.innerHTML = items.map(r => {
        const stageLabel = r.etapa || 'Sin etapa';
        let stageClass = 'badge-unknown';
        if (stageLabel === 'NEW') stageClass = 'badge-new';
        else if (stageLabel === 'CONTACTED') stageClass = 'badge-contacted';
        else if (stageLabel === 'CLOSED_WON') stageClass = 'badge-won';

        const tempLabel = r.temperatura || 'N/A';
        const tempClass = tempLabel === 'HOT' ? 'badge-hot' : tempLabel === 'COLD' ? 'badge-cold' : 'badge-unknown';

        return `<tr onclick="openDetail('${esc(r.id)}')">
            <td>${esc(r.nombre || '—')}</td>
            <td>${esc(r.phone || r.phone_masked || '—')}</td>
            <td>${esc(r.origen || '—')}</td>
            <td><span class="badge-stage ${stageClass}">${esc(stageLabel)}</span></td>
            <td>${esc(r.ejecutivo || 'Sin Asignar')}</td>
            <td><span class="badge-stage ${tempClass}">${esc(tempLabel)}</span></td>
            <td>${r.dias_desde_creacion ?? '—'}</td>
        </tr>`;
    }).join('');

    const total = data.total || 0;
    const limit = data.limit || 50;
    const totalPages = Math.ceil(total / limit) || 1;
    pagBar.innerHTML = `
        <button ${page <= 1 ? 'disabled' : ''} onclick="loadTable(${page - 1})">Anterior</button>
        <span>Pag ${page} de ${totalPages} (${total} leads)</span>
        <button ${page >= totalPages ? 'disabled' : ''} onclick="loadTable(${page + 1})">Siguiente</button>
    `;
}

function sortTable(field) {
    if (currentSort.field === field) {
        currentSort.dir = currentSort.dir === 'asc' ? 'desc' : 'asc';
    } else {
        currentSort.field = field;
        currentSort.dir = 'desc';
    }
    loadTable(1);
}

function debounceSearch() {
    clearTimeout(SEARCH_TIMER);
    SEARCH_TIMER = setTimeout(() => loadTable(1), 350);
}

async function openDetail(id) {
    try {
        const resp = await fetch(`/api/analytics/leads/${id}/detail`);
        if (!resp.ok) { alert('Lead no encontrado o sin acceso'); return; }
        const d = await resp.json();
        const body = document.getElementById('detail-body');
        const pub = d.public || {};
        const mgmt = d.management || {};
        const cls = d.classification || {};
        const tl = d.timeline || [];

        const stage = pub.etapa || '—';
        body.innerHTML = `
            <div class="detail-section">
                <h6>Datos del Lead</h6>
                <div class="detail-field"><span class="label">Nombre</span><span class="value">${esc(pub.nombre || '—')}</span></div>
                <div class="detail-field"><span class="label">Telefono</span><span class="value">${esc(pub.phone_masked || pub.phone || '—')}</span></div>
                <div class="detail-field"><span class="label">Origen</span><span class="value">${esc(pub.origen || '—')}</span></div>
                <div class="detail-field"><span class="label">Etapa</span><span class="value">${esc(stage)}</span></div>
                <div class="detail-field"><span class="label">Ejecutivo</span><span class="value">${esc(pub.ejecutivo || 'Sin Asignar')}</span></div>
                <div class="detail-field"><span class="label">Temperatura</span><span class="value">${esc(pub.temperatura || '—')}</span></div>
            </div>
            ${pub.propiedad ? `
            <div class="detail-section">
                <h6>Propiedad</h6>
                <div class="detail-field"><span class="label">Codigo</span><span class="value">${esc(pub.propiedad.codigo || '—')}</span></div>
                <div class="detail-field"><span class="label">Comuna</span><span class="value">${esc(pub.propiedad.comuna || '—')}</span></div>
                <div class="detail-field"><span class="label">Tipo</span><span class="value">${esc(pub.propiedad.tipo || '—')}</span></div>
                <div class="detail-field"><span class="label">Operacion</span><span class="value">${esc(pub.propiedad.operacion || '—')}</span></div>
                <div class="detail-field"><span class="label">Precio UF</span><span class="value">${pub.propiedad.precio_uf || '—'}</span></div>
            </div>` : ''}
            <div class="detail-section">
                <h6>Gestion</h6>
                <div class="detail-field"><span class="label">Estado</span><span class="value">${mgmt.managed?.available === false ? '<span class="text-warning">No disponible</span>' : esc(String(mgmt.managed?.value ?? '—'))}</span></div>
            </div>
            ${cls.resultado_chat ? `
            <div class="detail-section">
                <h6>Clasificacion chatbot</h6>
                <div class="detail-field"><span class="label">Resultado</span><span class="value">${esc(cls.resultado_chat)}</span></div>
                <div class="detail-field"><span class="label">Recuperabilidad</span><span class="value">${esc(cls.recuperabilidad || '—')}</span></div>
            </div>` : ''}
            <div class="detail-section">
                <h6>Linea de tiempo</h6>
                ${tl.length === 0 ? '<p class="text-secondary small">Sin eventos registrados</p>' :
                tl.map(e => `<div class="timeline-entry"><div><div class="tl-date">${esc(String(e.timestamp || '').substring(0, 19))}</div><div class="tl-desc">${esc(e.label || '')}</div></div></div>`).join('')}
            </div>
        `;
        openPanel();
    } catch (e) { console.error('Detail error:', e); }
}

function openPanel() {
    document.getElementById('detail-panel').classList.add('open');
    document.getElementById('detail-overlay').style.display = 'block';
}
function closeDetail() {
    document.getElementById('detail-panel').classList.remove('open');
    document.getElementById('detail-overlay').style.display = 'none';
}

function toggleTheme() {
    CURRENT_THEME = CURRENT_THEME === 'dark' ? 'light' : 'dark';
    localStorage.setItem('procasa_theme', CURRENT_THEME);
    document.documentElement.setAttribute('data-theme', CURRENT_THEME);
    const icon = document.getElementById('themeIcon');
    if (icon) icon.className = CURRENT_THEME === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
    loadAll();
}

function toggleMobileMenu() {
    document.getElementById('sidebar').classList.toggle('mobile-open');
    const ov = document.getElementById('sidebarOverlay');
    ov.style.display = document.getElementById('sidebar').classList.contains('mobile-open') ? 'block' : 'none';
}
function closeSidebar() {
    document.getElementById('sidebar').classList.remove('mobile-open');
    document.getElementById('sidebarOverlay').style.display = 'none';
}

function showLoading() { document.getElementById('loading-overlay').style.display = 'flex'; }
function hideLoading() { document.getElementById('loading-overlay').style.display = 'none'; }

function baseLayout(isDark) {
    const textColor = isDark ? '#f1f5f9' : '#1e293b';
    const gridColor = isDark ? '#1e293b' : '#e2e8f0';
    return {
        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: textColor, family: 'Outfit', size: 11 },
        xaxis: { gridcolor: gridColor, zerolinecolor: gridColor },
        yaxis: { gridcolor: gridColor, zerolinecolor: gridColor },
    };
}

function esc(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
