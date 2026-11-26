"""Exhaustive count search on a finite Ex fleet and one serial receiving gate."""
import argparse
import gc
import itertools
import json
import multiprocessing as mp
import os
from collections import Counter
from pathlib import Path

import numpy as np

import receiving_delays as delay

trial = delay.trial
sim = trial.sim
OUT = Path(__file__).parent / 'results' / 'count_search'


def grid():
    for robots in range(7):
        for cross in range(3):
            for fork, rack in itertools.product(range(4-cross), range(3-cross)):
                if robots == 0 and not (min(fork, rack) or cross):
                    continue
                for method in (('SAP', 'IoT') if not robots else ('Ex',)):
                    yield (robots, fork, rack, cross, method)


class CountFleet(delay.Feeding, trial.Robot):
    def setup(self, config, cycle, outage):
        robots, fork, rack, cross, method = config
        self.configure(False)
        self.setup_feed(cycle, 120 if method == 'SAP' else 0, 'provided', 0)
        if method == 'SAP':
            self.slots = None
        self.dedicated = set(range(100,100+fork))
        self.ready_feed = self.dedicated.copy()
        if robots:
            workers = [-i-1 for i in range(rack+cross)]
            self.cars.update({i:(0.,0.) for i in workers})
        else:
            workers = list(range(rack+cross))
        self.cross_workers = set(workers[rack:])
        self.cross_last = {i:'rack' for i in self.cross_workers}
        self.scan_driver = delay.options.DrivenTrip(self)
        self.repeats = Counter()
        self.passengers = Counter()
        self.outage = outage
        if outage and robots:
            self.cover_fault = False
            self.e.process(self.fault_clock())

    def service(self, car):
        if self.robot_count:
            yield from super().service(car)
        else:
            yield self.waitforever

    def idle(self, car, position):
        if car < 0:
            self.cars[car] = (self.e.now, position)
        else:
            super().idle(car, position)

    def choose(self, jobs):
        if not self.robot_count or not any(c >= 0 for c in self.cars):
            return super().choose(jobs)
        workers = {c:pos for c,pos in self.cars.items() if c < 0}
        for car in workers:
            del self.cars[car]
        try:
            return super().choose(jobs)
        finally:
            self.cars.update(workers)

    def signal(self):
        if hasattr(self, 'repeats'):
            if self.outage and delay.FAULT <= self.e.now < delay.FAULT+self.outage*3600 and 0 in self.cars:
                self.parked[0] = self.take_car(0)
            while self.feed_queue and self.ready_feed:
                self.start_feed(self.ready_feed.pop(), 0.)
            if self.feed_queue:
                ready_rack = any(self.pools[2 if st == 10 else sku//10] and (p in self.location or entered.triggered)
                                 for p,(_,_,sku,st,entered,_,_) in self.jobs.items())
                transferable = sorted(c for c in self.cars if c in self.cross_workers or 0 <= c < self.robot_count)
                if args.reserve_rack:
                    transferable = [c for c in transferable if c != 0]
                # If a dedicated supply vehicle exists, lend a shared vehicle only to an overdue feed
                # when no rack assignment is ready. A shared vehicle otherwise alternates legs.
                for car in transferable:
                    if not self.feed_queue:
                        break
                    p = self.feed_queue[0][0]
                    if self.dedicated and (ready_rack or self.e.now-self.x['pallets'][p]['ready'] < self.cycle):
                        break
                    lane = self.lane[self.pallet_case[p]]
                    if ready_rack and (self.repeats[car] >= 3 or self.buffer.zones[lane].count >= 19):
                        continue
                    self.start_feed(car, self.take_car(car))
        # Feeding.signal() is the fixed one-robot dispatch; use the shared warehouse wake-up.
        trial.Robot.signal(self)

    def start_feed(self, car, pos):
        self.repeats[car] += 1
        super().start_feed(car,pos)

    def deliver(self, car, pos, p, event):
        start = self.e.now
        if car in self.cross_workers and self.cross_last[car] != 'feed':
            yield self.e.timeout(60.)
            self.cross_last[car] = 'feed'
        speed = .8 if 0 <= car < self.robot_count else 1.
        yield self.e.timeout(self.cycle+pos/speed)
        arrived = self.e.now
        pad = self.pad.request()
        yield pad
        self.pads[p] = pad
        self.feed_times[p] = (start, arrived, self.e.now)
        self.feeding.remove(car)
        event.succeed()
        if car in self.dedicated:
            self.ready_feed.add(car)
        else:
            self.idle(car,0.)
        self.signal()

    def transfer(self, c, p, st, entry, event, car, dest, pos, origin):
        self.repeats[car] = 0
        if car in self.cross_workers and self.cross_last[car] != 'rack':
            yield self.e.timeout(60.)
            self.cross_last[car] = 'rack'
        if trial.START <= self.e.now < trial.END:
            self.passengers['auto' if 0 <= car < self.robot_count else 'driver'] += 1
        handler = self.scan_driver if car < 0 else self
        yield from trial.options.Fleet.transfer(handler,c,p,st,entry,event,car,dest,pos,origin)

    def step(self, c, n, seconds, p=''):
        if n == 19 and self.held_truck.get(p,(0,))[0] < 0:
            seconds += 25*self.draws.factors[19][0,self.draws.p_index[p]]
        yield from super().step(c,n,seconds,p)

    def handover_reject(self,c,p,address,car,pos):
        if car in self.cross_workers and self.cross_last[car] != 'rack':
            yield self.e.timeout(60.)
            self.cross_last[car] = 'rack'
        yield from trial.options.Fleet.handover_reject(self.scan_driver if car < 0 else self,c,p,address,car,pos)


def setup_input(seed, multiple):
    x=trial.manifest(seed,multiple)
    draws=sim.old.Inputs(x,sim.P.SEED,json.loads(sim.old.IO_FILE.read_bytes()))
    models,_=sim.old.compare.train_forecasters(x)
    manual=trial.options.Fleet(x,'ASIS',draws,models)
    manual.execute()
    decisions={a['quality']:(t,a['decision']) for k,t,c,p,a in manual.events if k=='quality_decision'}
    for v in x['pallets'].values():
        v.update(scan_factor=1.3,check_factor=1.3)
    del manual
    gc.collect()
    return x,draws,models,decisions


def run(cfg):
    robots,fork,rack,cross,method=cfg
    tag=f'{method}-{robots}-{fork}-{rack}-{cross}'
    suffix = '_rack' if args.reserve_rack else ''
    path=OUT/f'{args.load*320}_{args.seed}_{args.cycle}_{args.outage}_{tag}{suffix}.json'
    if path.exists() and (not args.details or 'incident_minutes' in json.loads(path.read_bytes())):
        return
    x,draws,models,decisions=context
    mode='RETURN' if robots else 'GATE_RETURN' if method=='IoT' else 'DATA_RETURN'
    m=CountFleet.__new__(CountFleet)
    m.robot_count=robots
    m.__init__(x,mode,draws,models,True,.9,100.,False,robots or rack+cross,.8 if robots else 1.,
               charge_hours=1.,chargers=1)
    m.pallet_case={p:g['case'] for g in x['receipts'] for p in g['members']}
    m.setup(cfg,args.cycle,args.outage)
    for q,(t,state) in decisions.items():
        m.quality_known[q]=m.e.process(m.authorized(q,t,state))
    m.execute()
    # Common ledger, capacity, quality and receiver exclusivity checks of the established stand.
    r=trial.report(m,tag,args.seed)
    durations=[m.closed[g['case']]-g['ready'] for g in x['receipts'] if g['case'] in m.cohort and g['case'] in m.closed]
    assert r['closed_all']==len(durations)
    assert len(m.feeding) == len(set(m.feeding)) and not set(m.cars)&m.feeding
    p95=float(np.percentile(durations,95)) if durations else None
    result=dict(tag=tag,robots=robots,fork=fork,rack=rack,cross=cross,method=method,load=320*args.load,
                seed=args.seed,cycle=args.cycle,outage=args.outage,eligible=r['eligible'],closed=r['closed_all'],
                good=r['eligible_pallets'],placed=r['p20_in_30'],backlog=r['backlog'],mean=r['delay_all'],p95=p95,
                over60=sum(t>3600 for t in durations),within60=r['onemin'],receiver=r['receiver'],
                checks=r['checks'],rack_auto=m.passengers['auto'],rack_driven=m.passengers['driver'])
    result['reserve_rack'] = args.reserve_rack
    if args.details:
        breakdown = delay.metrics(m, r, args.seed, tag)
        result.update(incident_minutes=breakdown['by_incident']['avg']/60,
                      start_incident=breakdown['points'][0], end_incident=breakdown['points'][32],
                      stages=breakdown['stages'], tail_stages=breakdown['tail_stages'])
    tmp=path.with_suffix(f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(result,default=lambda v:v.item(),allow_nan=False))
    tmp.replace(path)


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--seed',type=int,default=241126)
    ap.add_argument('--load',type=int,default=1)
    ap.add_argument('--cycle',type=int,default=120)
    ap.add_argument('--outage',type=int,default=0)
    ap.add_argument('--details',action='store_true')
    ap.add_argument('--reserve-rack',action='store_true')
    ap.add_argument('--workers',type=int,default=4)
    ap.add_argument('--tags',nargs='+')
    ap.add_argument('--min-robots',type=int,default=0)
    args=ap.parse_args()
    all_configs=[cfg for cfg in grid() if cfg[0] >= args.min_robots]
    if args.tags:
        all_configs=[cfg for cfg in all_configs if (cfg[-1]+'-'+'-'.join(map(str,cfg[:4]))) in args.tags]
        assert len(all_configs)==len(set(args.tags))
    if args.reserve_rack:
        assert all(r > 0 and r+f+x >= 2 for r,f,s,x,_ in all_configs)
    OUT.mkdir(exist_ok=True)
    context=setup_input(args.seed,args.load)
    with mp.get_context('fork').Pool(args.workers,maxtasksperchild=10) as pool:
        for _ in pool.imap_unordered(run,all_configs):
            pass
    print(f'Completed {len(all_configs)} fleet configurations',flush=True)
