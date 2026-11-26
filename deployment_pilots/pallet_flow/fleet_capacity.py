"""Paired S1 warehouse experiments, with a finite operating fleet and queue-free controls."""
import argparse
import gc
import gzip
import heapq
import json
import runpy
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import simpy

HERE = Path(__file__).resolve().parent
model = runpy.run_path(str(HERE / "run.py"), run_name="pallet_flow_model")
old = model["old"]
P = model["P"]


class ChargingFleet(model["Fleet"]):
    """Run a staggered availability test; finish already granted work before charging."""

    def __init__(self, *args, charge_hours, chargers):
        super().__init__(*args)
        if not self.finite or charge_hours <= 0 or chargers < 1:
            raise ValueError("Charging schedules require a finite fleet and at least one charger")
        self.chargers = simpy.Resource(self.e, capacity=chargers)
        self.charge_seconds = charge_hours * 3600.
        self.due = {}
        self.charge_starts = 0
        self.charger_waits = []
        for car in range(self.fleet_size):
            self.e.process(self.service(car))

    def idle(self, car, position):
        if car in self.due:
            self.due.pop(car).succeed(position)
        else:
            super().idle(car, position)

    def service(self, car):
        on_duty = 8 * 3600.
        yield self.e.timeout((car + 1) * (on_duty + self.charge_seconds) / self.fleet_size)
        while True:
            if car in self.cars:
                _,position = self.cars.pop(car)
                if car in self.returning:
                    start,origin = self.returning.pop(car)
                    position = max(0., origin - (self.e.now - start) * self.travel_speed)
            else:
                self.due[car] = self.e.event()
                position = yield self.due[car]
            # Scenario assumption: the safe charging bay is at the home end of the route.
            yield self.e.timeout(position / self.travel_speed)
            arrival = self.e.now
            with self.chargers.request() as plug:
                yield plug
                if P.COHORT_FROM <= self.e.now < P.COHORT_TO:
                    self.charge_starts += 1
                    self.charger_waits.append(self.e.now - arrival)
                yield self.e.timeout(self.charge_seconds)
            super().idle(car, 0.)
            self.signal()
            yield self.e.timeout(on_duty)


def inputs(load, seed):
    if seed != P.SEED:
        return old.compare.input_for_load(load, None, seed)
    path = old.compare.INPUT_FILE if load == "L2" else (
        HERE.parent / "full_s1_l2" / "results" / "revised_clean" / load / "input_manifest.json.gz")
    with gzip.open(path, "rt", encoding="utf8") as stream:
        return json.load(stream)


def rezone_quarantine(m, count):
    """Convert empty permanent addresses; keep building capacity, rack distances and initial stock."""
    for i in range(count):
        group = i % 2
        _,previous = heapq.heappop(m.pools[group])
        address = m.addr.pop(previous)
        name = f"QZONE-{5329+i:04d}"
        m.addr[name] = old.Address(name, 2, True, address.distance)
        m.free(name, notify=False)
    assert len(m.addr) == 5329 and sum(a.quarantine for a in m.addr.values()) == 129 + count


