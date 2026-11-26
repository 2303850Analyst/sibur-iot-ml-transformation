"""Matched PSV receipts with mandatory pallet-level QC and a shared finite vehicle fleet."""
import argparse
import json
import multiprocessing as mp
from collections import Counter, defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import simpy
import count_search as cs

trial, sim = cs.trial, cs.sim
DAY, START, END = trial.DAY, trial.START, trial.END
OUT = Path(__file__).parent / 'results' / 'per_pallet_quality'


class FullInspection:
    def set_quality(self, seats):
        self.lab = simpy.Resource(self.e, capacity=seats)
        self.lab_log = {}
        self.doc_owner = {}
        self.inbound_reservations = Counter()
        for p in self.x['pallets']:
            self.quality_known[p] = self.e.event()

    def signal(self):
        # Commit a physical receiving slot before sending any vehicle to collect a pallet.
        # Otherwise the last two vehicles can be holding inbound loads while the buffer
        # requires a vehicle to remove a pallet. A reservation lasts until buffer entry.
        if not hasattr(self, 'inbound_reservations') or not self.feed_queue:
            return super().signal()
        queue=self.feed_queue
        subset=deque()
        count=self.inbound_reservations.copy()
        limit=len(self.cars)+len(self.ready_feed)
        while queue and len(subset)<limit:
            p=queue[0][0]
            lane=self.lane[self.pallet_case[p]]
            z=self.buffer.zones[lane]
            if count[lane]+z.count>=z.capacity:
                break
            count[lane]+=1
            subset.append(queue.popleft())
        self.feed_queue=subset
        try:
            super().signal()
        finally:
            queue.extendleft(reversed(subset))
            self.feed_queue=queue

    def start_feed(self, car, pos):
        p=self.feed_queue[0][0]
        self.inbound_reservations[self.lane[self.pallet_case[p]]] += 1
        super().start_feed(car,pos)

    def admit(self,c,p,boundary):
        token=yield from super().admit(c,p,boundary)
        lane=self.lane[c]
        self.inbound_reservations[lane]-=1
        assert self.inbound_reservations[lane]>=0
        self.signal()
        return token

    def scan(self,raw):
        machine,fallback,route=super().scan(raw)
        if self.robot_count:
            return machine,fallback,route
        p=raw['pallet']
        c=self.pallet_case[p]
        first=self.doc_owner.setdefault(c,p)
        # Model a clean receipt as 120 s per document and 156 s per pallet;
        # keep the existing excess of damaged-label re-reading over clean labels.
        check=sim.old.compare.P['physical_check_mode_s']*raw['check_factor']
        nominal_read=sim.old.compare.P['tsd_normal_mode_s']*raw['scan_factor']
        return 0.,156.-30.-check+(120. if p==first else 0.)+max(0.,fallback-nominal_read),route

    def inspect(self,c,p):
        arrive=self.e.now
        with self.lab.request() as ticket:
            yield ticket
            start=self.e.now
            yield self.e.timeout(600.)
            finish=self.e.now
        self.lab_log[p]=(arrive,start,finish)
        state='failed' if self.x['pallets'][p]['failed'] else 'released'
        self.record('quality_decision',c,p,quality=p,authority='quality',decision=state)
        self.registered_quality[p]=state
        self.record('quality_delivered',c,p,quality=p,decision=state)
        self.quality_known[p].succeed(state)

    def transfer(self,c,p,st,entry,event,car,dest,pos,origin):
        if st!=10:
            yield from super().transfer(c,p,st,entry,event,car,dest,pos,origin)
            return
        # P10 is mandatory under this scenario. Preserve the existing driver's
        # machine-change rule and the ordinary competition for jobs and addresses.
        if self.robot_count:
            self.repeats[car]=0
            if car in self.cross_workers and self.cross_last[car]!='rack':
                yield self.e.timeout(60.)
                self.cross_last[car]='rack'
        elif car==0:
            self.feed_streak=0
            if self.staffing=='cross' and self.cross_mode=='feed':
                yield self.e.timeout(60.)
                self.cross_mode='rack'
        visit=self.flights.pop(p)
        start=visit['grant']=self.e.now
        empty=abs(pos-origin)
        loaded=abs(self.addr[dest].distance-origin)
        # Five minutes from dispatch-ready vehicle to drop, including approach.
        # Split the time at pickup by the stand's distance and handling proportions.
        work=100. if 0<=car<self.robot_count else 180.
        speed=.8 if 0<=car<self.robot_count else 1.
        approach=300.*empty/(empty+loaded+speed*work)
        yield self.e.timeout(approach)
        assert entry.triggered
        token=yield entry
        self.record('pickup',c,p,source='BUFFER',destination=dest,vehicle=car)
        self.in_buffer.remove(p)
        self.buffer.release(token)
        self.e.process(self.balance_event(p,False))
        yield self.at(start+300.)
        self.location[p]=dest
        self.record('deposit',c,p,address=dest,stage=10,sku=self.x['pallets'][p]['sku'])
        visit.update(finish=self.e.now,address=dest,empty_m=empty,loaded_m=loaded)
        assert abs(visit['finish']-visit['grant']-300.)<1.e-6
        self.idle(car,self.addr[dest].distance)
        event.succeed(dest)
        self.e.process(self.inspect(c,p))
        self.signal()


