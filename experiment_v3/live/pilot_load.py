"""Open-loop S1/C000 technical pilot. Every role has a separate authenticated session.
Intervals are wall-clock seconds. Pilot output is kept outside both main manifests.
"""
import argparse,concurrent.futures as futures,json,os,queue,threading,time,uuid
from pathlib import Path
os.environ['V3_ODOO_URL']='http://127.0.0.1:18080'
os.environ['V3_ODOO_DB']='wms_v3_trial'
from check_http import ROOT,Terminal
from mqtt_bridge import Bridge

ROLES={'production':2,'operator':6,'driver':4,'quality':2,'dispatch':2,'closing':2,'supervisor':2}
def run(out,interval):
    out.mkdir(parents=True,exist_ok=False)
    start=time.monotonic()
    clock=lambda:time.monotonic()-start
    queues={r:queue.Queue() for r in ROLES}
    stopped=threading.Event()
    lock=threading.Lock()
    users=[];requests=[];outcomes=[];resources=[]
    def pause_until(t):
        stopped.wait(max(0,t-clock()))
    def worker(role,i,delay):
        pause_until(delay)
        t=Terminal(role,i)
        users.append(dict(role=role,index=i,logged_in=clock()))
        while not stopped.is_set():
            try:msg,offered,f=queues[role].get(timeout=.4)
            except queue.Empty:
                if role=='supervisor':
                    # Read-only role monitors application state. No business actions assigned.
                    stopped.wait(4)
                    if stopped.is_set():return
                    sent=clock()
                    res=t.http.get(os.environ['V3_ODOO_URL']+'/v3/tsd',timeout=15)
                    requests.append(dict(method='GET /v3/tsd',role=role,start=sent,end=clock(),status=res.status_code))
                continue
            time.sleep(.12) # single declared user think primitive, laboratory seconds
            sent=clock()
            try:
                code,reply=t.step(msg)
                requests.append(dict(method='POST /v3/step',role=role,case=msg['case'],operation=msg['operation'],queued=offered,
                                     start=sent,end=clock(),status=code,event=msg['event_id'],result=reply))
                if code!=200:raise RuntimeError(str((code,reply)))
                f.set_result(reply)
            except Exception as ex:f.set_exception(ex)
    def route(index,planned,cohort,bridge):
        name=f'TRIAL-{index:04d}'
        members=[f'{name}-{i}' for i in (0,1)]
        def op(n,r,data=None):
            if stopped.is_set():raise InterruptedError()
            msg=dict(case=name,role=r,operation=n,version=1,event_id=uuid.uuid4().hex,data=data or {})
            if r=='exchange':
                sent=clock()
                status,reply=bridge.submit('exchange',msg)
                requests.append(dict(method='MQTT to Odoo',role=r,case=name,operation=n,event=msg['event_id'],start=sent,end=clock(),status=status,result=reply))
                if status!=200:raise RuntimeError(str(reply))
                return reply
            f=futures.Future()
            queues[r].put((msg,clock(),f))
            return f.result(timeout=max(.1,441-clock()))
        result='open';error=None
        try:
            op(1,'production',{'members':members})
            for p in members:op(2,'operator',{'pallet':p,'barcode':p})
            op(3,'operator',{'document':name})
            for n in (4,5):op(n,'operator')
            op(6,'quality',{'quality_case':'Q-'+name})
            op(7,'quality')
            op(8,'quality',{'decision':'released'})
            op(9,'quality',{'quality_case':'Q-'+name,'quality_version':1})
            op(11,'quality')
            for n in (12,13,14,15):op(n,'operator')
            for p in members:
                op(16,'operator',{'pallet':p})
                op(10,'driver',{'pallet':p})
                a=op(17,'dispatch',{'pallet':p})['address']
                for n in (18,19):op(n,'driver',{'pallet':p,'address':a})
                for n in (20,21):op(n,'operator',{'pallet':p})
            op(22,'closing');op(23,'closing');op(24,'exchange',{'ack':name+'/1'});op(25,'closing')
            result='standard'
        except Exception as ex:error=type(ex).__name__+': '+str(ex)[:200]
        outcomes.append(dict(case=name,planned=planned,cohort=cohort,end=clock(),outcome=result,error=error))
    def monitor():
        import subprocess
        while not stopped.is_set():
            q=subprocess.run(['docker','stats','--no-stream','--format','{{.Name}} {{.CPUPerc}} {{.MemUsage}}','wms-inbound-v3-trial-1','wms-inbound-v3-db-1'],capture_output=True,text=True)
            resources.append(dict(time=clock(),samples=q.stdout.splitlines()))
            stopped.wait(10)
    workers=[]
    with Bridge(out/'inbox.sqlite') as bridge, futures.ThreadPoolExecutor(30) as routes:
        start=time.monotonic()
        for role,count in ROLES.items():
            for i in range(count):
                th=threading.Thread(target=worker,args=(role,i,len(workers)),daemon=True)
                workers.append(th);th.start()
        threading.Thread(target=monitor,daemon=True).start()
        times=list(range(20,440,interval))
        for idx,t in enumerate(times):
            pause_until(t)
            routes.submit(route,idx,t,80<=t<260,bridge)
        pause_until(440);stopped.set()
        for th in workers:th.join(20)
    main=[r for r in outcomes if r['cohort']]
    timely=[r for r in main if r['outcome']=='standard' and r['end']<=r['planned']+180]
    result=dict(status='TECHNICAL_PILOT_ONLY',clock='wall_seconds',scenario='S1',load='L1',mask=0,seed=2900,
                offered=len([t for t in times if 80<=t<260]),success_180s=len(timely),session_count=len(users),
                users_ready_by_20s=sum(r['logged_in']<=20 for r in users),interval_seconds=interval,
                coverage_of_main_manifest=0,requests=len(requests),duration=clock())
    for name,value in [('users',users),('operations',requests),('cases',outcomes),('resources',resources),('summary',result)]:
        (out/(name+'.json')).write_text(json.dumps(value,indent=2))
    print(json.dumps(result),flush=True)
if __name__=='__main__':
    import sys
    run(Path(sys.argv[1]),30)

