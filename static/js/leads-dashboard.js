let CURRENT_THEME = 'dark', FLATPICKR, SEARCH_TIMER, currentSort = { field: 'created_at', dir: 'desc' }, currentPage = 1, activeTab = 'operacion';

(function () {
    const s = localStorage.getItem('procasa_theme') || 'dark';
    CURRENT_THEME = s;
    document.documentElement.setAttribute('data-theme', s);
    const ic = document.getElementById('themeIcon');
    if (ic) ic.className = s === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
})();

document.addEventListener('DOMContentLoaded', () => {
    FLATPICKR = flatpickr('#dateRange', { mode: 'range', dateFormat: 'Y-m-d', locale: 'es', defaultDate: [d(30), d(0)], onChange: applyFilters });
    loadFilters();
    loadAll();
});

function d(offset) { const dt = new Date(); dt.setDate(dt.getDate() - offset); return dt; }

function getRange() {
    if (!FLATPICKR || !FLATPICKR.selectedDates || FLATPICKR.selectedDates.length < 2) return { s: '', e: '' };
    const f = x => x.toISOString().split('T')[0];
    return { s: f(FLATPICKR.selectedDates[0]), e: f(FLATPICKR.selectedDates[1]) };
}

function gv(id) { const e = document.getElementById(id); return e ? e.value : ''; }

function applyFilters() {
    document.getElementById('btnClear').style.display = (gv('filterStage') || gv('filterTemperature') || gv('filterSource') || gv('filterExecutive')) ? 'inline-flex' : 'none';
    loadAll();
}

function clearAllFilters() {
    document.getElementById('filterExecutive').value = '';
    document.getElementById('filterStage').value = '';
    document.getElementById('filterTemperature').value = '';
    document.getElementById('filterSource').value = '';
    document.getElementById('filterUniverse').value = 'current_active';
    if (FLATPICKR) FLATPICKR.clear();
    document.getElementById('searchInput').value = '';
    applyFilters();
}

async function loadAll() {
    const { s, e } = getRange();
    const exec = gv('filterExecutive'), universe = gv('filterUniverse');
    const f = buildFilters();
    await Promise.all([
        loadSummary(s, e, exec, f),
        loadTrends(s, e),
        loadComposition(exec, f, universe),
        loadDistributions(s, e, exec, f, universe),
        loadTable(1, exec, f, universe, s, e),
        loadCoverageFloater(exec, universe)
    ]);
}

function buildFilters() {
    const f = {};
    const v = gv('filterStage') || gv('tblStage');
    if (v) f.stage = v;
    const t = gv('filterTemperature') || gv('tblTemp');
    if (t) f.temperature = t;
    const src = gv('filterSource') || gv('tblSource');
    if (src) f.source = src;
    return f;
}

async function loadFilters() {
    try {
        const r = await (await fetch('/api/analytics/leads/filters')).json();
        populate('filterExecutive', r.executives);
        populate('filterStage', r.stages);
        populate('tblStage', r.stages);
        populate('filterSource', r.sources);
        populate('tblSource', r.sources);
    } catch (_) {}
}

function populate(id, items) {
    const el = document.getElementById(id); if (!el) return;
    const cv = el.value;
    el.innerHTML = id.includes('Executive') ? '<option value="">Ejecutivo</option>' : id.includes('Stage') || id.includes('tblStage') ? '<option value="">Etapa</option>' : '<option value="">Origen</option>';
    (items || []).forEach(x => {
        const v = x.value || x.stage || x.executive || x._id || x;
        const l = x.label || x.value || v;
        const c = x.count !== undefined ? ` (${x.count})` : '';
        el.innerHTML += `<option value="${esc(v)}">${esc(l)}${c}</option>`;
    });
    try { el.value = cv; } catch (_) {}
}

