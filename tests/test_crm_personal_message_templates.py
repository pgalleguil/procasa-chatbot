from pathlib import Path
import shutil
import subprocess

import mongomock
import pytest

from chatbot.crm_personal_message_templates import (
    read_library, write_library, validate_template, MAX_TEMPLATES,
)


def entry(title="Mi saludo", body="Hola [[nombre]], soy [[ejecutivo]]. [[enlace]]"):
    return {"title": title, "body": body, "category": "stage1"}


def test_library_is_private_persistent_and_versioned():
    db = mongomock.MongoClient()["test"]
    a = write_library(db, "hernan", {"version": 0, "templates": [entry()]})
    assert a["version"] == 1
    assert read_library(db, "hernan") == a
    assert read_library(db, "mariela") == {"version": 0, "templates": []}
    b = write_library(db, "mariela", {"version": 0, "templates": [entry("Otro")]})
    assert b["templates"][0]["title"] == "Otro"
    assert write_library(db, "hernan", {"version": 0, "templates": []}) is None
    saved = dict(a["templates"][0], title="Actualizada")
    updated = write_library(db, "hernan", {"version": 1, "templates": [saved]})
    assert updated["templates"][0]["id"] == saved["id"]
    assert read_library(db, "mariela") == b
    assert write_library(db, "hernan", {"version": 2, "templates": []})["templates"] == []


@pytest.mark.parametrize("body", ["https://www.procasa.cl/1234", "www.procasa.cl/1234",
                                  "+56 9 12345678", "[[variable_inexistente]]"])
def test_unsafe_or_unknown_template_values_rejected(body):
    with pytest.raises(ValueError):
        validate_template(entry(body=body))


def test_limit_and_invalid_payloads():
    db = mongomock.MongoClient()["test"]
    with pytest.raises(ValueError):
        write_library(db, "u", {"version": 0, "templates": [entry()] * (MAX_TEMPLATES + 1)})
    with pytest.raises(ValueError):
        write_library(db, "u", {"version": True, "templates": []})


def test_composer_link_rendering_and_javascript_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for composer test")
    html = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8")
    source = html[html.index("        let pilotSelectedKey = null;"):html.index("        document.getElementById(\'waPilotClear\').onclick")] + html[html.index("        function addPropertyLink("):html.index("        function updateSendLink(")]
    script = """
const assert = require('assert');
const elements = {};
function element(id) {
    return elements[id] ||= {value:'', classList:{add(){},remove(){},toggle(){}},
      addEventListener(){},append(){},focus(){},setRangeText(){},textContent:''};
}
const document = {getElementById:element,createElement:()=>element('button')};
const window = {crmWhatsappComposerPilot:true};
let currentCommTarget = 'lead', currentCommChannel = 'wa';
let originalTemplateText = '';
const semState = {currentCategory:'stage1'};
const leadData = {nombre:'Ana Pérez',execName:'Hernán Castro',propiedad:'6318',
    comuna:'Ñuñoa',ownerName:'Dueño',publicContactUrl:'https://www.procasa.cl/6318'};
function updateSendLink(){}
""" + source + """
selectTemplate('Hola [[nombre]], soy [[ejecutivo]]. [[enlace]]');
assert.equal(element('commMessage').value,'Hola Ana, soy Hernán. https://www.procasa.cl/6318');
selectTemplate('Hola [[nombre]], ¿coordinamos visita?');
assert(element('commMessage').value.includes(leadData.publicContactUrl));
assert.equal(element('commMessage').value.split(leadData.publicContactUrl).length, 2);
assert(reusableDraft().includes('[[enlace]]'));
selectTemplate('Busquemos alternativas.', '', 'no-property');
assert(!element('commMessage').value.includes(leadData.publicContactUrl));
leadData.publicContactUrl = '';
selectTemplate('Hola [[nombre]], [[enlace]]');
assert(!element('commMessage').value.includes('http'));
assert(!element('commMessage').value.includes('[[enlace]]'));
currentCommTarget = 'owner';
leadData.publicContactUrl = 'https://www.procasa.cl/6318';
selectTemplate('Hola [[owner_name]]');
assert.equal(element('commMessage').value, 'Hola Dueño');
"""
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_api_scopes_library_to_authenticated_user_and_keeps_provider_logic():
    source = Path("webhook.py").read_text(encoding="utf-8")
    route = source[source.index('@app.get("/api/crm/my-whatsapp-templates")'):
                   source.index("# ---- Phone masking helper ----")]
    assert 'get_current_user_doc(request)' in route
    assert 'read_library(_sync_db(), user["_id"])' in route
    assert 'write_library(_sync_db(), user["_id"], payload)' in route
    assert route.count('if not composer_pilot_enabled(user["_id"]):') == 2
    assert "run_in_executor" in route
    assert "send_whatsapp" not in route


