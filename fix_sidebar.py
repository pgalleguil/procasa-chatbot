import re

with open(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html', 'r', encoding='utf-8') as f:
    text = f.read()

# Fix Convenios link
text = text.replace('<a href="/visitas/dashboard" class="nav-link active"><i class="fa-solid fa-file-contract"></i> <span>Convenios</span></a>',
                    '<a href="/contracts/dashboard" class="nav-link"><i class="fa-solid fa-file-contract"></i> <span>Convenios</span></a>')

# Fix double wrapper
double_wrapper = """{% if user_role in ['supervisor', 'admin'] %}
        {% if user_role in ['supervisor', 'admin'] %}
        <a href="/visitas/dashboard" class="nav-link active"><i class="fa-solid fa-map-location-dot"></i> <span>Órdenes de Visita</span></a>
        {% endif %}
        {% endif %}"""

single_wrapper = """{% if user_role in ['supervisor', 'admin'] %}
        <a href="/visitas/dashboard" class="nav-link active"><i class="fa-solid fa-map-location-dot"></i> <span>Órdenes de Visita</span></a>
        {% endif %}"""

text = text.replace(double_wrapper, single_wrapper)

with open(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html', 'w', encoding='utf-8') as f:
    f.write(text)

print('Sidebar fixed')