async function loadSummary(ps, pe, exec, f) {
    try {
        const p = new URLSearchParams();
        if (ps) p.set('period_start', ps);
        if (pe) p.set('period_end', pe);
        if (exec) p.set('executive', exec);
        if (f.stage) p.set('stage', f.stage);
        if (f.temperature) p.set('temperature', f.temperature);
        if (f.source) p.set('source', f.source);
        const d = await (await fetch('/api/analytics/leads/summary?' + p)).json();
        const s = d.stock || {};
        setKpi(0, d.flow?.received_in_period ?? '--', 'Periodo');
        setKpi(1, s.active_operational ?? '--', 'Actual');
        setKpi(2, s.assigned ?? '--', 'Actual');
        setKpi(3, s.unassigned ?? '--', 'Actual');
        setKpi(4, s.hot ?? '--', 'Actual');
        setKpi(5, (s.closed_won_current ?? '--') + '*', 'Actual');
    } catch (_) {}
}

function setKpi(idx, val, scope) {
    const cards = document.querySelectorAll('#kpiRow .kpi-card');
    if (idx >= cards.length) return;
    const c = cards[idx];
    const s = c.querySelector('.kpi-scope'), v = c.querySelector('.kpi-value');
    if (s) s.textContent = scope;
    if (v) { v.textContent = val; v.classList.remove('skel'); }
}

async function loadTrends(ps, pe) {
    try {
        const p = new URLSearchParams(); if (ps) p.set('period_start', ps); if (pe) p.set('period_end', pe);
        const d = await (await fetch('/api/analytics/leads/trends?' + p)).json();
        const daily = d.daily || [];
        const el = document.getElementById('chartTrends');
        if (!daily.length) { el.innerHTML = '<div class="empty-state">Sin datos en el periodo</div>'; return; }
        const dates = daily.map(r => r.date), vals = daily.map(r => r.received);
        const isDark = CURRENT_THEME === 'dark';
        const textColor = isDark ? '#f8fafc' : '#172033';
        const gridColor = isDark ? '#1e293b' : '#cbd5e1';
        Plotly.newPlot(el, [{ x: dates, y: vals, type: 'scatter', mode: 'lines+markers', line: { shape: 'spline', color: '#6366f1', width: 3 }, fill: 'tozeroy', fillcolor: 'rgba(99,102,241,0.08)', marker: { size: 3 } }],
            { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: textColor, family: 'Outfit', size: 11 }, xaxis: { gridcolor: gridColor }, yaxis: { gridcolor: gridColor }, showlegend: false, margin: { t: 8, b: 30, l: 40, r: 8 } },
            { responsive: true });
    } catch (_) {}
}

async function loadComposition(exec, f, universe) {
    try { const s = await (await fetch('/api/analytics/leads/summary?' + new URLSearchParams({ executive: exec || '', stage: f.stage || '', temperature: f.temperature || '', source: f.source || '' }))).json();
        const stock = s.stock || {}, total = stock.active_operational || 1;
        const stages = s.by_stage || [];
        const el = document.getElementById('compStages');
        el.innerHTML = stages.slice(0, 4).map(r => `<div class="compact-bar"><span class="bar-label">${esc(r.stage || 'Sin etapa')}</span><div class="bar-track"><div class="bar-fill indigo" style="width:${(r.count/total*100)}%"></div></div><span class="bar-count">${r.count}</span></div>`).join('');
        document.getElementById('barHot').style.width = (stock.hot / total * 100) + '%';
        document.getElementById('barHotCnt').textContent = stock.hot ?? '--';
        document.getElementById('barCold').style.width = (stock.cold / total * 100) + '%';
        document.getElementById('barColdCnt').textContent = stock.cold ?? '--';
        document.getElementById('barAssigned').style.width = (stock.assigned / total * 100) + '%';
        document.getElementById('barAssignedCnt').textContent = stock.assigned ?? '--';
        document.getElementById('barUnassigned').style.width = (stock.unassigned / total * 100) + '%';
        document.getElementById('barUnassignedCnt').textContent = stock.unassigned ?? '--';
    } catch (_) {}
}