def test_legacy_composer_restored_and_pilot_isolated():
    html = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8")
    lead_templates = html[html.index("const commTemplates = {"):html.index("            owner: {", html.index("const commTemplates = {"))]
    for name in ("1. Derivado Chatbot", "2. Interés Activo", "3. Confirmación Visita",
                 "4. Post Visita", "5. Oferta", "6. Seguimiento", "Solicitud Visita (Alt)",
                 "Datos Incompletos", "Solicitar Documentos", "Cierre Definitivo"):
        assert name in lead_templates
    assert 'const personalMode = pilotComposerActive();' in html
    assert "return loadLegacyTemplates(target,category,btnElement)" in html
    assert "if (!pilotComposerActive()) return selectLegacyTemplate(text,subject);" in html
    assert "renderLegacyCommTabs(target);" in html
    assert "#commModal.theme-wa #commModalDialog" not in html
    assert "document.getElementById('commMessage').rows = active ? 7 : 5" in html


def test_pablo_only_pilot_and_private_favorites():
    from chatbot.crm_personal_message_templates import (
        composer_pilot_enabled, PABLO_COMPOSER_PILOT_USER_ID,
    )
    from bson import ObjectId
    uid = PABLO_COMPOSER_PILOT_USER_ID
    assert composer_pilot_enabled(ObjectId(uid))
    assert not composer_pilot_enabled("Pablo Galleguillos")
    assert not composer_pilot_enabled(None)
    assert not composer_pilot_enabled("other")
    db = mongomock.MongoClient()["test"]
    assert read_library(db, uid) == {"version": 0, "templates": [], "favorites": []}
    a = write_library(db, uid, {"version": 0, "templates": [entry(body="Hola [[nombre]], propiedad [[operacion]]: [[enlace]]")], "favorites": ["builtin:1"]})
    key = "personal:" + a["templates"][0]["id"]
    b = write_library(db, uid, {"version": 1, "templates": a["templates"], "favorites": ["builtin:1", key]})
    assert b["favorites"] == ["builtin:1", key]
    assert read_library(db, uid) == b
    assert read_library(db, "other") == {"version": 0, "templates": []}
    with pytest.raises(ValueError):
        write_library(db, "other", {"version": 0, "templates": [], "favorites": ["builtin:1"]})
    with pytest.raises(ValueError):
        write_library(db, "other", {"version": 0, "templates": [entry(body="[[operacion]]")]})
    with pytest.raises(ValueError):
        write_library(db, uid, {"version": 2, "templates": [], "favorites": [key]})
    assert write_library(db, uid, {"version": 1, "templates": [], "favorites": []}) is None
    assert write_library(db, uid, {"version": 2, "templates": [], "favorites": ["builtin:1"]})["favorites"] == ["builtin:1"]


