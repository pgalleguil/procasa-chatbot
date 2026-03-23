import asyncio
import argparse
import logging
import os
import random
import re
import sys
import hashlib
from datetime import datetime, timezone
from itertools import cycle

import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# Import Config from parent
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config

# Lista estática de User-Agents para evitar dependencias externas problemáticas
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36"
]

# --- CONFIGURACIÓN ---
CONTACT_CONFIG = {
    "max_candidates": 100,
    "delay_min": 2,
    "delay_max": 5,
    "page_timeout": 30000,
}

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logging.getLogger("playwright").setLevel(logging.WARNING)

async def get_proxies() -> list:
    if Config.PROXIES:
        return [p.strip() for p in Config.PROXIES.split(",") if p.strip()]
    return []

def get_valid_rut():
    rut_base = random.randint(15000000, 25000000)
    reversed_digits = map(int, reversed(str(rut_base)))
    factors = cycle(range(2, 8))
    s = sum(d * f for d, f in zip(reversed_digits, factors))
    res = 11 - (s % 11)
    dv = '0' if res == 11 else ('K' if res == 10 else str(res))
    return f"{rut_base}-{dv}"

# --- PRE-CHECK LIGERO (SIN PROXY) ---
async def is_ad_alive(url: str) -> bool:
    """Verifica si el anuncio está vivo usando httpx SIN proxy."""
    try:
        await asyncio.sleep(random.uniform(0.8, 1.2))  # Anti-rate-limit
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": random.choice(USER_AGENTS)})
            if resp.status_code != 200:
                return False
            text = resp.text
            if any(x in text for x in ["Anuncio borrado", "ya no está disponible", "Oops", "desactivado", "expirado"]):
                return False
            return True
    except Exception as e:
        logging.debug(f"⚠️ Error en pre-check httpx: {e}")
        return True  # Ante la duda, dejamos que Playwright lo intente

# --- BLOQUEO AGRESIVO DE RECURSOS ---
BLOCKED_TYPES = {"image", "media", "font"}
BLOCKED_DOMAINS = [
    "google-analytics", "googletagmanager", "doubleclick",
    "facebook", "connect.facebook", "hotjar", "criteo",
    "bluekai", "analytics", "adnxs", "amazon-adsystem"
]

async def block_resources(route):
    """Bloquea recursos pesados y trackers para ahorrar proxy."""
    if route.request.resource_type in BLOCKED_TYPES:
        await route.abort()
        return

    url = route.request.url.lower()
    if any(t in url for t in BLOCKED_DOMAINS):
        await route.abort()
        return

    await route.continue_()

# --- EXTRACCIÓN DE CONTACTO (LÓGICA PRESERVADA) ---
def update_local_html(url: str, phone: str):
    """Inyecta el teléfono revelado en el HTML local para auditorías futuras sin proxy."""
    try:
        filename = hashlib.md5(url.encode()).hexdigest() + ".html"
        folder = os.path.join(os.path.dirname(__file__), "html_dumps")
        filepath = os.path.join(folder, filename)
        
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            
            # Tag para inyección
            tag = f'<meta name="revealed-phone" content="{phone}">'
            if tag not in content:
                # Inyectar en el <head> si existe, sino al final
                if "</head>" in content:
                    new_content = content.replace("</head>", f"    {tag}\n</head>", 1)
                else:
                    new_content = content + f"\n<!-- REVEALED_PHONE: {phone} -->"
                
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(new_content)
                logging.info(f"📝 HTML enriquecido con teléfono: {filename}")
        else:
            logging.debug(f"ℹ️ No se encontró HTML local para {filename}")
    except Exception as e:
        logging.error(f"⚠️ Error al actualizar HTML local: {e}")

