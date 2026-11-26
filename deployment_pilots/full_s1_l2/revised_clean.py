"""S1/L2 zero-queue action-level experiment, independent of previous service/route logs."""
import argparse
import gc
import heapq
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import simpy
from scipy.stats import t as student_t

import compare
from no_queue import AvailablePlaces
from policy import Address


P = compare.pilot
NAMES = {n: item["operation"] for n, item in P.ops.items()}
RULES = {int(k[1:]): (module, retained)
         for k, (module, retained) in P.settings["retained_manual_modes_s"].items()}
IO_FILE = P.V3/"v3_2"/"preflight"/"io_samples.json"
ACTION = {
    1: "Подготовить и отправить основание", 3: "Найти и сопоставить реквизиты",
    4: "Перенести результаты в акт", 5: "Передать сводку получателям",
    6: "Найти связь объекта качества с партией", 9: "Перенести утверждённый статус",
    12: "Собрать реквизиты для черновика", 13: "Ввести реквизиты записи",
    14: "Повторно сопоставить поля с источником", 16: "Перенести реквизиты паллеты в приход",
    17: "Уточнить адрес и подготовить задание", 20: "Сопоставить подтверждение с заданием",
    22: "Собрать результаты по строкам", 23: "Собрать итоговый документ",
    24: "Отправить итог и проверить обмен", 25: "Перенести итоговые статусы для закрытия",
}
ARMS = {"ASIS": 0, "GATE": 16, "SOFT": 15, "SOFT_GATE": 31, "NO_ML": 63, "FULL": 127}


def enabled(mask, h):
    return bool(mask & (1 << (h-1)))


class Inputs:
    """All stochastic streams are indexed by physical object, operation and replication."""
    def __init__(self, x, seed, io_samples):
        self.ids = {}
        objects = {"case": sorted(c["case"] for c in x["receipts"]),
                   "pallet": sorted(x["pallets"]),
                   "quality": sorted({p["q"] for p in x["pallets"].values()})}
        for typ, names in objects.items():
            self.ids[typ] = {name: i for i, name in enumerate(names)}
        self.step_type = {k: ("quality" if op["scope"].startswith("quality") else
                              "pallet" if op["scope"].startswith("pallet") else "case")
                          for k, op in P.ops.items()}
        self.factors, self.http = {}, {}
        raw = defaultdict(list)
        for row in io_samples:
            if isinstance(row["op"], int):
                raw[row["channel"], row["op"]].append(float(row["seconds"]))
        for k, typ in self.step_type.items():
            count = len(objects[typ])
            gen = np.random.default_rng([seed, k, 11])
            self.factors[k] = gen.triangular(.7, 1., 1.3, size=(3, count))
            self.http[k] = gen.choice(raw["http",k], size=count)
        self.mqtt = {}
        for k in (1,2,9,24):
            gen = np.random.default_rng([seed,k,12])
            self.mqtt[k] = gen.choice(raw["mqtt_e2e",k],size=len(objects[self.step_type[k]]))
        self.missing, self.linked = {}, {}
        for j, typ in enumerate(("case", "quality")):
            gen = np.random.default_rng([seed,j,21])
            self.missing[typ] = gen.random(len(objects[typ]))
            self.linked[typ] = gen.random(len(objects[typ]))
        self.q_index = self.ids["quality"]
        self.c_index = self.ids["case"]
        self.p_index = self.ids["pallet"]


class SensorCrew:
    """Only the asynchronous reconciliation task is scheduled through this interface."""
    def reserve(self, role, ready, base, case, step, visit_id):
        assert step == 26
        return ready, ready+base, f"aux/{visit_id}", 0., 1.


