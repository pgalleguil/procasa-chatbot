"""
auditoria_publicaciones_prop360.py
==================================
Auditoría completa de publicaciones para TODA la cartera activa de PROCASA SUCRE.

Diferencia con scraping_prop360_portales.py:
  - Procesa TODAS las propiedades disponibles (sin filtro por codigo_internacional).
  - Escribe el campo `auditoria_publicaciones` (NUNCA toca `publicaciones`).
  - Genera reporte resumido por portal al finalizar.

Portales detectados:
  - portal_inmobiliario  → portalinmobiliario.com
  - mercado_libre        → mercadolibre.cl
  - toctoc               → toctoc.com
  - yapo                 → yapo.cl
  - procasa              → procasa.cl  (con /propiedades/ o /detalle/ o código en URL)

Uso:
    python auditoria_publicaciones_prop360.py
"""

import asyncio
import os
import sys
import re
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Verificación de dependencias (misma lógica que el script original)
# ---------------------------------------------------------------------------
missing_deps = []

try:
    from playwright.async_api import async_playwright
except ModuleNotFoundError:
    missing_deps.append("playwright")

try:
    from playwright_stealth import Stealth
except ModuleNotFoundError:
    missing_deps.append("playwright-stealth")

try:
    # pyrefly: ignore [missing-import]
    from pymongo import MongoClient
except ModuleNotFoundError:
    missing_deps.append("pymongo")

from tqdm import tqdm

