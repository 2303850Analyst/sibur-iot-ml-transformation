"""Run all five receiving options against the same simulated production manifest."""
import argparse
import gc
import json
from collections import defaultdict
from pathlib import Path

import simpy

import fleet_capacity as sim

DAY = 86400
Fleet = sim.model['Fleet']


class ShiftFleet(Fleet):
    """The flex truck finishes the current trip and parks when its coverage ends."""
    def __init__(self, *args, base, first, span, **kwargs):
        self.flex, self.first, self.span = base, first, span
        self.parked = False
        super().__init__(*args, fleet_size=base + 1, **kwargs)
        if not self.on():
            self.cars.pop(self.flex)
            self.parked = True
        self.e.process(self.switch())

    def on(self):
        return (self.e.now / 3600 - self.first) % 24 < self.span

    def take_off(self):
        _, pos = self.cars.pop(self.flex)
        if self.flex in self.returning:
            start, origin = self.returning.pop(self.flex)
            pos = max(0., origin - (self.e.now - start) * self.travel_speed)
        self.e.process(self.park(pos))

    def signal(self):
        if getattr(self, 'name', '') == 'ASIS' and not self.on() and self.flex in self.cars:
            self.take_off()
        super().signal()

    def switch(self):
        while True:
            yield self.e.timeout((int(self.e.now // 3600) + 1) * 3600 - self.e.now)
            if self.on() and self.parked:
                self.parked = False
                super().idle(self.flex, 0.)
                self.signal()
            elif not self.on() and self.flex in self.cars:
                self.take_off()

    def park(self, pos):
        yield self.e.timeout(pos / self.travel_speed)
        if self.on():
            super().idle(self.flex, 0.)
            self.signal()
        else:
            self.parked = True

    def idle(self, car, pos):
        if car == self.flex and not self.on():
            self.e.process(self.park(pos))
        else:
            super().idle(car, pos)


class Hybrid(sim.ChargingFleet):
    """Finite, mobile people. Robot hands off only when a driver arrives."""
    def __init__(self, *args, people, **kwargs):
        super().__init__(*args, **kwargs)
        self.workers = {i: 0. for i in range(people)}
        self.transfer_drivers = simpy.Resource(self.e, capacity=people)
        self.manual_events = []

    def service_stop(self, tag, cid, p, pos, seconds):
        ready = self.e.now
        with self.transfer_drivers.request() as req:
            yield req
            who = min(self.workers, key=lambda i: (abs(self.workers[i]-pos), i))
            dist = abs(self.workers.pop(who)-pos)
            yield self.e.timeout(dist)  # Human-driven approach at 1 m/s.
            arrived = self.e.now
            yield self.e.timeout(seconds)
            self.workers[who] = pos
            self.manual_events.append((tag, cid, p, arrived-ready, seconds+dist))

    def transfer(self, cid, p, stage, entry, event, car, dest, pos, origin):
        source = self.location.get(p)
        target = self.addr[dest].distance
        empty, loaded = abs(pos-origin), abs(target-origin)
        if cid in self.cohort:
            self.audit['job_empty_m'] += empty
        v = self.flights.pop(p)
        v['grant'] = self.e.now
        yield self.e.timeout(empty/self.travel_speed)
        if source is None:
            token = yield entry
        yield from self.service_stop('start', cid, p, origin, 30.)
        self.record('pickup',cid,p,source=source or 'BUFFER',destination=dest,vehicle=car)
        if source is None:
            self.in_buffer.remove(p)
            self.buffer.release(token)
            self.e.process(self.balance_event(p,False))
        else:
            self.free(source)
        yield self.e.timeout(self.robot_s + loaded/self.travel_speed)
        if stage == 18:
            # One checked handoff at destination. Keep the robot reserved until
            # the operator completes the placement and the address is acknowledged.
            yield from self.service_stop('rack', cid, p, target, 120.)
            self.held_truck[p] = (car,target)
        else:
            self.idle(car,target)
        self.location[p] = dest
        self.record('deposit',cid,p,address=dest,stage=stage,sku=self.x['pallets'][p]['sku'])
        v.update(finish=self.e.now,address=dest,empty_m=empty,loaded_m=loaded)
        event.succeed(dest)
        self.signal()


def pct(seq):
    return 100 * sum(t <= 60 for t in seq)/len(seq) if seq else None


def evaluate(args):
    x = sim.inputs(args.load, args.seed)
    draws = sim.old.Inputs(x, args.seed, json.loads(sim.old.IO_FILE.read_bytes()))
    models, train = sim.old.compare.train_forecasters(x)
    assert set(train).isdisjoint(x['days'])
    baseline = Fleet(x,'ASIS',draws,models)
    baseline.execute()
    ref, decisions = sim.model['summarize'](baseline,args.load)
    base_labor = sim.model['labor_profile'](baseline)
    del baseline
    gc.collect()
    out = []
    for config in args.config:
        case, n, extra = map(int,config.split('/'))
        name = ['ASIS','DATA_RETURN','GATE_RETURN','RETURN','RETURN'][case]
        if case < 3:
            m = ShiftFleet(x,name,draws,models,finite=True,base=n,first=2 if case==0 else 0,span=extra)
        else:
            klass = Hybrid if case==3 else sim.ChargingFleet
            kw = {'people':extra} if case==3 else {}
            m = klass(x,name,draws,models,True,.9,100.,False,n,.8,
                      charge_hours=1.,chargers=args.chargers,**kw)
        m.fixed_storage = args.fixed_storage
        if args.quarantine_reallocation:
            if not args.fixed_storage:
                raise ValueError('Reallocation requires the fixed physical map')
            sim.rezone_quarantine(m,args.quarantine_reallocation)
        if case:
            for q,(t,state) in decisions.items():
                m.quality_known[q] = m.e.process(m.authorized(q,t,state))
            for q in {p['q'] for p in x['pallets'].values()}-decisions.keys():
                m.quality_known[q] = m.e.event()
        m.execute()
        r, dec = sim.model['summarize'](m,args.load)
        assert not r['spare'] and (not case or dec == decisions)
        byday = defaultdict(list)
        for v in m.visits:
            if v['case'] in m.cohort and v['op'] in (10,18) and v['branch']=='APPLIED':
                wait=(v['grant'] if v['grant'] is not None else m.e.now)-v['request']
                byday[int((m.x['pallets'][v['pallet']]['ready']-sim.P.COHORT_FROM)//DAY)].append(wait)
        def flatten(a):return [v for subset in a for v in subset]
        w=flatten(byday.values());last=flatten(z for day,z in byday.items() if day>=15)
        rec=dict(load=args.load,seed=args.seed,case=case,n=n,extra=extra,chargers=args.chargers if case>=3 else 0,fixed=args.fixed_storage,reallocated=args.quarantine_reallocation,
                 month=pct(w),last=pct(last),worst=min(map(pct,byday.values())),
                 closed=r['closed'],eligible=ref['closed'],pallets=r['n'],jobs=len(w),p20=r['p20'],p25=r['doc'],
                 placed=r['placed'],quarantine=r['quarantine_trips'],checks=r['ledger_checks'],overflow=m.extra.total(),
                 charge_starts=getattr(m,'charge_starts',0))
        crew_ok=True
        if case==3:
            events=[v for v in m.manual_events if v[1] in m.cohort]
            extracted={k:[z for z in events if z[0]==k] for k in ['start','rack']}
            assert len(extracted['rack'])==r['placed']
            for key,rows in extracted.items():
                rec[key+'_60']=pct([z[3] for z in rows])
                rec[key+'_last']=pct([z[3] for z in rows if m.x['pallets'][z[2]]['ready']>=sim.P.COHORT_FROM+15*DAY])
                crew_ok=crew_ok and min(rec[key+'_60'],rec[key+'_last'])>=95
            rec['driver_hours']=sum(v[4] for v in events)/3600
            rec['approach_hours']=rec['driver_hours']-(30*len(extracted['start'])+120*len(extracted['rack']))/3600
        if args.load == 'L2' and (not args.fixed_storage or r['closed'] == ref['closed']):
            role_seconds = sim.model['labor_profile'](m,base_labor)['role_seconds']
            rec['roles'] = {k:v/3600 for k,v in role_seconds.items()}
            if case == 3:
                rec['roles']['forklift_driver'] = rec['driver_hours']
        rec['pass']=bool(rec['month']>=95 and rec['last']>=95 and rec['closed']==rec['eligible'] and crew_ok)
        out.append(rec); print(config,rec['load'],round(rec['month'],2),round(rec['last'],2),round(rec.get('rack_60',0),2),round(rec.get('start_60',0),2),rec['closed'],rec['pass'],flush=True)
        args.out.write_text(json.dumps(out,indent=2),encoding='utf8')
        del m;gc.collect()
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('load',choices=['L1','L2','L3']);parser.add_argument('out',type=Path)
    parser.add_argument('config',nargs='+',help='case/base/flexible hours or case/robots/people')
    parser.add_argument('--seed',type=int,default=sim.P.SEED);parser.add_argument('--chargers',type=int,default=1)
    parser.add_argument('--fixed-storage',action='store_true',help='No extra temporary addresses; no pickup of permanent rejects during the horizon.')
    parser.add_argument('--quarantine-reallocation',type=int,default=0)
    evaluate(parser.parse_args())

