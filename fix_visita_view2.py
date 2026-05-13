path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_view.html'
with open(path, 'r', encoding='utf-8') as f:
    t = f.read()

# The Jinja template uses {{ contract.contract_code }} in various places
# In our visita collection, the field is "visita_code"
# But we pass the whole mongo doc as "contract" to the template: TemplateResponse("visita_view.html", {"contract": contract, ...})
# So {{ contract.visita_code }} is the right reference

# Fix Jinja references
t = t.replace('contract.contract_code', 'contract.visita_code')
t = t.replace('contract.contract_code', 'contract.visita_code')

# Fix JS: the contract_code JS variable was renamed to visita_code by fix_visitas_view.py
# But the template var used in JS inline is {{ contract.visita_code }}
# The JS fetch calls were already fixed to /visitas/api/ by fix_visitas_view.py
# Let's also make sure the "Convenio" title is "Orden de Visita"
t = t.replace('Firma de Convenio de Corretaje', 'Firma de Orden de Visita')
t = t.replace('Convenio de Corretaje', 'Orden de Visita')

# Fix any remaining /contracts/ URLs that weren't caught
t = t.replace('/contracts/api/', '/visitas/api/')
t = t.replace('/contracts/verify/', '/visitas/verify/')

with open(path, 'w', encoding='utf-8') as f:
    f.write(t)
print('Done fixing visita_view.html')
