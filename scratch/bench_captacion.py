import os, sys, time, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from api_captacion import get_captacion_list
from chatbot.storage import get_async_db

async def main():
    adb=get_async_db()
    samples=[]
    for i in range(5):
        t0=time.perf_counter()
        items,total = await asyncio.get_running_loop().run_in_executor(None, lambda: get_captacion_list(user_role='supervisor', user_name='test', page=1, limit=10))
        base_query={"details.es_propietario_directo": True}
        c1,c2 = await asyncio.gather(
            adb['yapo_propiedades'].count_documents({**base_query,'gestion.estado':'GESTION'}),
            adb['yapo_propiedades'].count_documents({**base_query,'gestion.estado':'CAPTADO'})
        )
        dt=(time.perf_counter()-t0)*1000
        samples.append(dt)
        print(f'run {i+1}: {dt:.1f}ms items={len(items)} total={total} gestion={c1} captado={c2}')
    print(f'avg={sum(samples)/len(samples):.1f}ms min={min(samples):.1f}ms max={max(samples):.1f}ms')

asyncio.run(main())
