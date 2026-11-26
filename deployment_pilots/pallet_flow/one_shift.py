"""320 pallets/day, one serial gate, one receiver, an optional manual driver."""
import argparse
import gc
import json
import random
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import simpy

import operating_options as options

sim = options.sim
DAY = 86400
START, END = DAY, 31 * DAY
HOUR = 3600


def night(t):
    return t % DAY < 6 * HOUR or t % DAY >= 22 * HOUR


def manifest(seed, load_multiplier=1):
    source = sim.inputs("L2", sim.P.SEED)
    donors = [c for c in source["receipts"] if len(c["members"]) == 5]
    rng = random.Random(seed)
    x = {k: source[k] for k in ("scenario", "load", "days", "seed")}
    x.update(receipts=[], pallets={}, departures=[])
    for d in range(31):
        for j in range(64 * load_multiplier):
            donor = rng.choice(donors)
            t0, cid = DAY * d + j * 1350 / load_multiplier, f"D{d:02d}-{j:03d}"
            group = dict(donor, case=cid, ready=t0, last_ready=t0 + 356, members=[])
            for i, p in enumerate(donor["members"]):
                r = source["pallets"][p]
                t, pid = t0 + 89 * i, f"{cid}-{i}"
                x["pallets"][pid] = dict(r, pallet=pid, ready=t, arrival=t + 30,
                    q=f"{d}/{r['q']}", lot=f"{d}/{r['lot']}", sscc="U"+pid, wrong_sscc="X"+pid)
                group["members"].append(pid)
                x["departures"].append((t + 7200, r["sku"]))
            x["receipts"].append(group)
    x["departures"].sort()
    return x


class Shift:
    def configure(self, day_only):
        self.day_only = day_only
        self.post = simpy.Resource(self.e, 1)
        self.employee = simpy.Resource(self.e, 1)
        self.occupied = []
        self.admissions = {}
        self.capacity = 40
        self.fixed_storage = True
        self.reject_hold_hours = 24
        sim.rezone_quarantine(self, 70)

    @contextmanager
    def employee_time(self):
        start = self.e.now
        try:
            yield
        finally:
            self.occupied.append((start, self.e.now))

    def dispatch(self):
        # The parent issues all currently assignable trips atomically at each wake-up.
        parent = super().dispatch()
        answer = None
        while True:
            if self.day_only and night(self.e.now):
                yield self.e.timeout((6 * HOUR - self.e.now) % DAY)
            answer = yield parent.send(answer)


class Manual(Shift, options.Fleet):
    def configure(self, day_only):
        super().configure(day_only)
        self.space = {lane: simpy.Resource(self.e, 20) for lane in "AB"}
        self.space_tokens = {}

    def identify(self, case, raw):
        yield self.at(raw["arrival"])
        with self.post.request() as gate:
            yield gate
            p = raw["pallet"]
            token = self.space[self.lane[case]].request()
            yield token
            self.space_tokens[p] = token
            with self.employee.request() as worker:
                yield worker
                with self.employee_time():
                    return (yield from super().identify(case, raw))

    def record(self, kind, c="", p="", **data):
        super().record(kind, c, p, **data)
        if kind == "pickup" and data["source"] == "BUFFER":
            token = self.space_tokens.pop(p)
            token.resource.release(token)
        if kind == "open_nonconformity":
            self.e.process(self.schedule_reject(c, p))

    def op(self, case, n, *args, **kw):
        if sim.P.ops[n]["primary_resource"] in ("receiver", "wms_operator") and not kw.get("skip"):
            with self.employee.request() as req:
                yield req
                with self.employee_time():
                    yield from super().op(case, n, *args, **kw)
        else:
            yield from super().op(case, n, *args, **kw)


class SensorSlots(sim.old.compare.BufferPlaces):
    def check_disagreement(self, lane, i, version, p, case):
        yield self.m.e.timeout(3 * sim.old.compare.POLL)
        if self.version[lane, i] != version or self.report[lane][i] == self.book[lane][i]:
            return
        with self.m.employee.request() as req:
            yield req
            with self.m.employee_time():
                yield self.m.e.timeout(8)
            if self.version[lane, i] == version:
                self.report[lane][i] = self.physical[lane][i]
                self.wake()
            self.m.counts["manual_slot_checks"] += 1