async function loadDistributions(ps, pe, exec, f, universe) {
    try {
        const p = new URLSearchParams();
        if (ps) p.set('period_start', ps); if (pe) p.set('period_end', pe);
        if (exec) p.set('executive', exec);
        if (f.stage) p.set('stage', f.stage); if (f.temperature) p.set('temperature', f.temperature); if (f.source) p.set('source', f.source);
        if (universe) p.set('universe', universe);
        const d = await (await fetch('/api/analytics/leads/distributions?' + p)).json();
        const isDark = CURRENT_THEME === 'dark';
        const textColor = isDark ? '#f8fafc' : '#172033';
        const gridColor = isDark ? '#1e293b' : '#cbd5e1';
        const layout = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: textColor, family: 'Outfit', size: 10 }, xaxis: { gridcolor: gridColor }, yaxis: { gridcolor: gridColor, automargin: true }, showlegend: false, margin: { t: 4, b: 20, l: 110, r: 10 } };
        const barData = (data, divId) => {
            const el = document.getElementById(divId);
            if (!data.length) { if (el) el.innerHTML = '<div class="empty-state">Sin datos</div>'; return; }
            const vals = data.map(r => r.count), labels = data.map(r => r.value || 'Sin info');
            Plotly.newPlot(el, [{ x: vals, y: labels, type: 'bar', orientation: 'h', marker: { color: isDark ? '#6366f1' : '#4f46e5' }, text: vals.map(String), textposition: 'outside', textfont: { color: textColor } }], layout, { responsive: true });
        };
        barData(d.sources || [], 'chartSources');
        window._distData = d;
        renderDemandTab();
    } catch (_) {}
}

function renderDemandTab() {
    const d = window._distData || {};
    let data;
    if (activeTab === 'operacion') data = d.operations || [];
    else if (activeTab === 'tipo') data = d.types || [];
    else data = d.communes || [];
    const el = document.getElementById('chartDemand');
    if (!data.length) { el.innerHTML = '<div class="empty-state">Sin datos</div>'; return; }
    const isDark = CURRENT_THEME === 'dark';
    const textColor = isDark ? '#f8fafc' : '#172033';
    const gridColor = isDark ? '#1e293b' : '#cbd5e1';
    const vals = data.map(r => r.count), labels = data.map(r => r.value || 'Sin info');
    const layout = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: textColor, family: 'Outfit', size: 10 }, xaxis: { gridcolor: gridColor }, yaxis: { gridcolor: gridColor, automargin: true }, showlegend: false, margin: { t: 4, b: 20, l: 100, r: 10 } };
    Plotly.newPlot(el, [{ x: vals, y: labels, type: 'bar', orientation: 'h', marker: { color: isDark ? '#6366f1' : '#4f46e5' }, text: vals.map(String), textposition: 'outside', textfont: { color: textColor } }], layout, { responsive: true });
}

function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.textContent.toLowerCase().includes(tab)));
    renderDemandTab();
}

async function loadCoverageFloater(exec, universe) {
    try {
        const p = new URLSearchParams(); if (exec) p.set('executive', exec); if (universe) p.set('universe', universe);
        const d = await (await fetch('/api/analytics/leads/coverage?' + p)).json();
        const vals = Object.values(d), total = vals.length ? vals.reduce((a,x) => a + (x.coverage_pct || 0), 0) / vals.length : 0;
        document.getElementById('qualityPct').textContent = `Calidad: ${Math.round(total)}%`;
        const dd = document.getElementById('qualityDropdown');
        dd.innerHTML = vals.map(x => `<div class="compact-bar"><span class="bar-label" style="font-size:0.74rem">${x.field}</span><div class="bar-track"><div class="bar-fill ${x.coverage_pct >= 90 ? 'green' : x.coverage_pct >= 70 ? 'amber' : 'red'}" style="width:${x.coverage_pct}%"></div></div><span class="bar-count" style="font-size:0.74rem">${x.populated}/${x.total}</span></div>`).join('');
    } catch (_) {}
}

function toggleQualityDropdown() {
    const dd = document.getElementById('qualityDropdown');
    dd.classList.toggle('open');
    setTimeout(() => { if (dd.classList.contains('open')) { const c = e => { if (!dd.contains(e.target) && e.target.id !== 'qualityFloater') { dd.classList.remove('open'); document.removeEventListener('click', c); } }; document.addEventListener('click', c); } }, 10);
}

