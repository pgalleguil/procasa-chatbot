path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html'
with open(path, 'r', encoding='utf-8') as f:
    t = f.read()

# Replace JS variable references
t = t.replace('contractCode', 'visitaCode')

# Fix Jinja template variable references - iterate 'visitas' not 'contracts'
t = t.replace('for contract in contracts', 'for visita in visitas')
t = t.replace('contract.status', 'visita.status')
t = t.replace('contract.created_at', 'visita.created_at')
t = t.replace('contract.executive_display', 'visita.executive_display')
t = t.replace('contract.property_data', 'visita.property_data')
t = t.replace('contract.client_data', 'visita.client_data')
t = t.replace('contract.phone', 'visita.phone')
t = t.replace('contract.origen', 'visita.origen')
t = t.replace('contract.edit_data', 'visita.edit_data')
t = t.replace('contracts | length', 'visitas | length')

with open(path, 'w', encoding='utf-8') as f:
    f.write(t)
print('Done fixing Jinja vars in visita_dashboard.html')
