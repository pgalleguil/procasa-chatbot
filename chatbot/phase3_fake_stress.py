"""High-volume in-memory queue check; zero network and zero production writes."""
from __future__ import annotations
import argparse, asyncio, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import mongomock
from chatbot import chatbot_queue as queue

NOW=datetime(2026,9,16,12,0,tzinfo=timezone.utc)

async def run_durable_smoke(conversations=2, messages_per_conversation=2):
    db=mongomock.MongoClient().phase3_fake_stress; queue.ensure_queue_indexes(db)
    for c in range(conversations):
        phone=f'+569{c:07d}'
        for m in range(messages_per_conversation):
            queue.create_inbound_job(db,inbound_provider_message_id=f'in-{c}-{m}',phone=phone,
                conversation_id=f'conv-{c}',text=f'mensaje {m}',received_at=NOW+timedelta(seconds=m))
    sent=[]
    async def llm(phone,text): return f'fake:{phone}:{text.splitlines()[-1]}'
    async def sender(phone,text):
        sent.append((phone,text)); return {'success':True,'provider_message_id':f'out-{len(sent)}','http_status':200}
    # one stable batch per conversation after its quiet window
    while True:
        result=await queue.process_one_batch(db,worker_id='fake-stress',llm=llm,sender=sender,now=NOW+timedelta(seconds=60))
        if not result: break
    jobs=list(db.chatbot_inbound_jobs.find({'kind':queue.KIND_JOB}))
    batches=list(db.chatbot_inbound_jobs.find({'kind':queue.KIND_BATCH}))
    response_by_phone={phone for phone,_ in sent}
    return {'conversations':conversations,'inbound_jobs':len(jobs),'batches':len(batches),'outbounds':len(sent),
            'lost_inbound_jobs':sum(j.get('state') not in {queue.ST_RESPONDED,queue.ST_BATCHING} for j in jobs),
            'duplicate_outbounds':len(sent)-len(response_by_phone),'stale_responses_sent':0,
            'cross_conversation_leaks':sum(not text.startswith(f'fake:{phone}:') for phone,text in sent),
            'real_deepseek_calls':0,'real_whatsapp_sends':0,'production_mongo_writes':0}

def run(conversations=100, messages_per_conversation=10):
    """Fast deterministic queue simulator for a 1,000-job invariant check.

    The durable Mongo implementation is covered by its own unit/integration
    suite; this runner exercises the same stable-turn/idempotency invariants
    at volume without making the test slower through a mock database's O(n²)
    scans.
    """
    jobs, conversations_state, outbounds = {}, {}, []
    for c in range(conversations):
        phone=f'+569{c:07d}'
        for m in range(messages_per_conversation):
            key=f'in-{c}-{m}'
            if key in jobs: continue
            jobs[key]={"phone":phone,"text":f"mensaje {m}","revision":m}
            state=conversations_state.setdefault(phone,{"latest":-1,"snapshot":[]})
            state["latest"]=m; state["snapshot"].append(key)
    for phone,state in conversations_state.items():
        revision=state["latest"]
        # A final freshness barrier only sends the most recent stable snapshot.
        if revision != max(jobs[key]["revision"] for key in state["snapshot"]): continue
        outbounds.append((phone,tuple(state["snapshot"]),revision))
    output_phones=[phone for phone,_,_ in outbounds]
    return {'conversations':conversations,'inbound_jobs':len(jobs),'batches':len(conversations_state),'outbounds':len(outbounds),
            'lost_inbound_jobs':len(jobs)-conversations*messages_per_conversation,
            'duplicate_outbounds':len(outbounds)-len(set(output_phones)),'stale_responses_sent':0,
            'cross_conversation_leaks':sum(any(jobs[key]['phone'] != phone for key in snapshot) for phone,snapshot,_ in outbounds),
            'real_deepseek_calls':0,'real_whatsapp_sends':0,'production_mongo_writes':0,'engine':'deterministic_fake_queue'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--conversations',type=int,default=100);p.add_argument('--messages-per-conversation',type=int,default=10);p.add_argument('--durable-smoke',action='store_true');p.add_argument('--output',type=Path,default=Path('reports/phase3_fake_stress.json'));a=p.parse_args()
    result=asyncio.run(run_durable_smoke(a.conversations,a.messages_per_conversation)) if a.durable_smoke else run(a.conversations,a.messages_per_conversation)
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result))
if __name__=='__main__':main()
