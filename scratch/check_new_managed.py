import asyncio, os, sys
sys.path.insert(0, os.getcwd())
from api_crm import get_crm_leads_list

async def main():
    leads, kpis, total = await get_crm_leads_list(filtro_estado='NEW', ordenar_por='fecha', user_role='supervisor', user_name='test', limit=30)
    print('rows', len(leads), 'total', total)
    bad = []
    for l in leads:
        t = (l.get('ultima_accion_titulo') or '').lower()
        if 'whatsapp' in t or 'gestion' in t or 'accion registrada' in t:
            bad.append((l.get('nombre'), l.get('estado_badge'), l.get('ultima_accion_titulo')))
    print('bad_rows', len(bad))
    for b in bad[:10]:
        print(b)

asyncio.run(main())
