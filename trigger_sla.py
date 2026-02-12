import asyncio
import logging
from chatbot.sla_service import monitor_sla_thresholds

# Configure logging to see the output
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def main():
    print("Iniciando ejecución manual del Monitor SLA...")
    await monitor_sla_thresholds()
    print("Ejecución finalizada.")

if __name__ == "__main__":
    asyncio.run(main())
