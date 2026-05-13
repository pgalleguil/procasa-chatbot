import os

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\contract_view.html'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

replacements = {
    'Ã±': 'ñ',
    'Ã‘': 'Ñ',
    'Ã¡': 'á',
    'Ã©': 'é',
    'Ã³': 'ó',
    'Ãº': 'ú',
    'Â¿': '¿',
    'Â¡': '¡',
    'â€“': '–',
    'â€”': '—',
}

# Special handling for í which is Ã followed by \xad (soft hyphen)
content = content.replace('Ã\xad', 'í')
content = content.replace('Ã\x8d', 'Í')

for k, v in replacements.items():
    content = content.replace(k, v)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Done fixing more accents!")
