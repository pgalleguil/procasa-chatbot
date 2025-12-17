# email_utils.py
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from config import Config
from dotenv import load_dotenv
import html
from urllib.parse import quote

load_dotenv()
config = Config()

def format_price_display(criteria: dict) -> str:
    """Formatea el precio para mostrar en el correo, manejando dicts de mongo o valores planos."""
    precio_uf = criteria.get('precio_uf') or criteria.get('precio', '')
    precio_clp = criteria.get('precio_clp', '')
    
    def format_val(val, curr):
        # Si es diccionario (ej: {$lte: 5000})
        if isinstance(val, dict):
            op = list(val.keys())[0]
            v = val[op]
            if op == '$lte': return f"Hasta {v} {curr}"
            elif op == '$gte': return f"Desde {v} {curr}"
            return f"{v} {curr}"
        # Si es valor plano (int, float, str)
        if val:
            # Formateo básico de miles si es número
            if isinstance(val, (int, float)):
                return f"{val:,.0f} {curr}".replace(",", ".")
            return f"{val} {curr}"
        return ""

    # Prioridad UF
    res_uf = format_val(precio_uf, 'UF')
    if res_uf: return res_uf

    # Si no hay UF, usar CLP
    res_clp = format_val(precio_clp, 'CLP')
    if res_clp:
        return res_clp.replace("millones", "MM").replace("millon", "MM")
    
    return None

def _attach_logo(msg):
    """Helper interno para adjuntar el logo principal y el de WhatsApp si existen."""
    base_dir = os.path.dirname(os.path.abspath(__file__)) 
    
    attached_procasa = False
    
    # Lista de archivos a buscar y su Content-ID
    logos_to_attach = [
        {'id': '<logo_procasa>', 'filename': 'logo.png'},
        {'id': '<logo_wa>', 'filename': 'whatsapp.png'} 
    ]
    
    for logo_data in logos_to_attach:
        logo_path = None
        # Buscar en las ubicaciones habituales
        possible_paths = [
            os.path.join(base_dir, 'static', logo_data['filename']),
            os.path.join(base_dir, '..', 'static', logo_data['filename']),
            os.path.join(os.getcwd(), 'static', logo_data['filename']) 
        ]
        
        for p in possible_paths:
            if os.path.exists(p):
                logo_path = p
                break

        if logo_path:
            try:
                with open(logo_path, 'rb') as f:
                    img = MIMEImage(f.read())
                    img.add_header('Content-ID', logo_data['id']) 
                    msg.attach(img)
                    if logo_data['id'] == '<logo_procasa>':
                        attached_procasa = True
            except Exception:
                pass # Continúa si falla adjuntar una imagen
                
    # Retorna True solo si el logo principal de Procasa se pudo adjuntar (mantiene la lógica original)
    return attached_procasa