def test_pilot_contexts_and_recommendation_draft_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    html = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8")
    webhook = Path("webhook.py").read_text(encoding="utf-8")
    assert '"crm_whatsapp_composer_pilot": composer_pilot_enabled(user.get("_id"))' in webhook
    assert "crm_whatsapp_composer_pilot|default(false)" in html
    assert "#commModal.wa-composer-pilot #waTemplateSearch" in html
    assert "grid-template-columns:minmax(220px, .85fr) minmax(0, 1.8fr)" in html
    assert "Recomendador de propiedades" in html
    assert "fetch('/api/crm/recommendations'" in html
    assert "btn.onclick = pilotComposerActive() ? preparePilotRecommendation : sendRecommendation;" in html
    source = html[html.index("        let pilotSelectedKey = null;"):html.index("        document.getElementById('waPilotClear').onclick")]
    script = """
const assert = require('assert');
const window = {crmWhatsappComposerPilot:true};
let currentCommTarget = 'lead', currentCommChannel = 'wa';
let originalTemplateText = 'old', currentTemplateLabel = 'old';
const semState = {selected:[{codigo:6318}]};
let loaded = null, dispatched = false, focused = false;
const elements = {
  commMessage:{value:'old',addEventListener(){},dispatchEvent(){dispatched=true},focus(){focused=true}},
  semRecommendMsgModal:{value:'Alternativa: https://www.procasa.cl/6318'}
};
const document={getElementById(id){return elements[id]}};
function loadTemplates(target,category){loaded=[target,category];}
function confirm(){return false;}
""" + source + """
assert(pilotComposerActive());
window.crmWhatsappComposerPilot = false;
assert(!pilotComposerActive());
window.crmWhatsappComposerPilot = true;
currentCommTarget = 'owner';
assert(!pilotComposerActive());
currentCommTarget = 'lead'; currentCommChannel = 'email';
assert(!pilotComposerActive());
currentCommChannel = 'wa';
const saleRows = pilotCommercialTemplates('venta');
const rentRows = pilotCommercialTemplates('arriendo');
const unknownRows = pilotCommercialTemplates();
assert.equal(saleRows.length,20);
assert.equal(rentRows.length,19);
assert.equal(unknownRows.length,18);
assert.equal(saleRows.filter(r=>r.category==='stage1').length,2);
assert.equal(saleRows.filter(r=>r.category==='stage2').length,11);
assert.equal(saleRows.filter(r=>r.category==='stage6').length,7);
assert.equal(new Set(saleRows.map(r=>r.key)).size,20);
assert(saleRows.slice(0,2).every(r=>r.text.includes('[[enlace]]')));
assert(saleRows.some(r=>r.label.includes('preaprobación')));
assert(rentRows.some(r=>r.label.includes('Documentos para postular')));
assert(!rentRows.some(r=>r.key==='builtin:19'));
assert(!saleRows.some(r=>r.key==='builtin:21'));
assert.equal(pilotDetectedOperation({contactOperations:['venta','arriendo'],contactLeadOperation:'Arriendo'}),'arriendo');
assert.equal(pilotDetectedOperation({contactOperations:['venta'],contactLeadOperation:'Arriendo'}),'');
const facts = {contactOrigin:'Portal Inmobiliario',publicContactUrl:'https://www.procasa.cl/6318',
 contactPublicFacts:{tipo:'Departamento',comuna:'Las Condes',dormitorios:3,banos:2,superficie_util:170,
 features:['has_terrace','unknown'],precio_venta:{moneda_publicada:'UF',precio_publicado_original:12950}}};
const sale=pilotResolveContext(saleRows[0].text,'venta',facts.publicContactUrl,facts);
const rent=pilotResolveContext(rentRows[0].text,'arriendo',facts.publicContactUrl,facts);
assert(sale.includes('el departamento en venta en Las Condes'));
assert(sale.includes('Portal Inmobiliario'));
assert(rent.includes('el departamento en arriendo en Las Condes'));
const docText=pilotResolveContext(rentRows.find(r=>r.key==='builtin:3').text,'arriendo','x');
assert(docText.includes('liquidaciones de sueldo') && docText.includes('Certificado de cotizaciones') && docText.includes('Confirmaré los requisitos exactos'));
assert(pilotResolveContext(saleRows.find(r=>r.key==='builtin:3').text,'venta','x').includes('crédito hipotecario preaprobado'));
const highlights=pilotResolveContext(saleRows.find(r=>r.key==='builtin:18').text,'venta',facts.publicContactUrl,facts);
assert(highlights.includes('3 dormitorios') && highlights.includes('170 m² útiles') && highlights.includes('Terraza') && highlights.includes('UF 12.950'));
assert(!highlights.includes('unknown'));
assert(!pilotResolveContext(saleRows[0].text,'','').includes('[[enlace]]'));
assert(!pilotResolveContext(saleRows[0].text,'','').includes('en venta'));
assert(!pilotResolveContext(saleRows[0].text,'','').includes('Portal Inmobiliario'));
const link='https://www.procasa.cl/6318';
assert(pilotHasPublicLink('Revisa '+link+'.',link));
assert(!pilotHasPublicLink('Revisa '+link+'9',link));
assert(!pilotHasPublicLink('Revisa '+link+'?other=1',link));
assert(!pilotHasPublicLink('Revisa https://other.cl/'+link,link));
assert.equal(pilotEnsureFirstContactLink('Hola',link),'Hola\\n\\n'+link);
assert.equal(pilotEnsureFirstContactLink('Hola '+link,link),'Hola '+link);
assert.equal(pilotEnsureFirstContactLink('Hola',''),'Hola');
assert(pilotFirstContactRequired('stage1','stage6'));
assert(!pilotFirstContactRequired('stage6','stage1'));
assert(pilotFirstContactRequired(null,'stage1'));
assert(pilotFirstContactError('Hola','', 'stage1','stage6'));
assert(pilotFirstContactError('Hola',link,'stage1','favorites'));
assert.equal(pilotFirstContactError('Hola '+link,link,'stage1','favorites'),'');
assert.equal(pilotFirstContactError('Hola','', 'stage6','stage1'),'');
const reusable='Hola [[nombre]], soy [[ejecutivo]]. [[enlace]] [[consulta]] [[saludo]] [[origen]] [[referencia]] [[atributos]]';
assert.equal(pilotPersonalStorage(pilotPersonalDisplay(reusable)),reusable);
assert(!pilotPersonalDisplay(reusable).includes('[['));
assert.equal(pilotReplaceKnownValue('Hola Ana, mañana Anaís. [[nombre]]','Ana','[[nombre]]'),'Hola [[nombre]], mañana Anaís. [[nombre]]');
assert.equal(pilotReplaceKnownValue('Ficha '+link,link,'[[enlace]]'),'Ficha [[enlace]]');
let prevented=false;
preparePilotRecommendation({preventDefault(){prevented=true}});
assert(prevented && focused && dispatched);
assert.equal(elements.commMessage.value,elements.semRecommendMsgModal.value);
assert.deepEqual(loaded,['lead','stage6']);
assert.equal(currentTemplateLabel,'Recomendación de propiedades');
elements.commMessage.value='Edited draft'; loaded=null;
preparePilotRecommendation({preventDefault(){}});
assert.equal(elements.commMessage.value,'Edited draft');
assert.equal(loaded,null);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_hybrid_search_sends_required_lead_context_and_reports_http_errors():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    html = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8")
    source = html[html.index("        async function searchSemantic()"):html.index("        function renderSemanticResults(")]
    script = """
