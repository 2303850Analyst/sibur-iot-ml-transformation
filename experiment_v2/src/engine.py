import heapq
import json
import math
from collections import Counter

import numpy as np

from src.data import DAY
from src.forecast import features
from src.simulator import Simulation, WARM, STOP, END

CONFIG = {
    'A2': (False,1,1,False,'F0'), 'B2': (True,1,1,True,'F0'), 'C2': (True,1,1,True,'F1'),
    'BASE': (False,0,0,False,'F0'), 'FIFO': (False,1,0,False,'F0'), 'NEAR': (False,0,1,False,'F0'),
    'IGN': (True,1,1,False,'F0'), 'IDEAL': (True,1,1,True,'F2'), 'FAIL': (True,1,1,True,'FAIL')
}


def cell_class(cell):
    return 4 if cell >= 5200 else 2*(cell//2600) + int(cell % 2600 < 520)


def job_class(pallet):
    return 4 if pallet.quality != 'released' else 2*(pallet.sku//10) + int(pallet.source_row%5 == 0)


def allowed(kind):
    return (kind,) if kind in (1,3,4) else (kind,kind+1)


class Warehouse(Simulation):
    def __init__(self, incoming, outgoing, scenario, seed, variant, models, trace):
        automatic,self.dynamic,self.adaptive,self.use_sensor,self.forecaster = CONFIG[variant]
        super().__init__(incoming,outgoing,scenario,seed,automatic,None,0,prep=False,log=False)
        self.trace = trace
        self.eventlog = []
        self.pools = [[] for _ in range(5)]
        for cells in self.free:
            for cell in cells:
                heapq.heappush(self.pools[cell_class(cell)],cell)
        self.jobs = [[] for _ in range(5)]
        self.busy = 0
        self.states = []
        self.positions = [0.,0.]
        self.work = [0.,0.]
        self.empty = self.loaded = self.travel = 0.
        self.stats = Counter()
        self.actions = []
        self.mail = []
        self.serial = self.seen = 0
        self.received = {}
        self.last_count = None
        self.last_stamp = -1e9
        self.registration = {}
        self.models = models
        self.history = np.zeros((2,END//1800+1))
        self.truth = np.zeros_like(self.history)
        for at,sku,_ in incoming:
            self.truth[sku//10,int(at//1800)] += 1
        self.pred = [0.,0.]
        self.baseline = [0.,0.]
        self.error = []
        self.movement = []
        self.pending_origin = {}
        self.audit = None
        self.observe_state()

    def observe_state(self):
        signature = (self.busy,*(bool(pool) for pool in self.pools))
        if not self.states or signature != self.states[-1][1]:
            self.states.append((self.env.now,signature))

    def sensor(self,pallet,enter):
        if not self.automatic:
            return
        self.serial += 1
        serial = self.serial
        if serial%101 == 0:
            self.stats['miss'] += 1
            return
        value = len(self.buffer_tokens) if serial%251 else self.buffer_limit+1
        message = (self.env.now+self.network(self.env.now,pallet.id,0),serial,self.env.now,value,pallet.id,enter)
        heapq.heappush(self.mail,message)
        if serial%97 == 0:
            heapq.heappush(self.mail,message)

    def deliver(self):
        while self.mail and self.mail[0][0] <= self.env.now:
            _,serial,stamp,value,key,enter = heapq.heappop(self.mail)
            if serial <= self.seen:
                self.stats['discarded_replay'] += 1
                continue
            self.seen = serial
            self.last_stamp = stamp
            self.last_count = value if value <= self.buffer_limit else None
            if self.last_count is None:
                self.stats['false'] += 1
            if enter:
                self.received[key] = stamp
            else:
                self.received.pop(key,None)

    def log(self,action,pallet=None,**extra):
        if action == 'physical_buffer':
            self.sensor(pallet,True)
        elif action == 'observation_received':
            self.history[pallet.sku//10,int(self.env.now//1800)] += 1
        elif action == 'register':
            self.registration[pallet.id] = self.env.now
        elif action == 'outbound':
            cell = extra['cell']
            heapq.heappush(self.pools[cell_class(cell)],cell)
            self.observe_state()
        if action == 'identity_exception' and WARM<=self.env.now<STOP:
            self.stats['corrections'] += 1
        if self.audit or self.trace:
            event = dict(time=round(self.env.now,6),event=action,pallet=pallet.id if pallet else None,**extra)
            if self.audit:
                self.audit.apply(event)
            if self.trace:
                self.eventlog.append(event)

    def queue(self,pallet):
        heapq.heappush(self.jobs[job_class(pallet)],(self.registration[pallet.id],pallet.id))
        self.notify()

    def predictor(self):
        for at in range(0,END,1800):
            yield self.env.timeout(at-self.env.now)
            index = at//1800
            for group,(weights,intercept,mean) in enumerate(self.models):
                vector = features(self.history[group],index,mean)
                baseline = vector[5]
                self.baseline[group] = baseline
                prediction = max(0.,float(np.dot(weights,vector)+intercept))
                self.pred[group] = (self.truth[group,index] if self.forecaster=='F2' else
                                    prediction if self.forecaster=='F1' else baseline)
                if WARM<=at<STOP:
                    self.error.append(abs(self.pred[group]-self.truth[group,index]))

    def candidates(self,position):
        options = []
        for kind,jobs in enumerate(self.jobs):
            while jobs:
                entered,key = jobs[0]
                pallet = self.pallets[key]
                if pallet.state in ('ready','ready_final') and job_class(pallet)==kind:
                    break
                heapq.heappop(jobs)
            if not jobs:
                continue
            cells = [self.pools[pool][0] for pool in allowed(kind) if self.pools[pool]]
            if not cells:
                continue
            source = 0 if pallet.cell < 0 else self.distances[pallet.cell]
            cell = min(cells,key=lambda addr: abs(self.distances[addr]-source)+35*(kind in (0,2) and cell_class(addr)%2)
                       ) if self.dynamic else min(cells)
            empty = abs(position-source)
            loaded = abs(self.distances[cell]-source)
            options.append((kind,key,cell,empty,loaded,entered,pallet.cell<0))
        return options

    def choice(self,options,sensor,pred):
        if not self.adaptive:
            return min(options,key=lambda item:(item[5],item[1]))
        valid = sensor and self.last_count is not None and self.env.now-self.last_stamp <= 120
        high = (self.last_count if valid else self.wms_buffer) >= .75*self.buffer_limit
        def priority(item):
            kind,key,cell,empty,loaded,entered,primary = item
            age_bonus = max(0,entered-self.received.get(key,entered)) if valid else 0
            score = self.env.now-entered-2*(empty+loaded+180)
            score += (80*pred[kind//2] if kind<4 else 0) + (1200+age_bonus)*bool(primary and high)
            return score,-key
        return max(options,key=priority)

    def drive(self,driver):
        while True:
            options = self.candidates(self.positions[driver])
            if not options:
                yield self.change
                continue
            self.deliver()
            chosen = self.choice(options,self.use_sensor,self.pred)
            kind,key,cell,empty,loaded,entered,primary = chosen
            start = self.env.now
            in_window = WARM <= start < STOP
            neutral = self.choice(options,False,self.pred)[1] if self.use_sensor else key
            simple = self.choice(options,self.use_sensor,self.baseline)[1] if self.forecaster in ('F1','F2') else key
            valid = self.use_sensor and self.last_count is not None and start-self.last_stamp <= 120
            estimated = self.last_count if valid else self.wms_buffer
            if in_window:
                self.stats['assignments'] += 1
                self.stats['iot_changed'] += int(neutral!=key)
                self.stats['forecast_changed'] += int(simple!=key)
                self.stats['stale_threshold'] += int((estimated>=.75*self.buffer_limit)!=(len(self.buffer_tokens)>=.75*self.buffer_limit))
                self.stats['fallback'] += int(self.use_sensor and not valid)
            heapq.heappop(self.jobs[kind])
            assert heapq.heappop(self.pools[cell_class(cell)]) == cell and cell not in self.owner
            pallet = self.pallets[key]
            origin = pallet.cell
            self.owner[cell] = key
            if primary:
                pallet.started_at = start
            elif in_window:
                self.stats['repeat_moves'] += 1
            self.busy += 1
            self.observe_state()
            pallet.state = 'moving'
            self.log('reserve_and_start',pallet,cell=cell,quality=pallet.quality)
            duration = empty+loaded+180*self.random['work'][key]
            active = self.overlap(start,start+duration)
            self.work[driver] += active
            self.empty += self.overlap(start,start+empty)
            self.loaded += self.overlap(start+empty,start+empty+loaded)
            if primary:
                self.movement.append((key,start,duration))
            if self.trace:
                decision = dict(time=start,driver=driver,forecast=list(self.pred),recommendation=key,selected=key,
                                actual=key,cell=cell,origin=origin,empty_m=empty,loaded_m=loaded,observed_count=estimated,
                                wms_count=self.wms_buffer,physical_count=len(self.buffer_tokens),
                                ignore_sensor=neutral,simple_forecast=simple,outcome=None)
                self.actions.append(decision)
            yield self.env.timeout(float(empty))
            if primary:
                self.buffer_change()
                self.buffer.release(self.buffer_tokens.pop(key))
                self.sensor(pallet,False)
            yield self.env.timeout(float(duration-empty))
            self.busy -= 1
            self.positions[driver] = self.distances[cell]
            if origin >= 0:
                del self.owner[origin]
                self.pending_origin[key] = origin
            if primary:
                pallet.physical_end_at = self.env.now
            pallet.cell,pallet.state = cell,'confirming'
            self.log('physical_move_end',pallet,cell=cell)
            self.stats['completed_jobs'] += int(WARM<=self.env.now<STOP)
            if self.trace:
                decision['outcome'] = self.env.now
            self.observe_state()
            self.env.process(self.confirm(pallet,cell//2600,primary))
            self.notify()

    def confirm(self,pallet,zone,initial):
        yield from super().confirm(pallet,zone,initial)
        previous = self.pending_origin.pop(pallet.id,None)
        if previous is not None:
            heapq.heappush(self.pools[cell_class(previous)],previous)
            self.observe_state()
            self.notify()

    def execute(self):
        for proc in (self.arrivals(),self.departures(),self.predictor(),self.drive(0),self.drive(1)):
            self.env.process(proc)
        self.env.run(until=END)
        self.buffer_change()
