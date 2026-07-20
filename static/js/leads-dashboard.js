let CURRENT_THEME = 'dark', FP, SEARCH_T, sort = { f: 'created_at', d: 'desc' }, page = 1, tab = 'operacion';

(function () {
    const s = localStorage.getItem('procasa_theme') || 'dark';
    CURRENT_THEME = s;
    document.documentElement.setAttribute('data-theme', s);
    const ic = document.getElementById('themeIcon');
    if (ic) ic.className = s === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
})();

function $(id) { return document.getElementById(id); }
function gv(id) { const e = $(id); return e ? e.value : ''; }
function esc(s) { if (s === null || s === undefined) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function fmt(dt) { return dt.toISOString().split('T')[0]; }
function ago(d) { const dt = new Date(); dt.setDate(dt.getDate() - d); return dt; }
function range() {
    if (!FP || !FP.selectedDates || FP.selectedDates.length < 2) return { s: fmt(ago(30)), e: fmt(ago(0)) };
    return { s: fmt(FP.selectedDates[0]), e: fmt(FP.selectedDates[1]) };
}

async function api(path, params) {
    const p = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') p.set(k, v); });
    const url = path + (p.toString() ? '?' + p : '');
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const ct = res.headers.get('content-type') || '';
    if (!ct.includes('application/json')) throw new Error(`Expected JSON, got ${ct}`);
    return res.json();
}

function showError(msg) {
    const el = $('errBanner');
    if (!el) return;
    el.textContent = msg || 'Error al cargar datos.';
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 8000);
}

function logErr(ctx, err) { console.error(`[ANALYTICS] ${ctx}:`, err); showError(ctx + ': ' + (err.message || err)); }

document.addEventListener('DOMContentLoaded', function () {
    FP = flatpickr('#dateRange', { mode: 'range', dateFormat: 'Y-m-d', locale: 'es', defaultDate: [ago(30), ago(0)], onChange: refresh });
    loadFilters().then(refresh);
});

function refresh() {
    $('btnClear').style.display = (gv('filterExecutive') || gv('filterStage') || gv('filterTemperature') || gv('filterSource')) ? 'inline-flex' : 'none';
    loadAll();
}

function clearAll() {
    ['filterExecutive','filterStage','filterTemperature','filterSource'].forEach(id => { const e = $(id); if(e) e.value = ''; });
    $('filterUniverse').value = 'current_active';
    $('searchInput').value = '';
    if (FP) FP.clear();
    refresh();
}

async function loadFilters() {
    try {
        const d = await api('/api/analytics/leads/filters');
        fill('filterExecutive', d.executives, 'Ejecutivo');
        fill('filterStage', d.stages, 'Etapa');
        fill('tblStage', d.stages, 'Etapa');
        fill('filterSource', d.sources, 'Origen');
        fill('tblSource', d.sources, 'Origen');
    } catch (e) { logErr('filtros', e); }
}

function fill(id, items, placeholder) {
    const el = $(id); if (!el) return;
    const cv = el.value;
    el.innerHTML = `<option value="">${placeholder}</option>`;
    (items || []).forEach(x => {
        const v = x.value || x.stage || x.executive || x._id || x;
        const c = x.count !== undefined ? ` (${x.count})` : '';
        el.innerHTML += `<option value="${esc(v)}">${esc(x.label || x.value || v)}${c}</option>`;
    });
    try { el.value = cv; } catch (_) {}
}

async function loadAll() {
    const { s, e } = range();
    const exec = gv('filterExecutive'), uni = gv('filterUniverse'), f = getF();
    const jobs = [
        loadSummary(s, e, exec, f),
        loadTrends(s, e),
        loadComposition(s, e, exec, f, uni),
        loadDistributions(s, e, exec, f, uni),
        loadTable(s, e, exec, f, uni),
        loadCoverage(exec, uni),
    ];
    await Promise.allSettled(jobs);
}

function getF() {
    const f = {};
    const s = gv('filterStage') || gv('tblStage'), t = gv('filterTemperature') || gv('tblTemp'), src = gv('filterSource') || gv('tblSource');
    if (s) f.stage = s;
    if (t) f.temperature = t;
    if (src) f.source = src;
    return f;
}

async function loadSummary(ps, pe, exec, f) {
    try {
        const d = await api('/api/analytics/leads/summary', { period_start: ps, period_end: pe, executive: exec, stage: f.stage, temperature: f.temperature, source: f.source });
        const s = d.stock || {};
        setKpi(0, d.flow?.received_in_period, 'Periodo');
        setKpi(1, s.active_operational, 'Actual');
        setKpi(2, s.assigned, 'Actual');
        setKpi(3, s.unassigned, 'Actual');
        setKpi(4, s.hot, 'Actual');
        setKpi(5, s.closed_won_current, 'Actual*');
    } catch (e) { logErr('summary', e); }
}

