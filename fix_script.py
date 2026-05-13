import re

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\fix_visitas_dashboard.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace("js_payload.strip()", "lambda m: js_payload.strip()")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed the fix script!")
