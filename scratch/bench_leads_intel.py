import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from api_leads_intelligence import get_leads_executive_report

samples=[]
for i in range(5):
    t0=time.perf_counter()
    data=get_leads_executive_report()
    dt=(time.perf_counter()-t0)*1000
    samples.append(dt)
    print(f'run {i+1}: {dt:.1f}ms leads={len(data.get("leads",[]))}')
print(f'avg={sum(samples)/len(samples):.1f}ms min={min(samples):.1f}ms max={max(samples):.1f}ms')