const assert = require('assert');
const leadData={id:'69796bc4bbebf240378eb739'};
const semState={selected:[{codigo:6318}],lastResults:[]};
const elements={
    semanticQueryModal:{value:'  Departamento en Maipú  '},
    semanticResultsModal:{innerHTML:'',textContent:'',replaceChildren(e){this.textContent=e.textContent}},
    btnSemSearchModal:{disabled:false,innerHTML:'Buscar',dataset:{}},
    scopeSwitchModal:{checked:true},neighborsSwitchModal:{checked:false}
};
const document={getElementById(id){return elements[id]||null},createElement(){return {}}};
let request=null, status=200, payload={status:'ok',results:[{codigo:9999}]}, rendered=null;
function detectPriceUnit(){return 'CLP'}
function renderSemanticResults(results){rendered=results}
async function fetch(url,options){
    request={url,options,body:JSON.parse(options.body)};
    return {ok:status===200,status,json:async()=>payload};
}
""" + source + """
(async()=>{
    await searchSemantic();
    assert.equal(request.url,'/api/crm/recommendations');
    assert.equal(request.options.credentials,'same-origin');
    assert.equal(request.body.lead_id,leadData.id);
    assert.equal(request.body.query,'Departamento en Maipú');
    assert.equal(request.body.scope,'local');
    assert.deepEqual(request.body.exclude,[6318]);
    assert.deepEqual(rendered,[{codigo:9999}]);
    status=400;payload={detail:'lead_context_required'};
    await searchSemantic();
    assert(elements.semanticResultsModal.textContent.includes('validar'));
    assert(!elements.semanticResultsModal.textContent.includes('No se encontraron'));
    status=403;payload={detail:'denied'};
    await searchSemantic();
    assert(elements.semanticResultsModal.textContent.includes('No tienes acceso'));
    status=200;payload={status:'ok',results:[]};
    await searchSemantic();
    assert(elements.semanticResultsModal.innerHTML.includes('No se encontraron'));
    request=null;elements.semanticQueryModal.value='abc';
    await searchSemantic();
    assert.equal(request,null);
    assert(elements.semanticResultsModal.textContent.includes('5 caracteres'));
    assert(!elements.btnSemSearchModal.disabled);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_pilot_personal_first_contact_link_is_automatic_only_for_pilot():
    from chatbot.crm_personal_message_templates import PABLO_COMPOSER_PILOT_USER_ID
    db = mongomock.MongoClient()["test"]
    item = entry(body="Hola [[nombre]], gracias por tu consulta")
    pilot = write_library(db, PABLO_COMPOSER_PILOT_USER_ID,
                          {"version": 0, "templates": [item], "favorites": ["builtin:17"]})
    assert pilot["templates"][0]["body"].endswith("[[enlace]]")
    assert pilot["favorites"] == ["builtin:17"]
    nonpilot = write_library(db, "other", {"version": 0, "templates": [item]})
    assert nonpilot["templates"][0]["body"] == item["body"]
    other_stage = dict(item, category="stage6")
    assert validate_template(other_stage, pilot=True)["body"] == item["body"]
    assert validate_template(entry(body="[[consulta]]"), pilot=True)["body"].endswith("[[enlace]]")
    assert validate_template(entry(body="[[enlace]]"), pilot=True)["body"] == "[[enlace]]"
    with pytest.raises(ValueError):
        validate_template(entry(body="x" * 2000), pilot=True)


def test_pilot_first_contact_guard_precedes_log_and_provider():
    html = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8")
    assert "#commModal.wa-composer-pilot #waInsertProperty, #commModal.wa-composer-pilot #waVariableButtons { display:none !important; }" in html
    assert "pilotDraftCategory = templateCategory" in html
    assert "pilotFirstContactError(msgBody.value,leadData.publicContactUrl,pilotDraftCategory,category)" in html
    assert "if (hasMessage && !pilotBlocked && !requiredLinkProblem)" in html
    assert "senderName: {{ (user_name or \"\") | tojson }}" in html
    send = html[html.index("        function updateSendLink("):html.index("        // --- NOTES SYSTEM ---")]
    assert send.index("if (linkProblem)") < send.index("logAction(logType")
    assert send.index("if (linkProblem)") < send.index("/api/crm/executive-whatsapp/send")
    assert "pilotRestoreRequiredLink(msgBody)" in send
    assert "pilotRestoreRequiredLink(this)" in html
