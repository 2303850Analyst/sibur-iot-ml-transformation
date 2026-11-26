"""Paired receipt-from-production experiment. Run in the existing project Docker image."""
import argparse
import gc
import gzip
import heapq
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "full_s1_l2"))
import revised_clean as old

P = old.P


class Fleet(old.Warehouse):
    """Same warehouse stock, addresses, background shipments and quality rules."""
    def __init__(self, x, name, draws, models, finite=False, coverage=.9, robot_s=75., stationary=False, fleet_size=2, travel_speed=1.0, quality_grace_s=0., dual_slots=0):
        mask = {"ASIS":0, "DATA":15, "GATE":63, "ROBOT":63, "RIDGE":127,
                "RETURN":63, "RETURN_SIMPLE":63, "RETURN_ML":127,"DATA_RETURN":15,"GATE_RETURN":63}[name]
        super().__init__(x, mask, draws, models, .5, coverage)
        if travel_speed <= 0:
            raise ValueError('Travel speed must be positive')
        self.travel_speed = travel_speed
        if quality_grace_s < 0:
            raise ValueError('Quality grace interval must be nonnegative')
        self.quality_grace_s = quality_grace_s
        self.recheck = quality_grace_s > 0
        # Only specific permanent quarantine positions may also hold released product.
        # Temporary overflow positions always require a transfer to a normal storage position.
        quarantine = sorted((a for a in self.addr.values() if a.quarantine), key=lambda a: (a.distance,a.name))
        if not 0 <= dual_slots <= len(quarantine):
            raise ValueError('Dual-use slots must fit within permanent quarantine storage')
        self.dual_slots = {a.name for a in quarantine[:dual_slots]}
        self.fixed_storage = False
        self.reject_hold_hours = 0.
        self.reject_pickups = {}
        self.name, self.finite = name, finite
        self.robot = name in ("ROBOT","RIDGE") or name.startswith("RETURN")
        self.gate = name in ("GATE","GATE_RETURN") or self.robot
        self.robot_s, self.stationary = robot_s, stationary
        self.next_launch = {"A":0., "B":0.}
        self.tags = defaultdict(dict)
        self.waits = []
        self.returning = {}
        self.fleet_size=fleet_size
        self.cars={i:(0.,0.) for i in range(fleet_size)}
        self.spares=fleet_size

    def idle(self,car,position):
        if car>=self.fleet_size:
            return
        self.cars[car]=(self.e.now,position)
        home = self.name in ("RETURN","GATE_RETURN","DATA_RETURN") or (self.name=="RETURN_SIMPLE" and sum(self.f_simple.values())>=6) or (
            self.name=="RETURN_ML" and sum(self.f_ridge.values())>=6)
        if home:
            self.returning[car]=(self.e.now,position)

    def choose(self,jobs):
        # Idle return trips are interruptible at the current physical coordinate.
        for car,(start,position) in self.returning.items():
            self.cars[car]=(start,max(0.,position-(self.e.now-start)*self.travel_speed))
        return super().choose(jobs)

    def op(self, case, n, *args, **kwargs):
        yield from super().op(case,n,*args,**kwargs)
        if n==19:
            self.signal()

    def dispatch(self):
        while True:
            # A scheduled handover of confirmed nonconforming stock uses the same fleet.
            while self.reject_pickups and self.cars:
                p = next(iter(self.reject_pickups))
                cid, address = self.reject_pickups.pop(p)
                origin = self.addr[address].distance
                for car,(start,pos) in self.returning.items():
                    self.cars[car] = (start, max(0.,pos-(self.e.now-start)*self.travel_speed))
                car = min(self.cars, key=lambda i: (abs(self.cars[i][1]-origin), i))
                _,position = self.cars.pop(car)
                self.returning.pop(car,None)
                self.e.process(self.handover_reject(cid,p,address,car,position))
            while self.jobs and (self.cars or not self.finite):
                candidates = self.jobs
                if self.fixed_storage:
                    candidates = {p: job for p,job in self.jobs.items()
                                  if self.pools[2 if job[3] == 10 else job[2] // 10]
                                  and (p in self.location or job[4].triggered)}
                    if not candidates:
                        break
                _,p,car,dest,pos,origin = self.choose(candidates)
                cid,_,sku,stage,token,event,registered = self.jobs.pop(p)
                if car<self.fleet_size:
                    del self.cars[car]
                    if car in self.returning:
                        _,position=self.returning.pop(car)
                        self.flights[p]['return_before_job_m'] = position - pos
                        if cid in self.cohort:
                            self.audit["return_m"]+=position-pos
                else:
                    self.spares += 1
                group = 2 if stage==10 else sku//10
                assert heapq.heappop(self.pools[group])[1]==dest
                self.record("reserve",cid,p,address=dest,stage=stage,sku=sku,quality=self.x["pallets"][p]["q"])
                now = self.e.now
                self.waits.append(now-registered)
                if self.name=="ASIS":
                    move = super().run_trip(cid,p,sku,stage,token,event,now,car,dest,pos,origin)
                else:
                    move = self.transfer(cid,p,stage,token,event,car,dest,pos,origin)
                self.e.process(move)
            if self.wake.triggered:
                self.wake = self.e.event()
            yield self.wake

    def schedule_reject(self,cid,p):
        yield self.e.timeout(self.reject_hold_hours * 3600.)
        self.reject_pickups[p] = (cid,self.location[p])
        self.signal()

    def handover_reject(self,cid,p,address,car,position):
        origin = self.addr[address].distance
        yield self.e.timeout(abs(position-origin) / self.travel_speed)
        q = self.x["pallets"][p]["q"]
        assert self.registered_quality[q] == "failed" and self.location[p] == address
        del self.location[p]
        self.record("reject_pickup",cid,p,address=address,quality=q,vehicle=car)
        self.free(address)
        # The approved handover bay is at the home end of the route in this sensitivity test.
        yield self.e.timeout(self.robot_s + origin / self.travel_speed)
        self.record("reject_handover",cid,p,vehicle=car)
        self.idle(car,0.)
        self.signal()

    def transfer(self,cid,p,stage,entry,event,car,dest,pos,origin):
        source = self.location.get(p)
        target = self.addr[dest].distance
        work = self.robot_s if self.robot else 180.
        empty, loaded = abs(pos-origin), abs(target-origin)
        if cid in self.cohort:
            self.audit["job_empty_m"]+=empty
        v = self.flights.pop(p)
        v["grant"] = self.e.now
        yield self.e.timeout(empty / self.travel_speed)
        if source is None:
            token = yield entry
        self.record("pickup",cid,p,source=source or "BUFFER",destination=dest,vehicle=car)
        if source is None:
            self.in_buffer.remove(p)
            self.buffer.release(token)
            self.e.process(self.balance_event(p,False))
        else:
            self.free(source)
        yield self.e.timeout(work + loaded / self.travel_speed)
        self.location[p]=dest
        self.record("deposit",cid,p,address=dest,stage=stage,sku=self.x["pallets"][p]["sku"])
        v.update(finish=self.e.now,address=dest,empty_m=empty,loaded_m=loaded)
        if stage==18:
            self.held_truck[p]=(car,target)
        else:
            self.idle(car,target)
        event.succeed(dest)
        self.signal()

    def admit(self,cid,p,boundary):
        yield self.at(boundary)
        lane=self.lane[cid]
        token=self.buffer.zones[lane].request()
        yield token
        if self.slots:
            yield from self.slots.admit(lane,p,cid)
        self.in_buffer.add(p)
        self.record("enter",cid,p,capacity=self.capacity,lane=lane)
        self.e.process(self.balance_event(p,True))
        if self.fixed_storage:
            self.signal()
        return token

    def step(self,cid,n,seconds,p=""):
        v=self.new(cid,n,p,P.ops[n]["primary_resource"])
        v.update(grant=self.e.now,finish=self.e.now+seconds)
        yield self.e.timeout(seconds)

    def data_delay(self,cid,n,p=""):
        idx=self.draws.p_index[p] if p else self.draws.c_index[cid]
        return float(self.draws.http[n][idx])

    def authorized(self,q,stamp,decision):
        yield self.at(stamp)
        self.record("quality_decision",quality=q,authority="quality",decision=decision)
        yield self.e.timeout(float(self.draws.mqtt[9][self.draws.q_index[q]]))
        self.registered_quality[q]=decision
        self.record("quality_delivered",quality=q,decision=decision)
        return decision

    def manifest(self,cid,t0,complete):
        yield self.at(max(0.,t0-60.) if complete else t0)
        i=self.draws.c_index[cid]
        yield self.e.timeout(float(self.draws.mqtt[1][i]))
        if not complete:
            # One source correction per document in S1 (one SKU per synthetic receipt).
            for n,seconds in ((12,45.),(13,50.),(14,30.)):
                yield from self.step(cid,n,seconds*self.draws.factors[n][1,i]+self.data_delay(cid,n))
        return True

    def scan(self,r):
        prof=old.compare.P
        manual=(prof["tsd_damaged_mode_s"] if r["label_bad"] else prof["tsd_normal_mode_s"])*r["scan_factor"]
        if not self.gate:
            return 0.,manual,"tsd"
        if r["bypass"]<prof["chance_bypass"]:
            return 0.,prof["bypass_to_next_checkpoint_s"]+manual,"bypass"
        elapsed=prof["gate_setup_s"]
        for attempt,key,prob in ((1,"read1","p_read_first_clean_bad"),(2,"read2","p_read_retry_clean_bad")):
            if r[key]<prof[prob][int(r["label_bad"])]:
                elapsed+=prof["success_decode_s"]
                ambiguous=r["decode_type"]<prof["chance_ambiguous_after_decode"]
                foreign=r["decode_type"]<prof["chance_ambiguous_after_decode"]+prof["chance_other_valid_label_after_decode"]
                candidate=None if ambiguous else r["wrong_sscc"] if foreign else r["sscc"]
                if candidate==r["sscc"]:
                    return elapsed,0.,f"read{attempt}"
                break
            elapsed+=prof["no_read_timeout_s"]+(prof["retry_wait_s"] if attempt==1 else 0.)
        return elapsed,prof["walk_to_fallback_s"]+manual,"fallback"

    def unit(self,cid,r,document):
        p,q,sku=r["pallet"],r["q"],r["sku"]
        yield self.at(r["ready"])
        launch=self.e.now
        if self.finite and self.gate:
            lane=self.lane[cid]
            launch=max(launch,self.next_launch[lane])
            self.next_launch[lane]=launch+10.  # 2 m pitch / 0.2 m/s
        boundary=launch+30.
        entry=self.e.process(self.admit(cid,p,boundary))
        idx=self.draws.p_index[p]
        machine,fallback,route=self.scan(r)
        inspection=old.compare.P["physical_check_mode_s"]*r["check_factor"]
        if self.gate:
            read_end=launch+machine+float(self.draws.mqtt[2][idx])
            if fallback:
                id_end=max(boundary,read_end)+fallback+inspection
            else:
                id_end=max(read_end,(boundary if self.stationary else launch)+inspection)
        else:
            id_end=boundary+fallback+inspection
        yield self.at(id_end)
        self.reg_history[self.lane[cid]][int(self.e.now//1800)]+=1
        yield document
        yield from self.step(cid,16,self.data_delay(cid,16,p),p)
        self.tags[p].update(route=route,boundary=boundary,launch=launch,registered=self.e.now,identification_s=id_end-launch)
        result=self.quality_known[q]
        quarantine_deadline = boundary + self.quality_grace_s
        if not result.triggered and self.e.now < quarantine_deadline:
            yield result | self.at(quarantine_deadline)
        if self.registered_quality.get(q)!="released":
            yield self.submit(cid,p,sku,10,entry)
        yield result
        if self.registered_quality[q]=="failed":
            self.record("open_nonconformity",cid,p,quality=q)
            if self.reject_hold_hours:
                self.e.process(self.schedule_reject(cid,p))
            yield self.waitforever
        yield from self.step(cid,17,self.data_delay(cid,17,p),p)
        held_address = self.location.get(p)
        if held_address in self.dual_slots:
            # The earlier P10 deposit is complete. WMS now verifies its existing location
            # after signed release; the pallet enters saleable inventory only after P20.
            assert self.registered_quality[q] == "released"
            yield from self.step(cid,19,self.data_delay(cid,19,p),p)
            self.record("release_at_address",cid,p,address=held_address,quality=q)
            yield from self.step(cid,20,self.data_delay(cid,20,p),p)
            self.stock[sku].append(held_address)
            return
        address=yield self.submit(cid,p,sku,18,entry)
        confirm=self.data_delay(cid,19,p) if self.robot else 25.*self.draws.factors[19][0,idx]+self.data_delay(cid,19,p)
        yield from self.step(cid,19,confirm,p)
        car,position=self.held_truck.pop(p)
        self.idle(car,position)
        self.signal()
        yield from self.step(cid,20,self.data_delay(cid,20,p),p)
        self.stock[sku].append(address)

    def receipt(self,case):
        if self.name=="ASIS":
            yield from super().receipt(case)
            return
        cid,t0=case["case"],case["ready"]
        i=self.draws.c_index[cid]
        complete=self.draws.linked["case"][i]<self.coverage
        doc=self.e.process(self.manifest(cid,t0,complete))
        tasks=[self.e.process(self.unit(cid,self.x["pallets"][p],doc)) for p in case["members"]]
        yield self.e.all_of(tasks)
        for n,t in ((22,self.data_delay(cid,22)),(23,20.*self.draws.factors[23][0,i]+self.data_delay(cid,23)),
                    (24,float(self.draws.mqtt[24][i]))):
            yield from self.step(cid,n,t)
        self.record("exchange_ack",cid,version=1)
        yield from self.step(cid,25,self.data_delay(cid,25))
        self.closed[cid]=self.e.now


def stage_profile(m):
    """Elapsed time per completed operation, preserving its pallet, case or quality scope."""
    durations = defaultdict(list)
    cohort_pallets = {p for c in m.x['receipts'] if c['case'] in m.cohort for p in c['members']}
    quality_objects = {m.x['pallets'][p]['q'] for p in cohort_pallets}
    for visit in m.visits:
        relevant = visit['quality'] in quality_objects if visit.get('quality') else visit['case'] in m.cohort
        if relevant and visit['branch'] == 'APPLIED' and visit['finish'] is not None and visit['finish'] <= m.e.now:
            durations[visit['op']].append(visit['finish'] - visit['request'])
    if m.name != 'ASIS':
        durations[1] = [float(m.draws.mqtt[1][m.draws.c_index[c]]) for c in m.cohort]
        durations[2] = [m.tags[p]['identification_s'] for p in cohort_pallets if 'identification_s' in m.tags[p]]
        decisions = {}
        for kind, t, _, _, fields in m.events:
            q = fields.get('quality')
            if q in quality_objects:
                if kind == 'quality_decision':
                    decisions[q] = t
                elif kind == 'quality_delivered':
                    durations[9].append(t - decisions[q])
    return {n: {'count': len(values), 'mean_s': float(np.mean(values))} for n, values in sorted(durations.items()) if values}


def labor_profile(m, baseline_labor=None):
    """Direct person-seconds attributable to the cohort, shared quality work counted once."""
    members = {p for c in m.x['receipts'] if c['case'] in m.cohort for p in c['members']}
    qualities = {m.x['pallets'][p]['q'] for p in members}
    seconds = Counter()
    for visit in m.visits:
        step = visit['op']
        quality = visit.get('quality')
        belongs = quality in qualities if quality else visit['case'] in m.cohort
        if not belongs or visit['branch'] != 'APPLIED' or visit['finish'] is None or visit['finish'] > m.e.now or step > 25:
            continue
        if m.name == 'ASIS':
            if visit['manual_end'] is not None:
                seconds[step] += visit['manual_end'] - visit['manual_start']
        elif not m.robot and step in (10, 18):
            # Attribute the driven portion of the idle return to the next assignment.
            driven_m = visit.get('return_before_job_m', 0.) + visit['empty_m'] + visit['loaded_m']
            seconds[step] += 180. + driven_m / m.travel_speed
        elif not m.robot and step == 19:
            seconds[19] += visit['finish'] - visit['grant'] - m.data_delay(visit['case'], 19, visit['pallet'])
        elif step in (12, 13, 14, 23):
            seconds[step] += visit['finish'] - visit['grant'] - m.data_delay(visit['case'], step)

    if m.name != 'ASIS':
        assert m.name in ('RETURN', 'GATE_RETURN', 'DATA_RETURN') and baseline_labor is not None
        # Same quality decisions and same distinct objects in paired runs. Preserve
        # inspection, sign-off and opening a nonconformity, even when outside the WMS path.
        for step in (7, 8, 11):
            seconds[step] = baseline_labor['step_seconds'][step]
        for p in sorted(members):
            row = m.x['pallets'][p]
            _, fallback, route = m.scan(row)
            assert route == m.tags[p]['route']
            if route == 'bypass':
                # Travel to the next checkpoint is pallet transit, not receiver work.
                fallback -= old.compare.P['bypass_to_next_checkpoint_s']
            seconds[2] += old.compare.P['physical_check_mode_s'] * row['check_factor'] + fallback

    roles = Counter()
    for n in range(1, 26):
        assert seconds[n] >= 0
        roles[P.ops[n]['primary_resource']] += seconds[n]
    total = sum(seconds.values())
    assert np.isclose(total, sum(roles.values()))
    return dict(step_seconds={n: seconds[n] for n in range(1, 26)}, role_seconds=dict(roles),
                total_hours=total / 3600, minutes_per_receipt=total / len(m.cohort) / 60,
                minutes_per_pallet=total / len(members) / 60)


def summarize(m,load):
    check=old.compare.invariants(m)
    assert not check["violations"],check["violations"][:3]
    cohort=[c for c in m.x["receipts"] if c["case"] in m.cohort]
    members={p for c in cohort for p in c["members"]}
    events=defaultdict(lambda:defaultdict(list))
    quarantine_visits = 0
    via_quarantine = set()
    authority={}
    for k,t,cid,p,f in m.events:
        if k=="quality_decision":
            authority[f["quality"]]=(t,f["decision"])
        if p in members and k in ("enter","pickup","deposit"):
            events[p][k].append(t)
            if k == "deposit" and f["stage"] == 10:
                quarantine_visits += 1
                via_quarantine.add(p)
    p20={v["pallet"]:v["finish"] for v in m.visits if v["op"]==20 and v["branch"]=="APPLIED" and v["pallet"] in members and v['finish'] is not None and v['finish'] <= m.e.now}
    times=[]; buffer=[]; no_stop=0; direct=0; capped=[]; closed=[]; examples=[]
    by_size=defaultdict(list)
    used=Counter()
    for c in cohort:
        cid=c["case"]; finish=m.closed.get(cid,float("inf"))
        rejected=any(m.x["pallets"][p]["failed"] for p in c["members"])
        elapsed=finish-c["ready"]
        if elapsed<=P.H:
            assert not rejected and all(p in p20 for p in c["members"])
            closed.append(elapsed)
            by_size[len(c["members"])].append(elapsed)
        capped.append(min(elapsed,P.H))
        for p in c["members"]:
            rr=m.x["pallets"][p]; e=events[p]
            if not e["pickup"]:
                continue
            b=e["pickup"][0]-rr["arrival"]
            buffer.append(b)
            used[m.tags[p].get("route","legacy")]+=1
            if p not in p20:
                continue
            assert not rr["failed"] and e["deposit"][-1]<=p20[p]
            lane_wait=m.tags[p].get("launch",rr["ready"])-rr["ready"]
            handoff=e["pickup"][0]-m.tags[p].get("boundary",rr["arrival"])
            assert min(lane_wait,handoff)>-1.e-7
            direct+=p not in via_quarantine
            no_stop+=p not in via_quarantine and max(lane_wait,handoff)<=5.
            times.append([e["deposit"][-1]-rr["arrival"],p20[p]-rr["arrival"]])
            if load=="L2" and cid=="R-18-79-1-7-0":
                examples.append(dict(case=cid,pallet=p,sscc=rr["sscc"],release=rr["ready"]-c["ready"],entry=rr["arrival"]-c["ready"],
                                     pickup=e["pickup"][0]-c["ready"],deposit=e["deposit"][-1]-c["ready"],p20=p20[p]-c["ready"],p25=elapsed))
    pairs=np.array(times)
    pressure=[r for r in m.issues if P.COHORT_FROM<=r[0]*1800<P.COHORT_TO]
    actual=Counter((int(r["arrival"]//1800),m.z_of[p]) for p,r in m.x["pallets"].items())
    maes=[np.mean([abs(r[j]-actual[r[0],r[1]]) for r in pressure]) for j in (2,3)] if pressure else [None,None]
    return dict(load=load,arm=m.name,finite=m.finite,fleet_size=m.fleet_size,coverage=m.coverage,handling=m.robot_s,stationary=m.stationary,
                cases=len(cohort),n=len(members),closed=len(closed),placed=len(times),first_pickups=len(buffer),
                failed_cases=sum(any(m.x["pallets"][p]["failed"] for p in c["members"]) for c in cohort),
                y72=np.mean(capped),doc=np.mean(closed),doc_p90=np.quantile(closed,.9),physical=np.mean(pairs[:,0]),
                p20=np.mean(pairs[:,1]),p20_p50=np.median(pairs[:,1]),p20_p90=np.quantile(pairs[:,1],.9),
                buffer=np.mean(buffer),first_pick_p90=np.quantile(buffer,.9),direct=direct,no_stop=no_stop,quarantine_trips=quarantine_visits,
                direct_within5_pct=100*no_stop/len(times),choices=m.audit["choice_ridge_vs_simple"],many_ready=m.audit["multiple_ready"],
                spare=m.audit["immediate_spare"],extra_addresses=dict(m.extra),max_dispatch_wait=max(m.waits),
                return_m=m.audit["return_m"],job_empty_m=m.audit["job_empty_m"],
                simple_mae=maes[0],ridge_mae=maes[1],reads=dict(used),ledger_checks=sum(check["checks"].values()),examples=examples,
                by_size={n:{"n":len(v),"mean_s":np.mean(v)} for n,v in by_size.items()}),authority


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("out",type=Path)
    parser.add_argument("load",choices=("L1","L2","L3"))
    parser.add_argument("--seed",type=int,default=P.SEED)
    parser.add_argument("--pair",action="store_true",help="Only the clean ASIS/RIDGE comparison")
    parser.add_argument("--home",action="store_true",help="Compare three idle return policies")
    parser.add_argument("--target",action="store_true",help="Validate the chosen return policy and size the high-load fleet")
    parser.add_argument("--partial",action="store_true",help="Test idle return with conventional forklifts")
    parser.add_argument("--stages",action="store_true",help="Compare all 25 operations on the clean baseline and chosen design")
    parser.add_argument("--woob", action="store_true", help="Run the conditional WOOB reach-truck scenarios for rack locations within 3 m")
    args=parser.parse_args()
    load=args.load
    archive=HERE.parent/"full_s1_l2"/"results"/"revised_clean"
    manifest=old.compare.INPUT_FILE if load=="L2" else archive/load/"input_manifest.json.gz"
    with gzip.open(manifest,"rt",encoding="utf8") as stream:
        x=json.load(stream)
    draws=old.Inputs(x,args.seed,json.loads(old.IO_FILE.read_bytes()))
    models,days=old.compare.train_forecasters(x)
    assert set(days).isdisjoint(x["days"])
    designs=[(name,f,.9,75.,False) for f in (False,True) for name in ("ASIS","RIDGE")]
    if load=="L2" and args.seed==P.SEED:
        designs.extend((name,f,.9,75.,False) for f in (False,True) for name in ("DATA","GATE","ROBOT"))
        designs.extend([("RIDGE",False,.7,75.,False),("RIDGE",False,1.,75.,False),("RIDGE",False,.9,180.,False),
                        ("RIDGE",False,.9,75.,True),("RIDGE",True,.9,180.,False)])
    if args.pair:
        designs=[("ASIS",False,.9,75.,False),("RIDGE",False,.9,75.,False)]
    if args.home:
        designs=[("ASIS",False,.9,75.,False)]
        policies=("RETURN","RETURN_SIMPLE","RETURN_ML") if load=="L2" else ("RETURN",)
        designs.extend((name,f,.9,75.,False) for f in (False,True) for name in policies)
    if args.target:
        designs=[("ASIS",False,.9,75.,False),("RETURN",False,.9,75.,False)]
        if args.seed==P.SEED and load=="L2":
            designs.extend([("RETURN",False,.7,75.,False),("RETURN",False,1.,75.,False),
                            ("RETURN",False,.9,180.,False),("RETURN",False,.9,75.,True),
                            ("RETURN",True,.9,180.,False)])
        if load=="L3":
            designs.extend(("RETURN",True,.9,75.,False,n) for n in (3,4))
    if args.partial:
        designs=[("ASIS",False,.9,75.,False)]
        designs.extend((name,f,.9,75.,False) for f in (False,True) for name in ("DATA_RETURN","GATE_RETURN"))
    if args.stages:
        designs=[("ASIS",False,.9,75.,False),("RETURN",False,.9,75.,False)]
    if args.woob:
        # The listed reach truck has a travel speed ceiling of 1.38 m/s. Operating
        # speeds and service times are scenario inputs, not manufacturer guarantees.
        designs = [("ASIS", False, .9, 75., False),
                   ("RETURN", False, .9, 100., False, 2, .8),
                   ("RETURN", True, .9, 100., False, 2, .8)]
        if load == 'L2':
            designs.extend(("RETURN", False, .9, handling, False, 2, speed)
                           for speed, handling in ((.6, 100.), (1.38, 100.), (.8, 75.), (.8, 180.)))
            designs.append(("GATE_RETURN", False, .9, 180., False))
    output=[]; decisions=None
    for design in designs:
        name,finite,coverage,handling,stationary=design[:5]
        m=Fleet(x,name,draws,models,*design[1:])
        if name!="ASIS":
            for q,(stamp,state) in decisions.items():
                m.quality_known[q]=m.e.process(m.authorized(q,stamp,state))
            for q in {p["q"] for p in x["pallets"].values()}-decisions.keys():
                m.quality_known[q]=m.e.event()
        m.execute()
        row,authority=summarize(m,load)
        if args.stages or args.woob:
            row['stages'] = stage_profile(m)
            row['labor'] = labor_profile(m, output[0]['labor'] if output else None)
        if args.woob:
            row['travel_m_s'] = m.travel_speed
        if decisions is None:
            decisions=authority
        assert set(authority)==set(decisions)
        assert row["n"]>=row["first_pickups"] and row["closed"]+row["failed_cases"]<=row["cases"]
        if not finite:
            assert row["n"]==row["first_pickups"] and row["closed"]+row["failed_cases"]==row["cases"],row
        assert not (finite and row["spare"]) and (finite or row["max_dispatch_wait"]==0.)
        if args.seed==P.SEED and name=="ASIS" and not finite:
            saved=json.loads(((archive/"audit.json") if load=="L2" else archive/load/"comparisons.json").read_bytes())["runs"][0]
            assert abs(row["doc"]-saved["mean_s"])<1e-6 and abs(row["p20"]-saved["arrival_p20_mean_s"])<1e-6
        output.append(row)
        print(load,args.seed,name,finite,coverage,handling,stationary,round(row["doc"],1),round(row["p20"],1),flush=True)
        del m
        gc.collect()
    suffix="_woob" if args.woob else "_stages" if args.stages else "_partial" if args.partial else "_target" if args.target else "_home" if args.home else "_pair" if args.pair else ""
    dest=args.out/f"{load}_{args.seed}{suffix}.json"
    dest.write_text(json.dumps(dict(manifest=old.compare.digest(x),seed=args.seed,runs=output),ensure_ascii=False,indent=2,allow_nan=False,
                               default=lambda v:v.item()),encoding="utf8")


if __name__=="__main__":
    main()
