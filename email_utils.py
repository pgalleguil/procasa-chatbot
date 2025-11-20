# email_utils.py
# Versión corregida de email_utils.py: FIX principal: Remover server.quit() redundante en try.
# Mover quit() SOLO a finally (después de send). En except SMTPDataError, recrea server para fallback.
# Esto evita "SMTPServerDisconnected" al intentar quit() en server ya desconectado.
# Resto sin cambios (mantiene tu HTML moderno, etc.).

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime, timezone
from config import Config
from dotenv import load_dotenv
import re

load_dotenv()
config = Config()

def format_price_display(criteria: dict) -> str:
    precio_uf = criteria.get('precio_uf') or criteria.get('precio', '')
    precio_clp = criteria.get('precio_clp', '')
    
    # Helper para formatear dict a string legible
    def format_price_dict(price_dict, currency):
        if not isinstance(price_dict, dict) or not price_dict:
            return str(price_dict) if price_dict else ''
        op = list(price_dict.keys())[0]
        val = price_dict[op]
        if op == '$lte':
            return f"hasta {val} {currency}"
        elif op == '$gte':
            return f"desde {val} {currency}"
        elif op == '$eq' or op == '$in':
            return f"{val} {currency}"
        else:
            return f"{val} {currency} ({op})"  # Fallback genérico
    
    # Chequea si es UF (dict o string con 'uf')
    if precio_uf:
        if isinstance(precio_uf, dict):
            return format_price_dict(precio_uf, 'UF')
        elif 'uf' in str(precio_uf).lower():
            return f"{precio_uf} UF"
    
    # Chequea CLP (dict o string con 'millon')
    elif precio_clp:
        if isinstance(precio_clp, dict):
            return format_price_dict(precio_clp, 'CLP')
        elif precio_clp:
            return f"{precio_clp} CLP"
        else:
            raw_precio = criteria.get('precio', '')
            num_match = re.search(r'(\d+(?:\.\d+)?)\s*millon(?:es)?', str(raw_precio).lower())
            if num_match:
                num = num_match.group(1)
                return f"{num} millones CLP"
            return f"{raw_precio} CLP"
    
    return 'No especificada'

