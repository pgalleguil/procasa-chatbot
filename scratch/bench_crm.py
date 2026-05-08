import asyncio, time, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from api_crm import get_crm_leads_list

async def main():
    samples=[]
    for i in range(5):
        t0=time.perf_counter()
        leads,kpis,total=await get_crm_leads_list(
            filtro_estado=None,
            busqueda=None,
            ordenar_por='fecha',
            user_role='supervisor',
            user_name='test',
            ejecutivo_filter=None,
            limit=15,
            cursor_last_event_at=None,
        )
        dt=(time.perf_counter()-t0)*1000
        samples.append(dt)
        print(f'run {i+1}: {dt:.1f}ms leads={len(leads)} total={total}')
    print(f'avg={sum(samples)/len(samples):.1f}ms min={min(samples):.1f}ms max={max(samples):.1f}ms')

asyncio.run(main())
