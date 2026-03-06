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
    "max_candidates": 500,
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

async def block_resources(route):
    """Bloquea recursos pesados y trackers para ahorrar proxy."""
    if route.request.resource_type in ["image", "media", "font"]:
        await route.abort()
    elif any(x in route.request.url for x in ["google-analytics", "doubleclick", "facebook", "hotjar"]):
        await route.abort()
    else:
        await route.continue_()

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
        await page.goto(listing_url, wait_until="domcontentloaded", timeout=CONTACT_CONFIG["page_timeout"])
        await asyncio.sleep(3)
        
        # Verificar anuncio borrado (Ampliamos detección)
        content = await page.content()
        if any(x in content for x in ["Anuncio borrado", "desactivado", "expirado", "ya no está disponible", "Oops"]):
            logging.warning(f"💀 [W{worker_id}] Anuncio no disponible: {listing_url[-30:]}")
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
                    logging.info(f"📍 [W{worker_id}] País Chile seleccionado.")
                    await asyncio.sleep(0.5)
        except Exception as e: 
            logging.debug(f"⚠️ [W{worker_id}] Error país: {str(e)[:50]}")

        # 2. Llenar formulario (Simulación humana con RUT real)
        fake_email = f"busco.{random.randint(100,999)}@gmail.com"
        fake_name = random.choice(["Juan Perez", "Pedro Garcia", "Carlos Soto", "Inversionista Particular"])
        fake_phone = "9" + "".join([str(random.randint(0,9)) for _ in range(8)])
        fake_rut = get_valid_rut()

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
                        # Fallback por si fill() falla (ej. input no es modificable directamente)
                        await el.first.click(timeout=2000, force=True)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await page.keyboard.type(val, delay=random.randint(40, 70))
                    
                    await page.keyboard.press("Tab")
                    await asyncio.sleep(0.3)
                    logging.info(f"✍️ [W{worker_id}] Llenado: {sel}")
                    if sel in ['#cnmessage_phone_phonenumber', 'input[type="tel"]']:
                        phone_filled = True
                        break # Ya llenamos el teléfono
                else:
                    logging.warning(f"⚠️ [W{worker_id}] Selector no encontrado: {sel}")
            except Exception as e: 
                logging.warning(f"⚠️ [W{worker_id}] Fallo clic en {sel}: {str(e)[:50]}")

        if not phone_filled:
            logging.warning(f"⚠️ [W{worker_id}] No se pudo llenar el campo de teléfono en {listing_url[-20:]}")
            try:
                phone_html = await page.locator('.d3-property-contact__form').inner_html()
                logging.warning(f"HTML Formulario: {phone_html[:500]}...")
            except: pass

        await asyncio.sleep(1)

        # 3. Revelar contacto forzando submit para ver errores
        for btn_sel in ['.show-phone', '.show-whatsapp', 'button[type="submit"]']:
            if contact_data["phone"]: break
            try:
                btn = page.locator(btn_sel)
                if await btn.count() > 0:
                    await btn.first.evaluate('el => { el.removeAttribute("disabled"); el.click(); }')
                    await asyncio.sleep(2)
                    
                    # Guardar screenshot DESPUÉS de hacer click para ver validaciones (rojo)
                    try:
                        await page.screenshot(path=f"debug_worker_{worker_id}.png", timeout=3000)
                    except: pass
                    
                    await asyncio.sleep(3)
                    
                    # Respaldo: Leer texto del botón por si cambió a número
                    btn_text = await btn.first.inner_text()
                    m = re.search(r'(\+?56\s?9\s?\d{4}\s?\d{4}|\+?56\d{9}|9\d{8})', btn_text.replace(" ", ""))
                    if m: contact_data["phone"] = m.group(1)
            except: pass

        if contact_data["phone"]:
            # Normalizar teléfono (quitar caracteres no numéricos excepto +)
            clean_phone = re.sub(r'[^\d+]', '', contact_data["phone"])
            if not clean_phone.startswith('+') and len(clean_phone) == 9:
                clean_phone = "+56" + clean_phone
            
            await coll.update_one({"_id": doc_id}, {"$set": {"details.whatsapp_phone": clean_phone}})
            logging.info(f"✅ [W{worker_id}] Éxito: {clean_phone} | {listing_url[-20:]}")
            return True
        
        logging.warning(f"⚠️ [W{worker_id}] No se reveló el teléfono: {listing_url[-20:]}")
        return False

    except Exception as e:
        logging.warning(f"❌ [W{worker_id}] Error: {str(e)[:100]} | {listing_url[-20:]}")
        return False

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=CONTACT_CONFIG["max_candidates"])
    args = parser.parse_args()

    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db['yapo_propiedades']
    
    candidates = await coll.find({
        "details.es_propietario_directo": True,
        "details.whatsapp_phone": {"$in": [None, ""]},
        "status": {"$ne": "eliminado"}
    }).to_list(length=args.limit)

    if not candidates:
        logging.info("🏁 Sin candidatos para procesar.")
        client.close()
        return

    proxies = await get_proxies()
    proxy_cycle = cycle(proxies) if proxies else None

    logging.info(f"🚀 Extractor Paralelo | {len(candidates)} candidatos | {args.concurrency} workers")

    queue = asyncio.Queue()
    for c in candidates: await queue.put(c)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def worker(wid):
            while not queue.empty():
                cand = await queue.get()
                proxy = next(proxy_cycle) if proxy_cycle else None
                
                ctx_proxy = None
                if proxy:
                    ctx_proxy = {"server": f"http://{proxy}"}
                    if Config.PROXY_USER:
                        ctx_proxy["username"], ctx_proxy["password"] = Config.PROXY_USER, Config.PROXY_PASS
                
                logging.info(f"🌐 [W{wid}] Intentando: {cand['url'][-30:]} (Proxy: {proxy})")

                context = await browser.new_context(user_agent=random.choice(USER_AGENTS), proxy=ctx_proxy)
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)
                await page.route("**/*", block_resources)

                try:
                    await extract_contact(page, cand["url"], cand["_id"], coll, worker_id=wid)
                finally:
                    await context.close()
                    queue.task_done()
                    await asyncio.sleep(random.uniform(1, 4))

        workers = [asyncio.create_task(worker(i)) for i in range(args.concurrency)]
        await asyncio.wait(workers)
        await browser.close()

    logging.info("🎉 Proceso finalizado.")
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
