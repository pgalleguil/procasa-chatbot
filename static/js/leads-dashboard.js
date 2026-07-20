let TH='dark',FP,tab='operacion',mainTab='operacion',execSort='active',_dist={},_exec=[],_srcData=[];
(function(){const s=localStorage.getItem('procasa_theme')||'dark';TH=s;document.documentElement.setAttribute('data-theme',s);const i=document.getElementById('themeIcon');if(i)i.className=s==='light'?'fa-solid fa-moon':'fa-solid fa-sun'})();
function $(id){return document.getElementById(id)}
function gv(id){const e=$(id);return e?e.value:''}
function esc(s){if(s===null||s===undefined)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function ago(d){const dt=new Date();dt.setDate(dt.getDate()-d);return dt}

document.addEventListener('DOMContentLoaded',function(){
    FP=flatpickr('#dateRange',{mode:'range',dateFormat:'Y-m-d',locale:'es',defaultDate:[ago(30),ago(0)],onChange:load});
    Promise.all([
        fetch('/api/analytics/leads/filters',{credentials:'same-origin'}).then(r=>r.json()),
        fetch('/api/analytics/leads/distributions?universe=received_in_period',{credentials:'same-origin'}).then(r=>r.json())
    ]).then(([f,dist])=>{
        fill('filterExecutive',f.executives,'Ejecutivo');
        fill('filterSource',f.sources,'Origen');fill('filterStage',f.stages,'Etapa');
        fill('filterOperation',dist.operations,'Operacion');
        fill('filterType',dist.types,'Tipo');
        fill('filterCommune',dist.communes,'Comuna');
    }).catch(e=>console.error('[DASH] init',e));
    load();
});
function fill(id,items,ph){const el=$(id);if(!el)return;const cv=el.value;el.innerHTML=`<option value="">${ph}</option>`;(items||[]).forEach(x=>{const v=x.value||x.executive||x._id||x;el.innerHTML+=`<option value="${esc(v)}">${esc(x.label||x.value||v)}${x.count!==void 0?' ('+x.count+')':''}</option>`});try{el.value=cv}catch(_){}}
function range(){if(!FP||!FP.selectedDates||FP.selectedDates.length<2)return{s:fmt(ago(30)),e:fmt(ago(0))};return{s:fmt(FP.selectedDates[0]),e:fmt(FP.selectedDates[1])}}
function fmt(dt){return dt.toISOString().split('T')[0]}

function load(){
    $('btnClear').style.display=gv('filterExecutive')?'inline-flex':'none';
    const{s,e}=range(),exec=gv('filterExecutive'),p=new URLSearchParams();
    if(s)p.set('period_start',s);if(e)p.set('period_end',e);if(exec)p.set('executive',exec);
    fetch('/api/analytics/leads/dashboard?'+p,{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(render).catch(e=>{console.error('[DASH]',e);$('statusStrip').textContent='Error al cargar. Reintente.'})
}

function render(d){
    renderStrip(d);
    renderPriorities(d.priorities||{});
    renderCohort(d.funnel||[]);
    _exec=d.executive_load||[];renderExec();
    _srcData=d.source_performance||[];renderSources();
    renderPropRank(d.property_ranking||[],d.no_code_count||0);
    _dist=d.demand||{};drawDemand();
    renderCoverage(d.coverage?.fields||{});
    renderUnavailable(d);
}

function renderStrip(d){
    const k=d.kpis||{},r=k.received||{},s=k.active||{};
    const varCls='var-'+(r.variation_pct!==null?(r.variation_pct>=0?'green':'red'):'');
    const varIcon=r.variation_pct!==null?(r.variation_pct>=0?'↑':'↓'):'';
    const varTxt=r.variation_pct!==null?Math.abs(r.variation_pct)+'%':'N/A';
    const hot=k.temperature?.hot||0,cold=k.temperature?.cold||0,un=k.temperature?.unknown||0;
    $('statusStrip').innerHTML=
        `<span class="highlight">${r.value||0}</span> leads ingresados · <span class="${varCls}">${varIcon} ${varTxt}</span> frente al periodo anterior`+
        (s.value?` <span class="badge badge-green">${s.value} activos actuales</span>`:'')+
        (hot?` <span class="badge badge-hot">${hot} Hot</span>`:'')+
        (un?` <span class="badge badge-green">${un} sin temperatura</span>`:'')
}

function renderPriorities(p){
    const alerts=(p.alerts||[]).filter(a=>a.count>0);
    let html='<div class="prio-list">';
    // Build priority rows
    const items=[
        {key:'hot_unassigned',sev:'danger',lbl:'Hot sin ejecutivo',desc:'Leads Hot sin ejecutivo asignado',act:'Asignar ahora',cond:a=>a.type==='hot_unassigned',defCount:0},
        {key:'hot_new_assigned',sev:'danger',lbl:'Hot en etapa NEW',desc:'Leads Hot cuya etapa efectiva es NEW',act:'Revisar prioridad',cond:a=>a.type==='hot_new_assigned',defCount:0},
        {key:'critical',sev:'danger',lbl:'Prioridad critica actual',desc:'Leads con priority_bucket = CRITICAL',act:'Atender urgente',cond:a=>a.type==='priority_critical',defCount:0},
        {key:'unassigned_48h',sev:'warn',lbl:'Sin asignar por mas de 48 horas',desc:'Leads activos sin ejecutivo desde hace mas de 48h',act:'Asignar ahora',cond:a=>a.type==='unassigned_over_48h',defCount:0},
        {key:'stuck_new',sev:'warn',lbl:'Estancados en etapa inicial',desc:'NEW o sin etapa por mas de 7 dias',act:'Revisar en CRM',cond:a=>a.type==='new_over_7d',defCount:0},
    ];
    items.forEach(item=>{
        const found=alerts.find(item.cond);
        const count=found?found.count:item.defCount;
        const sev=count>0?item.sev:'ok';
        const label=count>0?item.lbl:`0 ${item.lbl}`;
        html+=`<div class="prio-row"><span class="prio-num ${sev}">${count}</span><div class="prio-desc"><strong>${label}</strong><span>${item.desc}</span></div><a href="/crm" class="prio-action">${item.act}</a></div>`
    });
    // Quality separator
    html+=`<hr class="prio-sep">`;
    // Quality alerts
    const noCode=alerts.find(a=>a.type==='no_source');
    const noExec=alerts.find(a=>a.type==='no_executive');
    const qItems=[
        {key:'no_code',lbl:'Sin codigo de propiedad',desc:'Leads activos sin codigo de propiedad registrado',sev:'neutral',cond:a=>a.type==='no_source',defCount:0},
    ];
    qItems.forEach(item=>{
        const found=alerts.find(item.cond);
        const count=found?found.count:item.defCount;
        html+=`<div class="prio-row"><span class="prio-num ${item.sev}">${count}</span><div class="prio-desc"><strong>${item.lbl}</strong><span>${item.desc}</span></div></div>`
    });
    html+='</div>';
    $('prioritiesBlock').innerHTML=html
}

function renderCohort(f){
    const el=$('cohortBlock');
    if(!f||!f.length<2){el.innerHTML='<div style="color:var(--t2);padding:20px 0">Sin datos de cohorte en el periodo</div>';return}
    // Determine biggest loss
    let maxLoss=0,maxLossLabel='';
    for(let i=1;i<f.length;i++){const loss=f[i-1].count-f[i].count;if(loss>maxLoss){maxLoss=loss;maxLossLabel=f[i-1].label+' -> '+f[i].label}}
    const colors=['#6366f1','#f59e0b','#10b981','#34d399'];
    const labels=['Recibidos','Asignados actualmente','Avanzados actualmente','Cerrados ganados actualmente'];
    const stageKeys=['received','assigned','advanced','won'];
    const steps=stageKeys.map((sk,i)=>{
        const s=f.find(x=>x.stage===sk)||{count:0,pct_of_cohort:0};
        return {count:s.count,pct:s.pct_of_cohort,label:labels[i],color:colors[i]}
    });
    let html='<div class="cohort-grid">';
    steps.forEach((s,i)=>{
        html+=`<div class="cohort-step step${i+1}"><div class="cohort-num">${s.count}</div><div class="cohort-lbl">${s.label}</div><div class="cohort-pct">${s.pct}%</div></div>`;
        if(i<steps.length-1){
            const loss=steps[i].count-steps[i+1].count;
            const pctLoss=steps[i].count>0?Math.round(loss/steps[i].count*100):0;
            html+=`<div class="cohort-arrow"><i class="fa-solid fa-arrow-right"></i><span style="font-size:.68rem">-${loss}</span></div>`
        }
    });
    html+='</div>';
    if(maxLoss>0)html+=`<div class="cohort-note">Mayor reduccion actual: ${maxLossLabel} (${maxLoss} leads)</div>`;
    el.innerHTML=html
}

function renderExec(){
    const el=$('execBlock');const load=_exec;
    if(!load||!load.length){el.innerHTML='<div style="color:var(--t2);padding:12px 0">Sin datos</div>';return}
    const sorted=[...load].sort((a,b)=>(b[execSort]||0)-(a[execSort]||0));
    const mx=sorted[0].active||1;
    let top8=sorted.slice(0,8);
    html='<div class="grid-h"><span class="g-c g-name">Ejecutivo</span><span class="g-c g-bar-c" style="flex:1">Cartera</span><span class="g-c g-hot">Hot</span><span class="g-c g-pend">Pend</span><span class="g-c g-crit">Crit</span><span class="g-c g-age">Edad</span></div>';
    top8.forEach(e=>{
        const hotPct=Math.round(e.hot/e.active*100);
        const restPct=100-hotPct;
        html+=`<div class="grid-r"><span class="g-c g-name">${esc(e.executive.split(' ')[0])}</span>
        <div class="g-c g-bar-c" style="flex:1"><div class="exec-stack" style="width:${Math.round(e.active/mx*60+20)}%"><div class="seg seg-hot" style="width:${hotPct}%"></div><div class="seg seg-rest" style="width:${restPct}%"></div></div><span class="g-c g-rec">${e.active}</span></div>
        <span class="g-c g-hot">${e.hot}</span><span class="g-c g-pend">${e.pending_gt_7d}</span><span class="g-c g-crit">${e.critical}</span><span class="g-c g-age">${e.median_age_days}d</span></div>`
    });
    if(load.length>8)html+=`<div style="text-align:center;padding:6px 0;font-size:.76rem;color:var(--ac);cursor:pointer" onclick="renderExecAll()">Ver todos (${load.length})</div>`;
    el.innerHTML=html
}
function renderExecAll(){
    const el=$('execBlock');const load=_exec;
    const sorted=[...load].sort((a,b)=>(b[execSort]||0)-(a[execSort]||0));
    const mx=sorted[0].active||1;
    let html='<div class="grid-h"><span class="g-c g-name">Ejecutivo</span><span class="g-c g-bar-c" style="flex:1">Cartera</span><span class="g-c g-hot">Hot</span><span class="g-c g-pend">Pend</span><span class="g-c g-crit">Crit</span><span class="g-c g-age">Edad</span></div>';
    sorted.forEach(e=>{
        const hotPct=Math.round(e.hot/e.active*100);
        const restPct=100-hotPct;
        html+=`<div class="grid-r"><span class="g-c g-name">${esc(e.executive.split(' ')[0])}</span>
        <div class="g-c g-bar-c" style="flex:1"><div class="exec-stack" style="width:${Math.round(e.active/mx*60+20)}%"><div class="seg seg-hot" style="width:${hotPct}%"></div><div class="seg seg-rest" style="width:${restPct}%"></div></div><span class="g-c g-rec">${e.active}</span></div>
        <span class="g-c g-hot">${e.hot}</span><span class="g-c g-pend">${e.pending_gt_7d}</span><span class="g-c g-crit">${e.critical}</span><span class="g-c g-age">${e.median_age_days}d</span></div>`
    });
    el.innerHTML=html
}

function sortExec(field){execSort=field;document.querySelectorAll('.sort-opt a').forEach(a=>a.classList.remove('sort-active'));const m=$('sort-'+{active:'active',hot:'hot',pending_gt_7d:'pend',critical:'crit',median_age_days:'age'}[field]||'active');if(m)m.classList.add('sort-active');renderExec()}

function renderSources(){
    const el=$('sourceBlock');const srcs=_srcData;
    if(!srcs||!srcs.length){el.innerHTML='<div style="color:var(--t2);padding:12px 0">Sin datos en el periodo</div>';return}
    const minSample=15;
    let mainSrcs=srcs.filter(s=>s.received>=minSample);
    let others=srcs.filter(s=>s.received<minSample);
    let display=mainSrcs.slice(0,5);
    if(others.length>0)display.push({source:'Otras fuentes ('+others.length+')',received:others.reduce((a,x)=>a+x.received,0),hot_pct:null,assigned_pct:null,advanced_pct:null,variation_pct:null});
    const topVol=display.reduce((a,x)=>Math.max(a,x.received||0),0);
    const bestPerf=display.reduce((a,x)=>(x.advanced_pct||0)>a?x.advanced_pct:a,0);
    const bestSrc=display.find(x=>x.advanced_pct===bestPerf);
    
    // Scatter plot
    const isD=TH==='dark',tc=isD?'#f8fafc':'#172033',gc=isD?'#1e293b':'#cbd5e1';
    const scatterEl=document.createElement('div');scatterEl.style.height='160px';
    el.innerHTML='';
    el.appendChild(scatterEl);
    const scatterData=display.filter(s=>s.hot_pct!==null&&s.advanced_pct!==null&&s.received>=minSample);
    if(scatterData.length>=2){
        Plotly.newPlot(scatterEl,scatterData.map(s=>({
            x:[s.received],y:[s.advanced_pct],mode:'markers+text',
            marker:{size:Math.max(8,s.hot_pct||10),sizeref:2,sizemode:'area',color:isD?'#6366f1':'#4f46e5'},
            text:[s.source],textposition:'top center',textfont:{color:tc,size:9},
            hovertemplate:`<b>%{text}</b><br>Volumen: %{x}<br>% Avanzados: %{y}<br>% Hot: ${s.hot_pct}%<extra></extra>`
        })),{
            paper_bgcolor:'transparent',plot_bgcolor:'transparent',
            font:{color:tc,family:'Outfit',size:10},
            xaxis:{title:{text:'Volumen',font:{size:10}},gridcolor:gc},
            yaxis:{title:{text:'% Avanzados',font:{size:10}},gridcolor:gc,range:[0,Math.min(100,Math.max(...scatterData.map(x=>x.advanced_pct||0))+20)]},
            showlegend:false,margin:{t:10,b:30,l:50,r:20},
            annotations:[
                topVol?{x:topVol,y:0,xanchor:'left',yanchor:'top',text:'Mayor volumen: '+display.find(x=>x.received===topVol)?.source,showarrow:false,font:{size:9,color:tc}}:{},
                bestSrc?{x:bestSrc.received,y:bestPerf,text:'Mejor perfil: '+bestSrc.source,showarrow:false,font:{size:9,color:tc},yanchor:'bottom'}:{}
            ]
        },{responsive:true,displayModeBar:false})
    } else {scatterEl.innerHTML='<div style="color:var(--t2);padding:30px;text-align:center">Fuentes insuficientes para grafico</div>'}

    // Ranking table
    let html='<div style="margin-top:8px"><div class="grid-h"><span class="g-c g-src">Fuente</span><span class="g-c g-rec">Leads</span><span class="g-c g-pct">Part</span><span class="g-c g-pct">Hot</span><span class="g-c g-pct">Avz</span><span class="g-c g-var">Var</span></div>';
    display.forEach(s=>{
        const vp=s.variation_pct;const vCls=vp!==null?(vp>=0?'pos':'neg'):'';const vD=vp!==null?(vp>=0?'+':'')+vp+'%':'--';
        const total=_srcData.reduce((a,x)=>a+(x.received||0),0);
        const pct=total>0?Math.round(s.received/total*100):0;
        html+=`<div class="grid-r"><span class="g-c g-src">${esc(s.source)}</span><span class="g-c g-rec">${s.received}</span><span class="g-c g-pct">${pct}%</span><span class="g-c g-pct" style="color:var(--ho)">${s.hot_pct!==null?s.hot_pct+'%':'--'}</span><span class="g-c g-pct" style="color:var(--gr)">${s.advanced_pct!==null?s.advanced_pct+'%':'--'}</span><span class="g-c g-var ${vCls}">${vD}</span></div>`
    });
    html+='</div>';
    el.innerHTML+=html
}

function renderPropRank(ranking,nc){
    const el=$('propBlock');
    if(!ranking||!ranking.length){el.innerHTML='<div style="color:var(--t2);padding:12px 0">Sin datos en el periodo</div>';return}
    const mx=ranking[0].count||1;
    html='<div class="grid-h"><span class="g-c g-name" style="width:70px">Codigo</span><span class="g-c g-bar-c" style="flex:1">Leads</span><span class="g-c g-hot">Hot</span><span class="g-c g-pct">Asig</span><span class="g-c g-pct">Avz</span><span class="g-c g-src">Fuente</span></div>';
    ranking.forEach(r=>{
        html+=`<div class="grid-r"><span class="g-c g-name" style="width:70px;font-size:.74rem">${esc(r.code)}</span><div class="g-c g-bar-c" style="flex:1"><div class="g-bar"><div class="g-bar-w" style="width:${r.count/mx*100}%;background:var(--ac)"></div></div><span class="g-c g-rec">${r.count}</span></div><span class="g-c g-hot">${r.hot_pct}%</span><span class="g-c g-pct">${r.assigned_pct}%</span><span class="g-c g-pct">${r.advanced_pct}%</span><span class="g-c g-src" style="font-size:.7rem">${esc(r.dominant_source.substring(0,8))}</span></div>`
    });
    if(nc)html+=`<div style="font-size:.74rem;color:var(--t2);margin-top:4px">${nc} leads sin codigo de propiedad</div>`;
    el.innerHTML=html
}

function drawDemand(){
    const d=_dist||{};let data=tab==='operacion'?(d.operations||[]):tab==='tipo'?(d.types||[]):(d.communes||[]);
    const el=$('chartDemand');if(!data.length){el.innerHTML='<div style="text-align:center;padding:40px;color:var(--t2)">Sin datos</div>';return}
    data=data.slice(0,tab==='comuna'?10:7);
    const isD=TH==='dark',tc=isD?'#f8fafc':'#172033',gc=isD?'#1e293b':'#cbd5e1';
    Plotly.newPlot(el,[{x:data.map(r=>r.count),y:data.map(r=>r.value||'Sin info'),type:'bar',orientation:'h',marker:{color:isD?'#6366f1':'#4f46e5'},text:data.map(r=>String(r.count)),textposition:'outside',textfont:{color:tc}}],
        {paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{color:tc,family:'Outfit',size:10},xaxis:{gridcolor:gc},yaxis:{gridcolor:gc,automargin:true},showlegend:false,margin:{t:4,b:20,l:100,r:10}},
        {responsive:true,displayModeBar:false})
}
function switchTab(t){tab=t;document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.textContent.toLowerCase().includes(t)));drawDemand()}

function switchMain(t){
    mainTab=t;document.querySelectorAll('.tab-main').forEach(b=>b.classList.toggle('active',b.textContent.toLowerCase().includes(t)));
    $('panelOperacion').classList.toggle('active',t==='operacion');
    $('panelDemanda').classList.toggle('active',t==='demanda');
    $('panelCalidad').classList.toggle('active',t==='calidad');
    if(t==='demanda')setTimeout(drawDemand,50)
}

function renderCoverage(cov){
    const el=$('coverageBlock');if(!cov){el.innerHTML='<div style="color:var(--t2);padding:12px 0">Sin datos</div>';return}
    const vals=Object.values(cov||{});
    let html='<div class="coverage-grid">';
    vals.forEach(x=>{
        const cls=x.coverage_pct>=90?'good':x.coverage_pct>=70?'warn':'bad';
        html+=`<div class="cov-item"><span class="field">${x.field}</span> <span class="val">${x.coverage_pct}%</span><div class="cov-bar ${cls}" style="width:${x.coverage_pct}%"></div><div style="font-size:.65rem;color:var(--t2)">${x.populated}/${x.total}</div></div>`
    });
    html+='</div>';el.innerHTML=html
}

function renderUnavailable(d){
    const el=$('unavailBlock');
    const items=[
        {label:'Primera respuesta',reason:!d.kpis?.management?.sample_sufficient?'Cobertura insuficiente: '+(d.kpis?.management?.total_with_evidence||0)+'/'+(d.kpis?.management?.total_assigned||0)+' leads':'Disponible'},
        {label:'SLA de gestion',reason:'Requiere instrumentacion de gestiones canonicas'},
        {label:'Productividad por ejecutivo',reason:'Requiere historial de gestiones con lead_id'},
        {label:'Conversion historica',reason:'No existe temperatura historica ni stage_history completo'},
    ];
    el.innerHTML=items.map(x=>`<div class="unavail-item"><span>${x.label}</span><span class="reason">${x.reason}</span></div>`).join('')
}

function toggleMore(){$('extraFilters').classList.toggle('open')}
function clearAll(){['filterExecutive','filterSource','filterOperation','filterType','filterCommune','filterTemp','filterStage'].forEach(id=>{const e=$(id);if(e)e.value=''});if(FP)FP.clear();load()}
function toggleTheme(){TH=TH==='dark'?'light':'dark';localStorage.setItem('procasa_theme',TH);document.documentElement.setAttribute('data-theme',TH);const i=$('themeIcon');if(i)i.className=TH==='light'?'fa-solid fa-moon':'fa-solid fa-sun';load()}
function toggleMobileMenu(){const s=$('sidebar');s.classList.toggle('mobile-open');$('sidebarOverlay').style.display=s.classList.contains('mobile-open')?'block':'none'}
function closeSidebar(){$('sidebar').classList.remove('mobile-open');$('sidebarOverlay').style.display='none'}
