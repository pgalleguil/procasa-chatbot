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

class PDFGenerator:
    
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
        """Genera el contrato original en base a la data."""
        buffer = io.BytesIO()
        # Reducimos márgenes para maximizar espacio (2cm = 56.7 pt)
        doc = SimpleDocTemplate(buffer, pagesize=letter,
            rightMargin=36, leftMargin=36, topMargin=24, bottomMargin=30)
        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle('ContractTitle', parent=styles['Heading1'],
            alignment=1, fontSize=12, spaceAfter=4, spaceBefore=0)
        normal_style = ParagraphStyle('ContractNormal', parent=styles['Normal'],
            spaceAfter=4, alignment=4, leading=12, fontSize=9.6)
        
        Story = []
        
        # Logo de Procasa — esquina superior izquierda
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
        
        tipo = contract_data.get("tipo", "Arriendo")
        
        if tipo == "Venta Exclusiva":
            Story.append(Paragraph("AUTORIZACIÓN DE CORRETAJE DE VENTA EXCLUSIVA", title_style))
        else:
            Story.append(Paragraph(f"AUTORIZACIÓN DE {tipo.upper()}", title_style))
        Story.append(Spacer(1, 0.01 * inch))
        
        if 'contract_code' in contract_data:
            Story.append(Paragraph(f"<b>Código de Verificación:</b> {contract_data['contract_code']}", normal_style))
            Story.append(Spacer(1, 0.02 * inch))
        
        fecha = datetime.now(CHILE_TZ).strftime('%d de %m de %Y').replace('de 01 de', 'de enero de').replace('de 02 de', 'de febrero de').replace('de 03 de', 'de marzo de').replace('de 04 de', 'de abril de').replace('de 05 de', 'de mayo de').replace('de 06 de', 'de junio de').replace('de 07 de', 'de julio de').replace('de 08 de', 'de agosto de').replace('de 09 de', 'de septiembre de').replace('de 10 de', 'de octubre de').replace('de 11 de', 'de noviembre de').replace('de 12 de', 'de diciembre de')
        nombre = contract_data.get('cliente_nombre', '')
        rut = contract_data.get('cliente_rut', '')
        direccion = contract_data.get('propiedad_direccion', '')
        comuna = contract_data.get('comuna', '')
        rol = contract_data.get('rol', '')
        vigencia = contract_data.get('vigencia', '30')
        precio = contract_data.get('precio', '')
        comision = contract_data.get('comision', '')
        codigo_prop = contract_data.get('property_code', '')
        email = contract_data.get('email', '')
        ciudad_firma = contract_data.get('ciudad_firma', 'Santiago de Chile')
        
        op = "la venta de" if tipo in ["Venta", "Venta Exclusiva"] else "en arriendo"
        
        p1 = f"En {ciudad_firma}, a {fecha}, yo <b>{nombre}</b>, rut <b>{rut}</b>, mediante la suscripción de la presente, autorizo a PROCASA S.A. y a sus franquiciados para ofrecer {op} mi propiedad ubicada en <b>{direccion}, comuna de {comuna}</b>, Rol de Avalúo <b>{rol}</b>, código interno <b>{codigo_prop}</b>; el nexo principal entre la franquicia master Procasa S.A. será el franquiciado INMOBILIARIA SUCRE SPA y el COMITENTE."
        Story.append(Paragraph(p1, normal_style))
        
        precio_texto = f" al precio de <b>{precio}</b>" if precio else ""
        if tipo == "Venta Exclusiva":
            p2 = f"<b>ANTECEDENTES:</b> La presente autorización se otorga con carácter de <b>EXCLUSIVIDAD</b>{precio_texto} y tendrá una validez de <b>{vigencia}</b> días corridos a contar de esta fecha, renovable por períodos iguales. Durante su vigencia, EL COMITENTE se obliga a trabajar exclusivamente con PROCASA S.A. y/o sus franquiciados para la comercialización del inmueble."
        else:
            p2 = f"<b>ANTECEDENTES:</b> La presente autorización se otorga SIN exclusividad{precio_texto} y tendrá una validez de <b>{vigencia}</b> días corridos a contar de esta fecha y se renovará, automática y sucesivamente, por períodos iguales. Asimismo el COMITENTE, autoriza expresamente a PROCASA S.A. y a sus franquiciados a extender órdenes de visita electrónicas, para mostrar la propiedad a posibles interesados, además el COMITENTE se compromete a pagar a PROCASA S.A. o a sus franquiciados por los servicios de corretaje para la venta o arriendo de la propiedad descrita."
        Story.append(Paragraph(p2, normal_style))
        
        if tipo in ["Venta", "Venta Exclusiva"]:
            comision_text = comision if comision else "dos por ciento (2 %)"
            if tipo == "Venta Exclusiva":
                p3 = f"<b>COMISIÓN:</b> En caso de formularse una oferta de compra respecto del inmueble y esta sea aceptada por EL COMITENTE, se devengará en favor de PROCASA S.A. y/o sus franquiciados una comisión equivalente al <b>{comision_text}</b> del precio de venta más I.V.A. Esta comisión también aplicará si EL COMITENTE vende directamente o por terceros no autorizados durante la vigencia, respecto de clientes presentados o gestionados por PROCASA."
                p4 = "<b>PROTECCIÓN DE CLIENTES PRESENTADOS:</b> EL COMITENTE reconoce protección comercial sobre los clientes presentados por PROCASA S.A. y/o sus franquiciados durante la vigencia del presente instrumento."
                Story.append(Paragraph(p3, normal_style))
                Story.append(Paragraph(p4, normal_style))
            else:
                # Excepción documental puntual: solo este convenio requiere una
                # cláusula de comisión distinta; el texto general no cambia.
                if contract_data.get('contract_code') == 'PROC-2026-3400':
                    p3 = "<b>COMISIÓN:</b> En caso de concretarse la compraventa del inmueble con un comprador presentado, contactado o gestionado por PROCASA S.A. y/o sus franquiciados, se devengará en favor de PROCASA S.A. y/o sus franquiciados una comisión equivalente al <b>2%</b> del precio de venta más I.V.A."
                    p4 = "Para efectos de la presente autorización, la comisión se entenderá devengada únicamente una vez que el inmueble se encuentre inscrito a nombre del comprador en el Conservador de Bienes Raíces correspondiente."
                    Story.append(Paragraph(p3, normal_style))
                    Story.append(Paragraph(p4, normal_style))
                else:
                    p3 = f"<b>COMISIÓN:</b> En caso de formularse una oferta de compra respecto del inmueble y esta sea aceptada por parte de EL COMITENTE se devengará en favor de PROCASA S.A. y/o a sus franquiciados una comisión equivalente al <b>{comision_text}</b> del precio de venta más el I.V.A."
                    Story.append(Paragraph(p3, normal_style))
        else:
            comision_text = comision if comision else "50%"
            p3 = f"<b>COMISIÓN:</b> En caso de formularse una oferta de arriendo respecto del inmueble y esta sea aceptada por parte de EL COMITENTE se devengará en favor de PROCASA S.A. y/o a sus franquiciados una comisión equivalente al <b>{comision_text}</b> de la renta mensual pactada más I.V.A. En los contratos de plazos superiores a 24 meses la comisión será de un dos por ciento (2 %) más IVA sobre el total de las rentas y con un límite de 60 meses."
            Story.append(Paragraph(p3, normal_style))
            
            if tipo == "Arriendo y Administración":
                p4 = "<b>ADMINISTRACIÓN:</b> EL COMITENTE encarga la administración de la propiedad a INMOBILIARIA SUCRE SPA, quien acepta la administración de la propiedad individualizada. INMOBILIARIA SUCRE SPA se encuentra expresamente facultada para tomar todas aquellas medidas de carácter administrativo que resulten pertinentes para el normal cumplimiento de lo convenido en este mandato. Dentro de las facultades de la administración que por este acto se otorgan al administrador, se entenderán las de cobrar y percibir las rentas de arrendamiento. Las facultades de administración se ejercerán durante la vigencia del presente contrato, incluyendo sus renovaciones e incluso los períodos de eventual incumplimiento del arrendatario."
                Story.append(Paragraph(p4, normal_style))
                
                admin_honorarios = comision if comision else "10"
                admin_duracion = vigencia if vigencia else "12"
                p5 = f"Por el desempeño de la administración, INMOBILIARIA SUCRE SPA, percibirá un honorario mensual de {admin_honorarios}% + IVA de la renta de arrendamiento que será descontado de ésta. La duración de la administración será de {admin_duracion} meses a contar de la fecha del contrato de arriendo, y se renovará automáticamente por periodos iguales y sucesivos si no mediare carta certificada de aviso de no renovación y correo electrónico de término del contrato, de cualquiera de las dos partes, con una anticipación de, a lo menos, 60 (sesenta) días corridos contados hacia atrás respecto de la fecha de vencimiento del periodo respectivo."
                Story.append(Paragraph(p5, normal_style))

        # Cláusulas sobre firma electrónica
        Story.append(Spacer(1, 0.1 * inch))
        Story.append(Paragraph('<b>CLAUSULA - FIRMA ELECTRONICA:</b> Las partes acuerdan que la firma electronica utilizada en este instrumento, conforme a la Ley 19.799, tendra el mismo valor legal que una firma manuscrita.', normal_style))
        Story.append(Paragraph('<b>CLAUSULA - USO DE MEDIOS ELECTRONICOS:</b> El firmante declara que el numero telefonico y correo electronico proporcionados son de su exclusivo uso y control, aceptando la utilizacion de dichos medios para la suscripcion del presente instrumento.', normal_style))
        Story.append(Paragraph('<b>CLAUSULA - VALIDEZ DEL PROCESO DE FIRMA:</b> El acceso al enlace enviado, la autenticacion mediante codigo de verificacion (OTP) y el registro de antecedentes tecnicos del sistema constituiran evidencia de la aceptacion y consentimiento del firmante.', normal_style))

        Story.append(Spacer(1, 0.14 * inch))
        Story.append(Paragraph("________________________________________________", normal_style))
        Story.append(Paragraph("<b>EL COMITENTE</b>", normal_style))
        Story.append(Paragraph(f"Nombre: {nombre} &nbsp;&nbsp; RUT: {rut}<br/>Teléfono: {contract_data.get('phone', '')} &nbsp;&nbsp; Correo: <a href='mailto:{email}'>{email}</a>", normal_style))
        
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
            img.hAlign = 'LEFT'
            Story.append(img)
            Story.append(Spacer(1, 0.01 * inch))
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
            
        # Acortar user_agent para que no rompa el layout
        ua_display = evidence_data.get('user_agent', '')[:70]

        hash_style = ParagraphStyle(
            'HashCell', parent=styles['Normal'],
            fontSize=6.5, leading=9, wordWrap='CJK'
        )
        orig_hash_p = Paragraph(evidence_data.get('original_hash', ''), hash_style)
        timeline_hash_p = Paragraph(evidence_data.get('timeline_hash', ''), hash_style)

        data = [
            ["Código único del contrato:", evidence_data.get('contract_code', '')],
            ["UUID de Transacción:", evidence_data.get('transaction_uuid', 'N/A')],
            ["Nombre completo:", nombre],
            ["RUT:", rut],
            ["Correo electrónico:", email],
            ["Teléfono:", phone],
            ["Dirección IP:", evidence_data.get('ip', '')],
            ["Fecha y hora exacta:", chile_time],
            ["Zona horaria:", evidence_data.get('timezone', "America/Santiago (CLT)")],
            ["Dispositivo/Navegador:", ua_display],
            ["Método de lectura:", evidence_data.get('read_method', 'scroll')],
            ["Tiempo de lectura del documento:", f"{evidence_data.get('read_time_seconds', 0)} segundos"],
            ["Confirmación de visualización completa:", evidence_data.get('scrolled_to_bottom', 'Sí')],
            ["Hash Original del documento (SHA256):", orig_hash_p],
            ["Hash del Proceso / Timeline (SHA256):", timeline_hash_p]
        ]
        
        t = Table(data, colWidths=[2.5*inch, 4*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), colors.lightgrey),
            ('TEXTCOLOR', (0,0), (-1,-1), colors.black),
            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
            ('FONTNAME', (1,0), (1,-1), 'Helvetica'),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('GRID', (0,0), (-1,-1), 1, colors.black)
        ]))
        Story.append(t)
        
        Story.append(Spacer(1, 0.5 * inch))
        qr_buffer = PDFGenerator._create_qr(verify_url)
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
            c.contract_code = doc.contract_code
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