class Manual(FullInspection,cs.delay.Manual):
    pass


class Robotic(FullInspection,cs.CountFleet):
    pass


def run(task):
    kind,seats,seed,perday,isolated=task
    if perday>320 and perday%320:
        raise ValueError('Higher loads must be integer multiples of the 320-pallet reference')
    x=trial.manifest(seed,max(1,perday//320))
    if perday<320:
        # Keep 64 donor slots/day in the full reference, select an evenly spaced
        # subset of groups and redistribute their starts across the entire day.
        num=perday//5
        assert perday%5==0 and num<=64
        members=set()
        receipts=[]
        departures=[]
        for g in x['receipts']:
            d,j=map(int,g['case'][1:].split('-'))
            if j>=num:
                continue
            delta=d*DAY+j*DAY/num-g['ready']
            g['ready']+=delta;g['last_ready']+=delta
            receipts.append(g)
            for p in g['members']:
                members.add(p)
                raw=x['pallets'][p]
                raw['ready']+=delta;raw['arrival']+=delta
                departures.append((raw['ready']+7200,raw['sku']))
        x.update(receipts=receipts,pallets={p:raw for p,raw in x['pallets'].items() if p in members},departures=sorted(departures))
    draws=sim.old.Inputs(x,sim.P.SEED,json.loads(sim.old.IO_FILE.read_bytes()))
    models,_=sim.old.compare.train_forecasters(x)
    if isolated:
        # Fixed clean test receipt; vary its number of pallets alone. Keep SSCC,
        # lot, lane, ordinary scan factors and I/O samples from the same donor.
        group=next(g for g in x['receipts'] if g['case']=='D01-024')
        group['members']=group['members'][:isolated]
        group['last_ready']=x['pallets'][group['members'][-1]]['ready']
        x['pallets']={p:x['pallets'][p] for p in group['members']}
        x['receipts']=[group]
        x['departures']=[]
        for raw in x['pallets'].values():
            raw.update(label_bad=False,failed=False,bypass=1.,read1=0.,decode_type=.9)
        draws.linked['case'][draws.c_index[group['case']]]=0.
    for p,raw in x['pallets'].items():
        raw.update(q=p,scan_factor=1.3,check_factor=1.3)
    machine=kind if kind in (1,2) else 0
    cls=Robotic if machine else Manual
    m=cls.__new__(cls)
    m.robot_count=machine
    m.pallet_case={p:g['case'] for g in x['receipts'] for p in g['members']}
    mode='RETURN' if machine else 'DATA_RETURN'
    m.__init__(x,mode,draws,models,True,.9,100.,False,machine or 2,.8 if machine else 1.,charge_hours=1.,chargers=1)
    if machine:
        m.setup((machine,0,0,1,'Ex'),120,0)
    else:
        m.configure(False)
        m.feed_limit=3
        m.setup_feed(120,0,'cross',0)
    m.set_quality(seats)
    m.execute()
    r=trial.report(m,'QC',seed)
    stages=defaultdict(dict)
    for v in m.visits:
        if v['branch']=='APPLIED' and v['op'] in (10,18,20) and v['finish'] is not None and v['finish']<=m.end:
            stages[v['pallet']][v['op']]=v
    out=[]
    terms=[]
    group_times=[]
    for g in x['receipts']:
        cid=g['case'];start=g['ready']
        if cid not in m.cohort:continue
        eligible=not any(x['pallets'][p]['failed'] for p in g['members'])
        if cid not in m.closed:
            out.append([cid,start,None,eligible,None])
            continue
        assert eligible
        p=max(g['members'],key=lambda p:stages[p][20]['finish'])
        tr=stages[p]; tags=m.tags[p]
        feed,arrived,dropped=m.feed_times[p]
        q=tr[10];rack=tr[18]
        qt,qs,qe=m.lab_log[p]
        assert 0<=abs(qt-q['finish'])<1.e-6
        stamps=[start,x['pallets'][p]['ready'],feed,arrived,dropped,tags['launch'],tags['registered'],
                q['grant'],q['finish'],qs,qe,rack['request'],rack['grant'],rack['finish'],tr[20]['finish'],m.closed[cid]]
        segments=np.diff(stamps)
        assert min(segments)>-1.e-6 and abs(segments.sum()-(m.closed[cid]-start))<1.e-5
        assert abs(segments[9]-600.)<1.e-6
        terms.append(segments)
        ps=g['members']
        group_times.append([sum(m.tags[u]['identification_s'] for u in ps),
                            max(m.tags[u]['registered'] for u in ps)-min(m.feed_times[u][2] for u in ps),
                            max(m.lab_log[u][2] for u in ps)-min(m.lab_log[u][1] for u in ps)])
        out.append([cid,start,m.closed[cid],eligible,segments.tolist()])
    valid=[v for v in out if v[3]]
    finished=[v[2]-v[1] for v in valid if v[2] is not None]
    assert r['closed_all']==len(finished) and r['eligible']==len(valid)
    placed={p:s[20]['finish'] for p,s in stages.items() if 20 in s}
    ready=np.sort([v['ready'] for v in x['pallets'].values() if not v['failed']])
    ends=np.sort([t for p,t in placed.items() if not x['pallets'][p]['failed']])
    tt=np.arange(START,END+1,900)
    backlog=(np.searchsorted(ready,tt)-np.searchsorted(ends,tt)).tolist()
    assert backlog[-1]==r['backlog'][-1]
    # True QC utilisation and requests waiting at each quarterly-hour instant.
    starts=np.array([s for _,s,_ in m.lab_log.values()])
    qends=np.array([e for _,_,e in m.lab_log.values()])
    qu=np.maximum(0,np.minimum(qends,END)-np.maximum(starts,START)).sum()/(END-START)/seats
    assert qu<=1+1e-8
    summary=dict(kind=kind,places=seats,seed=seed,load=perday,eligible=len(valid),closed=len(finished),closed30=r['closed_in_30'],good=r['eligible_pallets'],placed30=r['p20_in_30'],backlog=r['backlog'],late=len(valid)-sum(v<=3600 for v in finished),mean=float(np.mean(finished)) if finished else None,p95=float(np.percentile(finished,95)) if finished else None,mean_stages=np.mean(terms,axis=0).tolist() if terms else None,receiver=r['receiver'],qcutil=qu,checks=r['checks'])
    summary['stable']=(summary['closed']==summary['eligible'] and summary['placed30']>=.995*summary['good'] and max(backlog[-1]-backlog[0],backlog[-1]-backlog[15*96])<=np.ceil(perday/24.))
    summary.update(isolated=isolated,group_times=np.mean(group_times,axis=0).tolist() if group_times else None)
    if isolated:
        group=x['receipts'][0]
        summary['example']={'start':group['ready'],'close':m.closed[group['case']],'pallets':[]}
        for p in group['members']:
            q=stages[p][10];rack=stages[p][18]
            summary['example']['pallets'].append([x['pallets'][p]['ready'],m.feed_times[p][0],
                m.feed_times[p][2],m.tags[p]['launch'],m.tags[p]['registered'],q['grant'],q['finish'],
                m.lab_log[p][1],m.lab_log[p][2],rack['grant'],stages[p][20]['finish']])
    if seed==241126 and perday==320 and seats==3 and not isolated:
        summary.update(receipts=out,quarter_backlog=backlog)
    path=OUT/(f'size-{isolated}-{kind}.json' if isolated else f'{perday}-{seed}-{kind}-{seats}.json')
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(summary,ensure_ascii=False,allow_nan=False,default=lambda val:val.item()))
    tmp.replace(path)
    print(kind,seats,seed,perday,r['closed_all'],r['eligible'],r['backlog'][-1],summary['stable'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--places',nargs='+',type=int,default=[3])
    parser.add_argument('--seeds',nargs='+',type=int,default=[241126])
    parser.add_argument('--loads',nargs='+',type=int,default=[320])
    parser.add_argument('--kinds',nargs='+',type=int,default=[0,1,2])
    parser.add_argument('--workers',type=int,default=3)
    parser.add_argument('--isolated',nargs='+',type=int,default=[0],choices=[0,1,2,3,4,5])
    arg=parser.parse_args()
    cs.args=SimpleNamespace(reserve_rack=False)
    OUT.mkdir(exist_ok=True)
    if any(arg.isolated) and (arg.seeds!=[241126] or arg.places!=[3] or arg.loads!=[320]):
        parser.error('Isolated receipts use the common first seed and three QC places.')
    tasks=[(k,s,seed,d,n) for seed in arg.seeds for s in arg.places for d in arg.loads for k in arg.kinds for n in arg.isolated]
    with mp.get_context('fork').Pool(arg.workers,maxtasksperchild=4) as pool:
        pool.map(run,tasks,chunksize=1)

