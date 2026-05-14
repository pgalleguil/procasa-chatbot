import os
import io
import qrcode
from pathlib import Path
from datetime import datetime, timezone
from chatbot.constants import CHILE_TZ

BASE_DIR = Path(__file__).resolve().parent.parent
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        text = f"Página {self._pageNumber} de {page_count}"
        self.drawCentredString(letter[0] / 2.0, 0.5 * inch, text)
        self.restoreState()

class PDFGeneratorVisitas:
    
    @staticmethod
    def format_rut(rut: str) -> str:
        if not rut: return ""
        clean = rut.replace(".", "").replace("-", "").strip().upper()
        if not clean: return rut
        if len(clean) < 2: return clean
        cuerpo = clean[:-1]
        dv = clean[-1]
        try:
            cuerpo_fmt = "{:,}".format(int(cuerpo)).replace(",", ".")
            return f"{cuerpo_fmt}-{dv}"
        except:
            return rut

    @staticmethod
    def format_phone(phone: str) -> str:
        if not phone: return ""
        clean = str(phone).replace(" ", "").replace("-", "").strip()
        if clean.startswith("+569") and len(clean) == 12:
            return f"+56 9 {clean[4:8]} {clean[8:]}"
        if len(clean) == 9 and clean.startswith("9"):
            return f"+56 9 {clean[1:5]} {clean[5:]}"
        return phone
    
    @staticmethod
    def _create_qr(data: str) -> io.BytesIO:
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img_buffer = io.BytesIO()
        img.save(img_buffer, format="PNG")
        img_buffer.seek(0)
        return img_buffer


    @staticmethod
    def generate_original_contract(contract_data: dict) -> bytes:
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
        Story.append(Paragraph(f"<b>Rut:</b> {PDFGeneratorVisitas.format_rut(rut)}", normal_style))
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
        
        ejecutivo_telefono = PDFGeneratorVisitas.format_phone(contract_data.get('ejecutivo_telefono', ''))
        contact_line = f"Para coordinar la visita favor contactar a : {ejecutivo_nombre}"
        if ejecutivo_email:
            contact_line += f" / {ejecutivo_email}"
        if ejecutivo_telefono:
            contact_line += f" / {ejecutivo_telefono}"
        
        Story.append(Paragraph(contact_line, normal_style))
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
        
        Story.append(Paragraph(legal_text.replace('\n', ' '), normal_style))
        Story.append(Spacer(1, 0.2 * inch))
        
        # Cláusulas sobre firma electrónica
        Story.append(Paragraph('<b>CLAUSULA - FIRMA ELECTRONICA:</b> Las partes acuerdan que la firma electronica utilizada en este instrumento, conforme a la Ley 19.799, tendra el mismo valor legal que una firma manuscrita.', normal_style))
        Story.append(Paragraph('<b>CLAUSULA - USO DE MEDIOS ELECTRONICOS:</b> El firmante declara que el numero telefonico y correo electronico proporcionados son de su exclusivo uso y control, aceptando la utilizacion de dichos medios para la suscripcion del presente instrumento.', normal_style))
        Story.append(Paragraph('<b>CLAUSULA - VALIDEZ DEL PROCESO DE FIRMA:</b> El acceso al enlace enviado al firmante, la autenticacion mediante codigo de verificacion (OTP) y el registro de antecedentes tecnicos del sistema constituiran evidencia suficiente de la aceptacion, consentimiento y firma electronica del firmante, conforme a la Ley N\u00b0 19.799 sobre Documentos Electronicos y Firma Electronica.', normal_style))

        Story.append(Spacer(1, 0.4 * inch))
        
        # Signatures layout: Side by side (Centered)
        sig_left = Paragraph(f"<para align='center'><b>{ejecutivo_nombre}</b><br/>Procasa S.A.</para>", normal_style)
        sig_right = Paragraph(f"<para align='center'><b>{PDFGeneratorVisitas.format_rut(rut)} {nombre}</b><br/>Cliente</para>", normal_style)
        
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
    def generate_signed_contract(original_pdf_bytes: bytes, contract_data: dict, evidence_data: dict, verify_url: str) -> bytes:
        """Genera el contrato FINAL, anexando la hoja de firmas al PDF original."""
        import pypdf
        
        # 1. Generate ONLY the signature page
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter,
            rightMargin=36, leftMargin=36, topMargin=24, bottomMargin=30)
        styles = getSampleStyleSheet()
        normal_style = ParagraphStyle('ContractNormalS', parent=styles['Normal'],
            spaceAfter=4, alignment=4, leading=12, fontSize=9.6)
            
        Story = []
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
        annex_title_style = ParagraphStyle('AnnexTitle', parent=styles['Heading1'], alignment=1, fontSize=12, spaceAfter=4, spaceBefore=0)
        Story.append(Paragraph("ANEXO: CERTIFICADO DE FIRMA ELECTR\u00d3NICA", annex_title_style))
        Story.append(Spacer(1, 0.14 * inch))
        
        nombre = contract_data.get('client_data', {}).get('nombre', contract_data.get('cliente_nombre', ''))
        rut = contract_data.get('client_data', {}).get('rut', contract_data.get('cliente_rut', ''))
        email = contract_data.get('client_data', {}).get('email', contract_data.get('email', ''))
        phone = contract_data.get('phone', '')
        
        server_ts_utc = evidence_data.get('server_timestamp', '')
        try:
            dt_utc = datetime.fromisoformat(server_ts_utc)
            chile_time = dt_utc.astimezone(CHILE_TZ).strftime('%d-%m-%Y %H:%M:%S')
        except:
            chile_time = server_ts_utc
            
        data = [
            ["ID de transacci\u00f3n (UUID):", evidence_data.get('visita_code', '')],
            ["C\u00f3digo de Verificaci\u00f3n:", evidence_data.get('contract_code', '')],
            ["Nombre completo:", nombre],
            ["RUT:", PDFGeneratorVisitas.format_rut(rut)],
            ["Correo electr\u00f3nico:", email],
            ["Tel\u00e9fono:", PDFGeneratorVisitas.format_phone(phone)],
            ["Direcci\u00f3n IP:", evidence_data.get('ip', '')],
            ["Fecha y hora exacta:", chile_time],
            ["Zona horaria:", evidence_data.get('timezone', "America/Santiago (CLT)")],
            ["Dispositivo/Navegador:", evidence_data.get('user_agent', '')[:60]],
            ["M\u00e9todo de lectura:", evidence_data.get('read_method', 'scroll')],
            ["Tiempo de lectura del documento:", f"{evidence_data.get('read_time_seconds', 0)} segundos"],
            ["Confirmaci\u00f3n de visualizaci\u00f3n completa:", evidence_data.get('scrolled_to_bottom', 'S\u00ed')],
            ["Hash SHA256 del documento:", str(evidence_data.get('timeline_hash', ''))]
        ]
        
        t = Table(data, colWidths=[2.5*inch, 4*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), colors.lightgrey),
            ('TEXTCOLOR', (0,0), (-1,-1), colors.black),
            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
            ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
            ('FONTNAME', (1,0), (1,-1), 'Helvetica'),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('GRID', (0,0), (-1,-1), 1, colors.black)
        ]))
        Story.append(t)
        
        Story.append(Spacer(1, 0.5 * inch))
        qr_buffer = PDFGeneratorVisitas._create_qr(verify_url)
        qr_img = RLImage(qr_buffer, width=1.18*inch, height=1.18*inch)
        qr_img.hAlign = 'CENTER'
        Story.append(qr_img)
        
        msg_style = ParagraphStyle('Msg', parent=normal_style, alignment=1, fontName='Helvetica-Bold', fontSize=10)
        Story.append(Spacer(1, 0.1 * inch))
        legal_text = """El firmante declara haber leído íntegramente el documento,
comprendido su contenido y manifestado su consentimiento
expreso mediante autenticación OTP enviada a su número
de teléfono registrado.<br/><br/>
El documento fue firmado electrónicamente conforme a la
Ley 19.799."""
        Story.append(Paragraph(legal_text, msg_style))
        
        doc.contract_code = evidence_data.get('contract_code', '')
        doc.is_original = False
        
        def make_canvas_signed(*args, **kwargs):
            c = NumberedCanvas(*args, **kwargs)
            c.visita_code = getattr(doc, 'visita_code', '')
            c.is_original = doc.is_original
            return c
            
        doc.build(Story, canvasmaker=make_canvas_signed)
        sig_page_bytes = buffer.getvalue()
        buffer.close()

        # 2. Merge original with signature page
        merger = pypdf.PdfWriter()
        merger.append(io.BytesIO(original_pdf_bytes))
        merger.append(io.BytesIO(sig_page_bytes))
        
        merged_buffer = io.BytesIO()
        merger.write(merged_buffer)
        merged_bytes = merged_buffer.getvalue()
        merger.close()

        # 3. Normaliza numeración en todas las páginas del PDF final firmado
        reader = pypdf.PdfReader(io.BytesIO(merged_bytes))
        writer = pypdf.PdfWriter()
        total_pages = len(reader.pages)
        for idx, page in enumerate(reader.pages, start=1):
            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)
            overlay_buffer = io.BytesIO()
            overlay = canvas.Canvas(overlay_buffer, pagesize=(page_w, page_h))
            overlay.setFillColor(colors.white)
            overlay.rect((page_w / 2.0) - 74, 0.34 * inch, 148, 0.24 * inch, fill=1, stroke=0)
            overlay.setFillColor(colors.black)
            overlay.setFont("Helvetica", 8)
            overlay.drawCentredString(page_w / 2.0, 0.42 * inch, f"Página {idx} de {total_pages}")
            overlay.save()
            overlay_pdf = pypdf.PdfReader(io.BytesIO(overlay_buffer.getvalue()))
            page.merge_page(overlay_pdf.pages[0])
            writer.add_page(page)

        final_buffer = io.BytesIO()
        writer.write(final_buffer)
        return final_buffer.getvalue()

    @staticmethod
    def generate_legal_report(contract_data: dict, evidence: dict, timeline: list) -> bytes:
        """Genera el Informe Legal para presentar como prueba judicial."""
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=18)
        styles = getSampleStyleSheet()
        title_style = styles['Heading1']
        title_style.alignment = 1
        
        Story = []
        Story.append(Paragraph("INFORME LEGAL - EVIDENCIA DIGITAL", title_style))
        Story.append(Spacer(1, 0.14 * inch))
        
        Story.append(Paragraph("<b>1. Resumen Ejecutivo</b>", styles['Heading2']))
        Story.append(Paragraph("El presente documento detalla la cadena de custodia y evidencia digital recopilada durante el proceso de aceptación electrónica del contrato, en cumplimiento con la Ley 19.799 sobre Documentos Electrónicos y Firma Electrónica.", styles['Normal']))
        Story.append(Spacer(1, 0.14 * inch))
        
        # Timeline
        Story.append(Paragraph("<b>2. Línea de Tiempo (Timeline Inmutable)</b>", styles['Heading2']))
        
        timeline_data = [["Acción", "Timestamp UTC", "IP", "User Agent"]]
        for event in timeline:
            timeline_data.append([
                event.get("action", ""),
                event.get("server_timestamp", ""),
                event.get("ip", ""),
                (event.get("user_agent", "")[:30] + "..") if event.get("user_agent") else ""
            ])
            
        t_timeline = Table(timeline_data)
        t_timeline.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#004b87')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('GRID', (0,0), (-1,-1), 0.5, colors.grey)
        ]))
        Story.append(t_timeline)
        
        Story.append(Spacer(1, 0.3 * inch))
        Story.append(Paragraph("<b>3. Consistencia Criptográfica</b>", styles['Heading2']))
        Story.append(Paragraph(f"<b>Hash Original (SHA-256):</b> {evidence.get('original_hash', '')}", styles['Normal']))
        Story.append(Paragraph(f"<b>Hash de Evidencia (Timeline SHA-256):</b> {evidence.get('timeline_hash', '')}", styles['Normal']))
        Story.append(Paragraph(f"<b>Firma del Servidor (HMAC SHA-256):</b> {evidence.get('server_hmac', '')}", styles['Normal']))
        
        doc.build(Story)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        return pdf_bytes


