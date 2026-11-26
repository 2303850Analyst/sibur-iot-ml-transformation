"""Timed process pilot. The simulator owns truth; dispatch receives observations only."""
import gzip,heapq,itertools,json,math,sys
from collections import Counter,defaultdict,deque
from pathlib import Path
import numpy as np
import pandas as pd
import simpy
from inputs import DAY,ROOT,fixtures,digest,rng,features
sys.path.insert(0,str(ROOT/'odoo_addons/wms_research_v3'))
from policy import enabled,observed_count,pick_job,Address,choose_address

# (role, active seconds per visit). Hypothetical primitive times; overrides documented in registry.
ACTIONS={1:('production',22),2:('operator',12),3:('operator',24),4:('operator',18),5:('operator',10),
6:('quality',12),7:('quality',15),8:('quality',12),9:('quality',18),10:('driver',0),11:('quality',35),
12:('operator',18),13:('operator',22),14:('operator',15),15:('operator',30),16:('operator',12),
17:('dispatch',14),18:('driver',0),19:('driver',12),20:('operator',12),21:('operator',35),
22:('closing',20),23:('closing',18),24:('closing',14),25:('closing',10)}
FLOW={12,13,14,16,22,23,25}
BLOCKS=[0]+[1]*5+[2]*6+[3]*5+[4]*5+[5]*4
class Process:
    def __init__(self,inputs,mask,dest):
        self.x=inputs
        self.mask=mask
        self.e=simpy.Environment()
        self.dest=dest
        self.mod=lambda n:enabled(mask,n)
        self.people={r:simpy.Resource(self.e,1) for r in ('operator','production','dispatch','closing','quality','driver')}
        self.capacity=10 if inputs['scenario']=='S4' else 40
        self.buffer=simpy.Resource(self.e,self.capacity)
        self.in_buffer=set()
        self.registered_quality={}
        self.waitforever=self.e.event()
        self.known_buffer=set()
        self.snapshot=None
        self.fork=simpy.Store(self.e,capacity=2)
        for i in range(2): self.fork.put((i,0.))
        self.qready={}
        self.quality_known={}
        self.location={}
        self.stock=defaultdict(deque)
        self.pools=[[] for _ in range(3)]
        self.addr={}
        self.constrained=inputs['scenario']=='S5'
        self.free_event=self.e.event()
        self.queue=[]
        self.wake=self.e.event()
        self.jobs={}
        self.totals=defaultdict(Counter)
        self.closed={}
        self.idgen=itertools.count(1)
        self.models=json.loads((ROOT/'data/ridge.json').read_text())
        self.history=np.zeros((2,34*48+1))
        self.counts=Counter()
        self.summary=[]
        self.end=34*DAY
        self.issue=0
        self.f0=self.f1=(0.,0.)
        for i in range(5329):
            g=min(2,i//2600)
            distance=(40,80,30)[g]+(20,20,10)[g]*(1-(i%129)/128)
            address=Address(f'V3-{i:04d}',g,g==2,distance)
            self.addr[address.name]=address
            if g!=2 and rng(inputs['seed'],i,'initial_stock').random()<(.95 if self.constrained else .70):
                self.stock[10*g+i%10].append(address.name)
            else:self.free(address.name,notify=False)
    def free(self,name,notify=True):
        a=self.addr[name]
        rank=(a.distance,a.name) if self.mod(4) else (a.name,)
        heapq.heappush(self.pools[a.group],(rank,name))
        if notify:
            self.free_event.succeed()
            self.free_event=self.e.event()
            self.signal()
    def record(self,kind,c='',p='',**fields):
        self.stream.write(json.dumps(dict(kind=kind,id=next(self.idgen),time=self.e.now,case=c,pallet=p,**fields),separators=(',',':'))+'\n')
    def signal(self):
        if not self.wake.triggered:self.wake.succeed()
    def at(self,t):
        return self.e.timeout(max(0,t-self.e.now))
    def emit(self,c,n,start,work,mode,p='',labor=0,machine=0,result='applied',**data):
        now=self.e.now
        row=dict(id=next(self.idgen),case=c,pallet=p,op=n,block=BLOCKS[n],entered=start,started=work,finished=now,
                 labor=labor,machine=machine,mode=mode,result=result,**data)
        self.stream.write(json.dumps(row,separators=(',',':'))+'\n')
        v=self.totals[c]
        v['labor']+=labor
        v['machine']+=machine
        v['manual_actions']+=int(mode=='manual')
        v['visits']+=1
    def op(self,c,n,p='',auto=False,skip=None,duration=None,**extra):
        start=self.e.now
        if skip:
            self.emit(c,n,start,start,'branch',p,result='NOT_APPLICABLE:'+skip,**extra)
            return
        role,nominal=ACTIONS[n]
        if duration is None:duration=nominal
        if auto:
            yield self.e.timeout(.3)
            self.emit(c,n,start,start,'automatic',p,**extra)
        else:
            with self.people[role].request() as req:
                yield req
                begin=self.e.now
                yield self.e.timeout(duration)
            self.emit(c,n,start,begin,'manual',p,labor=duration,**extra)
    def network(self):
        t=self.e.now%DAY
        outage=self.x['scenario']=='S3' and 43200<=t<43800
        if outage:self.counts['deferred_transmission']+=1
        return self.e.timeout(.3+(43800-t if outage else 0))
    def balance_event(self,p,enter):
        yield self.network()
        if enter:self.known_buffer.add(p)
        else:self.known_buffer.discard(p)
    def device(self):
        seq=0
        while True:
            stamp=self.e.now
            count=len(self.in_buffer)
            seq+=1
            yield self.network()
            if seq%101:
                self.snapshot=(stamp,count if seq%251 else -1)
            yield self.at(stamp+30)
    def forecasts(self):
        for idx in range(34*48):
            yield self.at(idx*1800)
            self.issue=idx
            f0=[];f1=[]
            for g in (0,1):
                w,b,mean=self.models[f"{self.x['load']}/{g}"]
                v=features(self.history[g],idx,mean)
                f0.append(sum(v[:4])/4)
                f1.append(max(0,np.dot(w,v)+b))
            self.f0,self.f1=f0,f1
    def quality(self,c,raw):
        q=raw['q']
        yield from self.op(c,6,quality=q)
        if raw['held']:
            yield from self.op(c,7,quality=q)
            yield self.at(self.qready[q])
        else:yield from self.op(c,7,skip='prior_authorized_result',quality=q)
        yield from self.op(c,8,quality=q)
        self.record('quality_decision',c,quality=q,authority='quality',decision='failed' if raw['failed'] else 'released')
        yield self.network()
        yield from self.op(c,9,auto=self.mod(2),quality=q)
        self.registered_quality[q]='failed' if raw['failed'] else 'released'
        self.record('quality_delivered',c,quality=q,decision=self.registered_quality[q])
        yield from self.op(c,11,skip=None if raw['failed'] else 'released',quality=q)
    def identify(self,c,raw):
        p=raw['pallet']
        yield self.at(raw['arrival'])
        req=self.buffer.request()
        yield req
        self.in_buffer.add(p)
        self.record('enter',c,p,capacity=self.capacity)
        self.e.process(self.balance_event(p,True))
        auto=self.mod(5) and not raw['label_bad'] and rng(self.x['seed'],p,'reader_miss').random()>=.01
        delay=120 if raw['label_bad'] else 60 if self.mod(5) and not auto else 12
        yield from self.op(c,2,p,auto=auto,duration=delay,buffer=len(self.in_buffer),raw_arrival=raw['arrival'])
        yield self.network()
        self.history[raw['sku']//10,min(int(self.e.now//1800),self.history.shape[1]-1)]+=1
        return req
    def submit(self,c,p,sku,stage,token):
        event=self.e.event()
        self.jobs[p]=(c,p,sku,stage,token,event,self.e.now)
        self.signal()
        return event
    def dispatch(self):
        while True:
            while self.fork.items and self.jobs:
                choices=[]
                for p,(c,_,sku,stage,token,event,reg) in self.jobs.items():
                    g=2 if stage==10 else sku//10
                    if self.pools[g]:
                        a=self.addr[self.pools[g][0][1]]
                        choices.append((reg,p,sku//10,180+a.distance+abs(self.fork.items[0][1])))
                if not choices:break
                n,mode=observed_count(self.e.now,self.snapshot if self.mod(6) else None,len(self.known_buffer),self.capacity)
                chosen=pick_job(choices,self.e.now,n,self.capacity,self.f1 if self.mod(7) else self.f0,self.mask)
                alt=pick_job(choices,self.e.now,n,self.capacity,self.f0,self.mask)
                self.counts['dispatches']+=1
                self.counts['forecast_changed']+=chosen[1]!=alt[1]
                self.counts['sensor_used']+=mode=='sensor'
                c,p,sku,stage,token,event,reg=self.jobs.pop(chosen[1])
                g=2 if stage==10 else sku//10
                _,dest=heapq.heappop(self.pools[g])
                self.record('reserve',c,p,address=dest,stage=stage,sku=sku,quality=self.x['pallets'][p]['q'])
                truck=yield self.fork.get()
                self.e.process(self.move(c,p,sku,stage,token,event,reg,truck,dest))
            if self.wake.triggered:self.wake=self.e.event()
            yield self.wake
    def move(self,c,p,sku,stage,token,event,reg,truck,dest):
        i,pos=truck
        src=self.location.get(p)
        origin=self.addr[src].distance if src else 0.
        travel=abs(pos-origin)
        target=self.addr[dest].distance
        start=self.e.now
        yield self.e.timeout(travel)
        self.record('pickup',c,p,source=src or 'BUFFER',destination=dest,vehicle=i)
        if src:self.free(src)
        else:
            self.in_buffer.remove(p)
            self.buffer.release(token)
            self.e.process(self.balance_event(p,False))
        yield self.e.timeout(abs(target-origin)+180)
        self.location[p]=dest
        self.emit(c,stage,reg,start,'machine',p,machine=self.e.now-start,source=src or 'BUFFER',address=dest,vehicle=i,sku=sku)
        self.record('deposit',c,p,address=dest,stage=stage,sku=sku)
        self.fork.put((i,target))
        event.succeed(dest)
        self.signal()
    def pallet(self,c,raw,req):
        p=raw['pallet'];q=raw['q'];sku=raw['sku']
        token=yield req
        yield from self.op(c,16,p,auto=self.mod(3),quantity=1)
        if not self.quality_known[q].triggered or self.registered_quality.get(q)=='failed':
            yield self.submit(c,p,sku,10,token)
        else:yield from self.op(c,10,p,skip='released')
        yield self.quality_known[q]
        if self.registered_quality[q]=='failed':
            self.record('open_nonconformity',c,p,quality=q)
            yield self.waitforever
        yield from self.op(c,17,p,auto=self.mod(4))
        address=yield self.submit(c,p,sku,18,token)
        yield self.network()
        yield from self.op(c,19,p,duration=12,address=address)
        yield from self.op(c,20,p,auto=self.mod(3),address=address)
        if raw['placement_mismatch']:
            yield from self.op(c,21,p,result_detail='address_readback_discrepancy')
            yield from self.op(c,20,p,address=address,result_detail='corrected_readback')
        else:
            yield from self.op(c,21,p,skip='address_verified')
        self.stock[sku].append(address)
    def receipt(self,case):
        c=case['case']
        yield self.at(case['ready'])
        raws=[self.x['pallets'][p] for p in case['members']]
        scans=[self.e.process(self.identify(c,r)) for r in raws]
        yield from self.op(c,1,auto=self.mod(1))
        yield self.network()
        for r in raws:
            if r['q'] not in self.quality_known:
                self.quality_known[r['q']]=self.e.process(self.quality(c,r))
            else:self.record('shared_quality_link',c,r['pallet'],quality=r['q'])
        yield from self.op(c,3)
        if case['defect']:yield from self.op(c,3,duration=30,result_detail='document_clarification')
        for n in (4,5,12,13,14):yield from self.op(c,n,auto=self.mod(3) and n in FLOW)
        yield from self.op(c,15,skip=None if case['defect'] else 'valid')
        if case['defect']:yield from self.op(c,14,auto=self.mod(3))
        yield self.e.all_of([self.e.process(self.pallet(c,r,req)) for r,req in zip(raws,scans)])
        for n in (22,23,24,25):
            if n==24:
                yield self.network()
                self.record('exchange_ack',c,version=1)
            yield from self.op(c,n,auto=self.mod(3) and n in FLOW)
        self.closed[c]=self.e.now
    def outgoing(self):
        for t,sku in self.x['departures']:
            yield self.at(t)
            if self.stock[sku]:
                name=self.stock[sku].popleft()
                self.record('departure',address=name,sku=sku)
                self.free(name)
    def run(self):
        self.dest.mkdir(parents=True,exist_ok=True)
        for r in self.x['pallets'].values():
            t=r['ready']+(86400 if self.constrained else 7200) if r['held'] else r['ready']
            self.qready[r['q']]=min(self.qready.get(r['q'],t),t)
        with gzip.open(self.dest/'events.jsonl.gz','wt',encoding='utf8',compresslevel=1) as self.stream:
            self.record('initial_stock',stock={k:list(v) for k,v in self.stock.items()})
            self.e.process(self.dispatch());self.e.process(self.forecasts());self.e.process(self.outgoing())
            if self.mod(6):self.e.process(self.device())
            for c in self.x['receipts']:self.e.process(self.receipt(c))
            self.e.run(until=self.end)
        for c in self.x['receipts']:
            t=c['ready'];k=c['case']
            if not DAY<=t<31*DAY:continue
            success=self.closed.get(k)
            elapsed=success-t if success is not None else float('inf')
            self.summary.append(dict(case=k,t0=t,last_ready=c['last_ready'],members=len(c['members']),t25=success,
                success72=elapsed<=3*DAY,T72_min=min(3*DAY,elapsed)/60,**self.totals[k]))
        df=pd.DataFrame(self.summary)
        df.to_parquet(self.dest/'cases.parquet',index=False)
        return dict(input_sha256=digest(self.x),mask=self.mask,cases=len(df),standard72=int(df.success72.sum()),
                    mean_T72_min=df.T72_min.mean(),observed_source_days=self.x['days'],**self.counts)

