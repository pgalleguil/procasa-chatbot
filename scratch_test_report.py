import sys
import os
sys.path.append('c:/Users/pgall/Desktop/Python/ChatBot_v4_Grok')

import asyncio
from datetime import datetime, timedelta
from chatbot.captacion_report import check_and_run_meta_diaria_report, get_daily_progress_stats
from chatbot.constants import CHILE_TZ

async def main():
    try:
        now_cl = datetime.now(CHILE_TZ)
        days_back = 3 if now_cl.weekday() == 0 else 1
        target_date = now_cl - timedelta(days=days_back)
        
        print(f"Testing stats generation for target date: {target_date}...")
        stats = await get_daily_progress_stats(target_date)
        print("Stats ok:")
        print(stats)
        
        print("Force sending report...")
        await check_and_run_meta_diaria_report(force=True)
        print("DONE")
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
