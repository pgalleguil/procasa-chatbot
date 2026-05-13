import re

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\services\pdf_generator_visitas.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace class name and self-references
content = content.replace('class PDFGenerator:', 'class PDFGeneratorVisitas:')
content = content.replace('PDFGenerator._create_qr', 'PDFGeneratorVisitas._create_qr')
content = content.replace('PDFGenerator.generate_original_contract', 'PDFGeneratorVisitas.generate_original_contract')

new_generate_method = """
    @staticmethod
    def generate_original_contract(contract_data: dict) -> bytes:
        \"\"\"Genera la orden de visita original en base a la data.\"\"\"
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter,
            rightMargin=36, leftMargin=36, topMargin=24, bottomMargin=30)
        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle('ContractTitle', parent=styles['Heading1'],
            alignment=1, fontSize=12, spaceAfter=4, spaceBefore=0)
        normal_style = ParagraphStyle('ContractNormal', parent=styles['Normal'],
            spaceAfter=4, alignment=4, leading=12, fontSize=9.6)
        bold_style = ParagraphStyle('ContractBold', parent=normal_style, fontName='Helvetica-Bold')
        
        Story = []
        
        # Logo
        logo_path = BASE_DIR / "static" / "logo.png"
        if logo_path.exists():
            from reportlab.lib.utils import ImageReader
            img_reader = ImageReader(str(logo_path))
            iw, ih = img_reader.getSize()
            aspect = ih / float(iw)
            width = 1.35 * inch
            height = width * aspect
            img = RLImage(str(logo_path), width=width, height=height)
            img.hAlign = 'CENTER'
            Story.append(img)
            Story.append(Spacer(1, 0.01 * inch))
        
        fecha = datetime.now(CHILE_TZ).strftime('%d de %m de %Y').replace('de 01 de', 'de enero de').replace('de 02 de', 'de febrero de').replace('de 03 de', 'de marzo de').replace('de 04 de', 'de abril de').replace('de 05 de', 'de mayo de').replace('de 06 de', 'de junio de').replace('de 07 de', 'de julio de').replace('de 08 de', 'de agosto de').replace('de 09 de', 'de septiembre de').replace('de 10 de', 'de octubre de').replace('de 11 de', 'de noviembre de').replace('de 12 de', 'de diciembre de')
        
        nombre = contract_data.get('cliente_nombre', '')
        rut = contract_data.get('cliente_rut', '')
        direccion_cliente = contract_data.get('cliente_direccion', '')
        comuna_cliente = contract_data.get('cliente_comuna', '')
        region_cliente = contract_data.get('cliente_region', '')
        
        property_code = contract_data.get('property_code', '')
        property_tipo = contract_data.get('property_tipo', '')
        property_comuna = contract_data.get('property_comuna', '')
        property_region = contract_data.get('property_region', '')
        precio = contract_data.get('precio', '')
        
        ejecutivo_nombre = contract_data.get('ejecutivo_nombre', '')
        ejecutivo_email = contract_data.get('ejecutivo_email', '')
        
        Story.append(Paragraph(f"<b>Fecha:</b> {fecha}", normal_style))
        Story.append(Paragraph(f"<b>Al Sr.(a):</b> {nombre}", normal_style))
        Story.append(Paragraph(f"<b>Rut:</b> {rut}", normal_style))
        Story.append(Paragraph(f"<b>Dirección:</b> {direccion_cliente}, {comuna_cliente} {region_cliente}", normal_style))
        Story.append(Spacer(1, 0.1 * inch))
        
        Story.append(Paragraph("Orden de Visita", title_style))
        Story.append(Spacer(1, 0.1 * inch))
        
        if 'contract_code' in contract_data:
            Story.append(Paragraph(f"<b>Código de Verificación:</b> {contract_data['contract_code']}", normal_style))
            Story.append(Spacer(1, 0.02 * inch))
        
        Story.append(Paragraph("Cumpliendo su encargo para comprar/arrendar una propiedad le ofrecemos las siguientes:", normal_style))
        Story.append(Spacer(1, 0.05 * inch))
        
        Story.append(Paragraph("<b>Propiedad</b>", normal_style))
        Story.append(Paragraph(f"<b>Código:</b> {property_code}", normal_style))
        Story.append(Spacer(1, 0.05 * inch))
        
        Story.append(Paragraph("<b>Características</b>", normal_style))
        Story.append(Paragraph(f"<b>Tipo Propiedad:</b> {property_tipo} &nbsp;&nbsp;&nbsp;&nbsp; <b>Región:</b> {property_region} &nbsp;&nbsp;&nbsp;&nbsp; <b>Comuna:</b> {property_comuna}", normal_style))
        Story.append(Paragraph(f"<b>Precio:</b> {precio}", normal_style))
        Story.append(Spacer(1, 0.1 * inch))
        
        Story.append(Paragraph(f"Para coordinar la visita favor contactar a : <b>{ejecutivo_nombre}</b> - {ejecutivo_email}", normal_style))
        Story.append(Spacer(1, 0.1 * inch))
        
        legal_text = \"\"\"El comitente, suscrito o su cónyuge hemos encargado personal, telefónicamente, vía email, whatsapp o por algún medio
electrónico esta orden de visita y efectuaremos toda la transacción respecto de ellas, sólo por intermedio de PROCASA S.A y/o
sus franquiciados. En consecuencia, por venta, nos comprometemos a pagar, al momento de firmar la escritura de compraventa,
una comisión del 2% más IVA Sobre el monto de la operación, CON UN PAGO MINIMO DE $1.000.000 + IVA. Siendo esta
orden personal e intransferible, si en cualquier época y aún no estando vigente el plazo del Convenio de Corretaje, con el
propietario, nos entendiéramos directamente con éste o por intermedio de otro corredor o si proporcionáramos su uso o
información a terceros y éstos efectuaren el negocio por su cuenta, pagaremos un 4% más IVA como cláusula penal. Las
superficies son meramente ilustrativas; las ventas son “AD CORPUS” Artículo1833 del Código Civil. Por arriendos nos
comprometemos a pagar 50% más IVA de la renta mensual pactada; con un pago mínimo de $100.000 más IVA. En los contratos
de plazos superiores a 24 meses la comisión será de un 2% más IVA sobre el total de las rentas y con un límite de 60 meses.
Cualquier diferencia que se produzca entre las partes respecto de este contrato, será resuelta en forma breve por un árbitro
arbitrador designado por Centro Nacional de Arbitrajes S.A.("CNA"), de acuerdo a sus Reglamentos.\"\"\"
        
        Story.append(Paragraph(legal_text.replace('\\n', ' '), normal_style))
        Story.append(Spacer(1, 0.2 * inch))
        
        # Cláusulas sobre firma electrónica
        Story.append(Paragraph('<b>CLAUSULA - FIRMA ELECTRONICA:</b> Las partes acuerdan que la firma electronica utilizada en este instrumento, conforme a la Ley 19.799, tendra el mismo valor legal que una firma manuscrita.', normal_style))
        Story.append(Paragraph('<b>CLAUSULA - USO DE MEDIOS ELECTRONICOS:</b> El firmante declara que el numero telefonico y correo electronico proporcionados son de su exclusivo uso y control, aceptando la utilizacion de dichos medios para la suscripcion del presente instrumento.', normal_style))
        Story.append(Paragraph('<b>CLAUSULA - VALIDEZ DEL PROCESO DE FIRMA:</b> El acceso al enlace enviado, la autenticacion mediante codigo de verificacion (OTP) y el registro de antecedentes tecnicos del sistema constituiran evidencia de la aceptacion y consentimiento del firmante.', normal_style))

        Story.append(Spacer(1, 0.3 * inch))
        
        # We put both names at the bottom side by side if possible, or just one below another.
        data_signatures = [
            [Paragraph(f"<b>{ejecutivo_nombre}</b>", normal_style), Paragraph(f"<b>{rut} {nombre}</b>", normal_style)]
        ]
        t = Table(data_signatures, colWidths=[3.5*inch, 3.5*inch])
        Story.append(t)
        
        doc.contract_code = contract_data.get('contract_code', '')
        doc.is_original = True
        
        def make_canvas(*args, **kwargs):
            c = NumberedCanvas(*args, **kwargs)
            c.contract_code = doc.contract_code
            c.is_original = doc.is_original
            return c
            
        doc.build(Story, canvasmaker=make_canvas)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        return pdf_bytes
"""

# We'll use regex to replace the generate_original_contract method
content = re.sub(
    r'    @staticmethod\s+def generate_original_contract\(contract_data: dict\) -> bytes:.*?(?=    @staticmethod\s+def generate_signed_contract)',
    new_generate_method + '\n',
    content,
    flags=re.DOTALL
)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Done fixing pdf_generator_visitas.py!")