async def extract_contact(page, listing_url, doc_id, coll, contact_data, worker_id=0):
    # Ya no definimos los listeners aquí para evitar duplicados en reutilización
    contact_data["phone"] = None 
    
    try:
        # --- NAVEGACIÓN OPTIMIZADA: wait_until="commit" + wait_for_selector ---
        await page.goto(listing_url, wait_until="commit", timeout=CONTACT_CONFIG["page_timeout"])
        await page.wait_for_selector('#cnmessage_name', timeout=7000)

        # Verificar anuncio borrado
        content = await page.content()
        if any(x in content for x in ["Anuncio borrado", "desactivado", "expirado", "ya no está disponible", "Oops"]):
            logging.info(f"💀 [W{worker_id}] Anuncio eliminado: {listing_url[-30:]}")
            await coll.update_one({"_id": doc_id}, {"$set": {"status": "eliminado"}})
            return True

        # 0. Aceptar cookies si existe
        try:
            cookie_btn = page.locator('#didomi-notice-agree-button')
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click(timeout=2000)
                await asyncio.sleep(0.5)
        except: pass

        # Esperar a que Yapo inyecte el plugin JS del teléfono (máx 10s)
        try:
            await page.wait_for_selector('input[type="tel"], .iti__tel-input', timeout=10000)
        except: 
            logging.debug(f"⚠️ [W{worker_id}] Timeout esperando plugin de teléfono.")

        # 1. Asegurar país Chile (+56)
        try:
            dropdown = page.locator('.iti__selected-flag, button.iti__selected-country')
            if await dropdown.count() > 0:
                await dropdown.first.click(timeout=3000, force=True)
                chile = page.locator('li[data-country-code="cl"]')
                if await chile.count() > 0:
                    await chile.first.click(timeout=3000, force=True)
                    await asyncio.sleep(0.5)
        except Exception as e: 
            logging.debug(f"⚠️ [W{worker_id}] Error país: {str(e)[:50]}")

        # 2. Llenar formulario (Simulación humana con RUT real)
        fake_email = f"busco.{random.randint(100,999)}@gmail.com"
        fake_name = random.choice(["Juan Perez", "Pedro Garcia", "Carlos Soto", "Inversionista Particular"])
        fake_phone = "9" + "".join([str(random.randint(0,9)) for _ in range(8)])
        fake_rut = get_valid_rut()

        logging.info(f"✍️ [W{worker_id}] Completando formulario...")

        # Selectores exactos según volcado HTML
        form_data = [
            ('#cnmessage_fromemail', fake_email),
            ('#cnmessage_name', fake_name),
            ('#cnmessage_extra_fields_rut', fake_rut),
            ('#cnmessage_phone_countrycode', "56"),
            ('#cnmessage_phone_phonenumber', fake_phone),
            ('input[type="tel"]', fake_phone)
        ]

        phone_filled = False
        for sel, val in form_data:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    try:
                        await el.first.fill(val, timeout=2000, force=True)
                    except:
                        # Fallback por si fill() falla
                        await el.first.click(timeout=2000, force=True)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await page.keyboard.type(val, delay=random.randint(40, 70))
                    
                    await page.keyboard.press("Tab")
                    await asyncio.sleep(0.2)
                    if sel in ['#cnmessage_phone_phonenumber', 'input[type="tel"]']:
                        phone_filled = True
            except Exception: pass

        if not phone_filled:
            logging.warning(f"⚠️ [W{worker_id}] No se pudo llenar teléfono en {listing_url[-20:]}")

        await asyncio.sleep(1)

        # 3. Revelar contacto - SOLO mediante botones explícitos de teléfono/WhatsApp
        # NO se usan fallbacks de HTML ni submit genérico para evitar capturar teléfonos incorrectos
        reveal_source = "none"
        
        # Verificar si existe un botón de revelar teléfono REAL en este anuncio
        has_phone_button = False
        for btn_sel in ['.show-phone', '.show-whatsapp']:
            try:
                if await page.locator(btn_sel).count() > 0:
                    has_phone_button = True
                    break
            except: pass
        
        if not has_phone_button:
            # Este anuncio solo tiene formulario de mensaje - no tiene teléfono público
            logging.info(f"📵 [W{worker_id}] Anuncio sin botón de teléfono (solo formulario): {listing_url[-30:]}")
        else:
            for btn_sel in ['.show-phone', '.show-whatsapp']:
                if contact_data["phone"]: break
                try:
                    btn = page.locator(btn_sel)
                    if await btn.count() > 0:
                        logging.info(f"🖱️ [W{worker_id}] Haciendo click en {btn_sel}")
                        await btn.first.evaluate('el => { el.removeAttribute("disabled"); el.click(); }')
                        
                        # Espera progresiva de hasta 8 segundos (16 x 0.5s)
                        for _ in range(16):
                            if contact_data["phone"]: 
                                reveal_source = "network_intercept"
                                break
                            await asyncio.sleep(0.5)
                        
                        # Fallback: leer texto del botón por si cambió a número visible
                        if not contact_data["phone"]:
                            try:
                                btn_text = await btn.first.inner_text()
                                clean_text = btn_text.replace("+56", "").replace("-", "").replace(" ", "")
                                m = re.search(r'(9\d{8})', clean_text)
                                if m:
                                    digits = m.group(1)
                                    if digits != fake_phone:  # EVITAR AUTO-CAPTURA
                                        contact_data["phone"] = "+56" + digits
                                        reveal_source = "button_text"
                            except: pass
                except: pass
        
        # ELIMINADO: Fallback de búsqueda en HTML completo de la página.
        # Era la causa de captura de teléfonos incorrectos (del formulario de ProCasa,
        # scripts embebidos, datos de sesión, etc.)

        if contact_data["phone"]:
            # Normalizar teléfono
            clean_phone = re.sub(r'[^\d+]', '', contact_data["phone"])
            if not clean_phone.startswith('+'):
                if len(clean_phone) == 9: clean_phone = "+56" + clean_phone
                elif clean_phone.startswith('56'): clean_phone = "+" + clean_phone
            
            # Guardar también el log de dónde salió e inyectar en HTML local
            update_local_html(listing_url, clean_phone)
            await coll.update_one(
                {"_id": doc_id}, 
                {"$set": {
                    "details.whatsapp_phone": clean_phone,
                    "metadata.phone_extraction_source": reveal_source,
                    "metadata.extraction_date": datetime.now(timezone.utc)
                }}
            )
            logging.info(f"✅ [W{worker_id}] Éxito: {clean_phone} (Fuente: {reveal_source}) | {listing_url[-20:]}")
            return True
        
        logging.warning(f"⚠️ [W{worker_id}] No se reveló el teléfono: {listing_url[-20:]}")
        return False

    except asyncio.TimeoutError:
        logging.warning(f"⏱️ [W{worker_id}] Timeout en {listing_url[-20:]}")
        return "timeout"
    except Exception as e:
        if "Timeout" in str(e):
            return "timeout"
        logging.warning(f"❌ [W{worker_id}] Error: {str(e)[:100]} | {listing_url[-20:]}")
        return False

