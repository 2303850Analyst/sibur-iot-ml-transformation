"""Matched receiving shifts with digital identification, driven trucks, and a reserve driver."""
import argparse
import gc
import json
from pathlib import Path

import one_shift as trial


class Driven(trial.Robot):
    """Digital gate and one receiver. The existing driven fleet keeps the manual trip times."""
    def service(self, car):
        yield self.waitforever


class FlexDriven(Driven, trial.options.ShiftFleet):
    """Use the same daily second-driver coverage as the manual control."""
    def __init__(self, *args, span):
        # ShiftFleet owns the fleet-size argument and parks its second truck.
        self.due = {}
        trial.options.ShiftFleet.__init__(self, *args, finite=True, base=1, first=8, span=span)

    def signal(self):
        if hasattr(self, 'cars') and not self.on() and self.flex in self.cars:
            self.take_off()
        super().signal()


class Carrier(trial.Robot, trial.options.Hybrid):
    """Same gate, one driver handles all fork handovers, AGVs handle horizontal travel."""


class DrivenTrip:
    """Reuse the warehouse's established driven trip and position bookkeeping."""
    robot = False
    travel_speed = 1.0
    robot_s = 180.0

    def __init__(self, warehouse):
        self.warehouse = warehouse

    def __getattr__(self, name):
        return getattr(self.warehouse, name)


class Reserve(trial.Robot):
    """One surviving robot, one existing driven stacker, both available during the outage."""
    def configure(self, day_only):
        super().configure(day_only)
        self.manual = -1
        self.cars[self.manual] = (0., 0.)
        self.driven = DrivenTrip(self)
        self.manual_trips = 0

    def choose(self, jobs):
        # Offer the driver's vehicle only when the working robot is occupied.
        if self.manual in self.cars and len(self.cars) > 1:
            available = self.cars.pop(self.manual)
            try:
                return super().choose(jobs)
            finally:
                self.cars[self.manual] = available
        return super().choose(jobs)

    def idle(self, car, position):
        if car == getattr(self, 'manual', None):
            self.cars[car] = (self.e.now, position)
        else:
            super().idle(car, position)

    def transfer(self, cid, p, stage, entry, event, car, dest, pos, origin):
        if car != self.manual:
            yield from super().transfer(cid, p, stage, entry, event, car, dest, pos, origin)
            return
        if trial.START <= self.e.now < trial.END:
            self.manual_trips += 1
        yield from trial.options.Fleet.transfer(self.driven, cid, p, stage, entry, event, car, dest, pos, origin)

    def step(self, c, n, seconds, p=""):
        if n == 19 and self.held_truck.get(p, (None,))[0] == self.manual:
            seconds += 25 * self.draws.factors[19][0, self.draws.p_index[p]]
        yield from super().step(c, n, seconds, p)

    def handover_reject(self, cid, p, address, car, position):
        handler = self.driven if car == self.manual else self
        yield from trial.options.Fleet.handover_reject(handler, cid, p, address, car, position)


class ScheduledReserve(Reserve):
    """The paid driver helps with routine jobs in one fixed daily window, starting at 08:00."""
    def configure(self, day_only):
        super().configure(day_only)
        self.parked_driver = self.cars.pop(self.manual)[1]
        self.e.process(self.driver_clock())

    def assisting(self):
        return (self.e.now / trial.HOUR - 8) % self.assist_period < self.assist_hours

    def driver_clock(self):
        while True:
            yield self.e.timeout((int(self.e.now // trial.HOUR) + 1) * trial.HOUR - self.e.now)
            if self.assisting() and self.parked_driver is not None:
                self.cars[self.manual] = (self.e.now, self.parked_driver)
                self.parked_driver = None
                self.signal()
            elif not self.assisting() and self.manual in self.cars:
                self.parked_driver = self.cars.pop(self.manual)[1]

    def idle(self, car, position):
        if car == getattr(self, 'manual', None) and not self.assisting():
            self.parked_driver = position
        else:
            super().idle(car, position)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('cases', nargs='+')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=241126)
    parser.add_argument('--multiplier', type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    x = trial.manifest(args.seed, args.multiplier)
    sim = trial.sim
    draws = sim.old.Inputs(x, sim.P.SEED, json.loads(sim.old.IO_FILE.read_bytes()))
    models, _ = sim.old.compare.train_forecasters(x)
    ref = trial.options.Fleet(x, 'ASIS', draws, models)
    ref.execute()
    decisions = {a['quality']: (t, a['decision']) for k, t, c, p, a in ref.events if k == 'quality_decision'}
    del ref
    gc.collect()
    for case in args.cases:
        count = int(case[1:])
        driven = case[0] in 'DVTP'
        handheld = case[0] in 'TP'
        cls = {'D': Driven, 'T': Driven, 'P': FlexDriven, 'V': FlexDriven, 'R': trial.Robot, 'H': Carrier, 'S': Reserve, 'W': ScheduledReserve, 'J': ScheduledReserve}[case[0]]
        mode = 'DATA_RETURN' if handheld else 'GATE_RETURN' if driven else 'RETURN'
        common = (x, mode, draws, models)
        if cls is FlexDriven:
            m = cls(*common, span=count)
        else:
            extra = {'people': 1} if cls is Carrier else {}
            fleet_size = 1 if cls is ScheduledReserve else count
            m = cls(*common, True, .9, 100., False, fleet_size, 1. if driven else .8, charge_hours=1., chargers=1, **extra)
            if cls is ScheduledReserve:
                m.assist_hours = count
                m.assist_period = 12 if case[0] == 'J' else 24
        m.configure(False)
        if handheld:
            m.slots = None
        for q, (t, state) in decisions.items():
            m.quality_known[q] = m.e.process(m.authorized(q, t, state))
        m.execute()
        row = trial.report(m, case, args.seed)
        row['manual_reserve_trips'] = getattr(m, 'manual_trips', None)
        encoded = json.dumps(row, allow_nan=False, default=lambda n: n.item())
        (args.out / f'{case}-{args.seed}.json').write_text(encoded)
        print(case, row['backlog'], row['delay72'], row['onemin'], row['manual_reserve_trips'], flush=True)
        del m
        gc.collect()


if __name__ == '__main__':
    main()