class Robot(Shift, sim.ChargingFleet):
    def configure(self, day_only):
        super().configure(day_only)
        self.buffer.zones = {lane: simpy.Resource(self.e, 20) for lane in "AB"}
        self.slots = SensorSlots(self)

    def unit(self, c, raw, document):
        p = raw["pallet"]
        yield self.at(raw["ready"])
        with self.post.request() as gate:
            yield gate
            with self.employee.request() as worker:
                yield worker
                launch = max(self.e.now, self.next_launch[self.lane[c]])
                yield self.at(launch)
                entered = self.admissions[p] = self.e.event()
                child = self.e.process(super().unit(c, raw, document))
                machine, fallback, _ = self.scan(raw)
                check = sim.old.compare.P["physical_check_mode_s"] * raw["check_factor"]
                read = machine + self.draws.mqtt[2][self.draws.p_index[p]]
                duration = max(30, read) + fallback + check if fallback else max(read, check)
                with self.employee_time():
                    yield self.e.timeout(duration)
            # Stand retains the gate until the pallet has entered a physical slot.
            # The receiver is free for exceptions and paperwork during that wait.
            yield entered
            del self.admissions[p]
        yield child

    def admit(self, c, p, boundary):
        token = yield from super().admit(c, p, boundary)
        self.admissions[p].succeed()
        return token

    def step(self, c, n, seconds, p=""):
        if n in (12, 13, 14, 23):
            with self.employee.request() as req:
                yield req
                with self.employee_time():
                    yield from super().step(c, n, seconds, p)
        else:
            yield from super().step(c, n, seconds, p)


class Relief(Manual, options.ShiftFleet):
    """One round-the-clock driver plus a second driver for the specified daily window."""


def breaks(m):
    for d in range(34):
        for hour in (2,14):
            yield m.at(d*DAY+hour*HOUR)
            with m.employee.request() as req:
                yield req
                yield m.e.timeout(1800)


def report(m, label, seed):
    x = m.x
    checks = sim.old.compare.invariants(m)
    assert not checks["violations"], checks["violations"][:4]
    assert m.spares == m.fleet_size and not m.extra
    groups = {c["case"]: c for c in x["receipts"]}
    valid = {c for c, g in groups.items() if not any(x["pallets"][p]["failed"] for p in g["members"])}
    eligible = m.cohort & valid
    expected = {c for c, g in groups.items() if START <= g["ready"] < END}
    assert m.cohort == expected and not (m.closed.keys() - valid)
    done = {c: m.closed[c] - groups[c]["ready"] for c in eligible & m.closed.keys()}
    within = [v for v in done.values() if v <= 72 * HOUR]
    placed = {}
    for v in m.visits:
        if v["op"] == 20 and v["branch"] == "APPLIED" and v["finish"] is not None and v["finish"] <= m.end:
            placed[v["pallet"]] = v["finish"]
    pending = []
    for d in [1, 8, 16, 24, 31]:
        t = d*DAY
        pending.append(sum(1 for p,r in x["pallets"].items() if r["ready"]<t and not r["failed"] and placed.get(p,float("inf"))>=t))
    req = [v for v in m.visits if v["case"] in m.cohort and v["op"] in (10,18) and v["branch"]=="APPLIED"]
    def pct(seq):
        return None if not seq else 100*sum((v["grant"] if v["grant"] is not None else m.end)-v["request"]<=60 for v in seq)/len(seq)
    if m.day_only:
        assert all(not night(v["grant"]) for v in req if v["grant"] is not None)
    utilization = sum(max(0,min(b,END)-max(a,START)) for a,b in m.occupied)/(END-START)
    assert utilization <= 1.00000001
    good = {p for c in m.cohort for p in groups[c]["members"] if not x["pallets"][p]["failed"]}
    accepted = {p:t for k,t,c,p,_ in m.events if k=="enter"}
    early = []
    for d in range(1,31):
        morning = d*DAY+6*HOUR
        early.append(sum(not r["failed"] and r["ready"]<morning<=placed.get(p,float("inf")) for p,r in x["pallets"].items()))
    pt = [t-x["pallets"][p]["arrival"] for p,t in placed.items() if p in good]
    night_done = [v for c,v in done.items() if night(groups[c]["ready"]) and v<=72*HOUR]
    receiver_intervals=sorted((a,b) for a,b in m.occupied if b>a)
    assert all(b <= c+1e-7 for (_,b),(c,_) in zip(receiver_intervals,receiver_intervals[1:]))
    driver_sec = 0.
    trip_times = []
    if label.startswith("M"):
        for v in m.visits:
            if v["op"] in (10,18,19) and v["branch"]=="APPLIED" and v["manual_start"] is not None:
                driver_sec += max(0,min(END,v["manual_end"])-max(START,v["manual_start"]))
                if v["op"] == 18 and START <= v["manual_start"] < END:
                    trip_times.append(v["manual_end"] - v["manual_start"])
    return dict(scenario=label,seed=seed,offered=sum(len(groups[c]["members"]) for c in expected),eligible_pallets=len(good),eligible=len(eligible),
        driver_active_share=driver_sec/(END-START)/m.fleet_size,mean_p18=np.mean(trip_times) if trip_times else None,
        processed_calendar=sum(START<=t<END for t in placed.values()),p20_in_30=sum(placed.get(p,float("inf"))<END for p in good),
        entries=sum(START<=t<END for t in accepted.values()),closed72=len(within),closed_all=len(done),
        closed_in_30=sum(m.closed.get(c,float("inf"))<END for c in eligible),
        delay_all=np.mean(list(done.values())) if done else None, delay72=np.mean(within) if within else None,
        p20mean=np.mean(pt) if pt else None, onemin=pct(req),onemin_last=pct([v for v in req if x["pallets"][v["pallet"]]["ready"]>=16*DAY]),
        receiver=utilization, backlog=pending,night_release=sum(night(x["pallets"][p]["ready"]) for c in m.cohort for p in groups[c]["members"]),
        night_place=sum(START<=t<END and night(t) for t in placed.values()),night_docs=sum(START<=t<END and night(t) for t in m.closed.values()),
        morning_backlog=np.mean(early), night_p25=np.mean(night_done) if night_done else None,night_docs_eligible72=len(night_done),
        peak_A=max_lane(m,"A"),peak_B=max_lane(m,"B"),checks=sum(checks["checks"].values()))


