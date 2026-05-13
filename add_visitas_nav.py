import os
import glob

# Nav link to insert
new_link = '        <a href="/visitas/dashboard" class="nav-link"><i class="fa-solid fa-map-location-dot"></i> <span>Órdenes de Visita</span></a>\n'

# Find all HTML files in templates
template_files = glob.glob(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\*.html')

for filepath in template_files:
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if '/visitas/dashboard' in content and 'nav-link' in content and 'Órdenes de Visita' in content:
        # Already added
        continue
        
    # We look for the line containing '/contracts/dashboard' and 'nav-link'
    lines = content.split('\n')
    new_lines = []
    for line in lines:
        new_lines.append(line)
        if '/contracts/dashboard' in line and 'nav-link' in line:
            # Add the new link right after
            # Keep same indentation
            indent = line[:len(line) - len(line.lstrip())]
            new_lines.append(indent + '<a href="/visitas/dashboard" class="nav-link"><i class="fa-solid fa-map-location-dot"></i> <span>Órdenes de Visita</span></a>')
            
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(new_lines))

print("Added nav links!")