function setKpi(idx, val, scope) {
    const cards = document.querySelectorAll('#kpiRow .kpi-card');
    if (idx >= cards.length) return;
    const c = cards[idx];
    const s = c.querySelector('.kpi-scope'), v = c.querySelector('.kpi-value');
    if (s) s.textContent = scope;
    if (v) { v.textContent = val !== undefined && val !== null ? val : '--'; v.classList.remove('skel'); }
}

async function loadTrends(ps, pe) {
    try {
        const d = await api('/api/analytics/leads/trends', { period_start: ps, period_end: pe });
        const daily = d.daily || [];
        const el = $('chartTrends');
        if (!daily.length) { el.innerHTML = '<div class="empty-state">Sin datos en el periodo</div>'; return; }
        const dates = daily.map(r => r.date), vals = daily.map(r => r.received);
        const isD = CURRENT_THEME === 'dark';
        const tc = isD ? '#f8fafc' : '#172033', gc = isD ? '#1e293b' : '#cbd5e1';
        Plotly.newPlot(el, [{ x: dates, y: vals, type: 'scatter', mode: 'lines+markers', line: { shape: 'spline', color: '#6366f1', width: 3 }, fill: 'tozeroy', fillcolor: 'rgba(99,102,241,0.08)', marker: { size: 3 } }],
            { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: tc, family: 'Outfit', size: 11 }, xaxis: { gridcolor: gc }, yaxis: { gridcolor: gc }, showlegend: false, margin: { t: 8, b: 30, l: 40, r: 8 } },
            { responsive: true });
    } catch (e) { logErr('trends', e); }
}

async function loadComposition(ps, pe, exec, f, uni) {
    try {
        const d = await api('/api/analytics/leads/summary', { period_start: ps, period_end: pe, executive: exec, stage: f.stage, temperature: f.temperature, source: f.source });
        const s = d.stock || {}, t = s.active_operational || 1;
        const stages = d.by_stage || [];
        $('compStages').innerHTML = stages.slice(0, 4).map(r => {
            const pct = Math.min((r.count / t * 100), 100).toFixed(0);
            return `<div class="compact-bar"><span class="bar-label">${esc(r.stage || 'Sin etapa')}</span><div class="bar-track"><div class="bar-fill indigo" style="width:${pct}%"></div></div><span class="bar-count">${r.count}</span></div>`;
        }).join('');
        const setBar = (id, cnt) => { $(id).style.width = Math.min((cnt / t * 100), 100) + '%'; };
        setBar('barHot', s.hot || 0); setBar('barCold', s.cold || 0); setBar('barAssigned', s.assigned || 0); setBar('barUnassigned', s.unassigned || 0);
        ['barHotCnt','barColdCnt','barAssignedCnt','barUnassignedCnt'].forEach((id, i) => { const el = $(id); if (el) el.textContent = [s.hot, s.cold, s.assigned, s.unassigned][i] ?? '--'; });
    } catch (e) { logErr('composition', e); }
}

async function loadDistributions(ps, pe, exec, f, uni) {
    try {
        const d = await api('/api/analytics/leads/distributions', { period_start: ps, period_end: pe, executive: exec, stage: f.stage, temperature: f.temperature, source: f.source, universe: uni });
        const isD = CURRENT_THEME === 'dark';
        const tc = isD ? '#f8fafc' : '#172033', gc = isD ? '#1e293b' : '#cbd5e1';
        const lay = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: tc, family: 'Outfit', size: 10 }, xaxis: { gridcolor: gc }, yaxis: { gridcolor: gc, automargin: true }, showlegend: false, margin: { t: 4, b: 20, l: 110, r: 10 } };
        const bars = (data, divId) => {
            const el = $(divId);
            if (!data || !data.length) { if (el) el.innerHTML = '<div class="empty-state">Sin datos</div>'; return; }
            const vals = data.map(r => r.count), labels = data.map(r => r.value || 'Sin info');
            Plotly.newPlot(el, [{ x: vals, y: labels, type: 'bar', orientation: 'h', marker: { color: isD ? '#6366f1' : '#4f46e5' }, text: vals.map(String), textposition: 'outside', textfont: { color: tc } }], lay, { responsive: true });
        };
        bars(d.sources || [], 'chartSources');
        window._dist = d;
        drawDemand();
    } catch (e) { logErr('distributions', e); }
}

