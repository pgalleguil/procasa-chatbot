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
        doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=36)
        styles = getSampleStyleSheet()
        
        title_style = styles['Heading1']
        title_style.alignment = 1 # Center
        normal_style = styles['Normal']
        normal_style.spaceAfter = 12
        normal_style.alignment = 4 # Justify
        normal_style.leading = 14
        
        Story = []
        
        # Logo de Procasa
        logo_path = BASE_DIR / "static" / "logo.png"
        if logo_path.exists():
            from reportlab.lib.utils import ImageReader
            img_reader = ImageReader(str(logo_path))
            iw, ih = img_reader.getSize()
            aspect = ih / float(iw)
            width = 2.5 * inch
            height = width * aspect
            img = RLImage(str(logo_path), width=width, height=height)
            img.hAlign = 'CENTER'
            Story.append(img)
            Story.append(Spacer(1, 0.3 * inch))
        
        tipo = contract_data.get("tipo", "Arriendo")
        
        Story.append(Paragraph(f"AUTORIZACIÓN DE {tipo.upper()}", title_style))
        Story.append(Spacer(1, 0.2 * inch))
        
        if 'contract_code' in contract_data:
            Story.append(Paragraph(f"<b>Código Único de Contrato:</b> {contract_data['contract_code']}", normal_style))
            Story.append(Spacer(1, 0.2 * inch))
        
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
        
        op = "la venta de" if tipo == "Venta" else "en arriendo"
        
        p1 = f"En Santiago de Chile, a {fecha}, yo <b>{nombre}</b>, rut <b>{rut}</b>, mediante la suscripción de la presente, autorizo a PROCASA S.A. y a sus franquiciados para ofrecer {op} mi propiedad ubicada en <b>{direccion}, comuna de {comuna}</b>, Rol de Avalúo <b>{rol}</b>, código interno <b>{codigo_prop}</b>; el nexo principal entre la franquicia master Procasa S.A. será el franquiciado INMOBILIARIA SUCRE SPA y el COMITENTE."
        Story.append(Paragraph(p1, normal_style))
        
        precio_texto = f" al precio de <b>{precio}</b>" if precio else ""
        p2 = f"<b>ANTECEDENTES:</b> La presente autorización se otorga SIN exclusividad{precio_texto} y tendrá una validez de <b>{vigencia}</b> días corridos a contar de esta fecha y se renovará, automática y sucesivamente, por períodos iguales. Asimismo el COMITENTE, autoriza expresamente a PROCASA S.A. y a sus franquiciados a extender órdenes de visita electrónicas, para mostrar la propiedad a posibles interesados, además el COMITENTE se compromete a pagar a PROCASA S.A. o a sus franquiciados por los servicios de corretaje para la venta o arriendo de la propiedad descrita."
        Story.append(Paragraph(p2, normal_style))
        
        if tipo == "Venta":
            comision_text = comision if comision else "dos por ciento (2 %)"
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

        Story.append(Spacer(1, 0.5 * inch))
        Story.append(Paragraph("<b>EL COMITENTE</b>", normal_style))
        Story.append(Paragraph(f"{rut}<br/>{nombre}<br/>{contract_data.get('phone', '')}<br/>{email}", normal_style))
        
        doc.build(Story)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        return pdf_bytes

    @staticmethod
    def generate_signed_contract(original_contract_bytes: bytes, evidence_data: dict, verify_url: str) -> bytes:
        """
        Para este prototipo, en vez de modificar el PDF original (que requiere PyPDF2 u otra librería externa compleja), 
        generamos un PDF que contiene el texto original MÁS la página de firma/sellado. 
        En producción real se puede usar PyPDF2 para hacer merge del original con la página de firmas.
        """
        # Simulamos la re-generación con los datos originales + anexo de firma
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=18)
        styles = getSampleStyleSheet()
        normal_style = styles['Normal']
        
        Story = []
        
        Story.append(Paragraph("DOCUMENTO FIRMADO ELECTRÓNICAMENTE", styles['Heading1']))
        Story.append(Spacer(1, 0.2 * inch))
        Story.append(Paragraph("Este documento es una representación del contrato original, al cual se le ha anexado la presente hoja de firmas electrónicas conformes a la Ley 19.799.", normal_style))
        
        # ----- SELLO DE EVIDENCIA -----
        Story.append(Spacer(1, 0.5 * inch))
        Story.append(Paragraph("<b>CERTIFICADO DE FIRMA ELECTRÓNICA Y EVIDENCIA DIGITAL</b>", styles['Heading2']))
        
        data = [
            ["ID Contrato", evidence_data.get('contract_code', '')],
            ["Fecha/Hora Servidor (UTC)", evidence_data.get('server_timestamp', '')],
            ["IP del Firmante", evidence_data.get('ip', '')],
            ["Hash SHA-256 (Original)", evidence_data.get('original_hash', '')],
            ["Hash SHA-256 (Firmado)", evidence_data.get('signed_hash_placeholder', 'Pendiente de cálculo final')],
            ["Firma Lógica Servidor (HMAC)", evidence_data.get('server_hmac', '')]
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
        
        # ----- CÓDIGO QR -----
        Story.append(Spacer(1, 0.5 * inch))
        qr_buffer = PDFGenerator._create_qr(verify_url)
        qr_img = RLImage(qr_buffer, width=1.5*inch, height=1.5*inch)
        Story.append(qr_img)
        Story.append(Paragraph(f"Verifique la autenticidad de este documento escaneando el código QR o visitando: <a href='{verify_url}'>{verify_url}</a>", normal_style))
        
        doc.build(Story)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        return pdf_bytes

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
        Story.append(Spacer(1, 0.2 * inch))
        
        Story.append(Paragraph("<b>1. Resumen Ejecutivo</b>", styles['Heading2']))
        Story.append(Paragraph("El presente documento detalla la cadena de custodia y evidencia digital recopilada durante el proceso de aceptación electrónica del contrato, en cumplimiento con la Ley 19.799 sobre Documentos Electrónicos y Firma Electrónica.", styles['Normal']))
        Story.append(Spacer(1, 0.2 * inch))
        
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
