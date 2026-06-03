# validate_properties.py
import os
import sys
import asyncio

# Allow imports from the project root (e.g. config.py) regardless of CWD
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import logging
import re
import argparse
from datetime import datetime, timezone, timedelta
from itertools import cycle
from tqdm import tqdm

from curl_cffi import requests as curl_requests
from motor.motor_asyncio import AsyncIOMotorClient
from config import Config

# Desactivar logs de librerías para garantizar limpieza absoluta en la terminal
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)

# Lista de User-Agents estables de producción (evita depender de librerías externas)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
]

STATS = {
    "total_checked": 0,
    "active": 0,
    "became_suspect": 0,
    "became_inactive": 0,
    "recovered": 0,
    "blocks": 0,
    "errors": 0,
    "bytes_consumed": 0,  # Tráfico de red acumulado
}

EXAMPLES = {
    "active": [],
    "suspect": [],
    "inactive": [],
}

BURNED_PROXIES = {}
pbar = None  # Instancia global para control de línea única en tiempo real

async def get_proxies_from_api(api_url: str) -> list:
    try:
        async with curl_requests.AsyncSession(timeout=10.0) as client:
            resp = await client.get(api_url)
            STATS["bytes_consumed"] += len(resp.content)
            if resp.status_code == 200:
                text = resp.text.strip()
                if text.startswith("[") or text.startswith("{"):
                    data = resp.json()
                    if isinstance(data, list): return data
                    if isinstance(data, dict) and "proxies" in data: return data["proxies"]
                return [p.strip() for p in text.splitlines() if p.strip()]
    except Exception:
        pass
    return []

async def check_url(url: str, session: curl_requests.AsyncSession) -> tuple[str, str]:
    """
    Verifica la existencia física de la propiedad usando lógica multicapa y determinista.
    Retorna: (resultado, motivo)
    - resultado: 'active', 'inactive', 'block', 'error'
    - motivo: descripción textual de la detección (p. ej., "redirect_without_id", "404")
    """
    resp = await session.get(url, timeout=12.0)
    html_lower = resp.text.lower()
    
    # Registrar consumo de tráfico web en bytes
    STATS["bytes_consumed"] += len(resp.content)

    # 1. Redirecciones a listados generales (cuando el aviso ya no existe)
    final_url = resp.url
    if final_url and final_url != url:
        # Extraer el ID numérico (7 a 11 dígitos) de la URL original
        id_match = re.search(r'[/_](\d{7,11})', url)
        original_id = id_match.group(1) if id_match else None

        # Si el ID original ha desaparecido de la URL final, es redirección por baja
        if original_id and original_id not in str(final_url):
            return "inactive", "redirect_without_id"

    # 2. Detección de bloqueos reales por proxy / Captchas
    if resp.status_code in [403, 429]:
        return "block", f"HTTP_{resp.status_code}"

    real_challenge_keywords = [
        "checking your browser",
        "please wait",
        "ray id",
        "g-recaptcha",
        "cf-browser-verification",
        "distilnetworks",
        "access denied",
    ]
    if any(k in html_lower for k in real_challenge_keywords):
        return "block", "captcha_or_challenge"

    # 3. HTTP status codes definitivos de baja
    if resp.status_code in [404, 410]:
        return "inactive", f"HTTP_{resp.status_code}"

    # 4. Códigos HTTP de fallas temporales
    if resp.status_code != 200:
        return "error", f"HTTP_{resp.status_code}"

    # 5. Páginas truncadas o vacías
    if len(resp.content) < 1500 or "</body>" not in html_lower:
        return "inactive", "empty_or_truncated_html"

    # 6. Frases exactas de publicación eliminada
    deletion_keywords = [
        "esta publicación ya no está disponible",
        "aviso eliminado",
        "no encontramos esta publicación",
        "publicación desactivada",
        "página no encontrada",
        "oops!",
        "el aviso que buscas no existe"
    ]
    for kw in deletion_keywords:
        if kw in html_lower:
            return "inactive", f"keyword_{kw.replace(' ', '_')}"

    # 7. Marcadores estructurales mínimos (Para evitar falsos activos)
    title_match = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.IGNORECASE | re.DOTALL)
    if title_match:
        title_text = title_match.group(1).lower().strip()
        if any(x in title_text for x in ["página no encontrada", "oops!", "error"]):
            return "inactive", "title_error_page"

    # Confirmar marcadores globales de Yapo en el HTML
    has_yapo_markers = (
        "document.__yapo__" in html_lower or
        "loopadata" in html_lower or
        "d3-property-info" in html_lower or
        "product-comments" in html_lower
    )
    if not has_yapo_markers:
        return "error", "missing_yapo_dom_markers"

    return "active", "alive"


