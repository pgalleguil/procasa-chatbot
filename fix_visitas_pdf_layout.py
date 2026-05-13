import re

path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\services\pdf_generator_visitas.py'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

# Replace generate_original_contract body
start = text.find('def generate_original_contract')
end = text.find('def generate_signed_contract')

new_method = '''def generate_original_contract(contract_data: dict) -> bytes:
        """Genera la orden de visita original en base a la data."""
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter,
            rightMargin=36, leftMargin=36, topMargin=24, bottomMargin=30)
        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle('ContractTitle', parent=styles['Heading1'],
            alignment=1, fontSize=12, spaceAfter=10, spaceBefore=0)
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
            Story.append(Spacer(1, 0.05 * inch))
        
        # TITLE
        Story.append(Paragraph("ORDEN DE VISITA", title_style))
        Story.append(Spacer(1, 0.1 * inch))
        
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
        operacion = contract_data.get('operacion', '')
        
        ejecutivo_nombre = contract_data.get('ejecutivo_nombre', '')
        ejecutivo_email = contract_data.get('ejecutivo_email', '')
        
        # HEADERS
        Story.append(Paragraph(f"<b>Fecha:</b> {fecha}", normal_style))
        Story.append(Paragraph(f"<b>Al Sr.(a):</b> {nombre}", normal_style))
        Story.append(Paragraph(f"<b>Rut:</b> {rut}", normal_style))
        Story.append(Paragraph(f"<b>Dirección:</b> {direccion_cliente}, {comuna_cliente} {region_cliente}", normal_style))
        Story.append(Spacer(1, 0.1 * inch))
        
        if 'visita_code' in contract_data:
            Story.append(Paragraph(f"<b>Código de Verificación:</b> {contract_data['visita_code']}", normal_style))
            Story.append(Spacer(1, 0.02 * inch))
        
        Story.append(Paragraph("Cumpliendo su encargo para comprar/arrendar una propiedad le ofrecemos las siguientes:", normal_style))
        Story.append(Spacer(1, 0.05 * inch))
        
        Story.append(Paragraph("<b>Propiedad</b>", normal_style))
        Story.append(Paragraph(f"<b>Código:</b> {property_code}", normal_style))
        Story.append(Spacer(1, 0.05 * inch))
        
        Story.append(Paragraph("<b>Características</b>", normal_style))
        chars = f"<b>Tipo Propiedad:</b> {property_tipo} &nbsp;&nbsp;&nbsp;&nbsp; <b>Región:</b> {property_region} &nbsp;&nbsp;&nbsp;&nbsp; <b>Comuna:</b> {property_comuna}"
        Story.append(Paragraph(chars, normal_style))
        
        precios = f"<b>Precio:</b> {precio}"
        if operacion:
            precios += f" &nbsp;&nbsp;&nbsp;&nbsp; <b>Operación:</b> {operacion}"
        Story.append(Paragraph(precios, normal_style))
        Story.append(Spacer(1, 0.1 * inch))
        
        Story.append(Paragraph(f"Para coordinar la visita favor contactar a : <b>{ejecutivo_nombre}</b> - {ejecutivo_email}", normal_style))
        Story.append(Spacer(1, 0.1 * inch))
        
        legal_text = """El comitente, suscrito o su cónyuge hemos encargado personal, telefónicamente, vía email, whatsapp o por algún medio
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
arbitrador designado por Centro Nacional de Arbitrajes S.A.("CNA"), de acuerdo a sus Reglamentos."""
        
        Story.append(Paragraph(legal_text.replace('\\n', ' '), normal_style))
        Story.append(Spacer(1, 0.2 * inch))
        
        # Cláusulas sobre firma electrónica
        Story.append(Paragraph('<b>CLAUSULA - FIRMA ELECTRONICA:</b> Las partes acuerdan que la firma electronica utilizada en este instrumento, conforme a la Ley 19.799, tendra el mismo valor legal que una firma manuscrita.', normal_style))
        Story.append(Paragraph('<b>CLAUSULA - USO DE MEDIOS ELECTRONICOS:</b> El firmante declara que el numero telefonico y correo electronico proporcionados son de su exclusivo uso y control, aceptando la utilizacion de dichos medios para la suscripcion del presente instrumento.', normal_style))
        Story.append(Paragraph('<b>CLAUSULA - VALIDEZ DEL PROCESO DE FIRMA:</b> El acceso al enlace enviado, la autenticacion mediante codigo de verificacion (OTP) y el registro de antecedentes tecnicos del sistema constituiran evidencia de la aceptacion y consentimiento del firmante.', normal_style))

        Story.append(Spacer(1, 0.4 * inch))
        
        # Signatures layout: Side by side (Centered)
        sig_left = Paragraph(f"<para align='center'><b>{ejecutivo_nombre}</b><br/>Procasa S.A.</para>", normal_style)
        sig_right = Paragraph(f"<para align='center'><b>{rut} {nombre}</b><br/>Cliente</para>", normal_style)
        
        data_signatures = [[sig_left, sig_right]]
        t = Table(data_signatures, colWidths=[3.5*inch, 3.5*inch])
        Story.append(t)
        
        doc.visita_code = contract_data.get('visita_code', '')
        doc.is_original = True
        
        def make_canvas(*args, **kwargs):
            c = NumberedCanvas(*args, **kwargs)
            c.visita_code = getattr(doc, 'visita_code', '')
            c.is_original = doc.is_original
            return c
            
        doc.build(Story, canvasmaker=make_canvas)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        return pdf_bytes

    @staticmethod
    '''

text = text[:start] + new_method + text[end+17:] # +17 to skip '    @staticmethod' if we included it

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)

print('Updated PDF layout')
