import sys
import os
import time
import random
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None

# Configuración de rutas para importar módulos del proyecto
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import Config
from chatbot.storage import get_db

def split_rol(rol):
    """Separa el rol en manzana y predio. Ejemplo: 00494-00006"""
    if not rol or '-' not in rol:
        return None, None
    parts = rol.split('-')
    return parts[0].strip(), parts[1].strip()

def run_automation():
    db = get_db()
    universo = db[Config.COLLECTION_NAME]
    tasaciones = db["tasaciones"]

    # --- CONFIGURACIÓN DE BÚSQUEDA ---
    TARGET_CODE = None  # Cambia esto a None para procesar todas las pendientes
    # --------------------------------

    codigos_ya_procesados = tasaciones.distinct("codigo_propiedad")

    if TARGET_CODE:
        query = {"codigo": TARGET_CODE}
        print(f"Modo: Procesando ÚNICAMENTE el código {TARGET_CODE}")
    else:
        query = {
            "disponible": True, 
            "oficina": "PROCASA SUCRE",
            "operacion": "Venta",
            "codigo": {"$nin": codigos_ya_procesados}
        }
        print(f"Modo: Procesando propiedades pendientes en RM (Venta). Omitiendo {len(codigos_ya_procesados)} ya procesadas.")

    properties = list(universo.find(query).limit(400)) 
    print(f"Propiedades encontradas para procesar: {len(properties)}")

    if not properties:
        print("No se encontraron propiedades que cumplan los criterios.")
        return

    with sync_playwright() as p:
        # headless=False para que puedas ver el proceso en tu pantalla
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        
        if stealth_sync:
            stealth_sync(page)

        # 1. Login obligatorio
        try:
            print("Iniciando sesión manual en Propiteq...")
            page.goto("https://www.propiteq.com/login")
            time.sleep(random.uniform(1.0, 2.5))
            page.locator('//*[@id="login-email"]').press_sequentially("referidosjpc@procasa.cl", delay=random.randint(50, 150))
            time.sleep(random.uniform(0.5, 1.5))
            page.locator('//*[@id="login-password"]').press_sequentially("Leoncarogalleguillos22305607929a$", delay=random.randint(50, 150))
            time.sleep(random.uniform(0.5, 1.5))
            page.click('//*[@id="__nuxt"]/div/div[3]/div/form[1]/div[3]/button')
            
            # Esperar a que la redirección tras el login se estabilice en el dashboard
            page.wait_for_url("**/cliente/dashboard**", timeout=30000)
            time.sleep(3)
            
            # Después del login nos metemos a búsqueda
            print("Sesión iniciada. Dirigiendo a la página de búsqueda de tasación...")
            page.goto("https://www.propiteq.com/servicios/tasacion-online/busqueda", wait_until="commit")
            
            # Esperamos específicamente que la interfaz de búsqueda esté lista
            page.wait_for_load_state("domcontentloaded")
            time.sleep(3)

        except Exception as e:
            print(f"Error fatal en login o inicio de contexto: {e}")
            browser.close()
            return
        time.sleep(2)
        
        # Eliminar banner de cookies si existe
        print("Intentando aceptar ventana de cookies...")
        try:
            page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const cookieBtn = buttons.find(b => 
                    b.textContent.toLowerCase().includes('aceptar') || 
                    b.innerText.toLowerCase().includes('aceptar') ||
                    b.textContent.toLowerCase().includes('entendido')
                );
                if (cookieBtn) cookieBtn.click();
            }""")
        except Exception as e_cook:
            print(f"Error o sin cookies: {e_cook}")

        for prop in properties:
            codigo = prop.get("codigo")
            rol = prop.get("rol")
            comuna = prop.get("comuna")

            print(f"\n--- Procesando propiedad {codigo} ---")

            manzana, predio = split_rol(rol)
            if not manzana or not predio:
                print(f"Propiedad {codigo}: Rol inválido o inexistente ({rol})")
                continue

            try:
                # --- PASO 1: Asegurarse de estar en la pestaña ROL y que los campos estén listos ---
                tab_listo = False
                for intento_tab in range(3):
                    try:
                        tab_btn = page.locator('button#tab-1')
                        tab_btn.click(force=True, timeout=8000)
                        time.sleep(1)
                        # Verificar que el tab está realmente activo y el input visible
                        if tab_btn.get_attribute('aria-selected') == 'true':
                            page.wait_for_selector('input#manzana', state='visible', timeout=5000)
                            tab_listo = True
                            break
                        else:
                            print(f"  Reintento {intento_tab+1}/3: El botón Rol no quedó como seleccionado...")
                    except Exception:
                        print(f"  Reintento {intento_tab+1}/3: Esperando que pestaña Rol cargue...")
                        time.sleep(2)
                
                if not tab_listo:
                    raise Exception("No se pudo activar la pestaña Rol después de 3 intentos")
                
                time.sleep(random.uniform(0.8, 1.5))
                
                # --- PASO 2: Ingresar Manzana y Predio con verificación ---
                campo_manzana = page.locator('//*[@id="manzana"]')
                campo_manzana.fill("")
                time.sleep(0.3)
                campo_manzana.press_sequentially(manzana, delay=100)
                time.sleep(random.uniform(0.4, 0.8))
                
                # Verificar que se escribió
                val_manzana = campo_manzana.input_value()
                if not val_manzana or manzana not in val_manzana:
                    print(f"  Aviso: Manzana no se escribió correctamente. Reintentando...")
                    campo_manzana.fill(manzana)
                    time.sleep(0.5)
                
                campo_predio = page.locator('//*[@id="predio"]')
                campo_predio.fill("")
                time.sleep(0.3)
                campo_predio.press_sequentially(predio, delay=100)
                time.sleep(random.uniform(0.4, 0.8))
                
                val_predio = campo_predio.input_value()
                if not val_predio or predio not in val_predio:
                    print(f"  Aviso: Predio no se escribió correctamente. Reintentando...")
                    campo_predio.fill(predio)
                    time.sleep(0.5)
                
                # --- PASO 3: Seleccionar Comuna con hasta 3 reintentos ---
                print(f"Buscando comuna: {comuna}...")
                comuna_seleccionada = False
                for intento_com in range(3):
                    try:
                        campo_com = page.locator('//*[@id="comuna"]')
                        campo_com.fill("")
                        time.sleep(0.5)
                        campo_com.press_sequentially(comuna, delay=random.randint(60, 110))
                        # Esperar el dropdown con más tiempo
                        page.wait_for_selector('li[role="option"]', timeout=8000)
                        page.locator('li[role="option"]').nth(0).click(force=True)
                        # Verificar que la selección fue reconocida
                        time.sleep(0.5)
                        val_com = campo_com.input_value()
                        if val_com and len(val_com) > 2:
                            print(f"Comuna seleccionada: '{val_com}'")
                            comuna_seleccionada = True
                            break
                        else:
                            print(f"  Reintento {intento_com+1}/3: La comuna no quedó seleccionada (valor: '{val_com}')")
                    except Exception as com_err:
                        print(f"  Reintento {intento_com+1}/3: {com_err}")
                        time.sleep(1)
                
                if not comuna_seleccionada:
                    raise Exception(f"No se pudo seleccionar la comuna '{comuna}' después de 3 intentos. Saltando.")
                
                # --- PASO 4: Validar que los campos estén completos ANTES de presionar Valorizar ---
                v_manz = page.locator('//*[@id="manzana"]').input_value()
                v_pred = page.locator('//*[@id="predio"]').input_value()
                v_com  = page.locator('//*[@id="comuna"]').input_value()
                print(f"Verificacion pre-Valorizar: manzana='{v_manz}' predio='{v_pred}' comuna='{v_com[:20] if v_com else ''}'")
                
                if not v_manz or not v_pred or not v_com:
                    raise Exception(f"Campos vacios antes de Valorizar: manzana={bool(v_manz)}, predio={bool(v_pred)}, comuna={bool(v_com)}")
                
                print(f"Haciendo clic en Valorizar...")
                page.locator('//*[@id="__nuxt"]/div/main/div/div[2]/div/form/div[3]/button').click(force=True)
                
                # --- DETECCIÓN DE ERROR / PROPIEDAD NO TASABLE ---
                # Algunas propiedades tardan más en mostrar la alerta, esperamos más tiempo
                time.sleep(4)
                print(f"URL actual tras valorizar: {page.url}")
                
                # Chequeamos la alerta roja usando el xpath y también con detectores de texto directos
                is_alert_div = page.locator('//*[@id="avisos"]/div/div').is_visible()
                is_alert_text_1 = page.locator('text="Esta propiedad no se puede valorizar"').first.is_visible()
                is_alert_text_2 = page.locator('text="no cuenta con tasación automática"').first.is_visible()
                
                # Check nuevo: Dialog "No encontramos la propiedad"
                is_not_found_dialog = False
                try:
                    dlg_no_encontrada = page.locator('dialog:has-text("No encontramos la propiedad")')
                    if dlg_no_encontrada.is_visible(timeout=1000):
                        is_not_found_dialog = True
                except Exception:
                    pass
                
                if is_alert_div or is_alert_text_1 or is_alert_text_2 or is_not_found_dialog:
                    if is_not_found_dialog:
                        msg_error = "No encontramos la propiedad"
                    else:
                        msg_error = "Propiedad sin tasación automática disponible."
                        try:
                            if is_alert_div:
                                msg_error = page.inner_text('//*[@id="avisos"]/div/div', timeout=2000).replace('\n', ' - ')
                        except:
                            pass

                    print(f"AVISO SISTEMA: {msg_error}. Saltando inteligentemente a la siguiente propiedad.")
                    status_str = "no_encontrada" if is_not_found_dialog else "no_tasable"
                    tasaciones.insert_one({
                        "codigo_propiedad": codigo,
                        "status": status_str,
                        "mensaje": msg_error,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    })
                    page.goto("https://www.propiteq.com/servicios/tasacion-online/busqueda", wait_until="commit")
                    continue

                # Esperar formulario de detalles — probamos selectores alternativos
                print("Esperando formulario de detalles...")
                formulario_visible = False
                for sel_ancla in ['//*[@id="dormitorios"]', '//*[@id="tempTerr"]', '//*[@id="gastosComunes"]']:
                    try:
                        page.wait_for_selector(sel_ancla, timeout=8000)
                        formulario_visible = True
                        break
                    except Exception:
                        pass
                
                if not formulario_visible:
                    print(f"AVISO: No apareció el formulario de detalles. URL: {page.url}. Marcando como error y saltando.")
                    tasaciones.insert_one({
                        "codigo_propiedad": codigo,
                        "status": "error_formulario_no_cargado",
                        "mensaje": f"El formulario no cargó tras Valorizar. URL: {page.url}",
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    })
                    page.goto("https://www.propiteq.com/servicios/tasacion-online/busqueda", wait_until="commit")
                    continue
                
                # 3. Rellenar detalles con suficiente tiempo entre campos
                print("Completando información de la propiedad...")
                detalles_enviados = {}
                
                def fill_spinner(selector, valor):
                    """Limpia y escribe en campos numéricos (spinners de +/-)"""
                    try:
                        el = page.locator(selector)
                        if not el.is_visible(timeout=3000):
                            return False
                        el.click(click_count=3)  # Triple-click para seleccionar todo
                        time.sleep(0.3)
                        el.fill("")        # Borra
                        time.sleep(0.3)
                        el.press_sequentially(str(valor), delay=random.randint(60, 120))
                        time.sleep(random.uniform(0.5, 1.0))
                        return True
                    except Exception as fe:
                        print(f"  Info: No se pudo completar campo {selector}: {fe}")
                        return False
                
                # Campos numéricos (spinners)
                for db_field, selector in [
                    ("dormitorios",      '//*[@id="dormitorios"]'),
                    ("banos",            '//*[@id="banos"]'),
                    ("estacionamientos", '//*[@id="estacionamientos"]'),
                    ("m2_terreno",       '//*[@id="tempTerr"]'),
                    ("gastos_comunes",   '//*[@id="gastosComunes"]'),
                ]:
                    val = prop.get(db_field)
                    if val is not None and str(val).strip() != "":
                        if fill_spinner(selector, val):
                            detalles_enviados[db_field] = val
                
                # Bodega: en la BD puede ser boolean (True/False) o número
                bodega_val = prop.get("bodega")
                if bodega_val is not None:
                    bodega_num = 1 if (bodega_val is True or bodega_val == 1 or str(bodega_val) == "1") else 0
                    if bodega_num > 0:
                        if fill_spinner('//*[@id="bodegas"]', bodega_num):
                            detalles_enviados["bodega"] = bodega_num
                
                # Orientacion: es un <select> dropdown — usar select_option, no teclear
                orientacion_val = prop.get("orientacion")
                if orientacion_val and str(orientacion_val).strip():
                    try:
                        sel_orient = page.locator('//*[@id="orientacionPropiedad"]')
                        if sel_orient.is_visible(timeout=3000):
                            time.sleep(0.5)
                            sel_orient.fill("")
                            sel_orient.press_sequentially(str(orientacion_val), delay=60)
                            time.sleep(0.8)
                            # Seleccionar la primera opción que aparezca en el dropdown
                            try:
                                page.wait_for_selector('li[role="option"]', timeout=3000)
                                page.locator('li[role="option"]').first.click(force=True)
                                detalles_enviados["orientacion"] = orientacion_val
                            except Exception:
                                # Si no aparece dropdown, presionar Enter como fallback
                                sel_orient.press("Enter")
                                detalles_enviados["orientacion"] = orientacion_val
                            time.sleep(random.uniform(0.4, 0.8))
                    except Exception as oe:
                        print(f"  Aviso: No se pudo seleccionar orientación: {oe}")
                
                print(f"Campos completados: {list(detalles_enviados.keys())}")
                time.sleep(1)  # Pausa final para que Vue/React procese los cambios

                # --- Lógica de Calcular Valor / Recalcular / Generar Informe ---
                btn_calcular_xpath = '//*[@id="__nuxt"]/div[1]/main/div[3]/div[2]/div[3]/button'
                btn_recalcular_xpath = '//*[@id="__nuxt"]/div[1]/main/div[3]/div[2]/div[3]/div/button[2]'
                btn_generar_xpath = '//*[@id="__nuxt"]/div[1]/main/div[3]/div[2]/div[3]/button'
                
                time.sleep(2)  # Espera pequeña para que el DOM se asiente tras ingresar los datos
                
                # 1. Detectar si hay que 'Recalcular' (aparece cuando los datos ya existen y cambian)
                print("Verificando si es necesario Recalcular...")
                recalcular_visible = page.is_visible(btn_recalcular_xpath)
                if recalcular_visible:
                    btn_text_recalc = page.locator(btn_recalcular_xpath).inner_text(timeout=2000).strip()
                    if "Recalcular" in btn_text_recalc:
                        print("Botón 'Recalcular' detectado. Presionando...")
                        page.locator(btn_recalcular_xpath).click(force=True)
                        print("Esperando a que el sistema procese los cambios...")
                        time.sleep(5)
                
                # 2. Detectar si el botón dice 'Calcular valor' (primera vez en la propiedad)
                try:
                    btn_text = page.locator(btn_calcular_xpath).inner_text(timeout=3000).strip()
                    if "Calcular" in btn_text and "Generar" not in btn_text:
                        print(f"Botón '{btn_text}' detectado. Presionando para calcular valores...")
                        page.locator(btn_calcular_xpath).click(force=True)
                        print("Valores calculados. Esperando que el botón cambie a 'Generar informe'...")
                        # Esperar a que el botón cambie de 'Calcular valor' a 'Generar informe'
                        for _ in range(30):
                            try:
                                nuevo_texto = page.locator(btn_calcular_xpath).inner_text(timeout=1000).strip()
                                if "Generar" in nuevo_texto:
                                    print(f"Botón cambió a '{nuevo_texto}'. Continuando...")
                                    break
                            except Exception:
                                pass
                            time.sleep(1)
                except Exception:
                    pass
                
                # 3. Intentar presionar 'Generar informe'
                print("Intentando presionar el botón final (Generar informe)...")
                try:
                    page.wait_for_selector(btn_generar_xpath, timeout=10000)
                    
                    # Esperar explícitamente a que el botón se habilite tras la consulta pesada
                    habilitado = False
                    print("Esperando respuesta del servidor a las características enviadas. La interfaz de Propiteq (esqueleto) está cargando los valores comerciales...")
                    
                    for iteracion in range(180): # Esperamos hasta 3 minutos por la carga asíncrona de los esqueletos azules
                        
                        # Interceptor del modal de iniciar sesión (re-autenticación intrusivo de Vue)
                        if iteracion % 3 == 0:
                            try:
                                # Buscar el ID de email en vez del texto, es más certero
                                locator_email = page.locator('#login-email').last
                                if locator_email.is_visible():
                                    print("\n[!] El servidor invalidó el token y lanzó modal de Iniciar sesión. Re-autenticando en vivo...")
                                    try:
                                        locator_email.fill("")
                                        locator_email.press_sequentially("referidosjpc@procasa.cl", delay=40)
                                        
                                        locator_pass = page.locator('#login-password').last
                                        locator_pass.fill("")
                                        locator_pass.press_sequentially("Leoncarogalleguillos22305607929a$", delay=40)
                                        
                                        # Usamos el botón de iniciar sesión dentro de forms para evitar clickear otras cosas
                                        page.locator('form button:has-text("Iniciar sesión"), button:has-text("Iniciar sesión")').last.click(force=True)
                                        
                                        print("Credenciales inyectadas exitosamente en el modal. Retomando actividad...")
                                        time.sleep(3)
                                    except Exception as el_err:
                                        print(f"No se pudo completar modal de login: {el_err}")
                            except Exception:
                                pass
                                
                        # Interceptor del dialog 'Cantidad alta de obras'
                        if iteracion % 2 == 0:
                            try:
                                btn_si_continuar = page.locator('button:has-text("Sí, continuar")')
                                if btn_si_continuar.is_visible():
                                    print("\n[!] Dialog 'Cantidad alta de obras' detectado. Presionando 'Sí, continuar'...")
                                    btn_si_continuar.click(force=True)
                                    time.sleep(1)
                            except Exception:
                                pass
                        
                        if page.is_enabled(btn_generar_xpath):
                            habilitado = True
                            sys.stdout.write("\nEl botón ha procesado la info y ya es clickeable.\n")
                            sys.stdout.flush()
                            break
                        sys.stdout.write(f"\rCargando celdas de valores comerciales... ({iteracion}s) esperando botón...  ")
                        sys.stdout.flush()
                        time.sleep(1)
                    
                    if habilitado:
                        page.locator(btn_generar_xpath).click(force=True)
                        print("¡Petición enviada! Recopilando información...")
                        
                        print("Monitoreando estado: 'Consolidando información...'")
                        
                        start_wait = time.time()
                        url_cambiada = False
                        error_post_calculo = False
                        
                        # Esperamos hasta 300 segundos (5 minutos) para reportes muy lentos
                        tiempo_transcurrido = 0
                        while tiempo_transcurrido < 300:
                            if "informe-web" in page.url:
                                url_cambiada = True
                                print("\nTransición completada.")
                                break
                                
                            try:
                                # 1. Evaluar si el banco de repente arroja el error tarde.
                                is_alert_late = page.locator('//*[@id="avisos"]/div/div').is_visible() or page.locator('text="Esta propiedad no se puede valorizar"').first.is_visible()
                                if is_alert_late:
                                    error_post_calculo = True
                                    break
                                    
                                # 2. Interceptor de caducidad de token (en caso de que el modal salte durante Consolidación)
                                if tiempo_transcurrido % 6 == 0:  # Chequear cada 6 segundos
                                    locator_email = page.locator('#login-email').last
                                    if locator_email.is_visible():
                                        print("\n[!] Modal de Iniciar sesión detectado bajo 'Consolidando'. Re-autenticando en vivo...")
                                        try:
                                            locator_email.fill("")
                                            locator_email.press_sequentially("referidosjpc@procasa.cl", delay=40)
                                            locator_pass = page.locator('#login-password').last
                                            locator_pass.fill("")
                                            locator_pass.press_sequentially("Leoncarogalleguillos22305607929a$", delay=40)
                                            page.locator('form button:has-text("Iniciar sesión"), button:has-text("Iniciar sesión")').last.click(force=True)
                                            print("Credenciales pasadas. Dejando que el modal desaparezca...")
                                            time.sleep(4)
                                            
                                            # La SPA muchas veces cancela la generación en progreso al tirar el popup.
                                            # Verificamos si nos botó de vuelta a la pantalla donde tenemos que re-clickear el botón.
                                            if page.is_visible(btn_generar_xpath) and page.is_enabled(btn_generar_xpath):
                                                print("Re-presionando el botón 'Generar informe' ya que el sistema interrumpió el envío original...")
                                                page.locator(btn_generar_xpath).click(force=True)
                                                tiempo_transcurrido = 0  # Reiniciar el reloj de timeout de 5 minutos
                                                print("Nuevo intento en camino. Monitoreando estado de nuevo...")
                                        except Exception as rx:
                                            print(f"Falla al re-intentar sesión: {rx}")
                                
                                # 3. Imprimir el estado
                                text_1 = page.locator('text="Consolidando información"').first.is_visible()
                                text_2 = page.locator('text="Estamos analizando el mercado"').first.is_visible()
                                
                                if text_1 or text_2:
                                    sys.stdout.write("\rConsolidando reporte... esto demora, por favor espere pacientemente.  ")
                                    sys.stdout.flush()
                                else:
                                    sys.stdout.write("\rProcesando petición de reporte en el servidor...                      ")
                                    sys.stdout.flush()
                            except Exception:
                                pass
                            
                            time.sleep(2)
                            tiempo_transcurrido += 2
                            
                        if error_post_calculo:
                            print("\nAVISO SISTEMA: Propiedad sin tasación automática (detectado post-calcular). Saltando.")
                            tasaciones.insert_one({
                                "codigo_propiedad": codigo,
                                "status": "no_tasable",
                                "mensaje": "Esta propiedad no se puede valorizar (detectado al instante de calcular)",
                                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                            })
                            page.goto("https://www.propiteq.com/servicios/tasacion-online/busqueda", wait_until="commit")
                            raise ValueError("SKIP_PROPERTY_NO_TASABLE")
                            
                        if not url_cambiada:
                            print("\nAdvertencia: El tiempo de espera (5 mins) expiró, forzando verificación de URL...")
                            page.wait_for_url("**/informe-web*", timeout=30000)

                        print("\nPágina de informe cargada correctamente.")
                        time.sleep(4) # Esperar a que se renderice todo
                        
                        # 1. Guardar contenido HTML
                        html_dir = r"C:\Users\pgall\Desktop\Tasaciones\HTML"
                        os.makedirs(html_dir, exist_ok=True)
                        html_path = os.path.join(html_dir, f"{codigo}.html")
                        with open(html_path, "w", encoding="utf-8") as f:
                            f.write(page.content())
                        print(f"HTML guardado con éxito en: {html_path}")
                        
                        # 2. Descargar el PDF
                        print("Iniciando descarga del PDF...")
                        btn_descargar_xpath = '//button[contains(., "Descargar el Informe")]'
                        try:
                            page.wait_for_selector(btn_descargar_xpath, timeout=10000)
                            with page.expect_download(timeout=60000) as download_info:
                                page.locator(btn_descargar_xpath).click(force=True)
                            
                            download = download_info.value
                            pdf_dir = r"C:\Users\pgall\Desktop\Tasaciones"
                            os.makedirs(pdf_dir, exist_ok=True)
                            pdf_path = os.path.join(pdf_dir, f"{codigo}.pdf")
                            download.save_as(pdf_path)
                            print(f"PDF descargado y guardado en: {pdf_path}")
                        except Exception as e_pdf:
                            print(f"Error al descargar o guardar el PDF: {e_pdf}")
                        
                        # 3. Analizar informe con IA
                        texto_resumen = ""
                        texto_analisis_ia = ""
                        
                        # XPaths clave según estructura actual de Propiteq:
                        XPATH_BTN_ANALIZAR    = '//*[@id="__nuxt"]/div/div/button'                                  # Botón "Analiza este informe"
                        XPATH_SPINNER         = '//*[@id="__nuxt"]/div/div/dialog/div/div/div/div/div/span'          # Texto "Ya casi está listo..."
                        XPATH_RESUMEN_H4      = '//*[@id="__nuxt"]/div/div/dialog/div/div/div/div/div[1]/div[1]/h4' # "Resumen Ejecutivo" (confirma que listo)
                        XPATH_RESUMEN_TEXT    = '//*[@id="__nuxt"]/div/div/dialog/div/div/div/div/div[1]/div[2]'    # Texto del resumen
                        XPATH_BTN_COMPLETO    = '//*[@id="__nuxt"]/div/div/dialog/div/div/div/div/div[2]/button'    # Botón "Ver análisis completo"
                        XPATH_ANALISIS_H4     = '//*[@id="__nuxt"]/div/div/dialog/div/div/div/div/div[2]/div[1]/h4' # "Análisis Completo" (confirma expansión)
                        XPATH_ANALISIS_TEXT   = '//*[@id="__nuxt"]/div/div/dialog/div/div/div/div/div[2]/div[2]'   # Texto del análisis completo
                        
                        print("Presionando Botón 'Analiza este informe'...")
                        try:
                            # El botón puede tardar en aparecer tras cargar la página de informe
                            page.wait_for_selector(XPATH_BTN_ANALIZAR, timeout=10000)
                            page.locator(XPATH_BTN_ANALIZAR).click(force=True)
                            print("Botón 'Analiza este informe' presionado. Esperando que la IA termine...")
                            
                            # --- ESPERAR QUE LA IA TERMINE ---
                            # Criterio de listo: el h4 de Resumen Ejecutivo ya dice "Resumen Ejecutivo" (no spinner)
                            ia_lista = False
                            for seg_ia in range(120):
                                try:
                                    # Si el spinner sigue visible, seguimos esperando
                                    spinner_visible = page.locator(XPATH_SPINNER).is_visible()
                                    # Si el h4 ya dice "Resumen Ejecutivo", la IA terminó
                                    h4_texto = page.locator(XPATH_RESUMEN_H4).inner_text(timeout=500)
                                    if "Resumen Ejecutivo" in h4_texto and not spinner_visible:
                                        ia_lista = True
                                        sys.stdout.write(f"\nIA lista tras {seg_ia}s. Extrayendo resumen...\n")
                                        sys.stdout.flush()
                                        break
                                except Exception:
                                    pass
                                sys.stdout.write(f"\rIA generando análisis... ({seg_ia}s) esperando 'Resumen Ejecutivo'...  ")
                                sys.stdout.flush()
                                time.sleep(1)
                            
                            if not ia_lista:
                                print("\nAviso: IA no confirmó 'Resumen Ejecutivo' tras 2 minutos. Intentando extracción de igual forma...")
                            
                            # --- PASO 1: Extraer RESUMEN EJECUTIVO ---
                            try:
                                texto_resumen = page.locator(XPATH_RESUMEN_TEXT).inner_text(timeout=5000).strip()
                                print(f"Resumen ejecutivo capturado: {len(texto_resumen)} chars.")
                            except Exception as e_res:
                                # Fallback: buscar por texto del h4 hermano
                                print(f"XPath exacto de resumen falló ({e_res}). Intentando fallback por texto...")
                                try:
                                    texto_resumen = page.locator('h4:has-text("Resumen Ejecutivo") + div, h4:has-text("Resumen Ejecutivo") ~ div').first.inner_text(timeout=3000).strip()
                                except Exception:
                                    texto_resumen = ""
                            
                            # --- PASO 2: Clic en 'Ver análisis completo' y extraer ANÁLISIS IA ---
                            try:
                                page.wait_for_selector(XPATH_BTN_COMPLETO, timeout=6000)
                                page.locator(XPATH_BTN_COMPLETO).scroll_into_view_if_needed()
                                time.sleep(0.5)
                                page.locator(XPATH_BTN_COMPLETO).click(force=True)
                                print("Clic en 'Ver análisis completo'. Esperando expansión...")
                                
                                # Esperar que el h4 de Análisis Completo confirme la expansión
                                analisis_listo = False
                                for seg_a in range(30):
                                    try:
                                        h4_a = page.locator(XPATH_ANALISIS_H4).inner_text(timeout=500)
                                        if "Análisis" in h4_a:
                                            analisis_listo = True
                                            break
                                    except Exception:
                                        pass
                                    time.sleep(0.5)
                                
                                if analisis_listo:
                                    texto_analisis_ia = page.locator(XPATH_ANALISIS_TEXT).inner_text(timeout=5000).strip()
                                    print(f"Análisis IA completo capturado: {len(texto_analisis_ia)} chars.")
                                else:
                                    # Fallback por texto del h4
                                    try:
                                        texto_analisis_ia = page.locator('h4:has-text("Análisis") + div, h4:has-text("Análisis") ~ div').first.inner_text(timeout=3000).strip()
                                    except Exception:
                                        texto_analisis_ia = ""
                            except Exception as e_comp:
                                print(f"Aviso: No se pudo expandir análisis completo: {e_comp}")
                                texto_analisis_ia = ""
                            
                            print("--- Extracción IA Exitosa ---")
                            try:
                                preview = (texto_analisis_ia or texto_resumen)[:120].encode('cp1252', errors='ignore').decode('cp1252')
                                print(f"Preview IA: {preview}...")
                            except Exception:
                                pass
                            
                            # Cerrar modal
                            try:
                                page.locator('dialog button').last.click(timeout=1000, force=True)
                            except Exception:
                                page.keyboard.press("Escape")
                            time.sleep(2)
                            
                        except Exception as e_ia:
                            print(f"Aviso: No se pudo completar análisis IA: {e_ia}")
                            texto_resumen = ""
                            texto_analisis_ia = ""
                        
                        status = "exito_informe_completo"
                        
                        # Insertar documento en Mongo DB (un solo documento por propiedad)
                        tasaciones.insert_one({
                            "codigo_propiedad": codigo,
                            "rol": rol,
                            "comuna": comuna,
                            "detalles_enviados": detalles_enviados,
                            "status": status,
                            "resumen_ejecutivo": texto_resumen,
                            "analisis_ia": texto_analisis_ia,
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                        })
                        print("Registro guardado en MongoDB.")

                    else:
                        print("El botón 'Generar informe' sigue deshabilitado.")
                        status = "error_boton_deshabilitado"
                        tasaciones.insert_one({
                            "codigo_propiedad": codigo,
                            "rol": rol,
                            "comuna": comuna,
                            "detalles_enviados": detalles_enviados,
                            "status": status,
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                        })
                except Exception as b_err:
                    if "SKIP_PROPERTY_NO_TASABLE" in str(b_err):
                        continue
                    print(f"No se pudo interactuar con el botón final o procesar el informe: {b_err}")
                    status = "error_proceso_informe"
                    tasaciones.insert_one({
                        "codigo_propiedad": codigo,
                        "rol": rol,
                        "comuna": comuna,
                        "detalles_enviados": detalles_enviados,
                        "status": status,
                        "error_msg": str(b_err),
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    })

                # Pausa para que el usuario pueda ver el resultado final
                print("Proceso de esta propiedad terminado. Esperando 5 segundos...")
                time.sleep(5)

                # Regresar a la página de búsqueda y limpiar el DOM (modales de Vue retenidos)
                try:
                    page.goto("https://www.propiteq.com/servicios/tasacion-online/busqueda", wait_until="commit")
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception as nav_err:
                    print(f"Aviso navegación de vuelta: {nav_err}")

            except Exception as e:
                err_str = str(e)
                print(f"Error general procesando {codigo}: {err_str[:200]}")
                
                # Guardar el error solo si no existe ya un documento exitoso para este código
                existe_exitoso = tasaciones.find_one({"codigo_propiedad": codigo, "status": "exito_informe_completo"})
                if not existe_exitoso:
                    existe = tasaciones.find_one({"codigo_propiedad": codigo})
                    if existe:
                        tasaciones.update_one(
                            {"codigo_propiedad": codigo},
                            {"$set": {"status": "error", "error_msg": err_str, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}}
                        )
                    else:
                        tasaciones.insert_one({
                            "codigo_propiedad": codigo,
                            "status": "error",
                            "error_msg": err_str,
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                        })
                
                # Si el browser se cerró, relanzarlo completamente para continuar con la próxima propiedad
                if "Target page" in err_str or "browser has been closed" in err_str or "context or browser" in err_str:
                    print("Browser/contexto cerrado. Relanzando navegador para continuar...")
                    try:
                        try:
                            browser.close()
                        except Exception:
                            pass
                        browser = p.chromium.launch(headless=False)
                        context = browser.new_context()
                        page = context.new_page()
                        if stealth_sync:
                            stealth_sync(page)
                        
                        print("Re-iniciando sesión manual tras error...")
                        page.goto("https://www.propiteq.com/login")
                        time.sleep(random.uniform(1.0, 2.5))
                        page.locator('//*[@id="login-email"]').press_sequentially("referidosjpc@procasa.cl", delay=random.randint(50, 150))
                        time.sleep(random.uniform(0.5, 1.5))
                        page.locator('//*[@id="login-password"]').press_sequentially("Leoncarogalleguillos22305607929a$", delay=random.randint(50, 150))
                        time.sleep(random.uniform(0.5, 1.5))
                        page.click('//*[@id="__nuxt"]/div/div[3]/div/form[1]/div[3]/button')
                        page.wait_for_url("**/cliente/dashboard**", timeout=30000)
                        time.sleep(3)
                        
                        page.goto("https://www.propiteq.com/servicios/tasacion-online/busqueda", wait_until="commit", timeout=20000)
                        page.wait_for_load_state("domcontentloaded")
                        time.sleep(3)
                        print("Navegador relanzado exitosamente. Continuando con la siguiente propiedad...")
                        time.sleep(3)
                    except Exception as relaunch_err:
                        print(f"No se pudo relanzar el navegador: {relaunch_err}. Abortando lote.")
                        break

        print("\nEjecución finalizada.")
        browser.close()

if __name__ == "__main__":
    run_automation()