def metrics(m, row):
    completed = [c for c in m.x["receipts"] if c["case"] in m.cohort
                 and m.closed.get(c["case"], float("inf")) - c["ready"] <= P.H]
    span = np.mean([c["last_ready"] - c["ready"] for c in completed])
    waits, by_day = [], defaultdict(list)
    quarantine = 0
    for v in m.visits:
        if v["case"] not in m.cohort or v["op"] not in (10, 18) or v["branch"] != "APPLIED":
            continue
        # Right-censored requests count against the service target if already overdue.
        delay = (m.e.now if v["grant"] is None else v["grant"]) - v["request"]
        waits.append(delay)
        by_day[int(m.x["pallets"][v["pallet"]]["ready"] // 86400)].append(delay)
        quarantine += v["op"] == 10 and v["finish"] is not None and v["finish"] <= m.e.now
    w = np.array(waits)
    assert np.min(w) >= -1e-6 and (m.finite or max(w) < 1e-6)
    assert row["closed"] == len(completed) and (not m.finite or row["spare"] == 0)
    service = lambda arr: float(100 * np.mean(np.asarray(arr) <= 60.))
    # A robot that has deposited a pallet in P18 still holds the forks until P19 ends.
    confirmations = {v["pallet"]: v["finish"] for v in m.visits if v["op"] == 19
                     and v["branch"] == "APPLIED" and v["finish"] is not None}
    occupancy = Counter()
    for v in m.visits:
        if v["op"] not in (10, 18) or v["branch"] != "APPLIED" or v["grant"] is None:
            continue
        start = max(v["grant"], P.COHORT_FROM)
        end = v["finish"]
        if v["op"] == 18:
            end = confirmations.get(v["pallet"], end)
        finish = min(m.e.now if end is None else end, P.COHORT_TO)
        if start < finish:
            occupancy[start] += 1
            occupancy[finish] -= 1
    active = peak = 0
    for t in sorted(occupancy):
        active += occupancy[t]
        peak = max(peak,active)
    assert not m.finite or peak <= m.fleet_size
    kept = sum(k == "release_at_address" and cid in m.cohort for k,t,cid,p,d in m.events)
    entered, picked = {}, {}
    for kind, stamp, cid, pallet, event in m.events:
        if cid not in m.cohort:
            continue
        if kind == "enter":
            entered[pallet] = stamp
        elif kind == "pickup" and event["source"] == "BUFFER":
            picked[pallet] = stamp
    handoff = np.asarray([picked.get(p,m.e.now)-t for p,t in entered.items()])
    assert len(picked) <= len(entered) and min(handoff) >= -1e-6
    return dict(cases=row["cases"], pallets=row["n"], placed=row["placed"], closed=len(completed),
                rejected=row["failed_cases"], p20_s=row["p20"], p25_s=row["doc"],
                after_last_s=row["doc"] - span, release_span_s=span,
                submitted_trips=len(waits), quarantine_trips=int(quarantine),
                wait_mean_s=float(w.mean()), wait_p95_s=float(np.quantile(w,.95)),
                wait_max_s=float(max(w)), immediate_pct=float(100 * np.mean(w <= 1e-6)),
                within_60_pct=service(w), worst_day_within_60_pct=min(map(service, by_day.values())),
                ledger_checks=row["ledger_checks"], extra_addresses=row["extra_addresses"], peak_busy=peak,
                released_in_place=kept, handoff_p95_s=float(np.quantile(handoff,.95)),
                handoff_within_120_pct=float(100*np.mean(handoff<=120.)), picked=len(picked),
                end_free_slots=[len(pool) for pool in m.pools],
                end_pending_by_group=dict(Counter(2 if job[3]==10 else job[2]//10 for job in m.jobs.values())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("load", choices=("L1","L2","L3"))
    parser.add_argument("out", type=Path)
    parser.add_argument("--seed", type=int, default=P.SEED)
    parser.add_argument("--grace", type=float, default=0.)
    parser.add_argument("--handling", type=float, default=100.)
    parser.add_argument("--speed", type=float, default=.8)
    parser.add_argument("--dual-slots", type=int, default=0)
    parser.add_argument("--charge-hours", type=float, default=0.)
    parser.add_argument("--chargers", type=int, default=1)
    parser.add_argument("--fixed-storage", action="store_true")
    parser.add_argument("--rezone-quarantine", type=int, default=0)
    parser.add_argument("--reject-hold-hours", type=float, default=0.)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--fleets", type=int, nargs="*", default=[])
    parser.add_argument("--controls", action="store_true")
    parser.add_argument("--stop-at-zero", action="store_true")
    args = parser.parse_args()
    x = inputs(args.load, args.seed)
    draws = old.Inputs(x, args.seed, json.loads(old.IO_FILE.read_bytes()))
    models, days = old.compare.train_forecasters(x)
    assert set(days).isdisjoint(x["days"])
    variants = [("ASIS", False, 2, 1., 75., 0.)]
    if args.controls:
        variants.extend(("RETURN", False, 2, speed, work, grace) for speed, work, grace in
                        [(1.,75.,0.),(.8,75.,0.),(.8,100.,0.),(.8,100.,30.),(.8,100.,60.),
                         (.8,100.,120.),(1.,75.,60.)])
    if args.clean:
        variants.append(("RETURN", False, 2, args.speed, args.handling, args.grace))
    variants.extend(("RETURN", True, n, args.speed, args.handling, args.grace) for n in args.fleets)
    output = dict(load=args.load, seed=args.seed, manifest=old.compare.digest(x), runs=[])
    valid_cases = {c["case"] for c in x["receipts"] if P.COHORT_FROM <= c["ready"] < P.COHORT_TO
                   and not any(x["pallets"][p]["failed"] for p in c["members"])}
    for name, finite, n, speed, handling, grace in variants:
        dual = args.dual_slots if name != "ASIS" else 0
        params = (x, name, draws, models, finite, .9, handling, False, n, speed, grace, dual)
        if finite and args.charge_hours:
            m = ChargingFleet(*params, charge_hours=args.charge_hours, chargers=args.chargers)
        else:
            m = model["Fleet"](*params)
        if args.fixed_storage and finite:
            # Use the existing map and sensor logic. A later quality release cancels an
            # as-yet unassigned quarantine trip; blocked pallets cannot be shipped.
            m.fixed_storage = True
            m.recheck = True
            m.slots = old.compare.BufferPlaces(m)
            m.buffer.zones = {z: simpy.Resource(m.e, len(m.slots.physical[z])) for z in "AB"}
            m.capacity = sum(z.capacity for z in m.buffer.zones.values())
            if args.rezone_quarantine:
                rezone_quarantine(m, args.rezone_quarantine)
            m.reject_hold_hours = args.reject_hold_hours
        if name != "ASIS":
            for q,(stamp,state) in decisions.items():
                m.quality_known[q] = m.e.process(m.authorized(q,stamp,state))
            for q in {p["q"] for p in x["pallets"].values()} - decisions.keys():
                m.quality_known[q] = m.e.event()
        m.execute()
        row, authority = model["summarize"](m, args.load)
        if name == "ASIS":
            decisions = authority
        else:
            assert authority == decisions
        result = metrics(m, row)
        result.update(arm=name, finite=finite, fleet=n, speed=speed, handling=handling,
                      grace=grace, dual_slots=dual, eligible=len(valid_cases))
        if m.fixed_storage:
            assert not m.extra
            result["fixed_storage"] = True
            result["rezoned"] = args.rezone_quarantine
            result["reject_hold_hours"] = args.reject_hold_hours
            result["reject_handovers"] = sum(k == "reject_handover" and cid in m.cohort
                                             for k,t,cid,p,d in m.events)
        if isinstance(m, ChargingFleet):
            result.update(charge_hours=args.charge_hours, chargers=args.chargers, charges=m.charge_starts,
                          mean_charger_wait_s=float(np.mean(m.charger_waits)))
        if not finite:
            assert result["closed"] == len(valid_cases)
        output["runs"].append(result)
        # Checkpoint after each variant so tested fleet sizes are inspectable during a sweep.
        tmp = args.out.with_suffix(".tmp")
        tmp.write_text(json.dumps(output, indent=2, allow_nan=False), encoding="utf8")
        tmp.replace(args.out)
        print(name, finite, n, speed, handling, grace, result["closed"],
              round(result["p20_s"],1), round(result["within_60_pct"],2),
              round(result["wait_max_s"],1), flush=True)
        del m
        gc.collect()
        if finite and args.stop_at_zero and result["wait_max_s"] <= 1e-6 and result["closed"] == len(valid_cases):
            break


if __name__ == "__main__":
    main()
