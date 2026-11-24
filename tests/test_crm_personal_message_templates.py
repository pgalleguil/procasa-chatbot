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
    source = html[html.index("        function addPropertyLink("):html.index("        function updateSendLink(")]
    script = """
const assert = require('assert');
const elements = {};
function element(id) {
    return elements[id] ||= {value:'', classList:{add(){},remove(){},toggle(){}},
      addEventListener(){},append(){},focus(){},setRangeText(){},textContent:''};
}
const document = {getElementById:element,createElement:()=>element('button')};
const window = {};
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
    assert "run_in_executor" in route
    assert "send_whatsapp" not in route


def test_lead_whatsapp_commercial_groups_are_compact_and_complete():
    import json
    html = Path("templates/crm_lead_detail.html").read_text(encoding="utf-8")
    lead_templates = html[html.index("const commTemplates = {"):html.index("            owner: {", html.index("const commTemplates = {"))]
    rows = [json.loads(line.strip().rstrip(",")) for line in lead_templates.splitlines()
            if '"channel":"wa"' in line]
    assert len(rows) == 16
    assert len({r["label"] for r in rows}) == len(rows)
    assert all("[[enlace]]" in r["text"] and "[[nombre]]" in r["text"] for r in rows[:4])
    assert all("[[día]]" not in r["text"] and "[[hora]]" not in r["text"] for r in rows)
    assert all("RUT" not in r["text"] and "10:00" not in r["text"] for r in rows[:4])
    tabs = html[html.index("        function renderCommTabs("):html.index("        function loadTemplates(")]
    for name in ("Primer contacto", "Gestionar interés y visitas", "Seguimiento", "Mis plantillas", "Recomendador"):
        assert name in tabs
    assert "commTemplates[target].tabs.filter" not in tabs
    assert "['stage2', 'stage3', 'stage4', 'stage5']" in html


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
    assert "grid-template-columns:minmax(240px, .95fr) minmax(0, 1.6fr)" in html
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
  commMessage:{value:'old',dispatchEvent(){dispatched=true},focus(){focused=true}},
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
const rows = pilotCommercialTemplates();
assert.equal(rows.length,13);
assert.equal(rows.filter(r=>r.category==='stage1').length,2);
assert.equal(rows.filter(r=>r.category==='stage2').length,5);
assert.equal(rows.filter(r=>r.category==='stage6').length,6);
assert.equal(new Set(rows.map(r=>r.key)).size,13);
assert(rows.slice(0,2).every(r=>r.text.includes('[[enlace]]')));
const sale=pilotResolveContext(rows[0].text,'venta','https://www.procasa.cl/6318');
const rent=pilotResolveContext(rows[0].text,'arriendo','https://www.procasa.cl/6318');
assert(sale.includes('propiedad en venta:'));
assert(rent.includes('propiedad en arriendo:'));
assert(pilotResolveContext(rows[2].text,'arriendo','x').includes('requisitos de arriendo'));
assert(!pilotResolveContext(rows[0].text,'','').includes('[[enlace]]'));
assert(!pilotResolveContext(rows[0].text,'','').includes('en venta'));
assert(!pilotResolveContext(rows[0].text,'','').includes('propiedad :'));
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