def max_lane(m, lane):
    level = peak = 0
    for k,t,c,p,a in m.events:
        if c and m.lane[c]==lane:
            if k=="enter": level+=1
            elif k=="pickup" and a["source"]=="BUFFER": level-=1
            peak=max(peak,level)
    assert peak<=20 and level>=0
    return peak


if __name__ == "__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("specs", nargs="+", help="M1 / R1..Rn; D daytime trips, B receiver breaks, F8 etc. second manual post from 08:00")
    ap.add_argument("--seed",type=int,default=241126)
    ap.add_argument("--out",type=Path,required=True)
    args=ap.parse_args()
    x=manifest(args.seed)
    draws=sim.old.Inputs(x,sim.P.SEED,json.loads(sim.old.IO_FILE.read_bytes()))
    models,_=sim.old.compare.train_forecasters(x)
    ref=options.Fleet(x,"ASIS",draws,models)
    ref.execute()
    decisions={a["quality"]:(t,a["decision"]) for k,t,c,p,a in ref.events if k=="quality_decision"}
    del ref
    gc.collect()
    for label in args.specs:
        n=int(label[1:].rstrip("DB"))
        if label[0]=="F":
            m=Relief(x,"ASIS",draws,models,finite=True,base=1,first=8,span=n)
        elif label[0]=="M":
            m=Manual(x,"ASIS",draws,models,finite=True,fleet_size=n)
        else:
            m=Robot(x,"RETURN",draws,models,True,.9,100.,False,n,.8,charge_hours=1.,chargers=1)
            for q,(t,state) in decisions.items():
                m.quality_known[q]=m.e.process(m.authorized(q,t,state))
        m.configure(label.endswith("D"))
        if label.endswith("B"):
            m.e.process(breaks(m))
        m.execute()
        r=report(m,label,args.seed)
        encoded=json.dumps(r,allow_nan=False,default=lambda n:n.item())
        print(encoded,flush=True)
        (args.out/f"{label}-{args.seed}.json").write_text(encoded)
        del m
        gc.collect()
