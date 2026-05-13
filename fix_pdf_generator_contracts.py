import os

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\services\pdf_generator_contracts.py'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

# Replace corrupted text
text = text.replace('AUTORIZACIÃ“N', 'AUTORIZACIÓN')
text = text.replace('COMISIÃ“N', 'COMISIÓN')
text = text.replace('PROTECCIÃ“N', 'PROTECCIÓN')
text = text.replace('ELECTRÃ“NICA', 'ELECTRÓNICA')
text = text.replace('DIRECCIÃ“N', 'DIRECCIÓN')
text = text.replace('FunciÃ³n', 'Función')
text = text.replace('AceptaciÃ³n', 'Aceptación')
text = text.replace('VisualizaciÃ³n', 'Visualización')
text = text.replace('CÃ³digo', 'Código')
text = text.replace('TelÃ©fono', 'Teléfono')
text = text.replace('ElectrÃ³nico', 'Electrónico')
text = text.replace('electrÃ³nico', 'electrónico')
text = text.replace('electrÃ³nica', 'electrónica')
text = text.replace('MÃ©todo', 'Método')
text = text.replace('SÃ\xad', 'Sí') # SÃ­
text = text.replace('Ãº', 'ú')
text = text.replace('Ã³', 'ó')
text = text.replace('Ã¡', 'á')
text = text.replace('Ã©', 'é')
text = text.replace('Ã\xad', 'í')
text = text.replace('Ã±', 'ñ')
text = text.replace('Ã“', 'Ó')
text = text.replace('Ã\x8d', 'Í')
text = text.replace('Ã\x81', 'Á')

# Fix specifically some known bad words
text = text.replace('SÃ\xad', 'Sí')
text = text.replace('S\u00ed', 'Sí')
text = text.replace('\u00f3', 'ó')
text = text.replace('\u00e9', 'é')
text = text.replace('\u00ed', 'í')
text = text.replace('\u00e1', 'á')
text = text.replace('\u00fa', 'ú')
text = text.replace('\u00f1', 'ñ')
text = text.replace('\u00d3', 'Ó')
text = text.replace('\u00cd', 'Í')
text = text.replace('\u00c1', 'Á')

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)

print("Fixed Mojibake in pdf_generator_contracts.py!")
