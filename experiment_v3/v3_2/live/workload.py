"""Run a tagged business workload against the fresh dedicated V3.2 Odoo clone."""
import contextlib,hashlib,json,os,queue,threading,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
BASE=Path(__file__).resolve().parent
V3=BASE.parents[1]
sys.path.insert(0,str(V3/'live'))
os.environ.update(V3_ODOO_URL='http://127.0.0.1:18132',V3_ODOO_DB='wms32_trial')
from check_http import Terminal
from mqtt_bridge import Bridge
from manifest import DESIGN,manifest
import subprocess
class V32Bridge(Bridge):
    def __enter__(self):
        self.client.connect('127.0.0.1',18932);self.client.loop_start()
        if not self.ready.wait(20):raise RuntimeError('broker subscribe timeout')
        self.thread=threading.Thread(target=self.worker,daemon=True);self.thread.start();return self
class Work:
    def __init__(self,mode,s,l,seed,mask,dir):
        self.mode,self.s,self.l,self.seed,self.mask,self.dir=mode,s,l,seed,mask,dir
        self.cases=manifest(s,l,seed);self.roles={};self.available={};self.logs=[];self.outcomes={};self.exc=[]
        self.physical_zone=set();self.observation_serial=0
        self.assignment_lock=threading.Lock();self.assigned={}
        self.lock=threading.Lock();self.inflight=0;self.peak=0;self.base=time.monotonic();self.deadline=None
    def on(self,n):return bool(self.mask & (1<<(n-1)))
    def time(self):return time.monotonic()-self.base
    def clients(self):
        keys=[(r,i) for r,n in DESIGN['session_roles'].items() for i in range(n)]
        with ThreadPoolExecutor(4) as pool:self.roles=dict(zip(keys,pool.map(lambda k:Terminal(*k),keys)))
        for (role,i),client in self.roles.items():self.available.setdefault(role,queue.Queue()).put((i,client))
        self.bridge_users={r:Terminal(r) for r in ('exchange','production_adapter','quality_adapter','sensor')}
    def send(self,name,n,role,data=None,expect=(200,),eid=None,version=1):
        if self.deadline is not None and self.time()>=self.deadline:raise TimeoutError('observation ended')
        key=eid or uuid.uuid4().hex
        msg=dict(case=name,operation=n,role=role,version=version,event_id=key,data=data or {})
        queued=self.time()
        channel={'exchange':'exchange','production_adapter':'production','quality_adapter':'quality','sensor':'arrival'}.get(role)
        token=None
        if role in self.available:token=self.available[role].get(timeout=60)
        start=self.time()
        with self.lock:self.inflight+=1;self.peak=max(self.peak,self.inflight)
        try:
            if channel and n!='reading':status,result=self.bridge.submit(channel,msg)
            else:status,result=(token[1] if token else self.bridge_users[role]).step(msg)
            self.logs.append(dict(case=name,n=n,role=role,user=token[0] if token else None,queued=queued,start=start,end=self.time(),http=status,
                                  event=key,result=result,clock='wall_seconds',channel=channel or 'http'))
            if status not in expect:raise RuntimeError(f'{n} {role} {status} {result}')
            return result
        finally:
            with self.lock:self.inflight-=1
            if token:self.available[role].put(token)
    def barrier(self,r):
        # Real contention: all six operators replay the same idempotent receive;
        # the other authenticated roles issue read-only requests at the same barrier.
        gate=threading.Barrier(20);eid=uuid.uuid4().hex
        def request(item):
            (role,i),client=item
            gate.wait(timeout=30);t=self.time()
            if role=='operator':
                msg=dict(case=r['case'],operation=16,role=role,version=1,event_id=eid,data={'pallet':r['pallets'][0]})
                status,res=client.step(msg)
            else:
                status=client.http.get(os.environ['V3_ODOO_URL']+'/v3/tsd',timeout=30).status_code;res={}
            self.logs.append(dict(case=r['case'],n='concurrent_receipt' if role=='operator' else 'role_read',role=role,user=i,start=t,end=self.time(),http=status,result=res))
            return role,status,res
        with ThreadPoolExecutor(20) as pool:got=list(pool.map(request,self.roles.items()))
        assert all(s==200 for _,s,_ in got) and len({x['picking'] for role,_,x in got if role=='operator'})==1
    def route(self,r,race=False):
        name=r['case'];pids=r['pallets'];step=lambda n,role,data=None,**kw:self.send(name,n,role,data,**kw)
        try:
            if r['stale_version']:step(1,'production',{'members':pids},expect=(422,),version=0)
            source='production_adapter' if self.on(1) else 'production'
            step(1,source,{'members':pids});step(6,'quality',{'quality_case':r['quality_ref']})
            for i,p in enumerate(pids):
                if i==0 and r['label_error']:step(2,'operator',{'pallet':p,'barcode':'UNREADABLE'},expect=(422,))
                via='sensor' if self.on(5) and not (r['label_error'] and i==0) else 'operator'
                with self.lock:self.physical_zone.add(p)
                step(2,via,{'pallet':p,'barcode':p})
            if r['stale_version']:step(3,'operator',{'document':'unconfirmed'},expect=(422,))
            step(3,'operator',{'document':name});step(4,'operator');step(5,'operator')
            if not self.on(3):
                for n in (12,13,14,15):step(n,'operator',{'correction':r['stale_version']} if n==15 else {})
            for i,p in enumerate(pids):
                if race and i==0:self.barrier(r)
                else:step(16,'operator',{'pallet':p})
            if r['quarantine']:
                for p in pids:
                    step(10,'driver',{'pallet':p})
                    with self.lock:self.physical_zone.discard(p)
            step(7,'quality');step(8,'quality',{'decision':r['decision']})
            step(9,'quality_adapter' if self.on(2) else 'quality',{'quality_case':r['quality_ref'],'quality_version':1})
            step(11,'quality')
            if r['decision']=='failed':
                step(22,'closing',expect=(422,));self.outcomes[name]=('QUALITY_OPEN',self.time());return
            if not r['quarantine']:
                for p in pids:step(10,'driver',{'pallet':p})
            # Process the recommendation actually returned by the shared dispatcher;
            # it may belong to another concurrent receipt in the same scope.
            while not all(p in self.assigned for p in pids):
                with self.assignment_lock:
                    if all(p in self.assigned for p in pids):break
                    if self.on(6):
                        from datetime import datetime,timezone
                        with self.lock:
                            self.observation_serial+=1;serial=self.observation_serial;count=len(self.physical_zone)
                        step('reading','sensor',dict(stamp=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),count=count,serial=serial))
                    rec=step('next','dispatch')
                    assert rec['candidates'] and rec['pallet'] not in self.assigned,rec
                    target,pid=rec['case'],rec['pallet']
                    if race and not self.assigned:
                        with ThreadPoolExecutor(2) as pool:
                            attempts=[pool.submit(self.send,target,17,'dispatch',{'pallet':pid},(200,422)) for _ in range(2)]
                            results=[f.result() for f in attempts]
                        reached=[r['address'] for r in results if 'address' in r]
                        assert len(reached)==1,results
                        self.assigned[pid]=reached[0]
                    else:self.assigned[pid]=self.send(target,17,'dispatch',{'pallet':pid})['address']
            addr={p:self.assigned[p] for p in pids}
            assert len(set(addr.values()))==len(addr)
            for i,p in enumerate(pids):
                step(18,'driver',{'pallet':p,'address':addr[p]})
                with self.lock:self.physical_zone.discard(p)
                if i==len(pids)-1 and self.mode=='B':
                    with ThreadPoolExecutor(2) as pool:
                        rejected=pool.submit(step,22,'closing',None,expect=(422,))
                        placed=pool.submit(step,19,'driver',{'pallet':p,'address':addr[p]})
                        rejected.result();placed.result()
                else:step(19,'driver',{'pallet':p,'address':addr[p]})
                step(20,'operator',{'pallet':p});step(21,'operator',{'pallet':p})
            step(22,'closing')
            if not self.on(3):step(23,'closing')
            ack={'ack':name+'/1'};eid=uuid.uuid4().hex
            step(24,'exchange',ack,eid=eid)
            if self.s=='S3':step(24,'exchange',ack,eid=eid)
            if not self.on(3):step(25,'closing')
            self.outcomes[name]=('STANDARD',self.time())
        except TimeoutError:self.outcomes[name]=('OPEN',self.time())
        except Exception as ex:self.exc.append([name,type(ex).__name__,str(ex)[:220]])
    def run(self):
        self.dir.mkdir(parents=True,exist_ok=True)
        self.clients();self.outage=[]
        with V32Bridge(self.dir/'inbox.sqlite') as self.bridge, ThreadPoolExecutor(len(self.cases)) as pool:
            self.base=time.monotonic();self.deadline=75 if self.mode=='C' else None
            # Suspend only the dedicated broker, never either historical stack.
            def fault():
                if self.mode=='C':time.sleep(8)
                before=self.time()
                subprocess.run(['docker','pause','wms32-live-broker-1'],check=True,stdout=subprocess.DEVNULL)
                try:time.sleep(2)
                finally:subprocess.run(['docker','unpause','wms32-live-broker-1'],check=True,stdout=subprocess.DEVNULL)
                self.outage.append([before,self.time()])
            th=None
            if self.s=='S3':
                th=threading.Thread(target=fault);th.start()
            jobs=[]
            for i,r in enumerate(self.cases):
                if self.mode=='C':time.sleep(max(0,r['planned_s']-self.time()))
                if self.mode=='B':
                    self.route(r,race=i==0)
                else:jobs.append(pool.submit(self.route,r))
            if self.mode=='C':time.sleep(max(0,75-self.time()))
            for f in jobs:f.result()
            if th:th.join()
        end=75 if self.mode=='C' else self.time()
        completed=sum(v=='STANDARD' and t<=end for v,t in self.outcomes.values())
        result=dict(mode=self.mode,scenario=self.s,load=self.l,seed=self.seed,mask=self.mask,users=len(self.roles),offered=len(self.cases),complete=completed,open=len(self.cases)-completed,
                    failed=len(self.exc),requests=len(self.logs),peak_inflight=self.peak,actual_outage=self.outage,observation_s=end,
                    status='FAILED' if self.exc else 'EXECUTED_PENDING_DB_AUDIT')
        for k,v in [('requests',self.logs),('outcomes',self.outcomes),('errors',self.exc),('summary',result),('manifest',self.cases)]:
            (self.dir/(k+'.json')).write_text(json.dumps(v,indent=2),encoding='utf8')
        return result

