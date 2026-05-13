import re

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_view.html'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

replacements = {
    '/contracts/api/': '/visitas/api/',
    '/contracts/view/': '/visitas/view/',
    'contract.contract_code': 'contract.visita_code', # Well, wait. api_visitas actually uses contract_code still in the JSON?
    'contract_code': 'contract_code', # Let's see what api_visitas returns. I used replace "contract_code" to "visita_code" in api_visitas.py!
    'Revisa tu contrato': 'Revisa tu Orden de Visita',
    'Revisa tu Contrato': 'Revisa tu Orden de Visita',
    'Firmar Contrato Definitivamente': 'Firmar Orden de Visita Definitivamente',
    'Descargar contrato': 'Descargar orden',
    'Revisa tu Convenio de Corretaje': 'Revisa tu Orden de Visita',
    'Convenio de Corretaje': 'Orden de Visita',
    'Firma de Convenio de Corretaje': 'Firma de Orden de Visita',
    'Revisa tu contrato': 'Revisa tu orden de visita',
    'Ver contrato completo': 'Ver orden completa',
}

for k, v in replacements.items():
    content = content.replace(k, v)

# In api_visitas.py, I replaced "contract_code" with "visita_code". So the JS needs to be updated.
content = content.replace("contract_code", "visita_code")
# Revert URL strings that might have been mangled
content = content.replace("visitas/api/download_signed/${visita_code}", "visitas/api/download_signed/${visita_code}")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Done fixing visita_view.html!")
