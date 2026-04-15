import asyncio
import os
import sys
from datetime import datetime
import pytz

async def verify_clean_report():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.path.append(os.getcwd())
    
    from chatbot.captacion_report import get_daily_progress_stats
    from chatbot.constants import CHILE_TZ
    
    # Yesterday 
    target_date = datetime(2026, 4, 14, tzinfo=CHILE_TZ)
    data = await get_daily_progress_stats(target_date)
    
    lines = [
        "🏠 *REPORTE DE CAPTACIÓN*",
        f"📅 {data['date_label']}",
        "",
        "━━━━━━━━━━━━",
        "👥 *Avance*",
        ""
    ]
    
    for r in data['avance']:
        lines.append(f"{r['name']}: {r['count']} contactos")

    lines.extend(["", "━━━━━━━━━━━━", "⚪ *Sin turno*", ""])
    if data['sin_turno']:
        for n in data['sin_turno']: lines.append(f"{n}")
    else: lines.append("(vacío)")

    lines.extend(["", "━━━━━━━━━━━━", "🆕 *En configuración*", ""])
    if data['en_configuracion']:
        for n in data['en_configuracion']: lines.append(f"{n}")
    else: lines.append("(vacío)")

    lines.extend([
        "",
        "━━━━━━━━━━━━",
        "🎯 *Meta*",
        "10 contactos por ejecutivo"
    ])
    
    print("--- FINAL CLEAN REPORT PREVIEW ---")
    print("\n".join(lines))

if __name__ == "__main__":
    asyncio.run(verify_clean_report())
