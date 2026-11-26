from common import *
from crew import Crew
from process import Process,DAY
from inputs import rng
import simpy,numpy as np
from collections import Counter,defaultdict
from functools import lru_cache
IO=json.loads((HOME/'io_profile.json').read_bytes())
class Engine(Process):
    def __init__(self,x,mask):
        super().__init__(x,mask,DEST)
        self.crew=Crew(self.e);self.visits=[];self.events=[];self.visit_counts=Counter()
        self.p19={};self.flights={};self.decisions=[];self.forecast_truth=Counter()
        self.events_count=0
    def record(self,kind,c='',p='',**fields):
        self.events_count+=1
        self.events.append((kind,self.e.now,c,p,fields))
    def new(self,c,n,p,role,**fields):
        k=(c,n,p,fields.get('quality',''));self.visit_counts[k]+=1
        v=dict(id=len(self.visits)+1,case=c,op=n,pallet=p,role=role,ready=self.e.now,request=self.e.now,grant=None,
               service_start=None,finish=None,worker=None,truck=None,base=0.,fatigue=1.,switch=0.,manual_start=None,manual_end=None,
               machine_start=None,machine_end=None,reserve_start=None,reserve_end=None,branch='APPLIED',attempt=self.visit_counts[k],**fields)
        self.visits.append(v);return v
    def draw(self,v,mode,purpose):
        entity=v.get('quality') or v['pallet'] or v['case']
        return rng(self.x['seed'],entity,v['op'],v['attempt'],purpose).triangular(.7*mode,1.3*mode,mode)
    def op(self,c,n,p='',auto=False,skip=None,duration=None,**extra):
        spec=ops[n];role=spec['primary_resource'];v=self.new(c,n,p,role,**extra)
        if n==2 and 'raw_arrival' in extra:
            v['ready']=extra['raw_arrival']
        if skip:
            v.update(grant=self.e.now,service_start=self.e.now,finish=self.e.now,branch='NA:'+skip);return
        corrected=extra.get('result_detail')=='document_clarification'
        nominal=spec['manual_labor_s'];ret=nominal;system=0.
        rule=settings['retained_manual_modes_s'].get(f'P{n:02d}')
        if rule:
            switch,retained=rule
            ret=retained
            if self.mod(switch):system={1:IO['production_seconds'],9:IO['quality_seconds']}.get(n,IO['http_seconds'])
        if n==2:
            ret=20;nominal=35
            if auto:system=IO['sensor_seconds']
            elif duration in (60,120):nominal=20+duration
        if corrected:nominal=ret=30;system=0.
        # One common triangular variate preserves the specified triangular law
        # for the composite baseline plus the identical retained primitive.
        factor=self.draw(v,1.,'manual')
        base=(ret if system else nominal)*factor
        t,end,wid,sw,fat=self.crew.reserve(role,self.e.now,base,c,n,v['id'],slot=self.p19[p][0] if n==19 else None)
        v.update(grant=t,service_start=t,manual_start=t,manual_end=end,base=base,worker=wid,switch=sw,fatigue=fat)
        if n==19:
            truck,pos,held_since=self.p19[p]
            v.update(truck=truck,reserve_start=held_since,reserve_end=end)
        if n==7:
            lab=self.draw(v,720.,'external_lab');v['external_lab']=lab
        else:lab=0.
        v['system_s']=system
        v['finish']=end+lab+system
        yield self.at(v['finish'])
        if n==19:
            self.p19.pop(p);self.fork.put((truck,pos));self.signal()
    def quality(self,c,raw):
        q=raw['q']
        yield from self.op(c,6,quality=q)
        yield from self.op(c,7,quality=q,skip=None if raw['held'] else 'prior_authorized_result')
        if raw['held']:yield self.at(self.qready[q])
        yield from self.op(c,8,quality=q)
        self.record('quality_decision',c,quality=q,authority='quality',decision='failed' if raw['failed'] else 'released')
        yield self.network()
        yield from self.op(c,9,quality=q)
        self.registered_quality[q]='failed' if raw['failed'] else 'released'
        self.record('quality_delivered',c,quality=q,decision=self.registered_quality[q])
        yield from self.op(c,11,skip=None if raw['failed'] else 'released',quality=q)
    def submit(self,c,p,sku,stage,token):
        self.flights[p]=self.new(c,stage,p,'forklift_driver')
        return super().submit(c,p,sku,stage,token)
    def move(self,c,p,sku,stage,token,event,reg,truck,dest):
        i,pos=truck;src=self.location.get(p);origin=self.addr[src].distance if src else 0.;target=self.addr[dest].distance
        v=self.flights.pop(p);physical=abs(pos-origin)+abs(target-origin)+180
        reserved=self.e.now
        t,end,wid,sw,fat=self.crew.reserve('forklift_driver',reserved,physical,c,stage,v['id'],slot=i,physical=True)
        v.update(grant=t,service_start=t,manual_start=t,manual_end=end,worker=wid,truck=i,base=physical,switch=sw,
                 machine_start=t+sw,machine_end=end,reserve_start=reserved,reserve_end=end,finish=end,address=dest)
        yield self.at(t+sw+abs(pos-origin))
        self.record('pickup',c,p,source=src or 'BUFFER',destination=dest,vehicle=i)
        if src:self.free(src)
        else:
            self.in_buffer.remove(p);self.buffer.release(token);self.e.process(self.balance_event(p,False))
        yield self.at(end)
        self.location[p]=dest;self.record('deposit',c,p,address=dest,stage=stage,sku=sku)
        if stage==18:self.p19[p]=(i,target,end)
        else:self.fork.put((i,target))
        event.succeed(dest);self.signal()
    def execute(self):
        for r in self.x['pallets'].values():
            t=r['ready']+((86400 if self.constrained else 7200) if r['held'] else 0)
            self.qready[r['q']]=min(self.qready.get(r['q'],t),t)
        self.record('initial_stock',stock={k:list(v) for k,v in self.stock.items()})
        self.e.process(self.dispatch());self.e.process(self.forecasts());self.e.process(self.outgoing())
        if self.mod(6):self.e.process(self.device())
        for c in self.x['receipts']:self.e.process(self.receipt(c))
        self.e.run(until=self.end)
        return self