if missing_deps:
    print(
        "Error: faltan dependencias en este entorno: "
        + ", ".join(missing_deps)
        + ". Instala con: python -m pip install "
        + " ".join(missing_deps)
    )
    if "playwright" in missing_deps:
        print("Luego ejecuta: python -m playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Import Config (misma lógica que el script original)
# ---------------------------------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(__file__), "ChatBot_v4_Grok"))
try:
    from config import Config
except ImportError:
    print(
        "Error: No se pudo importar Config. "
        "Asegurate de que el script este en la raiz de c:\\Users\\pgall\\Desktop\\Python"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# CONFIGURACIÓN (reutilizada íntegramente del script original)
# ---------------------------------------------------------------------------
LOGIN_URL = "https://procasa.prop360.cl/"
PROPERTIES_URL = "https://procasa.prop360.cl/backOffice/propiedades/propiedades"
USERNAME = "pgalleguillos@procasa.cl"
PASSWORD = "Procasa.2026"
HEADLESS = True

# Portales a auditar: clave interna → fragmento de dominio usado para detección
PORTALES = {
    "portal_inmobiliario": "portalinmobiliario.cl",
    "mercado_libre": "mercadolibre.cl",
    "toctoc": "toctoc.com",
    "yapo": "yapo.cl",
    "procasa": "procasa.cl",
}


# ---------------------------------------------------------------------------
# CONEXIÓN MONGODB
# ---------------------------------------------------------------------------
def get_read_collection():
    """Retorna universo_cartera — SOLO LECTURA."""
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    return db["universo_cartera"]


def get_audit_collection():
    """Retorna auditoria_publicaciones — colección de escritura exclusiva."""
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    return db["auditoria_publicaciones"]


# ---------------------------------------------------------------------------
# CONSULTA: TODA la cartera activa de PROCASA SUCRE
# ---------------------------------------------------------------------------
async def get_properties_to_audit():
    """
    Lee TODA la cartera activa de PROCASA SUCRE desde universo_cartera.
    Solo lectura — no escribe ni modifica ningún documento.
    """
    coll = get_read_collection()
    query = {
        "disponible": True,
        "oficina": "PROCASA SUCRE",
    }
    projection = {"codigo": 1}
    cursor = list(coll.find(query, projection))
    return cursor


# ---------------------------------------------------------------------------
# NAVEGACIÓN: funciones reutilizadas del script original sin modificación
# ---------------------------------------------------------------------------
async def _go_to_next_results_page(page):
    """Intenta avanzar una página en el paginador inferior."""
    next_selectors = [
        "xpath=//*[@id='paginadorInferior']/div[2]/div/ul/li[8]/a",
        "xpath=//*[@id='paginadorInferior']/div[2]/div/ul/li[last()]/a",
        "#paginadorInferior a[aria-label*='Siguiente']",
        "#paginadorInferior a[title*='Siguiente']",
    ]

    for selector in next_selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() == 0:
                continue
            link = locator.first
            class_name = (await link.get_attribute("class")) or ""
            aria_disabled = (await link.get_attribute("aria-disabled")) or ""
            if "disabled" in class_name.lower() or aria_disabled.lower() == "true":
                continue
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1.5)
            return True
        except Exception:
            continue

    return False


async def _find_property_row_on_current_page(page, code_str, stats=None):
    """Busca una fila que coincida exactamente con el código en la página actual."""
    rows = page.locator("tr.rowProp, tr[id*='filaProp']")
    count = await rows.count()
    if stats is not None and "filas_encontradas" in stats:
        stats["filas_encontradas"] += count

    for i in range(count):
        row = rows.nth(i)
        row_id = await row.get_attribute("id") or ""

        try:
            td2 = row.locator("td:nth-child(2)")
            td2_text = await td2.inner_text() if await td2.count() > 0 else ""
            row_code = td2_text.replace(".", "").strip()
        except Exception:
            row_code = ""

        if not row_code:
            m = re.search(r"filaProp(\d+)", row_id)
            row_code = m.group(1) if m else ""

        if row_code == code_str:
            if stats is not None and "coincidencias_exactas" in stats:
                stats["coincidencias_exactas"] += 1
            ficha_candidate = row.locator(
                "a[href*='propiedades/show'], a[title='Ficha'], a:has-text('Ficha')"
            )
            if await ficha_candidate.count() > 0:
                return ficha_candidate.first, row_id

    return None, None


# ---------------------------------------------------------------------------
# EXTRACCIÓN: nueva función que produce auditoria_publicaciones
# ---------------------------------------------------------------------------
async def audit_portal_info(page, code, stats=None):
    """
    Busca una propiedad por código y construye el objeto auditoria_publicaciones.

    Retorna un dict con la estructura:
    {
        "ultima_revision": "ISO8601",
        "portal_inmobiliario": {"publicada": bool, "url": str|None},
        "mercado_libre":       {"publicada": bool, "url": str|None},
        "toctoc":              {"publicada": bool, "url": str|None},
        "yapo":                {"publicada": bool, "url": str|None},
        "procasa":             {"publicada": bool, "url": str|None},
    }
    o None si no fue posible navegar a la ficha.
    """
    if stats is None:
        stats = {}

    # 1. Buscar el código en el listado (lógica idéntica al script original)
    try:
        await page.wait_for_selector("#tbListingSearch", timeout=10000)
        await page.fill("#tbListingSearch", "")
        await asyncio.sleep(0.5)
        await page.type("#tbListingSearch", str(code), delay=100)
        await page.keyboard.press("Enter")

        code_str = str(code)
        link_ficha = None
        row_id = None

        for _ in range(12):
            await asyncio.sleep(1.5)
            link_ficha, row_id = await _find_property_row_on_current_page(
                page, code_str, stats
            )
            if link_ficha:
                break
            if not await _go_to_next_results_page(page):
                break

        if not link_ficha:
            tqdm.write(
                f" [WARNING] No se encontró coincidencia exacta para código {code}"
            )
            return None

        await link_ficha.click()
        tqdm.write(f" [OK] Match exacto para código {code} (fila: {row_id})")

    except Exception as e:
        tqdm.write(f" [ERROR] Fallo al buscar código {code}: {e}")
        return None

    # 2. Entrar a la pestaña Portales (lógica idéntica al script original)
    try:
        await page.wait_for_selector("#a-propPortales", timeout=15000)
        await page.click("#a-propPortales")
        await page.wait_for_selector(".portlet-body", timeout=10000)
        await asyncio.sleep(2)
    except Exception as e:
        tqdm.write(f" [ERROR] No se pudo abrir pestaña Portales para código {code}: {e}")
        return None

    # 3. Inicializar el resultado con todos los portales en False/null
    #    (REQUISITO: aunque no haya URL, debe quedar registrado explícitamente)
    resultado = {
        "ultima_revision": datetime.now(timezone.utc).isoformat(),
        "portal_inmobiliario": {"publicada": False, "url": None},
        "mercado_libre":       {"publicada": False, "url": None},
        "toctoc":              {"publicada": False, "url": None},
        "yapo":                {"publicada": False, "url": None},
        "procasa":             {"publicada": False, "url": None},
    }

    # 4. Extraer enlaces de portales
    #    Lógica de detección reutilizada del script original + fix bug mercadolibre
    try:
        all_links = page.locator("a[href]")
        for i in range(await all_links.count()):
            link = all_links.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue

            href_lower = href.lower().strip()

            # Descartar mailto, javascript y direcciones de correo
            if "mailto:" in href_lower or "javascript:" in href_lower or "@" in href_lower:
                continue

            # --- Procasa ---
            if "procasa.cl" in href_lower:
                if (
                    "/propiedades/" in href_lower
                    or "/detalle/" in href_lower
                    or str(code) in href_lower
                ):
                    resultado["procasa"]["publicada"] = True
                    resultado["procasa"]["url"] = href

            # --- Portal Inmobiliario ---
            elif "portalinmobiliario.cl" in href_lower or "portalinmobiliario.com" in href_lower:
                resultado["portal_inmobiliario"]["publicada"] = True
                resultado["portal_inmobiliario"]["url"] = href

            # --- Mercado Libre ---
            # NOTA: el script original tenía un bug aquí (guardaba bajo
            # "portal_inmobiliario" en vez de "mercado_libre"). Corregido.
            elif "mercadolibre.cl" in href_lower:
                resultado["mercado_libre"]["publicada"] = True
                resultado["mercado_libre"]["url"] = href

            # --- Toctoc ---
            # Cubre toctoc.com y toctoc.cl (ambos dominios en uso)
            elif "toctoc.com" in href_lower or "toctoc.cl" in href_lower:
                resultado["toctoc"]["publicada"] = True
                resultado["toctoc"]["url"] = href

            # --- Yapo ---
            elif "yapo.cl" in href_lower:
                resultado["yapo"]["publicada"] = True
                resultado["yapo"]["url"] = href

    except Exception as e:
        tqdm.write(f" [WARNING] Error al extraer links para código {code}: {e}")

    # 5. Volver al listado para la siguiente búsqueda (idéntico al script original)
    await page.goto(PROPERTIES_URL)

    return resultado


# ---------------------------------------------------------------------------
# RUNNER PRINCIPAL
# ---------------------------------------------------------------------------
async def run_audit():
    properties = await get_properties_to_audit()
    audit_coll = get_audit_collection()
    total = len(properties)

    print("=" * 60)
    print("   AUDITORÍA PUBLICACIONES PROP360 - PROCASA SUCRE")
    print("=" * 60)
    print(f"Propiedades activas a auditar: {total}")
    if total == 0:
        print("No hay propiedades activas. Finalizando.")
        return

    # Contadores para el reporte final
    stats = {
        "filas_encontradas": 0,
        "coincidencias_exactas": 0,
        "descartadas_doble_validacion": 0,
        "errores": 0,
        "revisadas": 0,
        "codigos_error": [],          # lista exacta de codigos que fallaron
        # contadores por portal: publicadas / no publicadas
        "portal_inmobiliario": {"publicadas": 0, "no_publicadas": 0},
        "mercado_libre":       {"publicadas": 0, "no_publicadas": 0},
        "toctoc":              {"publicadas": 0, "no_publicadas": 0},
        "yapo":                {"publicadas": 0, "no_publicadas": 0},
        "procasa":             {"publicadas": 0, "no_publicadas": 0},
    }

    PORTALES_KEYS = ["portal_inmobiliario", "mercado_libre", "toctoc", "yapo", "procasa"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()

        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        # --- Login (idéntico al script original) ---
        print("\nIniciando sesión en Prop360...")
        await page.goto(LOGIN_URL)
        await page.fill("#tbMail", USERNAME)
        await page.fill("#tbPassword", PASSWORD)
        await page.keyboard.press("Enter")

        try:
            await page.wait_for_url(
                re.compile(r".*/backoffice/.*", re.IGNORECASE), timeout=15000
            )
            print("Sesión iniciada correctamente.")
        except Exception:
            print("Intentando clic manual en botón de ingreso...")
            await page.click("#btnIngresar, .btn-login, button:has-text('Ingresar')")
            await page.wait_for_url(
                re.compile(r".*/backoffice/.*", re.IGNORECASE), timeout=20000
            )

        await page.goto(PROPERTIES_URL)

        # --- Iteración sobre propiedades ---
        for prop_summary in tqdm(properties, desc="Auditoría", unit="prop"):
            code = prop_summary["codigo"]
            try:
                auditoria = await audit_portal_info(page, code, stats)

                if auditoria:
                    stats["revisadas"] += 1

                    # Actualizar contadores por portal
                    for portal_key in PORTALES_KEYS:
                        if auditoria.get(portal_key, {}).get("publicada"):
                            stats[portal_key]["publicadas"] += 1
                        else:
                            stats[portal_key]["no_publicadas"] += 1

                    # Escribir en colección separada auditoria_publicaciones.
                    # universo_cartera NO es tocada en ningún punto del script.
                    audit_coll.update_one(
                        {"codigo": code},
                        {
                            "$set": {
                                "codigo": code,
                                **auditoria,
                            }
                        },
                        upsert=True,
                    )
                else:
                    stats["errores"] += 1
                    stats["codigos_error"].append(code)

            except Exception as e:
                tqdm.write(f"Error crítico en código {code}: {e}")
                stats["errores"] += 1
                stats["codigos_error"].append(code)

        await browser.close()

    # -----------------------------------------------------------------------
    # REPORTE FINAL
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("   REPORTE FINAL - AUDITORÍA PUBLICACIONES PROP360")
    print("=" * 60)
    print(f"  Total propiedades en cartera activa : {total}")
    print(f"  Total revisadas con éxito           : {stats['revisadas']}")
    print(f"  Total errores / no encontradas      : {stats['errores']}")
    print()
    print(f"  {'Portal':<28} {'Publicadas':>10} {'No Publicadas':>14}")
    print(f"  {'-'*28} {'-'*10} {'-'*14}")
    for portal_key in PORTALES_KEYS:
        label = portal_key.replace("_", " ").title()
        pub   = stats[portal_key]["publicadas"]
        no_pub = stats[portal_key]["no_publicadas"]
        print(f"  {label:<28} {pub:>10} {no_pub:>14}")

    print()
    print(f"  Filas encontradas en tabla          : {stats['filas_encontradas']}")
    print(f"  Coincidencias exactas de código     : {stats['coincidencias_exactas']}")
    if stats["codigos_error"]:
        print()
        print(f"  Códigos con error / no encontrados  : {len(stats['codigos_error'])}")
        print(f"  {', '.join(str(c) for c in stats['codigos_error'])}")
    print("=" * 60)

    # -----------------------------------------------------------------------
    # RECOMENDACIONES TÉCNICAS PARA DETECCIÓN DE CAMBIOS ENTRE EJECUCIONES
    # -----------------------------------------------------------------------
    print("""
RECOMENDACIONES TÉCNICAS — Detección de cambios entre ejecuciones
------------------------------------------------------------------
1. DIFF POR CAMPO:
   Antes de hacer $set, leer el doc actual y comparar campo a campo.
   Si portal_X.publicada cambia (True→False o False→True), generar
   un evento en una colección aparte: 'auditoria_cambios'.

2. HISTORIAL DE ESTADOS (append-only):
   Añadir un campo 'auditoria_historial' como array con entradas:
     { "fecha": "...", "portal": "...", "estado": "publicada"|"retirada" }
   Permite trazar el ciclo de vida de cada publicación.

3. EJECUCIÓN INCREMENTAL:
   Para re-auditar solo propiedades con cambios recientes en Prop360,
   filtrar por 'ultima_actualizacion_scraping' > fecha_ultima_auditoria.

4. ALERTAS AUTOMÁTICAS:
   Si publicada cambia de True a False en portal_inmobiliario o procasa,
   disparar una notificación (webhook, email) para revisión manual.

5. FRECUENCIA RECOMENDADA:
   - Diaria para portales de alto volumen (Portal Inmobiliario, MercadoLibre).
   - Semanal para Yapo y Toctoc.
   - Inmediata tras ingreso o baja de una propiedad.

6. ÍNDICE MONGODB:
   Crear índice en 'auditoria_publicaciones.ultima_revision' para
   consultas rápidas de propiedades auditadas recientemente:
     db.universo_cartera.createIndex({"auditoria_publicaciones.ultima_revision": -1})
""")
    print("--- AUDITORÍA FINALIZADA ---")


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(run_audit())
