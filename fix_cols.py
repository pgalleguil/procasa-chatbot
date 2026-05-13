import re

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\templates\visita_dashboard.html'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

form_start = text.find('<form id="formNewContract"')
form_end = text.find('</form>')

form_content = text[form_start:form_end]
form_content = re.sub(r'col-lg-\d+ col-md-\d+', 'col-12', form_content)
form_content = form_content.replace('position-relative', '')

text = text[:form_start] + form_content + text[form_end:]

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)

print('Updated columns to 12')