async def validate_property(doc: dict, session: curl_requests.AsyncSession, coll, dry_run: bool) -> None:
    url = doc["url"]
    current_status = doc.get("status", "active")
    current_failed = doc.get("failed_checks", 0)
    
    result, reason = await check_url(url, session)
    now = datetime.now(timezone.utc)
    
    if result == "active":
        STATS["total_checked"] += 1
        STATS["active"] += 1
        
        if len(EXAMPLES["active"]) < 20:
            EXAMPLES["active"].append(url)
            
        if current_status in ["suspect", "inactive"]:
            STATS["recovered"] += 1
            
        if not dry_run:
            await coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "status": "active",
                    "last_verified": now,
                    "failed_checks": 0,
                    "inactive_date": None,
                    "last_check_reason": reason
                }}
            )
            
    elif result == "inactive":
        STATS["total_checked"] += 1
        new_failed = min(current_failed + 1, 3)
        
        if new_failed >= 3:
            STATS["became_inactive"] += 1
            if len(EXAMPLES["inactive"]) < 20:
                EXAMPLES["inactive"].append(url)
            if not dry_run:
                await coll.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {
                        "status": "inactive",
                        "last_verified": now,
                        "failed_checks": new_failed,
                        "inactive_date": now,
                        "last_check_reason": reason
                    }}
                )
        else:
            STATS["became_suspect"] += 1
            if len(EXAMPLES["suspect"]) < 20:
                EXAMPLES["suspect"].append(url)
            if not dry_run:
                await coll.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {
                        "status": "suspect",
                        "last_verified": now,
                        "failed_checks": new_failed,
                        "last_check_reason": reason
                    }}
                )
                
    elif result == "block":
        STATS["blocks"] += 1
        
    elif result == "error":
        STATS["errors"] += 1

