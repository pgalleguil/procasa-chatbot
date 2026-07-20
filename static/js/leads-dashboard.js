let TH='dark',FP,tab='operacion',execSort='active',_dist={};
(function(){const s=localStorage.getItem('procasa_theme')||'dark';TH=s;document.documentElement.setAttribute('data-theme',s);const i=document.getElementById('themeIcon');if(i)i.className=s==='light'?'fa-solid fa-moon':'fa-solid fa-sun'})();
function $(id){return document.getElementById(id)}
function gv(id){const e=$(id);return e?e.value:''}
function esc(s){if(s===null||s===undefined)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function ago(d){const dt=new Date();dt.setDate(dt.getDate()-d);return dt}
function fmt(dt){return dt.toISOString().split('T')[0]}

document.addEventListener('DOMContentLoaded',function(){
    FP=flatpickr('#dateRange',{mode:'range',dateFormat:'Y-m-d',locale:'es',defaultDate:[ago(30),ago(0)],onChange:load});
    fetch('/api/analytics/leads/filters',{credentials:'same-origin'}).then(r=>r.json()).then(d=>{
        fill('filterExecutive',d.executives,'Ejecutivo');
        fill('filterSource',d.sources,'Origen');
        fill('filterStage',d.stages,'Etapa');
        // populate operation/type/commune from distribution endpoint
        fetch('/api/analytics/leads/distributions?universe=received_in_period',{credentials:'same-origin'}).then(r=>r.json()).then(dist=>{
            fill('filterOperation',dist.operations,'Operacion');
            fill('filterType',dist.types,'Tipo');
            fill('filterCommune',dist.communes,'Comuna');
        }).catch(e=>console.error('[DASH] dist filters',e));
    }).catch(e=>console.error('[DASH] filters',e));
    load();
});

function fill(id,items,ph){const el=$(id);if(!el)return;const cv=el.value;el.innerHTML=`<option value="">${ph}</option>`;(items||[]).forEach(x=>{const v=x.value||x.executive||x._id||x;el.innerHTML+=`<option value="${esc(v)}">${esc(x.label||x.value||v)}${x.count!==void 0?' ('+x.count+')':''}</option>`});try{el.value=cv}catch(_){}}

function range(){
    if(!FP||!FP.selectedDates||FP.selectedDates.length<2)return{s:fmt(ago(30)),e:fmt(ago(0))};
    return{s:fmt(FP.selectedDates[0]),e:fmt(FP.selectedDates[1])}
}

function load(){
    $('btnClear').style.display=gv('filterExecutive')?'inline-flex':'none';
    const{s,e}=range(),exec=gv('filterExecutive'),p=new URLSearchParams();
    if(s)p.set('period_start',s);if(e)p.set('period_end',e);if(exec)p.set('executive',exec);
    fetch('/api/analytics/leads/dashboard?'+p,{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(render).catch(e=>{console.error('[DASH]',e);$('summaryText').textContent='Error al cargar. Reintente.'})
}

function render(d){
    renderKpis(d.kpis||{});
    $('summaryText').textContent=d.summary?.text||'';
    renderTrends(d.trends||{},d.kpis?.received?.daily_trend||[],d.kpis?.received?.previous_daily||[]);
    renderPriorities(d.priorities||{});
    renderFunnel(d.funnel||[]);
    renderManagement(d.kpis?.management||{});
    renderExecLoad(d.executive_load||[]);
    renderSourcePerf(d.source_performance||[]);
    renderPropRank(d.property_ranking||[],d.no_code_count||0);
    _dist=d.demand||{};drawDemand();
    renderCoverage(d.coverage?.fields||{});
}

function renderKpis(k){
    setKpi(0,k.received?.value,k.received?.variation_label);
    setKpi(1,k.active?.value);
    const t=k.temperature||{};
    const k2=$('kpi2');if(!k2)return;
    const vl=k2.querySelector('.kpi-val');if(vl){vl.classList.remove('skel');vl.innerHTML=(t.hot||0)+' <span style="font-size:.9rem;color:var(--ho)">H</span> / '+(t.cold||0)+' <span style="font-size:.9rem;color:var(--cy)">C</span>'+(t.unknown?' <span style="font-size:.72rem;color:var(--t2)">?'+t.unknown+'</span>':'');}
    const dist=k.distribution||{};
    const k3=$('kpi3');if(k3){const v3=k3.querySelector('.kpi-val');if(v3){v3.classList.remove('skel');v3.innerHTML=(dist.assigned||0)+' <span style="font-size:.9rem;color:var(--ac)">A</span> / '+(dist.unassigned||0)+' <span style="font-size:.9rem;color:var(--t2)">S</span>';}}
    const pa=k.pending_attention||{};setKpi(4,pa.value,pa.pct_of_active+'% de activos');
    const mg=k.management||{};
    if(mg.sample_sufficient&&mg.median_minutes!==null){setKpi(5,Math.round(mg.median_minutes)+'min',mg.before_threshold_pct+'% antes de critico')}
    else{const k5=$('kpi5');if(k5){const v5=k5.querySelector('.kpi-val');if(v5){v5.textContent='S/I';v5.classList.remove('skel');}const sb=k5.querySelector('.kpi-sub');if(sb)sb.textContent=mg.coverage_pct+'% cobertura';}}
}
function setKpi(idx,val,sub){const cards=document.querySelectorAll('#kpiGrid .kpi');if(idx>=cards.length)return;const c=cards[idx],v=c.querySelector('.kpi-val'),s=c.querySelector('.kpi-sub');if(v){v.textContent=val!==void 0&&val!==null?val:'--';v.classList.remove('skel')}if(s)s.textContent=sub||''}

function renderTrends(ct,curDaily,prevDaily){
    if(!ct&&!curDaily.length)return;$('trendSub').textContent='Actual: '+(ct.current?.total||'--')+' | Anterior: '+(ct.previous?.total||'--')+' | Var: '+(ct.variation_pct!==null&&ct.variation_pct!==void 0?(ct.variation_pct>=0?'+':'')+ct.variation_pct+'%':'N/A');
    const el=$('chartTrends');
    if(!curDaily.length){el.innerHTML='<div style="text-align:center;padding:50px;color:var(--t2)">Sin datos en el periodo</div>';return}
    const isD=TH==='dark',tc=isD?'#f8fafc':'#172033',gc=isD?'#1e293b':'#cbd5e1';
    const traces=[{x:curDaily.map(r=>r.date),y:curDaily.map(r=>r.received),type:'scatter',mode:'lines+markers',name:'Actual',line:{shape:'spline',color:isD?'#6366f1':'#4f46e5',width:3},fill:'tozeroy',fillcolor:isD?'rgba(99,102,241,0.08)':'rgba(79,70,229,0.08)',marker:{size:3}}];
    if(prevDaily.length){const m={};prevDaily.forEach(r=>m[r.date]=r.received);const aligned=curDaily.map(r=>m[r.date]||null);traces.push({x:curDaily.map(r=>r.date),y:aligned,type:'scatter',mode:'lines',name:'Anterior',line:{shape:'spline',color:isD?'#94a3b8':'#64748b',width:2,dash:'dot'}})}
    Plotly.newPlot(el,traces,{paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{color:tc,family:'Outfit',size:11},xaxis:{gridcolor:gc},yaxis:{gridcolor:gc},legend:{orientation:'h',y:1.1,font:{size:10}},margin:{t:18,b:30,l:45,r:10},modebar:{remove:['zoom','pan','select','lasso','zoomin','zoomout','autoScale','resetScale','toggleSpikelines']}},{responsive:true,displayModeBar:false})
}

function renderPriorities(p){
    const items=(p.alerts||[]).filter(a=>a.count>0);
    $('prioritiesList').innerHTML=items.length?items.map(a=>`<div class="prio-item"><span class="prio-cnt ${a.severity==='high'?'high':a.severity==='medium'?'med':'low'}">${a.count}</span><span>${a.label}<span style="font-size:.7rem;color:var(--t2);margin-left:4px">${a.description}</span></span><a href="/crm" class="prio-link">Abrir Leads</a></div>`).join(''):'<div style="color:var(--t2);font-size:.82rem;padding:12px 0">Sin prioridades detectadas</div>'
}

function renderFunnel(f){
    const el=$('funnelContainer');if(!f||!f.length){el.innerHTML='<div style="color:var(--t2);font-size:.82rem;padding:12px 0">Cohorte sin datos en el periodo</div>';return}
    const colors={received:'#6366f1',assigned:'#f59e0b',advanced:'#10b981',won:'#34d399'}
    el.innerHTML='<div class="funnel-grid">'+f.map((s,i)=>{
        const col=colors[s.stage]||'#6366f1';
        const loss=i>0&&f[i-1].count>0?Math.round((1-s.count/f[i-1].count)*100):0;
        return `<div class="funnel-step s${i+1}" style="background:${col}15"><div class="funnel-num">${s.count}</div><div class="funnel-lbl">${s.label}</div><div class="funnel-pct">${s.pct_of_cohort}%</div>${i<f.length-1?`<div class="funnel-arrow"><i class="fa-solid fa-arrow-right"></i> -${loss}%</div>`:''}</div>`
    }).join('')+'</div>'
}

function renderManagement(mg){
    const el=$('managementContainer');
    if(!mg.sample_sufficient){
        el.innerHTML=`<div style="color:var(--t2);font-size:.8rem;padding:8px 0">La medicion de primera respuesta no tiene cobertura suficiente.</div><div style="font-size:.76rem;color:var(--t2)">${mg.total_with_evidence||0} de ${mg.total_assigned||0} leads asignados contienen timestamps verificables.</div>`
        return
    }
    let html=`<table class="mgmt-table"><tr><td>Mediana de respuesta</td><td>${Math.round(mg.median_minutes)} min</td></tr><tr><td>Percentil 90</td><td>${Math.round(mg.p90_minutes)} min</td></tr><tr><td>Antes de critico (&lt;${mg.threshold_minutes} min)</td><td>${mg.before_threshold_pct}%</td></tr></table>`;
    html+='<div style="font-size:.7rem;color:var(--t2);margin-top:2px">Cobertura: '+mg.coverage_pct+'% ('+mg.total_with_evidence+'/'+mg.total_assigned+')</div>';
    if(mg.distribution&&mg.distribution.length){
        html+='<div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap">';
        mg.distribution.forEach(d=>{html+=`<span style="font-size:.72rem;background:var(--el);border-radius:4px;padding:2px 6px">${d.tramo}: ${d.count}</span>`});
        html+='</div>'
    }
    el.innerHTML=html
}

function renderExecLoad(load){
    window._execData=load;
    const el=$('execContainer');
    if(!load||!load.length){el.innerHTML='<div style="color:var(--t2);font-size:.82rem;padding:12px 0">Sin datos</div>';return}
    const sorted=[...load].sort((a,b)=>(b[execSort]||0)-(a[execSort]||0));
    const mx=sorted[0].active||1;
    el.innerHTML='<div class="grid-header"><span class="g-name">Ejecutivo</span><span class="g-bar-c" style="flex:1">Activos</span><span class="g-hot">Hot</span><span class="g-pend">Pend</span><span class="g-crit">Crit</span><span class="g-age">Edad</span></div>'+
    sorted.map(e=>{
        return `<div class="grid-row"><span class="g-name">${esc(e.executive.split(' ')[0])}</span><div class="g-bar-c"><div class="g-bar"><div class="g-bar-f" style="width:${(e.active/mx*100).toFixed(0)}%"></div></div><span class="g-rec">${e.active}</span></div><span class="g-hot">${e.hot}</span><span class="g-pend">${e.pending_gt_7d}</span><span class="g-crit">${e.critical}</span><span class="g-age">${e.median_age_days}d</span></div>`
    }).join('')
}

function sortExec(field){execSort=field;document.querySelectorAll('.sort-opt a').forEach(a=>a.classList.remove('sort-active'));$('sort-'+field).classList.add('sort-active');renderExecLoad(window._execData||[])}

function renderSourcePerf(sources){
    const el=$('sourceContainer');window._execLoad=[]; // reuse from renderExecLoad if needed
    if(!sources||!sources.length){el.innerHTML='<div style="color:var(--t2);font-size:.82rem;padding:12px 0">Sin datos</div>';return}
    el.innerHTML='<div class="grid-header"><span class="g-name" style="width:100px">Fuente</span><span class="g-rec">Leads</span><span class="g-pct">%Hot</span><span class="g-pct">%Asig</span><span class="g-pct">%Avz</span><span class="g-var">Var</span></div>'+
    sources.map(s=>{
        const vp=s.variation_pct;const vCls=vp!==null?(vp>=0?'pos':'neg'):'';
        const vDisp=vp!==null?(vp>=0?'+':'')+vp+'%':'--';
        return `<div class="grid-row"><span class="g-name" style="width:100px">${esc(s.source)}</span><span class="g-rec">${s.received}</span><span class="g-pct" style="color:var(--ho)">${s.hot_pct}%</span><span class="g-pct">${s.assigned_pct}%</span><span class="g-pct">${s.advanced_pct}%</span><span class="g-var ${vCls}">${vDisp}</span></div>`
    }).join('')
}

function renderPropRank(ranking,nc){
    const el=$('propContainer');
    if(!ranking||!ranking.length){el.innerHTML='<div style="color:var(--t2);font-size:.82rem;padding:12px 0">Sin datos en el periodo</div>';return}
    const mx=ranking[0].count||1;
    el.innerHTML='<div class="grid-header"><span class="g-name" style="width:80px">Codigo</span><span class="g-bar-c" style="flex:1">Leads</span><span class="g-hot">Hot</span><span class="g-pct">Asig</span><span class="g-pct">Avz</span><span class="g-name" style="width:40px">Fuente</span></div>'+
    ranking.map(r=>`<div class="grid-row"><span class="g-name" style="width:80px">${esc(r.code)}</span><div class="g-bar-c"><div class="g-bar"><div class="g-bar-f" style="width:${(r.count/mx*100).toFixed(0)}%"></div></div><span class="g-rec">${r.count}</span></div><span class="g-hot">${r.hot_pct}%</span><span class="g-pct">${r.assigned_pct}%</span><span class="g-pct">${r.advanced_pct}%</span><span class="g-name" style="width:40px;font-size:.7rem">${esc(r.dominant_source.substring(0,6))}</span></div>`).join('')+
    (nc?`<div style="font-size:.72rem;color:var(--t2);margin-top:4px">${nc} leads sin codigo de propiedad</div>`:'')
}

function drawDemand(){
    const d=_dist||{};let data=tab==='operacion'?(d.operations||[]):tab==='tipo'?(d.types||[]):(d.communes||[]);
    const el=$('chartDemand');if(!data.length){el.innerHTML='<div style="text-align:center;padding:40px;color:var(--t2)">Sin datos</div>';return}
    data=data.slice(0,tab==='comuna'?10:8);
    const isD=TH==='dark',tc=isD?'#f8fafc':'#172033',gc=isD?'#1e293b':'#cbd5e1';
    Plotly.newPlot(el,[{x:data.map(r=>r.count),y:data.map(r=>r.value||'Sin info'),type:'bar',orientation:'h',marker:{color:isD?'#6366f1':'#4f46e5'},text:data.map(r=>String(r.count)),textposition:'outside',textfont:{color:tc}}],
        {paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{color:tc,family:'Outfit',size:10},xaxis:{gridcolor:gc},yaxis:{gridcolor:gc,automargin:true},showlegend:false,margin:{t:4,b:20,l:100,r:10},modebar:{remove:['zoom','pan','select','lasso','zoomin','zoomout','autoScale','resetScale','toggleSpikelines']}},
        {responsive:true,displayModeBar:false})
}
function switchTab(t){tab=t;document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.textContent.toLowerCase().includes(t)));drawDemand()}

function renderCoverage(cov){
    if(!cov)return;const vals=Object.values(cov||{});if(!vals.length)return;
    const avg=vals.reduce((a,x)=>a+(x.coverage_pct||0),0)/vals.length;const total=vals[0]?.total||0;
    $('qualityPct').textContent='Calidad: '+Math.round(avg)+'%';
    $('qualityPop').innerHTML=vals.map(x=>{
        const cls=x.coverage_pct>=90?'gr':x.coverage_pct>=70?'am':'re';
        return `<div style="display:flex;align-items:center;gap:6px;padding:2px 0;font-size:.74rem">
            <span style="width:90px;color:var(--t2)">${x.field}</span>
            <div style="flex:1;height:4px;background:var(--el);border-radius:2px;overflow:hidden">
                <div style="height:100%;border-radius:2px;width:${x.coverage_pct}%;background:var(--${cls})"></div>
            </div>
            <span style="width:40px;text-align:right">${x.populated}/${x.total}</span>
        </div>`
    }).join('')
}
function toggleQual(){const pop=$('qualityPop');pop.classList.toggle('open');if(pop.classList.contains('open')){setTimeout(()=>{const c=e=>{if(!pop.contains(e.target)&&e.target.id!=='qualityBtn'){pop.classList.remove('open');document.removeEventListener('click',c)}};document.addEventListener('click',c)},10)}}

function toggleMore(){$('extraFilters').classList.toggle('open')}
function clearAll(){$('filterExecutive').value='';['filterSource','filterOperation','filterType','filterCommune','filterTemp','filterStage'].forEach(id=>{const e=$(id);if(e)e.value=''});if(FP)FP.clear();load()}
function toggleTheme(){TH=TH==='dark'?'light':'dark';localStorage.setItem('procasa_theme',TH);document.documentElement.setAttribute('data-theme',TH);const i=$('themeIcon');if(i)i.className=TH==='light'?'fa-solid fa-moon':'fa-solid fa-sun';load()}
function toggleMobileMenu(){const s=$('sidebar');s.classList.toggle('mobile-open');$('sidebarOverlay').style.display=s.classList.contains('mobile-open')?'block':'none'}
function closeSidebar(){$('sidebar').classList.remove('mobile-open');$('sidebarOverlay').style.display='none'}
