import asyncio
import argparse
import logging
import os
import random
import re
import sys
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
async def extract_contact(page, listing_url, doc_id, coll, worker_id=0):
    contact_data = {"phone": None}

    async def handle_response(response):
        # Interceptamos la respuesta que contiene el teléfono
        if "cnmessage/send" in response.url or "contacts" in response.url:
            try:
                text = await response.text()
                # Buscamos patrones de teléfono móvil Chile (+569 o 9) con 9 dígitos totales
                phones = re.findall(r'(\+569\d{8}|9\d{8})', text.replace(" ", "").replace("-", ""))
                if phones:
                    # Filtrar posibles IDs de anuncio (comúnmente 8 dígitos o empiezan distinto)
                    valid_phones = [p for p in phones if len(p.replace("+56", "")) == 9]
                    if valid_phones:
                        # Priorizar el que tiene +56
                        with_prefix = [p for p in valid_phones if p.startswith("+56")]
                        contact_data["phone"] = with_prefix[0] if with_prefix else valid_phones[0]
                        logging.debug(f"🎯 Capturado por red: {contact_data['phone']}")
            except: pass

    page.on("response", handle_response)

    # Capturar popups (WhatsApp abre en nueva pestaña)
    async def handle_popup(popup):
        await popup.wait_for_load_state()
        p_url = popup.url
        m = re.search(r'phone=(\d+)', p_url)
        if m: contact_data["phone"] = "+" + m.group(1)
    page.on("popup", handle_popup)

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

        # 3. Revelar contacto forzando submit
        for btn_sel in ['.show-phone', '.show-whatsapp', 'button[type="submit"]']:
            if contact_data["phone"]: break
            try:
                btn = page.locator(btn_sel)
                if await btn.count() > 0:
                    await btn.first.evaluate('el => { el.removeAttribute("disabled"); el.click(); }')
                    
                    # Espera progresiva de hasta 8 segundos (16 x 0.5s)
                    for _ in range(16):
                        if contact_data["phone"]: break
                        await asyncio.sleep(0.5)
                    
                    # Fallback 1: leer texto del botón por si cambió a número
                    if not contact_data["phone"]:
                        try:
                            btn_text = await btn.first.inner_text()
                            m = re.search(r'(9\s?\d{4}\s?\d{4})', btn_text.replace("+56", "").replace("-", ""))
                            if m:
                                digits = re.sub(r'\s', '', m.group(1))
                                contact_data["phone"] = "+56" + digits
                        except: pass

                    # Fallback 2: buscar en el HTML completo de la página
                    if not contact_data["phone"]:
                        try:
                            html = await page.content()
                            m = re.search(r'(9\s?\d{4}\s?\d{4})', html)
                            if m:
                                digits = re.sub(r'\s', '', m.group(1))
                                contact_data["phone"] = "+56" + digits
                        except: pass

                    # Debug screenshot solo si falló todo
                    if not contact_data["phone"]:
                        try:
                            await page.screenshot(path=f"debug_worker_{worker_id}.png", timeout=3000)
                        except: pass
            except: pass

        if contact_data["phone"]:
            # Normalizar teléfono
            clean_phone = re.sub(r'[^\d+]', '', contact_data["phone"])
            if not clean_phone.startswith('+'):
                if len(clean_phone) == 9: clean_phone = "+56" + clean_phone
                elif clean_phone.startswith('56'): clean_phone = "+" + clean_phone
            
            await coll.update_one({"_id": doc_id}, {"$set": {"details.whatsapp_phone": clean_phone}})
            logging.info(f"✅ [W{worker_id}] Éxito: {clean_phone} | {listing_url[-20:]}")
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

    # Contador global para mostrar el total real restante en base de datos
    stats = {"total_db_pending": total_db_pending}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def worker(wid):
            logging.info(f"[W{wid}] Worker iniciado")

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

                        logging.info(f"🌐 [W{wid}] Procesando (Q {queue.qsize()} | DB {stats['total_db_pending']}): {cand['url'][-30:]}")

                        # Crear página dentro del contexto reutilizado
                        page = await context.new_page()
                        await Stealth().apply_stealth_async(page)
                        await page.route("**/*", block_resources)

                        try:
                            result = await extract_contact(page, cand["url"], cand["_id"], coll, worker_id=wid)
                            if result == True: # Éxito o Eliminado (ya actualizado en BD)
                                stats["total_db_pending"] -= 1
                            elif result == "timeout":
                                logging.warning(f"🔄 [W{wid}] Re-encolando por timeout: {cand['url'][-20:]}")
                                await queue.put(cand)
                            # Si es False, se queda en DB como pendiente, no decrementamos
                        except Exception as e:
                            logging.error(f"❌ [W{wid}] Error en worker: {e}")
                        finally:
                            await page.close()  # Solo cerrar la página, no el contexto
                            queue.task_done()
                            processed_in_session += 1
                            await asyncio.sleep(random.uniform(1, 3))

                finally:
                    await context.close()  # Cerrar contexto al final de la sesión
                    logging.info(f"[W{wid}] Sesión cerrada ({processed_in_session} ads procesados)")

            logging.info(f"[W{wid}] Worker finalizado")

        workers = [asyncio.create_task(worker(i)) for i in range(args.concurrency)]
        await asyncio.wait(workers)
        await browser.close()

    logging.info("🎉 Proceso finalizado.")
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
