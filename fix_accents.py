import os
import re

# Fix contract_view.html
path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\contract_view.html'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

replacements = {
    'vÃ¡lido': 'válido',
    'â€”': '—',
    'aceptaciÃ³n': 'aceptación',
    'electrÃ³nica': 'electrónica',
    'DesplÃ¡zate': 'Desplázate',
    'PÃ¡gina': 'Página',
    'nÃºmero': 'número',
    'telefÃ³nico': 'telefónico',
    'serÃ¡': 'será',
    'cÃ³digo': 'código',
    'verificaciÃ³n': 'verificación',
    'dÃ­gitos': 'dígitos',
    'mÃ¡ximo': 'máximo',
    'PodrÃ¡s': 'Podrás',
    'Â¿': '¿',
    'expirÃ³': 'expiró',
    'Â¡': '¡',
    'pondrÃ¡': 'pondrá',
    'max-height:68px;': 'max-height:110px;'
}

for k, v in replacements.items():
    content = content.replace(k, v)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

# Fix contract_dashboard.html
path2 = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\contract_dashboard.html'
with open(path2, 'r', encoding='utf-8') as f:
    content2 = f.read()

content2 = re.sub(r'(\.brand\s*\{[^\}]*)height:\s*50px;', r'\g<1>height: 80px;', content2, flags=re.MULTILINE|re.DOTALL)
content2 = content2.replace('max-height: 30px;', 'max-height: 55px;')

with open(path2, 'w', encoding='utf-8') as f:
    f.write(content2)

print("Done!")