function toggleExtraFilters() { document.getElementById('extraFilters').classList.toggle('open'); }

async function loadTable(page, exec, f, universe, ps, pe) {
    currentPage = page || 1;
    try {
        const p = new URLSearchParams();
        p.set('page', currentPage); p.set('limit', 50);
        p.set('sort_by', currentSort.field); p.set('sort_dir', currentSort.dir);
        if (exec) p.set('executive', exec);
        if (f.stage) p.set('stage', f.stage); if (f.temperature) p.set('temperature', f.temperature); if (f.source) p.set('source', f.source);
        if (universe) p.set('universe', universe);
        if (ps) p.set('period_start', ps); if (pe) p.set('period_end', pe);
        const sv = document.getElementById('searchInput')?.value?.trim();
        if (sv && sv.length >= 2) p.set('search', sv.substring(0, 60));
        const d = await (await fetch('/api/analytics/leads/table?' + p)).json();
        renderTable(d);
    } catch (_) {}
}

function renderTable(d) {
    const cols = [
        { k: 'nombre', l: 'Prospecto' }, { k: 'origen', l: 'Origen' },
        { k: 'operacion', l: 'Requerimiento' }, { k: 'etapa', l: 'Etapa' },
        { k: 'ejecutivo', l: 'Ejecutivo' }, { k: 'temperatura', l: 'Temp' },
        { k: 'dias_desde_creacion', l: 'Antig.' },
    ];
    document.getElementById('tableHead').innerHTML = '<tr>' + cols.map(c => {
        const icon = currentSort.field === c.k ? `<i class="fa-solid fa-sort-${currentSort.dir === 'asc' ? 'up' : 'down'}"></i>` : '';
        return `<th onclick="sortTable('${c.k}')">${c.l}${icon}</th>`;
    }).join('') + '</tr>';

    const items = d.items || [];
    if (!items.length) {
        document.getElementById('tableBody').innerHTML = '<tr><td colspan="7"><div class="empty-state">Sin resultados</div></td></tr>';
        document.getElementById('paginationBar').innerHTML = '';
        return;
    }

    document.getElementById('tableBody').innerHTML = items.map(r => {
        const s = r.etapa || 'Sin etapa';
        let sc = 'badge-other'; if (s === 'NEW') sc = 'badge-new'; else if (s === 'CONTACTED') sc = 'badge-contacted'; else if (s === 'CLOSED_WON') sc = 'badge-won';
        const t = r.temperatura || 'N/A';
        return `<tr onclick="openDetail('${esc(r.id)}')">
            <td>${esc(r.nombre || '-')}</td><td>${esc(r.origen || '-')}</td>
            <td>${esc(r.operacion || '-')}</td><td><span class="badge-sm ${sc}">${esc(s)}</span></td>
            <td>${esc(r.ejecutivo || 'Sin Asignar')}</td><td><span class="badge-sm ${t==='HOT'?'badge-hot':'badge-cold'}">${esc(t)}</span></td>
            <td>${r.dias_desde_creacion ?? '-'}d</td>
        </tr>`;
    }).join('');

    const tp = Math.ceil((d.total || 0) / (d.limit || 50)) || 1;
    document.getElementById('paginationBar').innerHTML = `
        <button ${currentPage <= 1 ? 'disabled' : ''} onclick="loadTable(${currentPage-1})">Anterior</button>
        <span>Pag ${currentPage} de ${tp} (${d.total || 0})</span>
        <button ${currentPage >= tp ? 'disabled' : ''} onclick="loadTable(${currentPage+1})">Siguiente</button>`;
}

function sortTable(field) {
    currentSort.field = field;
    currentSort.dir = currentSort.field === field && currentSort.dir === 'asc' ? 'desc' : 'asc';
    currentPage = 1;
    loadTable(1);
}

function debounceSearch() { clearTimeout(SEARCH_TIMER); SEARCH_TIMER = setTimeout(() => { currentPage = 1; loadTable(1); }, 350); }

