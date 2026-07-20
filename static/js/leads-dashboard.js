let TH = 'dark', FP, tab = 'operacion';

(function(){const s=localStorage.getItem('procasa_theme')||'dark';TH=s;document.documentElement.setAttribute('data-theme',s);const i=document.getElementById('themeIcon');if(i)i.className=s==='light'?'fa-solid fa-moon':'fa-solid fa-sun';})();
function $(id){return document.getElementById(id);}
function gv(id){const e=$(id);return e?e.value:'';}
function esc(s){if(s===null||s===undefined)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmt(d){return d.toISOString().split('T')[0];}
function ago(d){const dt=new Date();dt.setDate(dt.getDate()-d);return dt;}

function logE(ctx,err){console.error('[DASH] '+ctx,err);}

document.addEventListener('DOMContentLoaded',function(){
    FP=flatpickr('#dateRange',{mode:'range',dateFormat:'Y-m-d',locale:'es',defaultDate:[ago(30),ago(0)],onChange:load});
    fetch('/api/analytics/leads/filters',{credentials:'same-origin'}).then(r=>r.json()).then(d=>{
        fill('filterExecutive',d.executives,'Ejecutivo');
    }).catch(e=>logE('filters',e));
    load();
});

function fill(id,items,ph){
    const el=$(id);if(!el)return;const cv=el.value;
    el.innerHTML=`<option value="">${ph}</option>`;
    (items||[]).forEach(x=>{const v=x.value||x.executive||x._id||x;el.innerHTML+=`<option value="${esc(v)}">${esc(x.label||v)}${x.count!==undefined?' ('+x.count+')':''}</option>`;});
    try{el.value=cv;}catch(_){}
}

function range(){
    if(!FP||!FP.selectedDates||FP.selectedDates.length<2)return{s:fmt(ago(30)),e:fmt(ago(0))};
    return{s:fmt(FP.selectedDates[0]),e:fmt(FP.selectedDates[1])};
}

function load(){
    const show=!!gv('filterExecutive');$('btnClear').style.display=show?'inline-flex':'none';
    const{s,e}=range(),exec=gv('filterExecutive');
    const p=new URLSearchParams();if(s)p.set('period_start',s);if(e)p.set('period_end',e);if(exec)p.set('executive',exec);
    fetch('/api/analytics/leads/dashboard?'+p,{credentials:'same-origin'})
        .then(r=>{if(!r.ok)throw new Error(r.status+' '+r.statusText);return r.json();})
        .then(render)
        .catch(e=>{logE('dashboard',e);$('summaryText').textContent='Error al cargar. Reintente.';});
}

function render(d){
    const k=d.kpis||{};
    setKpi(0,k.received?.value,k.received?.variation_label,'Periodo');
    setKpi(1,k.active?.value,null,'Actual');
    setKpi(2,k.hot?.value,k.hot?.pct+'% de activos','Actual');
    setKpi(3,k.unassigned?.value,k.unassigned?.pct+'% de activos','Actual');
    setKpi(4,k.aging?.value?k.aging.value+'d':'--',null,'Mediana');

    $('summaryText').textContent=d.summary_text||'';

    renderTrends(d.comparative_trends);
    renderPriorities(d.priorities);
    renderSourceQuality(d.source_quality);
    renderExecLoad(d.executive_load);
    renderCartera(d.by_stage||[],d.closed_won_current||0);
    renderDist(d.distributions);
    renderQuality(d.coverage);
}

function setKpi(idx,val,sub,scope){
    const cards=document.querySelectorAll('#kpiGrid .kpi');
    if(idx>=cards.length)return;
    const c=cards[idx];
    const sc=c.querySelector('.kpi-scope'),v=c.querySelector('.kpi-val'),p=c.querySelector('.kpi-pct'),vr=c.querySelector('.kpi-var');
    if(sc)sc.textContent=scope;
    if(v){v.textContent=val!==undefined&&val!==null?val:'--';v.classList.remove('skel');}
    if(p) p.textContent = sub||'';
    if(vr) vr.textContent = sub||'';
    if(vr&&sub&&sub.includes('menos'))vr.classList.add('neg');else if(vr)vr.classList.remove('neg');
}

function renderTrends(ct){
    if(!ct){return;}
    const cur=ct.current||{},prev=ct.previous||{};
    const cD=cur.daily||[],pD=prev.daily||[];
    $('trendSub').textContent='Actual: '+cur.total+' (+'+cur.avg_daily+'/dia) | Anterior: '+prev.total+' | Var: '+(ct.variation_pct>=0?'+':'')+ct.variation_pct+'%';

    const el=$('chartTrends');if(!cD.length){el.innerHTML='<div style="text-align:center;padding:40px;color:var(--text-secondary)">Sin datos en el periodo</div>';return;}
    const isD=TH==='dark';
    const tc=isD?'#f8fafc':'#172033',gc=isD?'#1e293b':'#cbd5e1';
    const traces=[
        {x:cD.map(r=>r.date),y:cD.map(r=>r.received),type:'scatter',mode:'lines+markers',name:'Actual',line:{shape:'spline',color:'#6366f1',width:3},fill:'tozeroy',fillcolor:'rgba(99,102,241,0.08)',marker:{size:3}},
    ];
    if(pD.length){
        const pMap={};pD.forEach(r=>pMap[r.date]=r.received);
        const aligned=cD.map(r=>pMap[r.date]||null);
        traces.push({x:cD.map(r=>r.date),y:aligned,type:'scatter',mode:'lines',name:'Anterior',line:{shape:'spline',color:'#94a3b8',width:2,dash:'dot'}});
    }
    Plotly.newPlot(el,traces,
        {paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',font:{color:tc,family:'Outfit',size:11},xaxis:{gridcolor:gc},yaxis:{gridcolor:gc},legend:{orientation:'h',y:1.1,font:{size:10}},margin:{t:20,b:30,l:45,r:10}},
        {responsive:true});
}

function renderPriorities(p){
    if(!p)return;
    const items=(p.alerts||[]).filter(a=>a.count>0);
    $('prioritiesList').innerHTML=items.length?items.map(a=>{
        const sev=a.severity==='high'?'high':a.severity==='medium'?'medium':'low';
        return `<div class="prio-item"><span class="prio-count ${sev}">${a.count}</span><span>${a.label}</span><span style="font-size:0.7rem;color:var(--text-secondary);margin:0 4px">${a.description}</span><a href="/crm" class="prio-link">Ver en Leads</a></div>`;
    }).join(''):'<div style="color:var(--text-secondary);font-size:0.8rem;padding:8px 0">Sin prioridades detectadas</div>';
}

function renderSourceQuality(sq){
    if(!sq)return;
    const srcs=(sq.sources||[]).slice(0,8);
    $('sourceQuality').innerHTML=srcs.length?srcs.map(s=>{
        const mx=srcs[0].active||1;
        return `<div class="quality-row">
            <span class="q-src">${esc(s.source)}</span>
            <div class="q-bar"><div class="q-bar-inner" style="width:${(s.active/mx*100).toFixed(0)}%;background:var(--accent)"></div></div>
            <span class="q-count">${s.active}</span>
            <span class="q-metric" style="color:var(--hot)">${s.hot_pct}%</span>
            <span class="q-metric">${s.assigned_pct}%</span>
            <span class="q-metric">${s.contacted_pct}%</span>
        </div>`;
    }).join(''):'<div style="color:var(--text-secondary);font-size:0.8rem">Sin datos</div>';
}

function renderExecLoad(el){
    if(!el)return;
    const execs=(el.executives||[]).filter(e=>e.executive!=='Sin Asignar').slice(0,8);
    $('execLoad').innerHTML=execs.length?execs.map(e=>{
        const mx=execs[0].active||1;
        return `<div class="exec-row">
            <span class="exec-name">${esc(e.executive.split(' ')[0])}</span>
            <div class="exec-bar"><div class="exec-bar-fill" style="width:${(e.active/mx*100).toFixed(0)}%;background:var(--accent)"></div></div>
            <span class="exec-hot" title="Hot">${e.hot}</span>
            <span class="exec-new" title="NEW/sin etapa">${e.new_or_none}</span>
            <span class="exec-age" title="Mediana antiguedad">${e.median_age_days}d</span>
        </div>`;
    }).join(''):'<div style="color:var(--text-secondary);font-size:0.8rem">Sin datos</div>';
}

function renderCartera(stages,won){
    const map={};stages.forEach(s=>{map[s.stage||'Sin etapa']=s.count;});
    const contacted=map['CONTACTED']||0,newc=map['NEW']||0,none=map['Sin etapa']||0;
    const total=contacted+newc+none+won;
    if(!total){$('carteraState').innerHTML='<div style="color:var(--text-secondary);font-size:0.8rem">Sin datos</div>';return;}
    function seg(cls,v,lbl){return v>0?`<div class="cartera-seg ${cls}" style="width:${(v/total*100).toFixed(1)}%">${v>total*.05?lbl+' '+v:''}</div>`:'';}
    $('carteraState').innerHTML=
        '<div class="cartera-bar">'+seg('contacted',contacted,'Contactado')+seg('new',newc,'NEW')+seg('none',none,'Sin etapa')+seg('won',won,'Ganado')+'</div>'+
        '<div class="cartera-legend"><span><i class="fa-solid fa-circle" style="color:#6366f1"></i> Contactado '+contacted+'</span><span><i class="fa-solid fa-circle" style="color:#f59e0b"></i> NEW '+newc+'</span><span><i class="fa-solid fa-circle" style="color:#64748b"></i> Sin etapa '+none+'</span><span><i class="fa-solid fa-circle" style="color:#10b981"></i> Ganado '+won+'</span></div>';
}

function renderDist(d){
    if(!d)return;window._dist=d;drawDemand();
}

function drawDemand(){
    const d=window._dist||{};
    let data=tab==='operacion'?(d.operations||[]):tab==='tipo'?(d.types||[]):(d.communes||[]);
    const el=$('chartDemand');if(!data.length){el.innerHTML='<div style="text-align:center;padding:40px;color:var(--text-secondary)">Sin datos</div>';return;}
    data=data.slice(0,10);
    const isD=TH==='dark',tc=isD?'#f8fafc':'#172033',gc=isD?'#1e293b':'#cbd5e1';
    const vals=data.map(r=>r.count),labels=data.map(r=>r.value||'Sin info');
    Plotly.newPlot(el,[{x:vals,y:labels,type:'bar',orientation:'h',marker:{color:isD?'#6366f1':'#4f46e5'},text:vals.map(String),textposition:'outside',textfont:{color:tc}}],
        {paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',font:{color:tc,family:'Outfit',size:10},xaxis:{gridcolor:gc},yaxis:{gridcolor:gc,automargin:true},showlegend:false,margin:{t:4,b:20,l:100,r:10}},
        {responsive:true});
}

function switchTab(t){tab=t;document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.textContent.toLowerCase().includes(t)));drawDemand();}

function renderQuality(cov){
    if(!cov)return;
    const vals=Object.values(cov||{});if(!vals.length)return;
    const avg=vals.reduce((a,x)=>a+(x.coverage_pct||0),0)/vals.length;
    $('qualityPct').textContent='Calidad: '+Math.round(avg)+'%';
    $('qualityPop').innerHTML=vals.map(x=>{
        const cls=x.coverage_pct>=90?'green':x.coverage_pct>=70?'amber':'red';
        return `<div style="display:flex;align-items:center;gap:6px;padding:2px 0;font-size:0.74rem"><span style="width:90px;color:var(--text-secondary)">${x.field}</span><div style="flex:1;height:4px;background:var(--bg-elevated);border-radius:2px;overflow:hidden"><div style="height:100%;border-radius:2px;background:var(--${cls})" class="q-bar-inner" style="width:${x.coverage_pct}%"></div></div><span style="width:40px;text-align:right">${x.populated}</span></div>`;
    }).join('');
}

function toggleQual(){
    const pop=$('qualityPop');pop.classList.toggle('open');
    if(pop.classList.contains('open')){setTimeout(()=>{const c=e=>{if(!pop.contains(e.target)&&e.target.id!=='qualityBtn'){pop.classList.remove('open');document.removeEventListener('click',c);}};document.addEventListener('click',c);},10);}
}

function clearAll(){$('filterExecutive').value='';if(FP)FP.clear();load();}

function toggleTheme(){
    TH=TH==='dark'?'light':'dark';localStorage.setItem('procasa_theme',TH);
    document.documentElement.setAttribute('data-theme',TH);
    const i=$('themeIcon');if(i)i.className=TH==='light'?'fa-solid fa-moon':'fa-solid fa-sun';
    load();
}

function toggleMobileMenu(){const s=$('sidebar');s.classList.toggle('mobile-open');$('sidebarOverlay').style.display=s.classList.contains('mobile-open')?'block':'none';}
function closeSidebar(){$('sidebar').classList.remove('mobile-open');$('sidebarOverlay').style.display='none';}
