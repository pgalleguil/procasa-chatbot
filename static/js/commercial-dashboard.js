(function () {
  'use strict';

  const root = document.querySelector('.commercial-dashboard');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (v) => new Intl.NumberFormat('es-CL').format(Number(v ?? 0));
  const pct = (v) => (v != null ? Number(v).toLocaleString('es-CL', { maximumFractionDigits: 1 }) + '%' : 'S/I');
  const pp = (v) => (v != null ? (v >= 0 ? '+' : '') + Number(v).toFixed(1) + ' pp' : 'S/I');
  const varIcon = (v) => v > 0 ? '<i class="fa-solid fa-caret-up"></i>' : v < 0 ? '<i class="fa-solid fa-caret-down"></i>' : '';
  const isDark = () => document.documentElement.dataset.theme === 'dark';

  let data = null;
  let demandTab = 'operations';
  let propTab = 'opportunity';

  function dateISO(d) { return d.toISOString().slice(0, 10); }

  function setDefaultPeriod() {
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - 29);
    $('periodStart').value = dateISO(start);
    $('periodEnd').value = dateISO(end);
  }

  function params() {
    const p = new URLSearchParams();
    if ($('periodStart').value) p.set('period_start', $('periodStart').value);
    if ($('periodEnd').value) p.set('period_end', $('periodEnd').value);
    if ($('filterExecutive').value) p.set('executive', $('filterExecutive').value);
    return p.toString();
  }

  function buildFilters() {
    const f = {};
    if ($('filterSource').value) f.source = $('filterSource').value;
    if ($('filterOperation').value) f.operation = $('filterOperation').value;
    if ($('filterType').value) f.property_type = $('filterType').value;
    if ($('filterCommune').value) f.commune = $('filterCommune').value;
    if ($('filterTemperature').value) f.temperature = $('filterTemperature').value;
    if ($('filterAssignment').value) f.assignment = $('filterAssignment').value;
    return Object.keys(f).length ? f : null;
  }

  async function loadData() {
    $('commercialError').hidden = true;
    $('statusLoading').textContent = 'Cargando dashboard comercial...';
    try {
      const resp = await fetch('/api/analytics/commercial-dashboard?' + params(), {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      data = await resp.json();
      render();
    } catch (err) {
      console.error('[COMMERCIAL]', err);
      $('statusLoading').textContent = 'Error al cargar';
      $('commercialError').hidden = false;
      $('errorDetail').textContent = err.message || String(err);
    }
  }

  function render() {
    if (!data) return;
    $('statusLoading').textContent = 'Dashboard actualizado';
    $('statusBar').innerHTML = '<span class="status-ok"><i class="fa-solid fa-circle-check" style="color:var(--ok)"></i> Actualizado: ' + esc(data.meta.generated_at) + ' | Per\u00edodo: ' + esc(data.meta.period.start) + ' a ' + esc(data.meta.period.end) + ' | Unidad: lead._id</span>';
    const period = data.meta.period || {};
    if (period.current && period.previous) {
      const period = data.meta.period || {};
    if (period.current && period.previous) {
      $('metaPeriod').textContent = (period.current.label || '') + ' | Comparaci\u00f3n: ' + (period.previous.label || '');
    } else {
      $('metaPeriod').textContent = 'Per\u00edodo personalizado';
    }
    $('metaUpdated').textContent = data.meta.generated_at ? '\u00daltima actualizaci\u00f3n: ' + data.meta.generated_at.replace('T', ' ').replace('Z', ' UTC') : '';
    // Show temperature coverage if available
    const cov = data.kpis?._meta?.temperature_coverage;
    if (cov && cov.history_coverage_pct !== null && cov.history_coverage_pct !== undefined) {
      $('statusBar').innerHTML += ' | Cobertura temp. hist\u00f3rica: ' + pct(cov.history_coverage_pct);
    }
    renderKPIs();
    renderFunnel();
    renderInsights();
    renderEvolution();
    renderSLARisk();
    renderDemand();
    renderSources();
    renderProperties();
    renderExecutives();
    renderSecondary();
  }

  /* ---- KPI RENDER ---- */
  function renderKPIs() {
    const k = data.kpis || {};
    // Show hot_current in status bar as additional info
    if (k.hot_current && k.hot_current.value !== undefined) {
      $('statusBar').innerHTML += ' | Hot actual: ' + fmt(k.hot_current.value);
    }
    const map = {
      'kpi-leads': { k: 'leads_received', prefix: '' },
      'kpi-hot': { k: 'leads_hot_history', prefix: '' },
      'kpi-visit-intent': { k: 'visit_intent', prefix: '' },
      'kpi-visits': { k: 'visits_scheduled', prefix: '' },
      'kpi-sla': { k: 'sla_compliance', prefix: '' },
      'kpi-closed': { k: 'closed_won', prefix: '' },
    };
    Object.entries(map).forEach(([id, cfg]) => {
      const d = k[cfg.k] || {};
      const valEl = document.querySelector('#' + id + ' .kpi-value');
      const varEl = document.getElementById(id + '-var');
      const perEl = document.getElementById(id + '-period');
      if (valEl) valEl.textContent = d.value != null ? fmt(d.value) : 'S/I';
      if (perEl) perEl.textContent = 'vs. per\u00edodo anterior';
      if (varEl) {
        const v = d.variation_pct;
        if (v != null) {
          varEl.className = 'kpi-variation ' + (v > 0 ? 'up' : v < 0 ? 'down' : 'neutral');
          varEl.innerHTML = varIcon(v) + ' ' + (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
        } else {
          varEl.className = 'kpi-variation neutral';
          varEl.textContent = 'S/I';
        }
      }
    });
    // SLA KPI is special (percentage)
    const sla = k.sla_compliance || {};
    const slaVal = document.querySelector('#kpi-sla .kpi-value');
    const slaVar = document.getElementById('kpi-sla-var');
    const slaPer = document.getElementById('kpi-sla-period');
    if (slaVal) slaVal.textContent = sla.value != null ? pct(sla.value) : 'S/I';
    const slaPolicy = data.meta?.sla_policy || {};
    const slaLabel = slaPolicy.display_label || 'SLA 3 horas corridas';
    if (slaPer) slaPer.textContent = slaLabel;
    if (slaVar) {
      if (sla.pp_change != null) {
        slaVar.className = 'kpi-variation ' + (sla.pp_change > 0 ? 'up' : sla.pp_change < 0 ? 'down' : 'neutral');
        slaVar.innerHTML = varIcon(sla.pp_change) + ' ' + (sla.pp_change >= 0 ? '+' : '') + sla.pp_change.toFixed(1) + ' pp';
      } else {
        slaVar.className = 'kpi-variation neutral';
        slaVar.textContent = 'S/I';
      }
    }
  }

  /* ---- FUNNEL ---- */
  function renderFunnel() {
    const funnel = data.funnel || [];
    const el = $('funnelChart');
    if (!funnel.length) { el.innerHTML = '<div class="loading">Sin datos de embudo</div>'; return; }
    const labels = funnel.map((s) => s.label);
    const values = funnel.map((s) => s.count);
    const colors = ['#4f46e5', '#ef4444', '#f59e0b', '#0891b2', '#16a34a', '#22c55e', '#8b5cf6', '#d97706'];

    Plotly.newPlot(el, [{
      type: 'bar',
      x: labels,
      y: values,
      marker: { color: colors.slice(0, labels.length) },
      text: values.map((v) => fmt(v)),
      textposition: 'outside',
      textfont: { size: 11 },
      hovertemplate: '%{x}<br>%{y} leads<extra></extra>',
    }], {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: isDark() ? '#f5f7fb' : '#162033', family: 'Inter' },
      margin: { t: 10, b: 80, l: 10, r: 10 },
      xaxis: { tickangle: -30, tickfont: { size: 10 } },
      yaxis: { visible: false },
      bargap: 0.3,
      hovermode: 'x',
    }, { responsive: true });

    // Bottleneck
    let maxLeak = 0;
    let bottleneck = null;
    for (let i = 1; i < funnel.length; i++) {
      const leak = (funnel[i - 1].count || 0) - (funnel[i].count || 0);
      if (leak > maxLeak) { maxLeak = leak; bottleneck = { from: funnel[i - 1].label, to: funnel[i].label, loss: leak }; }
    }
    const bnEl = $('funnelBottleneck');
    if (bottleneck && bottleneck.loss > 0) {
      bnEl.innerHTML = '<strong>Mayor fuga:</strong> ' + esc(bottleneck.from) + ' \u2192 ' + esc(bottleneck.to) + ' <strong>' + fmt(bottleneck.loss) + '</strong> leads perdidos';
    } else {
      bnEl.innerHTML = '<span style="color:var(--ok)">Sin fugas significativas detectadas</span>';
    }
  }

  /* ---- INSIGHTS ---- */
  function renderInsights() {
    const ins = data.insights || [];
    const list = $('insightsList');
    if (!ins.length) {
      list.innerHTML = '<div class="insight-card info"><span class="insight-priority">INFO</span><div class="insight-title">Sin hallazgos relevantes</div><div class="insight-finding">Los indicadores se encuentran dentro de par\u00e1metros normales.</div></div>';
      return;
    }
    list.innerHTML = ins.map((i) => {
      const pri = i.priority || 'info';
      return '<div class="insight-card ' + pri + '"><span class="insight-priority">' + esc(pri.toUpperCase()) + '</span><div class="insight-title">' + esc(i.title) + '</div><div class="insight-finding">' + esc(i.finding || '') + '</div>' + (i.recommended_action ? '<div class="insight-action"><i class="fa-solid fa-arrow-right"></i> ' + esc(i.recommended_action) + '</div>' : '') + '</div>';
    }).join('');
  }

  /* ---- EVOLUTION ---- */
  function renderEvolution() {
    const trends = data.trends || {};
    const cur = trends.current || {};
    const prev = trends.previous || {};
    const curDaily = cur.daily || [];
    const prevDaily = prev.daily || [];
    const el = $('evolutionChart');
    if (!curDaily.length && !prevDaily.length) { el.innerHTML = '<div class="loading">Sin datos de tendencia</div>'; return; }

    const allDates = [...new Set([...curDaily.map((d) => d.date), ...prevDaily.map((d) => d.date)])].sort();
    const curMap = Object.fromEntries(curDaily.map((d) => [d.date, d.received]));
    const prevMap = Object.fromEntries(prevDaily.map((d) => [d.date, d.received]));

    Plotly.newPlot(el, [
      {
        x: allDates, y: allDates.map((d) => curMap[d] || 0),
        type: 'scatter', mode: 'lines+markers',
        name: 'Per\u00edodo actual',
        line: { color: '#4f46e5', width: 2.5 },
        marker: { size: 5, color: '#4f46e5' },
      },
      {
        x: allDates, y: allDates.map((d) => prevMap[d] || 0),
        type: 'scatter', mode: 'lines+markers',
        name: 'Per\u00edodo anterior',
        line: { color: '#94a3b8', width: 2, dash: 'dot' },
        marker: { size: 4, color: '#94a3b8' },
      },
    ], {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: isDark() ? '#f5f7fb' : '#162033', family: 'Inter', size: 10 },
      margin: { t: 10, b: 30, l: 40, r: 10 },
      xaxis: { tickfont: { size: 9 }, tickangle: -45 },
      yaxis: { tickfont: { size: 9 }, title: { text: 'Leads', font: { size: 10 } } },
      legend: { orientation: 'h', y: 1.08, font: { size: 10 } },
      hovermode: 'x',
    }, { responsive: true });
  }

  /* ---- SLA RISK ---- */
  function renderSLARisk() {
    const sla = data.sla_risk || {};
    const slaPolicy = data.meta?.sla_policy || {};
    const slaLabel = slaPolicy.display_label || 'SLA 3 horas corridas';
    $('statusBar').innerHTML += ' | ' + slaLabel;
    const strip = $('slaKpiStrip');
    strip.innerHTML = '<div class="sla-stat ok"><strong>' + pct(sla.within_sla_pct) + '</strong><span>dentro de SLA</span></div>' +
      '<div class="sla-stat critical"><strong>' + fmt(sla.critical_open) + '</strong><span>Hot cr\u00edticos actuales</span></div>' +
      '<div class="sla-stat warning"><strong>' + fmt(sla.breached_during_period || 0) + '</strong><span>incumpl. per\u00edodo</span></div>';

    const dist = sla.distribution || [];
    const el = $('slaDistributionChart');
    if (!dist.length) { el.innerHTML = '<div class="loading">Sin datos</div>'; return; }
    const labels = dist.map((d) => d.label);
    const values = dist.map((d) => d.count);
    const barColors = ['#22c55e', '#16a34a', '#f59e0b', '#ef4444', '#64748b'];

    Plotly.newPlot(el, [{
      type: 'bar',
      y: labels,
      x: values,
      orientation: 'h',
      marker: { color: barColors.slice(0, labels.length) },
      text: values.map((v) => fmt(v)),
      textposition: 'outside',
      textfont: { size: 10 },
      hovertemplate: '%{y}: %{x} leads<extra></extra>',
    }], {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: isDark() ? '#f5f7fb' : '#162033', family: 'Inter', size: 10 },
      margin: { t: 6, b: 6, l: 90, r: 50 },
      xaxis: { visible: false },
      yaxis: { tickfont: { size: 10 }, autorange: 'reversed' },
      bargap: 0.25,
    }, { responsive: true });
  }

  /* ---- DEMAND ---- */
  function renderDemand() {
    const tab = demandTab;
    const el = $('demandChart');
    root.querySelectorAll('[data-demand]').forEach((b) => b.classList.toggle('active', b.dataset.demand === tab));

    if (tab === 'price') {
      renderDemandByPrice(el);
      return;
    }

    const demand = data.demand_by_price || {};
    const distribution = demand[tab] || [];
    if (!distribution.length) { el.innerHTML = '<div class="loading">Sin datos de demanda</div>'; return; }

    const labels = distribution.map((d) => d.value || d.label || d.range || 'S/I');
    const counts = distribution.map((d) => d.count || 0);
    const total = counts.reduce((a, b) => a + b, 0);

    Plotly.newPlot(el, [{
      type: 'bar',
      x: labels,
      y: counts,
      marker: { color: '#4f46e5', opacity: 0.8 },
      text: counts.map((c) => fmt(c) + ' (' + (total ? (c / total * 100).toFixed(1) : 0) + '%)'),
      textposition: 'outside',
      textfont: { size: 9 },
      hovertemplate: '%{x}<br>%{y} leads<extra></extra>',
    }], {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: isDark() ? '#f5f7fb' : '#162033', family: 'Inter', size: 10 },
      margin: { t: 10, b: 60, l: 10, r: 10 },
      xaxis: { tickangle: -35, tickfont: { size: 9 } },
      yaxis: { visible: false },
      bargap: 0.35,
    }, { responsive: true });
  }

  function renderDemandByPrice(el) {
    const demand = data.demand_by_price || {};
    const ranges = demand.price_ranges || [];
    if (!ranges.length) { el.innerHTML = '<div class="loading">Sin datos de precio</div>'; return; }

    // Show as grouped bars per operation
    const traces = [];
    const colors = ['#4f46e5', '#f59e0b', '#0891b2'];
    ranges.forEach((op, idx) => {
      const r = op.ranges || [];
      traces.push({
        type: 'bar',
        name: op.operation,
        x: r.map((x) => x.range),
        y: r.map((x) => x.count),
        marker: { color: colors[idx % colors.length] },
        text: r.map((x) => fmt(x.count)),
        textposition: 'outside',
        textfont: { size: 9 },
        hovertemplate: op.operation + ' %{x}: %{y} leads<extra></extra>',
      });
    });

    Plotly.newPlot(el, traces, {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: isDark() ? '#f5f7fb' : '#162033', family: 'Inter', size: 10 },
      margin: { t: 10, b: 50, l: 10, r: 10 },
      xaxis: { tickangle: -30, tickfont: { size: 9 } },
      yaxis: { visible: false },
      barmode: 'group',
      bargap: 0.2,
      legend: { orientation: 'h', y: 1.08, font: { size: 10 } },
    }, { responsive: true });
  }

  /* ---- SOURCES ---- */
  function renderSources() {
    const sources = data.sources || [];
    const matrix = $('sourcesMatrix');
    const table = $('sourcesTable');

    if (!sources.length) {
      matrix.innerHTML = '<div class="loading">Sin fuentes en el per\u00edodo</div>';
      table.innerHTML = '';
      return;
    }

    // Bubble matrix: X = received, Y = advanced_pct, size = hot_pct
    const valid = sources.filter((s) => s.received >= 5);
    if (valid.length) {
      const maxRecv = Math.max(1, ...valid.map((s) => s.received));
      const maxAdv = Math.max(1, ...valid.map((s) => s.advanced_pct || 0));
      const colors = valid.map((s) => (s.advanced_pct || 0) > 30 ? '#16a34a' : (s.advanced_pct || 0) > 15 ? '#f59e0b' : '#ef4444');

      Plotly.newPlot(matrix, [{
        type: 'scatter',
        mode: 'markers+text',
        x: valid.map((s) => s.received),
        y: valid.map((s) => s.advanced_pct || 0),
        text: valid.map((s) => s.source),
        textposition: 'middle center',
        textfont: { size: 9, color: '#fff' },
        marker: {
          size: valid.map((s) => Math.max(24, Math.min(64, 20 + (s.hot_pct || 0) * 0.5))),
          color: colors,
          line: { color: isDark() ? '#1e293b' : '#fff', width: 2 },
        },
        hovertemplate: '<b>%{text}</b><br>Volumen: %{x} leads<br>Avanzados: %{y:.1f}%<extra></extra>',
      }], {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: isDark() ? '#f5f7fb' : '#162033', family: 'Inter', size: 10 },
        margin: { t: 20, b: 40, l: 50, r: 20 },
        xaxis: { title: { text: 'Volumen de leads', font: { size: 10 } }, tickfont: { size: 9 } },
        yaxis: { title: { text: '% Avanzados', font: { size: 10 } }, tickfont: { size: 9 }, range: [0, Math.min(100, maxAdv * 1.2)] },
        hovermode: 'closest',
        showlegend: false,
      }, { responsive: true });
    } else {
      matrix.innerHTML = '<div class="loading">Volumen insuficiente para matriz</div>';
    }

    // Source table
    table.innerHTML = '<div class="source-row header"><span>Fuente</span><span>Leads</span><span>%Hot</span><span>%Avanz</span><span>Variac.</span></div>' +
      sources.map((s) => {
        const varCls = (s.variation_pct || 0) > 0 ? 'up' : (s.variation_pct || 0) < 0 ? 'down' : '';
        return '<div class="source-row"><span class="source-name">' + esc(s.source) + '</span><strong>' + fmt(s.received) + '</strong>' +
          '<div class="source-bar hot"><span style="width:' + (s.hot_pct || 0) + '%"></span></div>' +
          '<span>' + pct(s.advanced_pct) + '</span>' +
          '<span class="kpi-variation ' + varCls + '">' + (s.variation_pct != null ? (s.variation_pct >= 0 ? '+' : '') + s.variation_pct.toFixed(1) + '%' : 'S/I') + '</span></div>';
      }).join('');
  }

  /* ---- PROPERTIES ---- */
  function renderProperties() {
    const props = data.properties || {};
    const rows = propTab === 'opportunity' ? (props.opportunity || []) : (props.leakage || []);
    const el = $('propContent');
    root.querySelectorAll('[data-prop]').forEach((b) => b.classList.toggle('active', b.dataset.prop === propTab));

    if (!rows.length) { el.innerHTML = '<div class="loading">Sin datos de propiedades</div>'; return; }

    if (propTab === 'opportunity') {
      el.innerHTML = '<div class="prop-row header"><span>C\u00f3digo</span><span>Leads</span><span>Hot</span><span>Visitas</span><span>Conv.</span></div>' +
        rows.slice(0, 8).map((p) =>
          '<div class="prop-row"><span class="prop-code">' + esc(p.code) + '</span><strong>' + fmt(p.leads) + '</strong><span>' + fmt(p.hot) + '</span><span>' + fmt(p.visit_scheduled) + '</span><span>' + pct(p.conversion_pct) + '</span></div>'
        ).join('');
    } else {
      el.innerHTML = '<div class="prop-row header"><span>C\u00f3digo</span><span>Int. visita</span><span>Sin coord.</span><span>Sin gest.</span><span>Ejecutiva</span></div>' +
        rows.slice(0, 8).map((p) =>
          '<div class="prop-row"><span class="prop-code">' + esc(p.code) + '</span><strong>' + fmt(p.visit_intent) + '</strong><span style="color:var(--risk)">' + fmt(p.uncoordinated) + '</span><span style="color:var(--amber)">' + fmt(p.unmanaged) + '</span><span>' + esc(p.dominant_executive) + '</span></div>'
        ).join('');
    }
  }

  /* ---- EXECUTIVES ---- */
  function renderExecutives() {
    const execs = data.executives || [];
    const el = $('executiveTable');
    if (!execs.length) { el.innerHTML = '<div class="loading">Sin datos de ejecutivas</div>'; return; }
    el.innerHTML = '<div class="executive-row header"><span>Ejecutiva</span><span>Asig.</span><span>Hot</span><span>SLA%</span><span>Visitas</span><span>Negoc.</span><span>Gan.</span><span>Perd.</span><span>Conv.Vis</span><span>Conv.Cie</span></div>' +
      execs.map((e) =>
        '<div class="executive-row"><span class="executive-name">' + esc(e.executive) + '</span>' +
        '<span class="executive-metric"><strong>' + fmt(e.assigned) + '</strong></span>' +
        '<span class="executive-metric exec-metric-hot"><strong>' + fmt(e.hot) + '</strong></span>' +
        '<span class="executive-metric">' + pct(e.sla_fulfilled) + '</span>' +
        '<span class="executive-metric">' + fmt(e.ever_visit_scheduled || 0) + '</span>' +
        '<span class="executive-metric">' + fmt(e.ever_negotiation || 0) + '</span>' +
        '<span class="executive-metric exec-metric-ok"><strong>' + fmt(e.ever_closed_won || 0) + '</strong></span>' +
        '<span class="executive-metric">' + fmt(e.ever_closed_lost || 0) + '</span>' +
        '<span class="executive-metric">' + pct(e.conversion_to_visit_pct) + '</span>' +
        '<span class="executive-metric">' + pct(e.conversion_to_close_pct) + '</span></div>'
      ).join('');
  }

  /* ---- SECONDARY ---- */
  function renderSecondary() {
    const cov = data.coverage || {};
    const fields = Object.values(cov).filter((v) => v && typeof v === 'object' && v.coverage_pct != null);
    const el = $('secondaryGrid');
    if (!fields.length) { el.innerHTML = '<div class="loading">Sin datos de cobertura</div>'; return; }
    const fieldLabels = {
      'prospecto.nombre': 'Nombre', 'prospecto.origen': 'Fuente', 'prospecto.operacion': 'Operaci\u00f3n',
      'prospecto.tipo': 'Tipo propiedad', 'prospecto.comuna': 'Comuna', 'prospecto.codigo': 'C\u00f3digo propiedad',
      'ejecutivo_asignado': 'Ejecutiva', 'pipeline_stage': 'Etapa', 'lead_temperature_effective': 'Temperatura',
      'created_at': 'Fecha v\u00e1lida',
    };
    el.innerHTML = fields.map((f) => {
      const label = fieldLabels[f.field] || f.field || f.key || 'Campo';
      return '<div class="secondary-card"><strong>' + fmt(f.populated) + '</strong><span>' + esc(label) + '</span><div class="sec-meter"><span style="width:' + (f.coverage_pct || 0) + '%"></span></div></div>';
    }).join('');
  }

  /* ---- BIND EVENTS ---- */
  function bind() {
    setDefaultPeriod();

    // Fetch filter options
    fetch('/api/analytics/leads/filters', { credentials: 'same-origin' })
      .then((r) => r.json())
      .then((filters) => {
        if (filters.executives) {
          $('filterExecutive').innerHTML = '<option value="">Todas</option>' + filters.executives.map((e) => '<option value="' + esc(e.value || e.executive || e) + '">' + esc(e.value || e.executive || e) + '</option>').join('');
        }
        if (filters.sources) {
          $('filterSource').innerHTML = '<option value="">Todas</option>' + filters.sources.map((s) => '<option value="' + esc(s.value || s.source || s) + '">' + esc(s.value || s.source || s) + '</option>').join('');
        }
        if (filters.stages) {
          $('filterStage').innerHTML = '<option value="">Todas</option>' + filters.stages.map((s) => '<option value="' + esc(s.value || s.stage || s) + '">' + esc(s.value || s.stage || s) + '</option>').join('');
        }
      })
      .catch((e) => console.error('[COMMERCIAL filters]', e));

    // Filter changes
    root.querySelectorAll('input, select').forEach((ctrl) => ctrl.addEventListener('change', loadData));

    // More filters toggle
    $('moreFiltersBtn').addEventListener('click', () => { $('filterDrawer').hidden = !$('filterDrawer').hidden; });

    // Reset filters
    $('resetFiltersBtn').addEventListener('click', () => {
      root.querySelectorAll('select').forEach((s) => s.value = '');
      setDefaultPeriod();
      $('filterDrawer').hidden = true;
      loadData();
    });

    // Retry
    $('retryBtn').addEventListener('click', loadData);

    // Demand tabs
    root.querySelectorAll('[data-demand]').forEach((btn) => btn.addEventListener('click', () => { demandTab = btn.dataset.demand; renderDemand(); }));

    // Property tabs
    root.querySelectorAll('[data-prop]').forEach((btn) => btn.addEventListener('click', () => { propTab = btn.dataset.prop; renderProperties(); }));

    // Initial load
    loadData();
  }

  // Theme toggle (shared)
  window.toggleTheme = () => {
    const html = document.documentElement;
    html.dataset.theme = html.dataset.theme === 'light' ? 'dark' : 'light';
    localStorage.setItem('theme', html.dataset.theme);
    const icon = document.getElementById('themeIcon');
    if (icon) icon.className = html.dataset.theme === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
    // Re-render charts if data exists
    if (data) { renderFunnel(); renderEvolution(); renderSLARisk(); renderDemand(); renderSources(); }
  };

  window.toggleMobileMenu = () => {
    const sidebar = document.getElementById('sidebar');
    sidebar.classList.toggle('mobile-open');
    document.getElementById('sidebarOverlay').classList.toggle('open', sidebar.classList.contains('mobile-open'));
  };

  window.closeSidebar = () => {
    document.getElementById('sidebar').classList.remove('mobile-open');
    document.getElementById('sidebarOverlay').classList.remove('open');
  };

  // Load saved theme
  (function () {
    const saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.dataset.theme = saved;
    const icon = document.getElementById('themeIcon');
    if (icon) icon.className = saved === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
  })();

  document.addEventListener('DOMContentLoaded', bind);
})();