def send_gmail_alert(phone: str, lead_score: int, criteria: dict,
                     last_response: str = '', last_user_msg: str = '',
                     full_history: list = None, lead_type: str = "Prospecto"):

    if not config.GMAIL_USER or not config.GMAIL_PASSWORD:
        print("[EMAIL] Credenciales no configuradas.")
        return

    recipient = config.ALERT_EMAIL_RECIPIENT
    cc_email = os.getenv("CC_EMAIL_JEFE")
    
    # --- Configuración Visual ---
    if lead_score >= 8:
        color_theme = "#DC2626" # Rojo
        bg_theme = "#FEF2F2"
        icon = "🔥"
        label = "ALTA PRIORIDAD (Visita)"
    elif lead_score >= 5:
        color_theme = "#D97706" # Naranja
        bg_theme = "#FFFBEB"
        icon = "⚡"
        label = "INTERÉS MEDIO"
    else:
        color_theme = "#059669" # Verde
        bg_theme = "#ECFDF5"
        icon = "🌱"
        label = "CONSULTA GENERAL"

    subject = f"{icon} Prospecto {label} | {phone}"

    # --- Formateo de Teléfono para mejor lectura ---
    phone_formatted = phone
    phone_digits = phone.replace('+', '').strip()
    
    # Si es un número chileno (+56) de 11 dígitos sin el '+', lo reformateamos
    if phone_digits.startswith('56') and len(phone_digits) == 11:
        # Formato esperado: +56 9 6640 9364
        try:
            phone_formatted = f"+{phone_digits[0:2]} {phone_digits[2]} {phone_digits[3:7]} {phone_digits[7:]}"
        except IndexError:
            # En caso de que la cadena no tenga la longitud esperada por algún error
            pass
    # --- FIN Formateo de Teléfono ---
    
    # Datos Propiedad
    precio_val = format_price_display(criteria)
    precio = precio_val if precio_val else '<span style="color:#CBD5E1; font-size:18px;">-</span>'
    
    tipo = str(criteria.get('tipo', 'Propiedad')).title()
    comuna = str(criteria.get('comuna', 'Zona General')).title()
    operacion = str(criteria.get('operacion', 'Operación')).upper()
    codigo_prop = criteria.get('codigo', '')
    
    if codigo_prop:
        tipo = f"{tipo} (Cod: {codigo_prop})"
    
    dorm = str(criteria.get('dormitorios', '-'))
    banos = str(criteria.get('banos', '-'))
    # ELIMINADO: estac = str(criteria.get('estacionamientos', '-')) 
    
    # --- Chat History (Últimos 6 mensajes para no hacer spam visual) ---
    chat_html = ""
    if full_history and isinstance(full_history, list):
        bubbles = []
        # Tomamos los últimos 8 mensajes para contexto
        recent_msgs = full_history[-15:]
        
        for msg in recent_msgs: 
            txt = html.escape(str(msg.get("content", "")).strip())
            role = msg.get("role", "")
            if not txt or role == "system": continue
            
            if role == "assistant":
                bubbles.append(f'''
                <div style="margin-bottom: 10px; overflow: hidden;">
                    <div style="float: left; width: auto; max-width: 88%;">
                        <div style="font-size: 9px; color: #94A3B8; margin-bottom: 2px; margin-left: 2px; font-weight: 700;">PROCASA AI</div>
                        <div style="background: #F8FAFC; color: #334155; padding: 8px 12px; border-radius: 12px 12px 12px 2px; font-size: 13px; line-height: 1.4; border: 1px solid #E2E8F0;">
                            {txt}
                        </div>
                    </div>
                </div>''')
            else:
                bubbles.append(f'''
                <div style="margin-bottom: 10px; overflow: hidden;">
                    <div style="float: right; width: auto; max-width: 88%;">
                        <div style="background: #004488; color: #FFFFFF; padding: 8px 12px; border-radius: 12px 12px 2px 12px; font-size: 13px; line-height: 1.4; box-shadow: 0 1px 2px rgba(0,0,0,0.1);">
                            {txt}
                        </div>
                    </div>
                </div>''')
        chat_html = "".join(bubbles)
    else:
        chat_html = "<div style='text-align:center; color:#94A3B8; font-size:12px; padding: 15px;'>Sin historial disponible.</div>"

    # Links
    wa_msg = quote(f"Hola, te escribo de Procasa. Vi que consultaste por {tipo}...")
    # wa_link y tel:phone usan la variable original 'phone' para asegurar el correcto marcado y link
    wa_link = f"https://wa.me/{phone.replace('+','')}?text={wa_msg}" 

    # --- HTML STRUCTURE ---
    body_html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
    <meta charset="UTF-8">
    <style>
        body {{ margin: 0; padding: 0; font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #F1F5F9; }}
        .wrapper {{ width: 100%; table-layout: fixed; background-color: #F1F5F9; padding: 30px 0; }}
        .card {{ 
            background: #FFFFFF; 
            max-width: 550px; 
            margin: 0 auto; 
            border-radius: 16px; 
            box-shadow: 0 10px 25px -5px rgba(0,0,0,0.05), 0 8px 10px -6px rgba(0,0,0,0.01);
            overflow: hidden; 
            border: 1px solid #E2E8F0;
        }}
    </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="card">
                
                <div style="padding: 25px 0 10px 0; text-align: center; background: #FFFFFF;">
                    <img src="cid:logo_procasa" alt="Procasa" width="140" style="display: block; margin: 0 auto; height: auto;">
                </div>

                <div style="padding: 5px 30px 20px 30px; text-align: center;">
                    
                    <div style="display: inline-block; background: {bg_theme}; color: {color_theme}; padding: 4px 12px; border-radius: 99px; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px;">
                        {icon} {label}
                    </div>
                    
                    <h1 style="margin: 0 0 6px 0; font-size: 30px; letter-spacing: -0.8px; color: #0F172A; font-weight: 800;">{phone_formatted}</h1>
                    <p style="margin: 0; color: #64748B; font-size: 15px;">
                        Interés en <strong style="color:#334155;">{comuna}</strong>
                    </p>
                </div>

                <div style="padding: 0 30px 25px 30px; text-align: center;">
                    <table width="100%" cellspacing="0" cellpadding="0">
                        <tr>
                            <td width="48%">
                                <a href="{wa_link}" style="display: block; background: #22C55E; color: #fff; text-decoration: none; padding: 10px 0; border-radius: 10px; font-weight: 600; font-size: 14px; text-align: center; box-shadow: 0 2px 4px rgba(34, 197, 94, 0.2); line-height: 1;">
                                    <img src="cid:logo_wa" alt="WhatsApp" width="18" height="18" style="vertical-align: middle; margin-right: 6px;">
                                    WhatsApp
                                </a>
                            </td>
                            <td width="4%"></td>
                            <td width="48%">
                                <a href="tel:{phone}" style="display: block; background: #F8FAFC; color: #334155; text-decoration: none; padding: 12px 0; border-radius: 10px; font-weight: 600; font-size: 14px; text-align: center; border: 1px solid #E2E8F0;">
                                    📞 Llamar
                                </a>
                            </td>
                        </tr>
                    </table>
                </div>

                <div style="background: #FAFAFA; padding: 20px 30px; border-top: 1px solid #F1F5F9; border-bottom: 1px solid #F1F5F9;">
                    <table width="100%" cellspacing="0" cellpadding="0" style="margin-bottom: 8px;">
                        <tr>
                            <td width="49%" style="background: #FFFFFF; padding: 10px; border-radius: 8px; border: 1px solid #E2E8F0; text-align: center;">
                                <div style="font-size: 9px; color: #94A3B8; text-transform: uppercase; margin-bottom: 2px;">Presupuesto</div>
                                <div style="font-size: 13px; color: #0F172A; font-weight: 600;">{precio}</div>
                            </td>
                            <td width="2%"></td>
                            <td width="49%" style="background: #FFFFFF; padding: 10px; border-radius: 8px; border: 1px solid #E2E8F0; text-align: center;">
                                <div style="font-size: 9px; color: #94A3B8; text-transform: uppercase; margin-bottom: 2px;">Operación</div>
                                <div style="font-size: 13px; color: #0F172A; font-weight: 600;">{operacion}</div>
                            </td>
                        </tr>
                    </table>
                    
                    <table width="100%" cellspacing="0" cellpadding="0">
                        <tr>
                            <td width="49%" style="background: #FFFFFF; padding: 8px; border-radius: 8px; border: 1px solid #E2E8F0; text-align: center;">
                                <div style="font-size: 9px; color: #94A3B8;">Dorms</div>
                                <div style="font-size: 13px; color: #0F172A; font-weight: 600;">{dorm}</div>
                            </td>
                            <td width="2%"></td>
                            <td width="49%" style="background: #FFFFFF; padding: 8px; border-radius: 8px; border: 1px solid #E2E8F0; text-align: center;">
                                <div style="font-size: 9px; color: #94A3B8;">Baños</div>
                                <div style="font-size: 13px; color: #0F172A; font-weight: 600;">{banos}</div>
                            </td>
                            </tr>
                    </table>
                </div>

                <div style="padding: 25px 30px 20px 30px;">
                    <div style="font-size: 9px; font-weight: 700; color: #94A3B8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 15px; text-align: center;">
                        Últimos Mensajes
                    </div>
                    {chat_html}
                </div>
            </div>
            
            <div style="text-align: center; margin-top: 15px; color: #CBD5E1; font-size: 11px;">
                © 2026 Procasa AI
            </div>
        </div>
    </body>
    </html>
    """

    server = None
    try:
        msg = MIMEMultipart('related')
        msg['Subject'] = subject
        msg['From'] = f"Procasa AI <{config.GMAIL_USER}>"
        msg['To'] = recipient
        msg.add_header('Content-Language', 'es')
        if cc_email: msg['Cc'] = cc_email

        msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        _attach_logo(msg)

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(config.GMAIL_USER, config.GMAIL_PASSWORD)
        server.send_message(msg)
        print(f"[EMAIL] Enviado a {recipient} | Score: {lead_score}")

    except Exception as e:
        print(f"[ERROR] Falló envío email: {e}")
    finally:
        if server: 
            try: server.quit() 
            except: pass