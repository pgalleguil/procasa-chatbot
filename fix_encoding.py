import os

p = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\contract_dashboard.html'
with open(p, 'r', encoding='utf-8') as f:
    text = f.read()

replacements = {
    'Ã¡': 'á',
    'Ã©': 'é',
    'Ã\xad': 'í',
    'Ã³': 'ó',
    'Ãº': 'ú',
    'Ã±': 'ñ',
    'Ã ': 'Á',
    'Ã‰': 'É',
    'Ã ': 'Í',
    'Ã“': 'Ó',
    'Ãš': 'Ú',
    'Ã‘': 'Ñ',
    'Ã¼': 'ü',
    'Ãœ': 'Ü',
    'â€œ': '"',
    'â€ ': '"',
    'Ã': 'í',  # Handle remaining ones like MÃ³dulo -> Módulo (wait, Ã³ is already handled. What if Ã is standalone?)
    'Ã\x8D': 'Í',
    'Ã\x93': 'Ó'
}

for k, v in replacements.items():
    text = text.replace(k, v)

with open(p, 'w', encoding='utf-8') as f:
    f.write(text)