async def main():
    global pbar
    parser = argparse.ArgumentParser(description="Validador continuo de publicaciones de Yapo")
    parser.add_argument("--concurrency", type=int, default=10, help="Nivel de concurrencia")
    parser.add_argument("--limit", type=int, default=None, help="Límite de propiedades a validar")
    parser.add_argument("--dry-run", action="store_true", help="Simula la auditoría sin modificar MongoDB")
    args = parser.parse_args()
    
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client["URLS"]
    coll = db["yapo_propiedades"]
    
    proxy_api_url = os.getenv("PROXY_API_URL")
    
    # Extraer lista base de proxies
    base_proxies = await get_proxies_from_api(proxy_api_url) if proxy_api_url else [p.strip() for p in Config.PROXIES.split(",") if p.strip()]
    
    # Inyectar autenticación si existe en la configuración
    proxies = []
    for p in base_proxies:
        p = p.replace("http://", "").replace("https://", "")
        if Config.PROXY_USER and Config.PROXY_PASS and "@" not in p:
            proxies.append(f"{Config.PROXY_USER}:{Config.PROXY_PASS}@{p}")
        else:
            proxies.append(p)
            
    proxy_cycle = cycle(proxies) if proxies else None
    
    # Crear índice compuesto si no es simulación
    if not args.dry_run:
        await coll.create_index([("status", 1), ("last_verified", 1)])
    
    now = datetime.now(timezone.utc)
    cut_active = now - timedelta(days=7)
    cut_suspect = now - timedelta(days=1)
    cut_inactive = now - timedelta(days=30)
    
    query = {
        "$or": [
            {"status": {"$exists": False}},
            {"last_verified": {"$exists": False}},
            {"status": "active", "last_verified": {"$lt": cut_active}},
            {"status": "suspect", "last_verified": {"$lt": cut_suspect}},
            {"status": "inactive", "last_verified": {"$lt": cut_inactive}}
        ]
    }
    
    cursor = coll.find(query, {"_id": 1, "url": 1, "status": 1, "failed_checks": 1})
    if args.limit:
        cursor = cursor.limit(args.limit)
        
    # Calcular el total para la barra de progreso
    total_propiedades = await coll.count_documents(query)
    if args.limit and total_propiedades > args.limit:
        total_propiedades = args.limit

    queue = asyncio.Queue(maxsize=args.concurrency * 2)
    
    # SOLUCIÓN CLAVE: Inicializar obligatoriamente con un 'postfix' de texto base.
    # Esto fuerza a la consola de Windows a reservar el espacio horizontal desde el inicio.
    pbar = tqdm(
        total=total_propiedades, 
        desc="🔍 Validando", 
        unit="prop",
        mininterval=0.1,
        miniters=1,
        postfix="Iniciando...",
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]{postfix}"
    )
    
    async def worker():
        while True:
            doc = await queue.get()
            if doc is None:
                queue.task_done()
                break

            proxy = None
            if proxy_cycle:
                for _ in range(len(proxies)):
                    p = next(proxy_cycle)
                    cooldown = BURNED_PROXIES.get(p)
                    if not cooldown or datetime.now() > cooldown:
                        proxy = p
                        break

            h_proxy = proxy if (not proxy or proxy.startswith("http")) else f"http://{proxy}"
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-CL,es;q=0.9",
                "Referer": "https://www.yapo.cl/",
            }

            validated = False

            # Intento 1: con proxy (si hay uno disponible)
            if h_proxy:
                try:
                    async with curl_requests.AsyncSession(
                        headers=headers,
                        proxies={"http": h_proxy, "https": h_proxy},
                        timeout=10.0,
                        impersonate="chrome120"
                    ) as session:
                        await validate_property(doc, session, coll, args.dry_run)
                    validated = True
                except Exception:
                    if proxy:
                        BURNED_PROXIES[proxy] = datetime.now() + timedelta(seconds=120)

            # Intento 2: directo sin proxy
            if not validated:
                try:
                    async with curl_requests.AsyncSession(
                        headers=headers,
                        proxies=None,
                        timeout=15.0,
                        impersonate="chrome120"
                    ) as session:
                        await validate_property(doc, session, coll, args.dry_run)
                except Exception:
                    STATS["errors"] += 1
            
            # Métricas de estado de Proxies en tiempo real
            now_time = datetime.now()
            proxies_desactivados = sum(1 for cooldown in BURNED_PROXIES.values() if now_time <= cooldown)
            proxies_activos = max(0, len(proxies) - proxies_desactivados) if proxies else 0
            
            mb_consumed = STATS["bytes_consumed"] / (1024 * 1024)
            
            # RE-PINTADO AGRESIVO: Se pasa un diccionario estructurado al postfix.
            # tqdm lo procesará automáticamente e imprimirá de inmediato al hacer el update/refresh.
            pbar.set_postfix({
                "Act": STATS['active'],
                "Susp": STATS['became_suspect'],
                "Inact": STATS['became_inactive'],
                "Blq": STATS['blocks'],
                "Err": STATS['errors'],
                "Proxies": f"[🟢{proxies_activos} 🔴{proxies_desactivados}]",
                "Tráfico": f"{mb_consumed:.2f}MB"
            }, refresh=True)
            
            pbar.update(1)
            pbar.refresh()  # Forzar refresco directo de la consola de Windows
            
            # Delay breve para evitar rate-limiting
            await asyncio.sleep(0.5)
            queue.task_done()
                
    workers = [asyncio.create_task(worker()) for _ in range(args.concurrency)]
    
    count = 0
    async for doc in cursor:
        await queue.put(doc)
        count += 1
        
    for _ in range(args.concurrency):
        await queue.put(None)
        
    await asyncio.gather(*workers)
    pbar.close()
    
    # Reporte de cierre limpio e informativo
    print(f"\n✅ Proceso Finalizado. Totales -> Activas: {STATS['active']} | Sospechosas: {STATS['became_suspect']} | Inactivas: {STATS['became_inactive']} | Tráfico final: {STATS['bytes_consumed'] / (1024 * 1024):.2f} MB\n")
    
    client.close()

if __name__ == "__main__":
    asyncio.run(main())