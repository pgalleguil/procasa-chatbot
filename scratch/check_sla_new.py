import asyncio, os, sys
sys.path.insert(0, os.getcwd())
from api_crm import get_crm_leads_list
from chatbot.constants import PipelineStage

async def main():
    leads, kpis, total = await get_crm_leads_list(filtro_estado='NEW', ordenar_por='fecha', user_role='supervisor', user_name='test', limit=15)
    sample = [(l.get('nombre'), l.get('estado'), l.get('sla_status'), l.get('sla_label'), l.get('fecha_asignacion_relativa')) for l in leads[:8]]
    print('rows', len(leads), 'total', total)
    for r in sample:
        print(r)

asyncio.run(main())