# --- MAIN ---
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=CONTACT_CONFIG["max_candidates"])
    args = parser.parse_args()

    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db['yapo_propiedades']
    
    query = {
        "details.es_propietario_directo": True,
        "details.whatsapp_phone": {"$in": [None, ""]},
        "status": {"$ne": "eliminado"}
    }
    
    total_db_pending = await coll.count_documents(query)
    candidates = await coll.find(query).to_list(length=args.limit)

    if not candidates:
        logging.info("🏁 Sin candidatos para procesar.")
        client.close()
        return

    proxies = await get_proxies()
    proxy_cycle = cycle(proxies) if proxies else None

    logging.info(f"🚀 Extractor Paralelo | {len(candidates)} de {total_db_pending} totales | {args.concurrency} workers")

    queue = asyncio.Queue()
    for c in candidates: await queue.put(c)

    # Contador global para mostrar el total real restante y tráfico
    stats = {
        "total_db_pending": total_db_pending,
        "traffic_mb": 0.0,
        "session_traffic": {} # wid -> current session traffic
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-webrtc",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--no-sandbox"
            ]
        )

        async def worker(wid):
            logging.info(f"[W{wid}] Worker iniciado")
            stats["session_traffic"][wid] = 0.0

            while not queue.empty():
                # --- CONTEXT REUSE: 6-8 anuncios por contexto ---
                ads_per_session = random.randint(6, 8)
                proxy = next(proxy_cycle) if proxy_cycle else None

                ctx_proxy = None
                if proxy:
                    ctx_proxy = {"server": f"http://{proxy}"}
                    if Config.PROXY_USER:
                        ctx_proxy["username"], ctx_proxy["password"] = Config.PROXY_USER, Config.PROXY_PASS

                logging.info(f"[W{wid}] Nueva sesión ({ads_per_session} ads) | Proxy: {proxy}")

                context = await browser.new_context(user_agent=random.choice(USER_AGENTS), proxy=ctx_proxy)
                
                # --- REUTILIZAR PÁGINA POR SESIÓN ---
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)
                await page.route("**/*", block_resources)

                # Monitor de tráfico por worker
                ad_traffic = [0]
                async def count_traffic(response):
                    try:
                        size = int(response.headers.get("content-length", 0))
                        ad_traffic[0] += size
                    except: pass
                processed_in_session = 0

                try:
                    while not queue.empty() and processed_in_session < ads_per_session:
                        cand = await queue.get()

                        # --- PRE-CHECK HTTPX (SIN PROXY) ---
                        is_alive = await is_ad_alive(cand['url'])
                        if not is_alive:
                            logging.info(f"⏭️ [W{wid}] Anuncio muerto (pre-check): {cand['url'][-30:]}")
                            await coll.update_one({"_id": cand["_id"]}, {"$set": {"status": "eliminado"}})
                            stats["total_db_pending"] -= 1
                            queue.task_done()
                            continue

                        # Crear página NUEVA por anuncio (más estable)
                        page = await context.new_page()
                        await Stealth().apply_stealth_async(page)
                        await page.route("**/*", block_resources)

                        # Captura de teléfono por red
                        contact_capture = {"phone": None}
                        async def handle_response(response):
                            if "cnmessage/send" in response.url or "contacts" in response.url:
                                try:
                                    text = await response.text()
                                    # Extraer números descartando el fake_phone usado en este worker
                                    resp_phones = re.findall(r'(\+569\d{8}|9\d{8})', text.replace(" ", "").replace("-", ""))
                                    if resp_phones:
                                        # Normalizar y filtrar auto-captura
                                        candidates = []
                                        for p in resp_phones:
                                            clean_p = p.replace("+56", "")
                                            if clean_p != fake_phone and len(clean_p) == 9:
                                                candidates.append("+56" + clean_p)
                                        
                                        if candidates:
                                            contact_capture["phone"] = candidates[0]
                                except: pass
                        
                        async def handle_popup(popup):
                            try:
                                await popup.wait_for_load_state()
                                m = re.search(r'phone=(\d+)', popup.url)
                                if m:
                                    extracted = m.group(1)
                                    if extracted.endswith(fake_phone):
                                        logging.warning(f"⚠️ [W{wid}] Detectada auto-captura en popup WhatsApp. Ignorando.")
                                    else:
                                        contact_capture["phone"] = "+" + extracted
                            except: pass

                        page.on("response", handle_response)
                        page.on("popup", handle_popup)

                        # Monitor de tráfico
                        ad_traffic = [0]
                        async def count_traffic(response):
                            try:
                                size = int(response.headers.get("content-length", 0))
                                ad_traffic[0] += size
                            except: pass
                        page.on("response", count_traffic)

                        logging.info(f"🌐 [W{wid}] Procesando (Q {queue.qsize()} | DB {stats['total_db_pending']}): {cand['url'][-30:]}")

                        try:
                            result = await extract_contact(page, cand["url"], cand["_id"], coll, contact_capture, worker_id=wid)
                            
                            ad_mb = ad_traffic[0] / (1024 * 1024)
                            stats["traffic_mb"] += ad_mb
                            logging.info(f"📊 [W{wid}] Tráfico ad: {ad_mb:.2f}MB | Total: {stats['traffic_mb']:.1f}MB")

                            if result == True:
                                stats["total_db_pending"] -= 1
                            elif result == "timeout":
                                logging.warning(f"🔄 [W{wid}] Re-encolando por timeout: {cand['url'][-20:]}")
                                await queue.put(cand)
                        except Exception as e:
                            logging.error(f"❌ [W{wid}] Error en worker: {e}")
                        finally:
                            await page.close()
                            queue.task_done()
                            processed_in_session += 1
                            await asyncio.sleep(random.uniform(1, 3))

                finally:
                    await context.close()
                    logging.info(f"[W{wid}] Sesión cerrada ({processed_in_session} ads procesados)")

            logging.info(f"[W{wid}] Worker finalizado")

        workers = [asyncio.create_task(worker(i)) for i in range(args.concurrency)]
        await asyncio.wait(workers)
        await browser.close()

    logging.info("🎉 Proceso finalizado.")
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