function drawDemand() {
    const d = window._dist || {};
    const data = tab === 'operacion' ? (d.operations || []) : tab === 'tipo' ? (d.types || []) : (d.communes || []);
    const el = $('chartDemand');
    if (!data.length) { el.innerHTML = '<div class="empty-state">Sin datos</div>'; return; }
    const isD = CURRENT_THEME === 'dark';
    const tc = isD ? '#f8fafc' : '#172033', gc = isD ? '#1e293b' : '#cbd5e1';
    const lay = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: tc, family: 'Outfit', size: 10 }, xaxis: { gridcolor: gc }, yaxis: { gridcolor: gc, automargin: true }, showlegend: false, margin: { t: 4, b: 20, l: 100, r: 10 } };
    const vals = data.map(r => r.count), labels = data.map(r => r.value || 'Sin info');
    Plotly.newPlot(el, [{ x: vals, y: labels, type: 'bar', orientation: 'h', marker: { color: isD ? '#6366f1' : '#4f46e5' }, text: vals.map(String), textposition: 'outside', textfont: { color: tc } }], lay, { responsive: true });
}

function switchTab(t) { tab = t; document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.textContent.toLowerCase().includes(t))); drawDemand(); }

async function loadCoverage(exec, uni) {
    try {
        const d = await api('/api/analytics/leads/coverage', { executive: exec, universe: uni });
        const vals = Object.values(d || {});
        if (!vals.length) return;
        const avg = vals.reduce((a, x) => a + (x.coverage_pct || 0), 0) / vals.length;
        $('qualityPct').textContent = 'Calidad: ' + Math.round(avg) + '%';
        $('qualityDropdown').innerHTML = vals.map(x => {
            const cls = x.coverage_pct >= 90 ? 'green' : x.coverage_pct >= 70 ? 'amber' : 'red';
            return `<div class="compact-bar"><span class="bar-label" style="font-size:0.74rem">${x.field}</span><div class="bar-track"><div class="bar-fill ${cls}" style="width:${x.coverage_pct}%"></div></div><span class="bar-count" style="font-size:0.74rem">${x.populated}/${x.total}</span></div>`;
        }).join('');
    } catch (e) { logErr('coverage', e); }
}

function toggleQual() {
    const dd = $('qualityDropdown');
    dd.classList.toggle('open');
    if (dd.classList.contains('open')) {
        setTimeout(() => {
            const c = e => { if (!dd.contains(e.target) && e.target.id !== 'qualityFloater') { dd.classList.remove('open'); document.removeEventListener('click', c); } };
            document.addEventListener('click', c);
        }, 10);
    }
}

function toggleExtra() { $('extraFilters').classList.toggle('open'); }

async function loadTable(ps, pe, exec, f, uni) {
    try {
        const p = { page: page, limit: 50, sort_by: sort.f, sort_dir: sort.d, executive: exec, stage: f.stage, temperature: f.temperature, source: f.source, universe: uni, period_start: ps, period_end: pe };
        const sv = ($('searchInput')?.value || '').trim();
        if (sv && sv.length >= 2) p.search = sv.substring(0, 60);
        const d = await api('/api/analytics/leads/table', p);
        renderTable(d);
    } catch (e) { logErr('table', e); }
}

function renderTable(d) {
    const cols = [
        { k: 'nombre', l: 'Prospecto' }, { k: 'origen', l: 'Origen' },
        { k: 'operacion', l: 'Requerimiento' }, { k: 'etapa', l: 'Etapa' },
        { k: 'ejecutivo', l: 'Ejecutivo' }, { k: 'temperatura', l: 'Temp' },
        { k: 'dias_desde_creacion', l: 'Antig.' },
    ];
    $('tableHead').innerHTML = '<tr>' + cols.map(c => {
        const icon = sort.f === c.k ? `<i class="fa-solid fa-sort-${sort.d === 'asc' ? 'up' : 'down'}"></i>` : '';
        return `<th onclick="doSort('${c.k}')">${c.l}${icon}</th>`;
    }).join('') + '</tr>';

    const items = d.items || [];
    if (!items.length) {
        $('tableBody').innerHTML = '<tr><td colspan="7"><div class="empty-state">Sin resultados</div></td></tr>';
        $('paginationBar').innerHTML = '';
        return;
    }

    $('tableBody').innerHTML = items.map(r => {
        const s = r.etapa || 'Sin etapa';
        let sc = 'badge-other'; if (s === 'NEW') sc = 'badge-new'; else if (s === 'CONTACTED') sc = 'badge-contacted'; else if (s === 'CLOSED_WON') sc = 'badge-won';
        const t = r.temperatura || 'N/A';
        return `<tr onclick="openDet('${esc(r.id)}')"><td>${esc(r.nombre || '-')}</td><td>${esc(r.origen || '-')}</td><td>${esc(r.operacion || '-')}</td><td><span class="badge-sm ${sc}">${esc(s)}</span></td><td>${esc(r.ejecutivo || 'Sin Asignar')}</td><td><span class="badge-sm ${t==='HOT'?'badge-hot':'badge-cold'}">${esc(t)}</span></td><td>${r.dias_desde_creacion ?? '-'}d</td></tr>`;
    }).join('');

    const tp = Math.ceil((d.total || 0) / (d.limit || 50)) || 1;
    $('paginationBar').innerHTML = `<button ${page<=1?'disabled':''} onclick="goPage(${page-1})">Anterior</button><span>Pag ${page} / ${tp} (${d.total||0})</span><button ${page>=tp?'disabled':''} onclick="goPage(${page+1})">Siguiente</button>`;
}

