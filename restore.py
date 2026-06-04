import sys

try:
    with open('webhook_old.py', 'r', encoding='utf-16') as f:
        content = f.read()
except UnicodeDecodeError:
    with open('webhook_old.py', 'r', encoding='utf-8') as f:
        content = f.read()

# Fix import
content = content.replace(
    'distribute_sourced_leads, format_relative_time as format_captacion_time',
    'distribute_sourced_leads, release_stale_captaciones, format_relative_time as format_captacion_time'
)

# Fix loop
loop_old = '''async def captacion_distribution_loop():
    logger.info("[BACKGROUND] Iniciando loop de distribución de captaciones...")
    while True:
        try:
            background_tasks_status["captacion_distributor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["captacion_distributor"]["status"] = "running"
            
            loop = asyncio.get_running_loop()
            count = await loop.run_in_executor(_WORKER_THREAD_POOL, distribute_sourced_leads)
            if count > 0:
                logger.info(f"[BACKGROUND] Se asignaron {count} nuevas captaciones automáticamente.")
                
            # Ejecutar cada 1 hora
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            background_tasks_status["captacion_distributor"]["status"] = "error"
            logger.error(f"[BACKGROUND] Error en distribuidor de captaciones: {e}")
            await asyncio.sleep(60)'''

loop_new = '''async def captacion_distribution_loop():
    logger.info("[BACKGROUND] Iniciando loop de distribución de captaciones...")
    while True:
        try:
            background_tasks_status["captacion_distributor"]["last_heartbeat"] = datetime.now(CHILE_TZ).isoformat()
            background_tasks_status["captacion_distributor"]["status"] = "running"
            
            loop = asyncio.get_running_loop()
            
            # 1. Liberar captaciones sin gestión por SLA
            released = await loop.run_in_executor(_WORKER_THREAD_POOL, release_stale_captaciones)
            if released > 0:
                logger.info(f"[BACKGROUND] {released} captaciones liberadas por SLA antes de redistribuir.")
                
            # 2. Distribuir propiedades sin ejecutivo
            count = await loop.run_in_executor(_WORKER_THREAD_POOL, distribute_sourced_leads)
            if count > 0:
                logger.info(f"[BACKGROUND] Se asignaron {count} nuevas captaciones automáticamente.")
                
            # Ejecutar cada 1 hora
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            background_tasks_status["captacion_distributor"]["status"] = "error"
            logger.error(f"[BACKGROUND] Error en distribuidor de captaciones: {e}")
            await asyncio.sleep(60)'''

if loop_old in content:
    content = content.replace(loop_old, loop_new)
    with open('webhook.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Webhook restored and patched successfully.")
else:
    print("Could not find loop_old in content.")