class Warehouse(compare.Warehouse):
    travel_speed = 1.0  # effective horizontal speed, m/s

    def __init__(self, x, mask, draws, models, manual_share, coverage, baseline_lead=0):
        level = 3 if enabled(mask,7) else 1 if enabled(mask,4) else 0
        super().__init__(x, level, False, models, 0.)
        self.modules = mask
        self.draws = draws
        self.manual_share = manual_share
        self.coverage = coverage
        self.baseline_lead = baseline_lead
        self.cohort = {c["case"] for c in x["receipts"] if P.COHORT_FROM <= c["ready"] < P.COHORT_TO}
        self.capacity = len(x["pallets"])
        self.buffer.zones = {z:simpy.Resource(self.e, self.capacity) for z in "AB"}
        self.slots = AvailablePlaces(self) if enabled(mask,6) else None
        self.crew = SensorCrew()
        self.fleet_size = 2
        self.cars = {i: (0., 0.) for i in range(self.fleet_size)}  # idle since, current coordinate
        self.spares = self.fleet_size
        self.extra = Counter()
        self.held_truck = {}
        self.audit = Counter()
        self.sref = 180. + 2*float(np.median([a.distance for a in self.addr.values()]))

    def network(self):
        # Each document/reader transaction is timed on its specific operation.
        return self.e.timeout(0)

    def balance_event(self, pallet, enter):
        yield self.e.timeout(float(self.draws.http[2][self.draws.p_index[pallet]]))
        if enter:
            self.known_buffer.add(pallet)
        else:
            self.known_buffer.discard(pallet)

    def op(self, case, n, p="", auto=False, skip=None, duration=None, **extra):
        visit = self.new(case,n,p,P.ops[n]["primary_resource"],**extra)
        start = self.e.now
        if skip:
            visit.update(grant=start, service_start=start, finish=start, branch="NA:"+skip)
            return
        typ = self.draws.step_type[n]
        name = p or extra.get("quality") or case
        idx = self.draws.ids[typ][name]
        f = self.draws.factors[n][:,idx]
        base = P.ops[n]["manual_labor_s"]
        rule = RULES.get(n)
        retained = rule[1] if rule else base
        data_type = "quality" if typ == "quality" else "case"
        record = self.draws.q_index[name] if typ == "quality" else self.draws.c_index[case]
        missing = self.draws.missing[data_type][record] < self.manual_share
        repaired = bool(rule) and enabled(self.modules,rule[0]) and self.draws.linked[data_type][record] < self.coverage
        optional = bool(rule) and missing and not repaired
        core = retained*f[0]
        residual = (base-retained)*f[1] if optional else 0.
        combined = (n == 9 and enabled(self.modules,2)) or (n in (14,23) and enabled(self.modules,3))
        handover = P.params["human_factors"]["context_switch_s"] if n in (9,14,23) and not combined else 0.
        ui = float(self.draws.http[n][idx])
        if (n == 8 and enabled(self.modules,2)) or (n in (13,22) and enabled(self.modules,3)):
            # Commit both actions once, at the end of the common card.
            ui = 0.
        if n in (1,9,24) and enabled(self.modules,2 if n == 9 else 1):
            ui = float(self.draws.mqtt[n][idx])
        early_io = float(extra.get("notice_io",0.))
        work = core+residual+handover
        lab = 720.*f[2] if n == 7 else 0.
        finish = start+early_io+work+lab+ui
        visit.update(grant=start, service_start=start, finish=finish, manual_start=start+early_io,
                     manual_end=start+early_io+work, core=core, residual=residual, optional=int(optional),
                     switch=handover, base=work, system_s=ui+early_io, external_lab=lab,
                     active_s=finish-start, worker=P.ops[n]["primary_resource"])
        yield self.at(finish)
        if n == 19:
            car, pos = self.held_truck.pop(p)
            if car < self.fleet_size:
                self.cars[car] = (self.e.now,pos)

    def quality(self, case, raw):
        q = raw["q"]
        yield from self.op(case,6,quality=q)
        yield from self.op(case,7,quality=q,skip=None if raw["held"] else "prior_authorized_result")
        if raw["held"]:
            yield self.at(self.qready[q])
        yield from self.op(case,8,quality=q)
        status = "failed" if raw["failed"] else "released"
        self.record("quality_decision",case,quality=q,authority="quality",decision=status)
        yield from self.op(case,9,quality=q)
        self.registered_quality[q] = status
        self.record("quality_delivered",case,quality=q,decision=status)
        yield from self.op(case,11,quality=q,skip=None if raw["failed"] else "released")

    def identify(self, case, raw):
        pallet, zone = raw["pallet"], self.lane[case]
        yield self.at(raw["arrival"])
        start = self.e.now
        token = self.buffer.zones[zone].request()
        yield token
        if self.slots:
            yield from self.slots.admit(zone,pallet,case)
        assert abs(self.e.now-start) < 1.e-7
        self.in_buffer.add(pallet)
        self.record("enter",case,pallet,capacity=self.capacity,lane=zone)
        self.e.process(self.balance_event(pallet,True))
        visit = self.new(case,2,pallet,"receiver")
        idx = self.draws.p_index[pallet]
        prof = compare.P
        scan = (prof["tsd_damaged_mode_s"] if raw["label_bad"] else prof["tsd_normal_mode_s"])*raw["scan_factor"]
        check = prof["physical_check_mode_s"]*raw["check_factor"]
        system = float(self.draws.http[2][idx])
        route = "tsd"
        if enabled(self.modules,5):
            if raw["bypass"] < prof["chance_bypass"]:
                route = "bypass_tsd"
                system += prof["bypass_to_next_checkpoint_s"]
            else:
                system += prof["gate_setup_s"]
                decoded = False
                for attempt, field, prob in ((1,"read1","p_read_first_clean_bad"),(2,"read2","p_read_retry_clean_bad")):
                    if raw[field] < prof[prob][int(raw["label_bad"])]:
                        system += prof["success_decode_s"]
                        decoded = True
                        break
                    system += prof["no_read_timeout_s"]
                    if attempt == 1:
                        system += prof["retry_wait_s"]
                system += self.draws.mqtt[2][idx]
                wrong = raw["decode_type"] < prof["chance_ambiguous_after_decode"]+prof["chance_other_valid_label_after_decode"]
                if decoded and not wrong:
                    route = f"read_{attempt}"
                    scan = 0.
                else:
                    route = "fallback_tsd"
                    system += prof["walk_to_fallback_s"]
        work = check+scan
        finish = start+work+system
        visit.update(grant=start, service_start=start, finish=finish, manual_start=start+system,
                     manual_end=finish, core=check, residual=scan, base=work, system_s=system,
                     active_s=finish-start, worker="receiver")
        self.scans[pallet] = dict(case=case,lane=zone,arrival_s=start,buffer_admit_s=start,
                                  p02_done_s=finish,verified_sscc=raw["sscc"],route=route)
        yield self.at(finish)
        self.reg_history[zone][int(self.e.now//1800)] += 1
        return token

    def receipt(self, case):
        cid, t0 = case["case"],case["ready"]
        idx = self.draws.c_index[cid]
        linked = self.draws.linked["case"][idx] < self.coverage
        early = enabled(self.modules,1) and linked
        lead = compare.DRAFT_LEAD if early else self.baseline_lead
        draft = None
        if lead:
            yield self.at(max(0.,t0-lead))
            io = self.draws.mqtt[1][idx] if early else self.draws.http[1][idx]
            draft = self.e.process(self.op(cid,12,notice_io=float(io)))
        yield self.at(t0)
        raws = [self.x["pallets"][p] for p in case["members"]]
        scans = [self.e.process(self.identify(cid,r)) for r in raws]
        yield from self.op(cid,1)
        for raw in raws:
            if raw["q"] not in self.quality_known:
                self.quality_known[raw["q"]] = self.e.process(self.quality(cid,raw))
        for step in (3,4,5):
            yield from self.op(cid,step)
        if draft is None:
            yield from self.op(cid,12)
        else:
            yield draft
        for step in (13,14):
            yield from self.op(cid,step)
        assert not case["defect"]
        yield from self.op(cid,15,skip="valid")
        yield self.e.all_of([self.e.process(self.pallet(cid,r,s)) for r,s in zip(raws,scans)])
        for step in (22,23,24):
            yield from self.op(cid,step)
        self.record("exchange_ack",cid,version=1)
        yield from self.op(cid,25)
        self.closed[cid] = self.e.now

    def destination(self, group):
        if not self.pools[group]:
            self.extra[group] += 1
            name = f"TEMP{self.extra[group]}-{group*2600:04d}"
            self.addr[name] = Address(name,group,group==2,(40.,80.,30.)[group])
            self.free(name,notify=False)
        return self.pools[group][0][1]

    def choose(self, jobs):
        smart = enabled(self.modules,4)
        cars = list(self.cars.items()) or [(self.spares,(self.e.now,0.))]
        fifo_car = min(cars,key=lambda item:(item[1][0],item[0]))[0]
        simple, learned, fifo = [], [], []
        # Buffer occupancy and forecasts are fixed during this dispatch decision.
        risks_by_lane = {}
        for p, job in jobs.items():
            cid, _, sku, stage, _, _, reg = job
            group = 2 if stage == 10 else sku//10
            dest = self.destination(group)
            origin = self.addr[self.location[p]].distance if p in self.location else 0.
            loaded = abs(self.addr[dest].distance-origin)
            z = self.lane[cid]
            # With a single ready job the risk term cancels for all candidate trucks.
            risks = (0., 0.)
            if smart and len(jobs) > 1 and p not in self.location:
                if z not in risks_by_lane:
                    count = self.slots.occupancy(z) if self.slots else sum(self.z_of[k]==z for k in self.known_buffer)
                    risks_by_lane[z] = tuple(min(1.,max(0.,(count+f[z]-self.outlook[z]-20)/20))
                                             for f in (self.f_simple,self.f_ridge))
                risks = risks_by_lane[z]
            for car,(idle,pos) in cars:
                travel = 180. + (abs(pos-origin) + loaded) / self.travel_speed
                record = (reg,p,car,dest,pos,origin)
                if car == fifo_car:
                    fifo.append(record)
                simple.append((travel-(self.e.now-reg)-self.sref*risks[0],record))
                learned.append((travel-(self.e.now-reg)-self.sref*risks[1],record))
        default = min(fifo)
        s, m = min(simple)[1], min(learned)[1]
        choice = (m if enabled(self.modules,7) else s) if smart else default
        if jobs[choice[1]][0] in self.cohort:
            self.audit["dispatches"] += 1
            self.audit["multiple_ready"] += int(len(jobs)>1)
            self.audit["choice_ridge_vs_simple"] += int(s!=m)
            self.audit["two_idle_core_trucks"] += int(len(self.cars)==2)
            self.audit["different_truck_vs_fifo"] += int(choice[2]!=fifo_car)
            self.audit["immediate_spare"] += int(not self.cars)
        return choice

    def dispatch(self):
        while True:
            while self.jobs:
                _, p, car, dest, pos, origin = self.choose(self.jobs)
                cid,_,sku,stage,token,event,reg = self.jobs.pop(p)
                if car < self.fleet_size:
                    del self.cars[car]
                else:
                    self.spares += 1
                group = 2 if stage == 10 else sku//10
                assert heapq.heappop(self.pools[group])[1] == dest
                self.record("reserve",cid,p,address=dest,stage=stage,sku=sku,quality=self.x["pallets"][p]["q"])
                self.e.process(self.run_trip(cid,p,sku,stage,token,event,reg,car,dest,pos,origin))
            if self.wake.triggered:
                self.wake = self.e.event()
            yield self.wake

    def run_trip(self,cid,p,sku,stage,token,event,reg,car,dest,pos,origin):
        source = self.location.get(p)
        target = self.addr[dest].distance
        empty, loaded = abs(pos-origin),abs(target-origin)
        duration = 180.+empty+loaded  # both speeds 1 m/s in the frozen physical model
        now = self.e.now
        assert abs(now-reg) < 1.e-7
        v = self.flights.pop(p)
        v.update(grant=now,service_start=now,finish=now+duration,manual_start=now,
                 manual_end=now+duration,base=duration,active_s=duration,system_s=0.,
                 empty_m=empty,loaded_m=loaded,truck=car,address=dest,worker=f"driver/{car}")
        yield self.at(now+empty)
        self.record("pickup",cid,p,source=source or "BUFFER",destination=dest,vehicle=car)
        if source is None:
            self.in_buffer.remove(p)
            self.buffer.release(token)
            self.e.process(self.balance_event(p,False))
        else:
            self.free(source)
        yield self.at(now+duration)
        self.location[p] = dest
        self.record("deposit",cid,p,address=dest,stage=stage,sku=sku)
        if stage==18:
            self.held_truck[p] = (car,target)
        elif car < self.fleet_size:
            self.cars[car] = (self.e.now,target)
        event.succeed(dest)
        self.signal()


def run_arm(x, draws, models, mask, manual, coverage, lead, detailed=False):
    m = Warehouse(x, mask, draws, models, manual, coverage, lead).execute()
    cases = compare.case_report(m)
    audit = compare.invariants(m)
    assert not audit["violations"], audit["violations"][:5]
    assert len(cases) == len(m.cohort)
    assert (cases.success72 == ~cases.failed_quality).all()
    closed = cases.loc[cases.success72]
    assert np.allclose(closed[[f"stage{i}_s" for i in range(1,5)]].sum(axis=1), closed.y72_s)
    scan_routes = Counter(s["route"] for s in m.scans.values() if s["case"] in m.cohort)
    assert sum(scan_routes.values()) == int(cases.pallets.sum())
    assert all(s["arrival_s"] == s["buffer_admit_s"] for s in m.scans.values())
    visits, placed = [], []
    max_wait = 0.
    for v in m.visits:
        if v["grant"] is None or v["branch"] != "APPLIED":
            continue
        max_wait = max(max_wait, abs(v["grant"]-v["ready"]))
        if v["case"] not in m.cohort or v["finish"] is None or v["finish"] > m.end:
            continue
        n = v["op"]
        entity = v["pallet"] or v.get("quality") or v["case"]
        if n == 20:
            placed.append(v["finish"]-x["pallets"][entity]["arrival"])
        if detailed:
            human = 0. if v["manual_end"] is None else v["manual_end"]-v["manual_start"]
            visits.append((n,entity,v["attempt"],v["case"],v["finish"]-v["ready"],human,
                           v.get("core",0.),v.get("residual",0.),v.get("system_s",0.),
                           v.get("optional",0),v.get("empty_m",0.),v.get("loaded_m",0.)))
    assert max_wait < 1.e-7
    assert all(m.scans[p]["case"] not in m.cohort for p in m.held_truck)
    info = dict(cases=len(cases), pallets=int(cases.pallets.sum()), closed=len(closed),
                quality_failed=int(cases.failed_quality.sum()), mean_s=closed.y72_s.mean(),
                p50_s=closed.y72_s.median(), p90_s=closed.y72_s.quantile(.9),
                capped_all_mean_s=cases.y72_s.mean(), placed=len(placed),
                arrival_p20_mean_s=float(np.mean(placed)),arrival_p20_p50_s=float(np.median(placed)),
                arrival_p20_p90_s=float(np.quantile(placed,.9)),
                people_hours=cases.people_s.sum()/3600., max_resource_wait_s=max_wait,
                route_counts=dict(scan_routes),dispatch=dict(m.audit),extra_addresses=dict(m.extra),
                ledger_checks=sum(audit["checks"].values()), ledger_violations=0,
                auxiliary_checks=sum(v["op"]==26 and v["case"] in m.cohort for v in m.visits))
    details = None
    if detailed:
        cols = ["step","entity","attempt","case","seconds","human","core","residual","system",
                "optional","empty_m","loaded_m"]
        details = pd.DataFrame.from_records(visits, columns=cols).set_index(["step","entity","attempt"]).sort_index()
        assert details.index.is_unique
    return info, cases, details


def save_csv(frame, path, index=False):
    frame.to_csv(path,index=index,sep=";",decimal=",",encoding="utf-8-sig",float_format="%.6f")


def paired_steps(base, full):
    left, right = base.align(full,join="inner",axis=0)
    rows = []
    for n in range(1,27):
        row = dict(step=f"P{n:02d}", name=NAMES.get(n,"Ручная сверка показания буфера"),
                   unit=P.ops[n]["scope"] if n in P.ops else "pallet_event")
        for tag, frame in (("asis",base),("full",full)):
            part = frame.loc[n] if n in frame.index.get_level_values("step") else None
            row["visits_"+tag]=0 if part is None else len(part)
            row["all_mean_s_"+tag]=None if part is None else part.seconds.mean()
        count = (left.index.get_level_values("step")==n).sum()
        row["matched_visits"] = int(count)
        if count:
            a,b = left.loc[n],right.loc[n]
            for tag, frame in (("asis",a),("full",b)):
                row["mean_s_"+tag]=frame.seconds.mean()
                row["p10_s_"+tag]=frame.seconds.quantile(.1)
                row["p90_s_"+tag]=frame.seconds.quantile(.9)
                row["residual_s_"+tag]=frame.residual.mean()
                row["manual_action_rate_"+tag]=frame.optional.mean()
                row["system_s_"+tag]=frame.system.mean()
            row["saving_s"]=(a.seconds-b.seconds).mean()
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out",type=Path)
    parser.add_argument("--probe",action="store_true")
    parser.add_argument("--additional",choices=["modules","L1","L3"])
    parser.add_argument("--min-pairs",type=int,default=8)
    parser.add_argument("--max-pairs",type=int,default=30)
    args=parser.parse_args()
    assert 2 <= args.min_pairs <= args.max_pairs
    out=args.out
    out.mkdir(parents=True,exist_ok=True)
    load=args.additional if args.additional in ("L1","L3") else "L2"
    x=compare.input_for_load(load,out)
    expected=compare.digest(x)
    assert (x["scenario"],x["load"]) == ("S1",load)
    if load=="L2":
        assert expected==json.loads((compare.PREVIOUS/"results/result.json").read_bytes())["design"]["input_sha256"]
    assert all(not r["defect"] for r in x["receipts"]) and all(not p["placement_mismatch"] for p in x["pallets"].values())
    raw=json.loads(IO_FILE.read_bytes())
    models,days=compare.train_forecasters(x)
    design=dict(input_sha256=expected,io_sha256=P.sha(IO_FILE),base_seed=P.SEED,
                nominal_load=load,scenario="S1",training_days=days,test_days=x["days"],
                durations="arithmetic mean among matched receipts closed in 72 h; seconds",
                manual_share=.5,coverage=.9,baseline_lead_s=0,new_notice_lead_s=compare.DRAFT_LEAD,
                handover_s=P.params["human_factors"]["context_switch_s"],
                min_pairs=args.min_pairs,max_pairs=args.max_pairs,
                precision_rule="stop at first n >= min_pairs with t half-width <= max(1 s, 5% of mean paired saving)")
    draws=Inputs(x,P.SEED,raw)
    if args.additional:
        masks=({"H1":1,"H2":2,"H3":4,"H4":8,"H1_H3":5}
               if args.additional=="modules" else {"FULL":127})
        baseline,paired,_=run_arm(x,draws,models,0,.5,.9,0)
        extras=[dict(arm="ASIS",saving_s=0.,**baseline)]
        for name,mask in masks.items():
            metrics,cases,_=run_arm(x,draws,models,mask,.5,.9,0)
            assert paired.index.equals(cases.index) and paired.success72.equals(cases.success72)
            saving=(paired.y72_s-cases.y72_s)[paired.success72].mean()
            extras.append(dict(arm=name,saving_s=saving,**metrics))
            print(f"{load} {name} mean={metrics['mean_s']:.2f}s saving={saving:.2f}s",flush=True)
            gc.collect()
        design.update(completed_pairs=1)
        (out/"comparisons.json").write_text(json.dumps(dict(design=design,runs=extras),ensure_ascii=False,indent=2),encoding="utf-8")
        return
    if args.probe:
        for name in ("ASIS","FULL"):
            info,_,_=run_arm(x,draws,models,ARMS[name],.5,.9,0,True)
            print(name,json.dumps(info,ensure_ascii=False),flush=True)
            gc.collect()
        return
    metrics, central, cases_by_arm, details = [], [], {}, {}
    cache = {}
    delta_records=[]
    sensitivities=[]
    for rep in range(args.max_pairs):
        if rep:
            draws=Inputs(x,P.SEED+rep,raw)
        def measure(tag,manual=.5,coverage=.9,lead=0,keep=False):
            key=(rep,tag,manual,coverage if tag!="ASIS" else None,lead)
            if key not in cache:
                info,cases,operations=run_arm(x,draws,models,ARMS[tag],manual,coverage,lead,keep)
                row=dict(rep=rep+1,arm=tag,manual=manual,coverage=coverage,baseline_lead=lead,**info)
                metrics.append(row)
                cache[key]=cases[["success72","y72_s"]]
                if keep:
                    cases_by_arm[tag]=cases
                    if tag in ("ASIS","FULL"):
                        details[tag]=operations
                if rep==0 and (manual,coverage,lead)==(.5,.9,0):
                    central.append(row)
                print(f"pair {rep+1} {tag} manual={manual:g} coverage={coverage:g} lead={lead} mean={info['mean_s']:.2f}s",flush=True)
                gc.collect()
            return cache[key]
        def saving(a,b):
            assert a.index.equals(b.index) and a.success72.equals(b.success72)
            return (a.y72_s-b.y72_s)[a.success72]
        a=measure("ASIS",keep=rep==0)
        b=measure("FULL",keep=rep==0)
        effect=saving(a,b)
        delta_records.append(dict(rep=rep+1,mean_closed_s_asis=a.y72_s[a.success72].mean(),
                                  mean_closed_s_full=b.y72_s[b.success72].mean(),paired_saving_s=effect.mean()))
        if rep==0:
            for tag in ("GATE","SOFT","SOFT_GATE","NO_ML"):
                measure(tag,keep=True)
            # Deterministic negative control using the complete event engine a second time.
            _,placebo,_=run_arm(x,draws,models,0,.5,.9,0)
            assert cases_by_arm["ASIS"].equals(placebo)
            design["identical_policy_replay_equal"]=True
            design["changed_receipt_times_no_ml_to_full"]=int((saving(cache[rep,"NO_ML",.5,.9,0],b).abs()>1.e-6).sum())
            for manual,coverage,lead in ((0.,.9,0),(.25,.9,0),(.5,.9,0),(.75,.9,0),(1.,.9,0),(.5,.5,0),(.5,1.,0),(.5,.9,60)):
                r0=measure("ASIS",manual,coverage,lead)
                r1=measure("FULL",manual,coverage,lead)
                d=saving(r0,r1)
                baseline=r0.y72_s[r0.success72].mean()
                sensitivities.append(dict(manual=manual,coverage=coverage,baseline_lead_s=lead,
                                         mean_asis_s=baseline,mean_full_s=baseline-d.mean(),
                                         saving_s=d.mean(),saving_pct=100*d.mean()/baseline))
            steps=paired_steps(details["ASIS"],details["FULL"])
            save_csv(steps,out/"operations.csv")
            people=pd.concat(cases_by_arm,names=["arm","case"])
            people.to_parquet(out/"receipts.parquet")
            del steps,people,placebo
            cases_by_arm.clear()
            details.clear()
        d=np.array([r["paired_saving_s"] for r in delta_records])
        half=float(student_t.ppf(.975,len(d)-1)*d.std(ddof=1)/np.sqrt(len(d))) if len(d)>1 else None
        if len(d)>=args.min_pairs and half<=max(1.,.05*abs(d.mean())):
            break
        cache.clear()
    design["completed_pairs"]=len(d)
    design["paired_mean_saving_s"]=float(d.mean())
    design["mc_halfwidth_s"]=half
    design["paired_mean_baseline_s"]=float(np.mean([r["mean_closed_s_asis"] for r in delta_records]))
    design["paired_mean_full_s"]=float(np.mean([r["mean_closed_s_full"] for r in delta_records]))
    save_csv(pd.DataFrame(central).drop(columns=["route_counts","dispatch","extra_addresses"]),out/"scenarios.csv")
    save_csv(pd.DataFrame(sensitivities),out/"sensitivity.csv")
    save_csv(pd.DataFrame(delta_records),out/"replications.csv")
    with (out/"audit.json").open("w",encoding="utf-8") as stream:
        json.dump(dict(design=design,runs=metrics),stream,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({k:design[k] for k in ("completed_pairs","paired_mean_saving_s","mc_halfwidth_s")}),flush=True)


if __name__=="__main__":
    main()