function goPage(p) { page = p; refresh(); }
function doSort(field) { sort.d = sort.f === field && sort.d === 'asc' ? 'desc' : 'asc'; sort.f = field; page = 1; refresh(); }
function doSearch() { clearTimeout(SEARCH_T); SEARCH_T = setTimeout(() => { page = 1; refresh(); }, 350); }

async function openDet(id) {
    try {
        const d = await api('/api/analytics/leads/' + id + '/detail');
        const p = d.public || {}, cl = d.classification || {}, tl = d.timeline || [];
        $('detailBody').innerHTML =
            `<div style="margin-bottom:14px"><div class="detail-field"><span class="label">Nombre</span><span class="value">${esc(p.nombre||'-')}</span></div><div class="detail-field"><span class="label">Telefono</span><span class="value">${esc(p.phone_masked||p.phone||'-')}</span></div><div class="detail-field"><span class="label">Origen</span><span class="value">${esc(p.origen||'-')}</span></div><div class="detail-field"><span class="label">Etapa</span><span class="value">${esc(p.etapa||'-')}</span></div><div class="detail-field"><span class="label">Ejecutivo</span><span class="value">${esc(p.ejecutivo||'Sin Asignar')}</span></div><div class="detail-field"><span class="label">Temperatura</span><span class="value">${esc(p.temperatura||'-')}</span></div></div>` +
            (p.propiedad ? `<div style="margin-bottom:14px"><h6 class="det-h6">Propiedad</h6><div class="detail-field"><span class="label">Codigo</span><span class="value">${esc(p.propiedad.codigo||'-')}</span></div><div class="detail-field"><span class="label">Comuna</span><span class="value">${esc(p.propiedad.comuna||'-')}</span></div><div class="detail-field"><span class="label">Tipo</span><span class="value">${esc(p.propiedad.tipo||'-')}</span></div><div class="detail-field"><span class="label">Operacion</span><span class="value">${esc(p.propiedad.operacion||'-')}</span></div><div class="detail-field"><span class="label">Precio UF</span><span class="value">${p.propiedad.precio_uf||'-'}</span></div></div>` : '') +
            `<div style="margin-bottom:14px"><h6 class="det-h6">Gestion</h6><div style="color:var(--amber);font-size:0.82rem">No disponible</div></div>` +
            (cl.resultado_chat ? `<div style="margin-bottom:14px"><h6 class="det-h6">Chatbot</h6><div class="detail-field"><span class="label">Resultado</span><span class="value">${esc(cl.resultado_chat)}</span></div><div class="detail-field"><span class="label">Recuperabilidad</span><span class="value">${esc(cl.recuperabilidad||'-')}</span></div></div>` : '') +
            `<div><h6 class="det-h6">Timeline</h6>${!tl.length ? '<div style="color:var(--text-secondary);font-size:0.82rem">Sin eventos</div>' : tl.map(e => `<div class="timeline-entry"><div><div style="color:var(--text-secondary);font-size:0.72rem">${esc(String(e.timestamp||'').substring(0,19))}</div><div style="font-size:0.82rem">${esc(e.label||'')}</div></div></div>`).join('')}</div>`;
        $('detailPanel').classList.add('open');
        $('detailOverlay').style.display = 'block';
    } catch (e) { logErr('detail', e); }
}
function closeDet() { $('detailPanel').classList.remove('open'); $('detailOverlay').style.display = 'none'; }

function toggleTheme() {
    CURRENT_THEME = CURRENT_THEME === 'dark' ? 'light' : 'dark';
    localStorage.setItem('procasa_theme', CURRENT_THEME);
    document.documentElement.setAttribute('data-theme', CURRENT_THEME);
    const ic = $('themeIcon');
    if (ic) ic.className = CURRENT_THEME === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
    loadAll();
}

function toggleMobileMenu() { const s = $('sidebar'); s.classList.toggle('mobile-open'); $('sidebarOverlay').style.display = s.classList.contains('mobile-open') ? 'block' : 'none'; }
function closeSidebar() { $('sidebar').classList.remove('mobile-open'); $('sidebarOverlay').style.display = 'none'; }