def send_gmail_alert(phone: str, lead_type: str, lead_score: int, criteria: dict,
                     last_response: str = '', last_user_msg: str = '',
                     recent_history: str = '', chat_id: str = None, full_history: list = None):
    import html
    from urllib.parse import quote

    if not config.GMAIL_USER or not config.GMAIL_PASSWORD:
        print("[WARNING] Gmail no configurado correctamente.")
        return

    recipient = config.ALERT_EMAIL_RECIPIENT
    cc_email = os.getenv("CC_EMAIL_JEFE")
    subject = f"Nuevo Lead | {phone} | Urgencia {lead_score}/10"

    precio_display = format_price_display(criteria)

    resumen_text = ""
    chat_html = ""
    if full_history and isinstance(full_history, list):
        total_msgs = len(full_history)
        if total_msgs > 25:
            resumen_text = f"Se detectó una conversación extensa de {total_msgs} mensajes. Se muestran los últimos 25."
            messages_to_display = full_history[-25:]
        else:
            messages_to_display = full_history
        bubbles = []
        for msg in messages_to_display:
            content = html.escape(str(msg.get("content", "")).strip())
            role = msg.get("role", "")
            if not content:
                continue
            if role == "assistant":
                bubbles.append(f"""
                    <div class="bubble bot">
                        <div class="content">{content}</div>
                    </div>
                """)
            else:
                bubbles.append(f"""
                    <div class="bubble user">
                        <div class="content">{content}</div>
                    </div>
                """)
        chat_html = "<div class='chat-window'>" + "".join(bubbles) + "</div>"
    else:
        chat_html = "<p style='color:#777;'>No hay historial de conversación disponible.</p>"

    tipo = criteria.get('tipo', 'propiedad')
    comuna = criteria.get('comuna', 'tu zona')
    precio = precio_display if precio_display != 'No especificada' else ''
    wa_message = (
        f"Hola 👋, soy del equipo Procasa. "
        f"Vi que te interesa una {tipo} en {comuna}{' (' + precio + ')' if precio else ''}. "
        f"¿Podrías confirmarme si sigues buscando?"
    )
    wa_encoded = quote(wa_message)
    wa_link = f"https://wa.me/+{phone.replace('+', '')}?text={wa_encoded}"

    criteria_summary = f"""
    <ul style="margin:0; padding-left:20px; font-size:15px; line-height:1.7;">
        <li><strong>Operación:</strong> {criteria.get('operacion', 'No especificada')}</li>
        <li><strong>Tipo:</strong> {tipo}</li>
        <li><strong>Comuna:</strong> {comuna}</li>
        <li><strong>Dormitorios:</strong> {criteria.get('dormitorios', 'No especificado')}</li>
        <li><strong>Precio:</strong> {precio_display}</li>
        <li><strong>Estacionamientos:</strong> {criteria.get('estacionamientos', 'No especificado')}</li>
        <li><strong>Baños:</strong> {criteria.get('banos', 'No especificado')}</li>
        <li><strong>Superficie:</strong> {criteria.get('superficie', 'No especificada')}</li>
    </ul>
    """

    # === HTML con idioma español forzado y texto oculto invisible para Gmail ===
    body_html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="Content-Language" content="es">
        <meta name="language" content="Spanish">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #f4f5f7;
                color: #333;
                margin: 0;
                padding: 25px 0;
                line-height: 1.7;
            }}
            .container {{
                max-width: 720px;
                margin: 0 auto;
                background: #fff;
                border-radius: 16px;
                border: 1px solid #e0e4e8;
                box-shadow: 0 4px 20px rgba(0,0,0,0.06);
                overflow: hidden;
            }}
            .header {{
                text-align: center;
                padding: 20px 20px 10px;
                background: #fff;
            }}
            .header h2 {{
                font-size: 23px;
                margin: 0 0 0 0;
                color: #003366;
                font-weight: 600;
                letter-spacing: -0.2px;
            }}
            .urgency {{
                text-align: center;
                background: linear-gradient(90deg, {'#FF8C42' if lead_score >= 6 else '#3BD173'}, {'#FF7A00' if lead_score >= 6 else '#2CA95E'});
                color: white;
                font-weight: 600;
                font-size: 15px;
                padding: 14px 0;
                border-top-left-radius: 16px;
                border-top-right-radius: 16px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.06);
                margin-bottom: 0;
                letter-spacing: 0.2px;
            }}
            .section {{
                padding: 28px 30px;
                border-bottom: 1px solid #f0f0f0;
            }}
            .section h3 {{
                font-size: 18px;
                color: #003366;
                margin: 0 0 16px;
                font-weight: 600;
                letter-spacing: -0.1px;
            }}
            .chat-window {{
                background: #fafbfc;
                border: 1px solid #e1e4e8;
                border-radius: 12px;
                padding: 18px 20px;
                max-height: 420px;
                overflow-y: auto;
            }}
            .bubble {{
                width: 100%;
                margin: 10px 0;
                clear: both;
                overflow: auto;
            }}
            .bubble .content {{
                display: inline-block;
                padding: 13px 17px;
                border-radius: 18px;
                font-size: 14px;
                line-height: 1.6;
                max-width: 75%;
                word-wrap: break-word;
                box-shadow: 0 1px 2px rgba(0,0,0,0.04);
            }}
            .bubble.user .content {{
                float: left;
                background: #f1f1f1;
                color: #111;
                border: 1px solid #e0e0e0;
                border-top-left-radius: 6px;
            }}
            .bubble.bot .content {{
                float: right;
                background: #d8e7ff;
                color: #002147;
                border: 1px solid #c9dbfa;
                border-top-right-radius: 6px;
            }}
            .actions {{
                text-align: center;
                padding: 28px 25px;
            }}
            .btn {{
                display: inline-block;
                padding: 14px 30px;
                border-radius: 8px;
                color: white !important;
                font-weight: 600;
                text-decoration: none;
                margin: 0 20px;
                transition: all 0.2s ease;
                font-size: 15px;
                letter-spacing: 0.1px;
                text-shadow: none;
            }}
            .btn-call {{
                background: #0052cc;
            }}
            .btn-wa {{
                background: #25D366;
            }}
            .btn:hover {{
                opacity: 0.95;
                transform: translateY(-1px);
            }}
            .footer {{
                text-align: center;
                font-size: 13px;
                color: #777;
                padding: 25px 20px;
                background: #fafafa;
                border-top: 1px solid #eee;
                line-height: 1.5;
            }}
            .footer img {{
                display: block;
                margin: 0 auto 8px auto;
                height: 70px;
                max-width: 220px;
            }}
            .footer a {{
                color: #003366;
                text-decoration: none;
            }}
            @media (max-width: 600px) {{
                .container {{ margin: 10px; border-radius: 16px; }}
                .section {{ padding: 20px 20px; }}
                .header {{ padding: 25px 15px 15px; }}
                .btn {{ display: block; margin: 15px auto; width: 90%; text-align: center; }}
                .chat-window {{ height: 300px; padding: 15px; }}
            }}
        </style>
    </head>
    <body>
        <!-- TEXTO INVISIBLE PARA INDICAR ESPAÑOL A GMAIL -->
        <span style="display:none;">Este mensaje está en español.</span>

        <div class="container">
            <div class="urgency">
                Nivel de urgencia: {lead_score}/10 ({'Alta' if lead_score >= 6 else 'Media'})
            </div>
            <div class="header">
                <h2>¡Lead Caliente Detectado!</h2>
            </div>
            <div class="section">
                <h3>Conversación Reciente</h3>
                {f"<p style='font-size:13px; color:#777; margin-bottom:12px;'>{resumen_text}</p>" if resumen_text else ""}
                {chat_html}
            </div>
            <div class="section">
                <h3>Datos y Preferencias del Cliente</h3>
                {criteria_summary}
            </div>
            <div class="actions">
                <a href="tel:+{phone.replace('+', '')}" class="btn btn-call">📞 Llamar</a>
                <a href="{wa_link}" class="btn btn-wa">💬 WhatsApp</a>
            </div>
            <div class="footer">
                <img src="cid:logo_procasa" alt="Procasa Logo">
                Procasa Jorge Pablo Caro Propiedades<br>
                Av. Sucre 2560, Ñuñoa | <a href="https://www.procasa.cl">www.procasa.cl</a>
            </div>
        </div>
    </body>
    </html>
    """

    server = None  # Inicializa fuera para finally
    try:
        msg = MIMEMultipart('related')
        msg['Subject'] = subject
        msg['From'] = f"Alertas Procasa <{config.GMAIL_USER}>"
        msg['To'] = recipient
        msg['MIME-Version'] = '1.0'  # ← Agregar: Header obligatorio
        msg['Content-Type'] = 'text/html; charset=utf-8'  # ← Sin quotes en charset
        msg.add_header('Content-Language', 'es')
        if cc_email and cc_email != "# Opcional":
            msg['Cc'] = cc_email

        alt = MIMEMultipart('alternative')
        alt.attach(MIMEText(body_html, 'html', 'utf-8'))
        msg.attach(alt)

        # Logo (sin cambios)
        logo_path = r"C:\Users\pgall\Desktop\Python\ChatBot_v3\static\logo.png"
        if os.path.exists(logo_path):
            with open(logo_path, 'rb') as f:
                logo = MIMEImage(f.read(), name='logo.png')
                logo.add_header('Content-ID', '<logo_procasa>')
                msg.attach(logo)

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(config.GMAIL_USER, config.GMAIL_PASSWORD)
        server.send_message(msg)
        # NO quit() aquí: Va en finally

        print(f"[EMAIL] Correo enviado correctamente a {recipient} ({phone})")
    except smtplib.SMTPRecipientsRefused as e:  # ← Granular: Errores de destinatario
        print(f"[SMTP ERROR] Destinatario inválido: {e.recipients}")
    except smtplib.SMTPAuthenticationError as e:  # ← Login fallido
        print(f"[SMTP ERROR] Autenticación fallida: {e}")
    except smtplib.SMTPDataError as e:  # ← Syntax en data (tu caso)
        print(f"[SMTP ERROR] Syntax en mensaje: {e}. Chequea headers/encoding.")
        # Fallback: Envía versión plain-text simple (recrea server nuevo)
        try:
            simple_msg = MIMEText(f"Lead urgente: {phone} - {lead_type} (score {lead_score}). Criterios: {criteria}", 'plain', 'utf-8')
            simple_msg['Subject'] = subject
            simple_msg['From'] = f"Alertas Procasa <{config.GMAIL_USER}>"
            simple_msg['To'] = recipient
            if cc_email and cc_email != "# Opcional":
                simple_msg['Cc'] = cc_email

            fallback_server = smtplib.SMTP('smtp.gmail.com', 587)
            fallback_server.starttls()
            fallback_server.login(config.GMAIL_USER, config.GMAIL_PASSWORD)
            fallback_server.send_message(simple_msg)
            fallback_server.quit()
            print(f"[EMAIL] Fallback plain-text enviado a {recipient} ({phone})")
        except Exception as fallback_e:
            print(f"[SMTP ERROR] Fallback falló: {fallback_e}")
    except Exception as e:
        print(f"[ERROR] Falló envío de correo: {e}")
    finally:
        if server:
            try:
                server.quit()
            except:
                pass  # Ignora si ya desconectado