async function openDetail(id) {
    try {
        const d = await (await fetch(`/api/analytics/leads/${id}/detail`)).json();
        if (!d || !d.public) { alert('No disponible'); return; }
        const p = d.public, mg = d.management, cl = d.classification, tl = d.timeline || [];
        document.getElementById('detailBody').innerHTML = `
            <div style="margin-bottom:14px"><div class="detail-field"><span class="label">Nombre</span><span class="value">${esc(p.nombre||'-')}</span></div>
            <div class="detail-field"><span class="label">Telefono</span><span class="value">${esc(p.phone_masked||p.phone||'-')}</span></div>
            <div class="detail-field"><span class="label">Origen</span><span class="value">${esc(p.origen||'-')}</span></div>
            <div class="detail-field"><span class="label">Etapa</span><span class="value">${esc(p.etapa||'-')}</span></div>
            <div class="detail-field"><span class="label">Ejecutivo</span><span class="value">${esc(p.ejecutivo||'Sin Asignar')}</span></div>
            <div class="detail-field"><span class="label">Temperatura</span><span class="value">${esc(p.temperatura||'-')}</span></div></div>
            ${p.propiedad ? `<div style="margin-bottom:14px"><h6 style="font-size:0.78rem;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Propiedad</h6>
            <div class="detail-field"><span class="label">Codigo</span><span class="value">${esc(p.propiedad.codigo||'-')}</span></div>
            <div class="detail-field"><span class="label">Comuna</span><span class="value">${esc(p.propiedad.comuna||'-')}</span></div>
            <div class="detail-field"><span class="label">Tipo</span><span class="value">${esc(p.propiedad.tipo||'-')}</span></div>
            <div class="detail-field"><span class="label">Operacion</span><span class="value">${esc(p.propiedad.operacion||'-')}</span></div>
            <div class="detail-field"><span class="label">Precio UF</span><span class="value">${p.propiedad.precio_uf||'-'}</span></div></div>` : ''}
            <div style="margin-bottom:14px"><h6 style="font-size:0.78rem;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Gestion</h6>
            <div style="color:var(--amber);font-size:0.82rem">No disponible</div></div>
            ${cl.resultado_chat ? `<div style="margin-bottom:14px"><h6 style="font-size:0.78rem;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Chatbot</h6>
            <div class="detail-field"><span class="label">Resultado</span><span class="value">${esc(cl.resultado_chat)}</span></div>
            <div class="detail-field"><span class="label">Recuperabilidad</span><span class="value">${esc(cl.recuperabilidad||'-')}</span></div></div>` : ''}
            <div><h6 style="font-size:0.78rem;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Timeline</h6>
            ${!tl.length ? '<div style="color:var(--text-secondary);font-size:0.82rem">Sin eventos</div>' : tl.map(e => `<div class="timeline-entry"><div><div style="color:var(--text-secondary);font-size:0.72rem">${esc(String(e.timestamp||'').substring(0,19))}</div><div style="font-size:0.82rem">${esc(e.label||'')}</div></div></div>`).join('')}</div>`;
        document.getElementById('detailPanel').classList.add('open');
        document.getElementById('detailOverlay').style.display = 'block';
    } catch (_) {}
}
function closeDetail() { document.getElementById('detailPanel').classList.remove('open'); document.getElementById('detailOverlay').style.display = 'none'; }

function toggleTheme() {
    CURRENT_THEME = CURRENT_THEME === 'dark' ? 'light' : 'dark';
    localStorage.setItem('procasa_theme', CURRENT_THEME);
    document.documentElement.setAttribute('data-theme', CURRENT_THEME);
    const ic = document.getElementById('themeIcon');
    if (ic) ic.className = CURRENT_THEME === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
    loadAll();
}

function toggleMobileMenu() {
    document.getElementById('sidebar').classList.toggle('mobile-open');
    document.getElementById('sidebarOverlay').style.display = document.getElementById('sidebar').classList.contains('mobile-open') ? 'block' : 'none';
}
function closeSidebar() { document.getElementById('sidebar').classList.remove('mobile-open'); document.getElementById('sidebarOverlay').style.display = 'none'; }

function esc(s) { if (s === null || s === undefined) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
