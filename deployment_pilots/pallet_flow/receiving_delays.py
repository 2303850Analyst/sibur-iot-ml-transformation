"""Paired stress tests of the filling-to-buffer trip and single receiving position."""
import argparse
import gc
import json
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import simpy

import zhengdu_options as options

trial = options.trial
HOUR, DAY = trial.HOUR, trial.DAY
FAULT = 15 * DAY + 8 * HOUR


class Feeding:
    def setup_feed(self, cycle, extra, staffing, outage):
        self.cycle, self.extra_scan, self.staffing = cycle, extra, staffing
        self.outage = outage
        self.feed_queue = deque()
        self.feed_times, self.pads = {}, {}
        self.pad = simpy.Resource(self.e, 1)
        self.parked = {}
        self.feeding = set()
        self.ready_feed = set()
        self.feed_streak = 0
        self.cross_mode = 'rack'
        if staffing == 'provided':
            self.ready_feed = {2}
        elif staffing != 'robot':
            self.slots = None
            self.ready_feed = set(range(1 if staffing in ('two', 'three', 'flex') else 3))
            if staffing == 'flex':
                self.parked[1] = self.cars.pop(1)[1]
                self.e.process(self.shifts())
            if staffing in ('cross','cross2'):
                self.ready_feed.clear()
                self.e.process(self.shifts())
        elif outage:
            self.e.process(self.fault_clock())

    def weekday_extra(self):
        # Day 1, first day of the measured period, is a Monday.
        return (int(self.e.now // DAY) - 1) % 7 < 5 and 8 <= self.e.now % DAY / HOUR < 17

    def shifts(self):
        while True:
            yield self.e.timeout(HOUR - self.e.now % HOUR)
            if self.staffing in ('cross','cross2'):
                for car in (8,9):
                    if self.staffing == 'cross' and self.weekday_extra() and car not in self.feeding:
                        self.ready_feed.add(car)
                    else:
                        self.ready_feed.discard(car)
                self.signal()
                continue
            if self.weekday_extra():
                if 1 not in self.feeding:
                    self.ready_feed.add(1)
                if 1 in self.parked:
                    self.idle(1, self.parked.pop(1))
            else:
                self.ready_feed.discard(1)
            self.signal()

    def fault_clock(self):
        yield self.at(FAULT)
        if self.cover_fault:
            self.ready_feed.add(9)
        self.signal()
        yield self.at(FAULT + self.outage * HOUR)
        self.ready_feed.discard(9)
        if 0 in self.parked:
            self.idle(0, self.parked.pop(0))
        self.signal()

    def take_car(self, car):
        _, pos = self.cars.pop(car)
        if car in self.returning:
            start, origin = self.returning.pop(car)
            pos = max(0., origin - (self.e.now - start) * self.travel_speed)
        return pos

    def signal(self):
        if hasattr(self, 'feed_queue'):
            if self.staffing == 'flex' and not self.weekday_extra() and 1 in self.cars:
                self.parked[1] = self.take_car(1)
            if self.staffing in ('cross','cross2'):
                while self.feed_queue and self.ready_feed:
                    self.start_feed(self.ready_feed.pop(), 0.)
                if self.feed_queue and 0 in self.cars and not self.rack_turn():
                    self.start_feed(0, self.take_car(0))
            elif self.staffing == 'robot':
                if 0 in self.cars and FAULT <= self.e.now < FAULT + self.outage * HOUR:
                    self.parked[0] = self.take_car(0)
                if self.feed_queue and 0 in self.cars and not self.rack_turn():
                    self.start_feed(0, self.take_car(0))
                if self.feed_queue and 9 in self.ready_feed:
                    self.ready_feed.remove(9)
                    self.start_feed(9, 0.)
            else:
                while self.feed_queue and self.ready_feed:
                    self.start_feed(self.ready_feed.pop(), 0.)
        super().signal()

    def rack_turn(self):
        if not self.feed_limit or self.feed_streak < self.feed_limit:
            return False
        return any(self.pools[2 if stage == 10 else sku // 10] and
                   (p in self.location or entered.triggered)
                   for p, (_, _, sku, stage, entered, _, _) in self.jobs.items())

    def transfer(self, case, pallet, stage, entry, event, car, dest, pos, origin):
        if car == 0:
            self.feed_streak = 0
            if self.staffing in ('cross','cross2') and self.cross_mode == 'feed':
                yield self.e.timeout(60.)
                self.cross_mode = 'rack'
        yield from super().transfer(case, pallet, stage, entry, event, car, dest, pos, origin)

    def start_feed(self, car, pos):
        pallet, event = self.feed_queue.popleft()
        assert car not in self.feeding
        self.feeding.add(car)
        if car == 0:
            self.feed_streak += 1
        self.e.process(self.deliver(car, pos, pallet, event))

    def deliver(self, car, pos, pallet, event):
        start = self.e.now
        if self.staffing in ('cross','cross2') and car == 0 and self.cross_mode == 'rack':
            yield self.e.timeout(60.)
            self.cross_mode = 'feed'
        yield self.e.timeout(self.cycle + pos / self.travel_speed)
        end_travel = self.e.now
        pad = self.pad.request()
        yield pad
        self.pads[pallet] = pad
        self.feed_times[pallet] = (start, end_travel, self.e.now)
        self.feeding.remove(car)
        event.succeed()
        if self.staffing in ('cross','cross2'):
            if car == 0:
                self.idle(0,0.)
            elif self.weekday_extra():
                self.ready_feed.add(car)
        elif self.staffing == 'robot' and car == 9:
            if self.e.now < FAULT + self.outage * HOUR:
                self.ready_feed.add(9)
        elif self.staffing == 'robot':
            self.idle(car, 0.)
        elif self.staffing != 'flex' or car == 0 or self.weekday_extra():
            self.ready_feed.add(car)
        self.signal()

    def unit(self, case, raw, document):
        if self.cycle:
            yield self.at(raw['ready'])
            event = self.e.event()
            self.feed_queue.append((raw['pallet'], event))
            self.signal()
            yield event
        yield from super().unit(case, raw, document)

    def admit(self, case, pallet, boundary):
        token = yield from super().admit(case, pallet, boundary)
        if self.cycle:
            self.pad.release(self.pads.pop(pallet))
        return token

    def scan(self, raw):
        read, fallback, how = super().scan(raw)
        if not self.gate:
            fallback += self.extra_scan
        return read, fallback, how


class Manual(Feeding, options.Driven):
    pass


class Automated(Feeding, options.Reserve):
    pass


def metrics(m, r, seed, key):
    groups = {g['case']: g for g in m.x['receipts'] if g['case'] in m.cohort}
    goods = {p for g in groups.values() for p in g['members'] if not m.x['pallets'][p]['failed']}
    visits = defaultdict(dict)
    for v in m.visits:
        if v['branch'] == 'APPLIED' and v['op'] in (10, 18, 20):
            visits[v['pallet']][v['op']] = v
    ends = {p: steps[20]['finish'] for p, steps in visits.items() if 20 in steps and steps[20]['finish'] is not None}
    curves, stages, shifts, wait = [], [], defaultdict(list), []
    for c, g in groups.items():
        if c not in m.closed:
            continue
        p = max(g['members'], key=ends.__getitem__)
        raw = m.x['pallets'][p]
        hr = raw['ready']
        start, moved, dropped = m.feed_times.get(p, (hr, hr, hr))
        tag = m.tags[p]
        v = visits[p][18]
        alloc, finish = v['grant'], v['finish']
        trip = visits[p].get(10)
        qwait = trip['grant'] - trip['request'] if trip else 0.
        qtrip = trip['finish'] - trip['grant'] if trip else 0.
        otr = v['request'] - tag['registered'] - qwait - qtrip
        row = [hr-g['ready'], start-hr, moved-start, dropped-moved, tag['launch']-dropped,
               tag['registered']-tag['launch'], qwait, qtrip, otr, alloc-v['request'], finish-alloc,
               ends[p]-finish, m.closed[c]-ends[p]]
        total = m.closed[c]-g['ready']
        assert min(row) > -1e-7 and abs(sum(row)-total)<1e-6
        start_shift = int((g['ready']-trial.START-8*HOUR)//(12*HOUR))
        if 0 <= start_shift < 59:
            shifts[start_shift].append(total)
        curves.append((g['ready'],total))
        stages.append(row)
    for p in goods:
        for n in (10, 18):
            if n in visits[p]:
                v = visits[p][n]
                wait.append((v['grant'] if v['grant'] is not None else m.end)-v['request'])
    def summary(a):
        return dict(avg=float(np.mean(a)), p50=float(np.percentile(a,50)), p95=float(np.percentile(a,95)),
                    p99=float(np.percentile(a,99)), maximum=float(max(a))) if len(a) else {}
    times = [v for _,v in curves]
    def outstanding(t):
        return sum(not d['failed'] and d['ready']<t and ends.get(p,float('inf'))>=t
                   for p,d in m.x['pallets'].items())
    # Sample the same instants in the incident and matched no-incident run.
    ready_sorted = np.sort([d['ready'] for d in m.x['pallets'].values() if not d['failed']])
    end_sorted = np.sort([t for p,t in ends.items() if not m.x['pallets'][p]['failed']])
    grid = FAULT + np.arange(289) * 900
    points = (np.searchsorted(ready_sorted,grid)-np.searchsorted(end_sorted,grid)).tolist()
    assert points[0] == outstanding(FAULT)
    cohort_ends = [ends[p]-m.x['pallets'][p]['ready'] for p in goods if p in ends]
    allreq = [v for v in m.visits if v['case'] in groups and v['op'] in (10,18) and v['branch']=='APPLIED']
    grant30 = sum((v['grant'] or m.end)-v['request']<=60 for v in allreq)/len(allreq)
    return dict(scenario=key,seed=seed,offered=r['offered'],good=len(goods),eligible=r['eligible'],closed=len(curves),
                placed30=r['p20_in_30'],backlog=r['backlog'],p25=summary(times),p20=summary(cohort_ends),
                shifts=summary([np.mean(v) for v in shifts.values()]),wait=summary(wait),within60=grant30,
                p25_over60=sum(t>3600 for t in times),p25_over30=sum(t>1800 for t in times),
                stages=np.mean(stages,axis=0).tolist() if stages else [],
                tail_stages=np.mean(np.asarray(stages)[np.asarray(times)>=np.percentile(times,95)],axis=0).tolist() if stages else [],
                by_incident=summary([v for start,v in curves if FAULT<=start<FAULT+8*HOUR]),
                robot_driver_trips=getattr(m,'manual_trips',None),receiver=r['receiver'],peak=[r['peak_A'],r['peak_B']],
                points=points, checks=r['checks'])


# Type, cycle of the first trip, additional manual check, peak inspection, unavailable hours.
CASES = {
    'old': ('robot',0,0,False,0), 'old2': ('legacy',0,0,False,0),
    'two0': ('two',120,0,True,0), 'flex0': ('flex',120,0,True,0),
    'flex60': ('flex',120,60,True,0), 'flex120': ('flex',120,120,True,0),
    'five0': ('five',120,0,True,0), 'five120': ('five',120,120,True,0),
    'five180': ('five',120,180,True,0), 'five_long': ('five',180,120,True,0),
    'five185': ('five',120,185,True,0), 'five190': ('five',120,190,True,0),
    'five195': ('five',120,195,True,0), 'five210': ('five',120,210,True,0),
    'three120': ('three',120,120,True,0), 'three0': ('three',120,0,True,0),
    'cross120': ('cross',120,120,True,0), 'cross0': ('cross',120,0,True,0),
    'cross2': ('cross2',120,120,True,0),
    's60': ('robot',60,0,True,0), 's120': ('robot',120,0,True,0),
    's150': ('robot',150,0,True,0), 's180': ('robot',180,0,True,0),
    's240': ('robot',240,0,True,0), 's8h': ('robot',120,0,True,8),
    's8cover': ('robot',120,0,True,8),
    's_physical': ('robot',120,0,True,0), 'five_physical': ('five',120,0,True,0),
    'flex_physical': ('flex',120,0,True,0),
    'balance120': ('robot',120,0,True,0), 'balance150': ('robot',150,0,True,0),
    'balance180': ('robot',180,0,True,0), 'balance240': ('robot',240,0,True,0),
    'balance8h': ('robot',120,0,True,8), 'balance8cover': ('robot',120,0,True,8),
    'balance_physical': ('robot',120,0,True,0),
    'provided120': ('provided',120,0,True,0)
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cases',nargs='+',choices=CASES)
    ap.add_argument('--seed',type=int,required=True)
    ap.add_argument('--load',type=int,default=1,choices=(1,2,3))
    args=ap.parse_args()
    x=trial.manifest(args.seed,args.load)
    sim=trial.sim
    draws=sim.old.Inputs(x,sim.P.SEED,json.loads(sim.old.IO_FILE.read_bytes()))
    models,_=sim.old.compare.train_forecasters(x)
    ref=trial.options.Fleet(x,'ASIS',draws,models)
    ref.execute()
    decisions={a['quality']:(t,a['decision']) for k,t,c,p,a in ref.events if k=='quality_decision'}
    del ref
    gc.collect()
    src={p:(d['scan_factor'],d['check_factor']) for p,d in x['pallets'].items()}
    dst=Path(__file__).parent/'results'/'receiving_delays'
    dst.mkdir(exist_ok=True)
    for key in args.cases:
        staffing,cycle,extra,high,outage=CASES[key]
        for p,d in x['pallets'].items():
            d['scan_factor'],d['check_factor']=(1.3,1.3) if high else src[p]
            if key.endswith('_physical'):
                d['check_factor'] += 120. / sim.old.compare.P['physical_check_mode_s']
        is_robot=staffing in ('robot','provided')
        cls=Automated if is_robot else Manual
        size=1 if staffing in ('two','robot','provided') else 2
        m=cls(x,'RETURN' if is_robot else 'DATA_RETURN',draws,models,True,.9,100.,False,size,.8 if is_robot else 1.,
              charge_hours=1.,chargers=1)
        m.configure(False)
        m.cover_fault = key in ('s8cover', 'balance8cover')
        m.feed_limit = 3 if key.startswith(('balance','cross')) else 0
        m.setup_feed(cycle,extra,staffing,outage)
        for q,(t,state) in decisions.items():
            m.quality_known[q]=m.e.process(m.authorized(q,t,state))
        m.execute()
        r=trial.report(m,key,args.seed)
        res=metrics(m,r,args.seed,key)
        (dst/f'{320*args.load}-{key}-{args.seed}.json').write_text(json.dumps(res,allow_nan=False,default=lambda v:v.item()))
        print(key,res['closed'],res['backlog'][-1],res['p25'].get('avg'),flush=True)
        del m
        gc.collect()


if __name__=='__main__':
    main()
