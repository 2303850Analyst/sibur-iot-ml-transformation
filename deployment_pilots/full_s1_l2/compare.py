"""Paired full-process experiment on the two receiving lanes under three loads."""
import argparse
import gc
import gzip
import heapq
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import simpy
from sklearn.linear_model import Ridge

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / "h5_gate_a_s1_l2"
sys.path.insert(0, str(PREVIOUS))
import run as pilot
from inputs import fixtures, features, sampled, source, rng, digest, DAY
from ledger import invariants

P = pilot.PROFILE
DRAFT_LEAD = 60
POLL = 2
TRANSPORT = 0.3
SENSOR_DELAY_PROB = 0.005
SENSOR_DELAY = 10
INPUT_FILE = PREVIOUS / "results/input_manifest.json.gz"
NOMINAL_PALLETS_PER_DAY = {"L1": 300, "L2": 450, "L3": 900}

def input_for_load(load, out, seed=pilot.SEED):
    if load == "L2" and seed == pilot.SEED:
        with gzip.open(INPUT_FILE, "rt", encoding="utf-8") as stream:
            return json.load(stream)

    events = fixtures("S1", load, seed)
    for receipt in events["receipts"]:
        lane_draw = rng(seed, receipt["case"], "entry_lane").random()
        receipt["lane"] = "A" if lane_draw < P["lane_A_case_probability"] else "B"
    for index, pallet in enumerate(events["pallets"].values()):
        identity = pallet["pallet"]
        pallet["sscc"] = pilot.synthetic_sscc(index + 1)
        pallet["wrong_sscc"] = pilot.synthetic_sscc(9000000 + index)
        for field in ("bypass", "read1", "read2", "decode_type"):
            pallet[field] = rng(seed, identity, "gate_" + field).random()
        pallet["scan_factor"] = rng(seed, identity, "tsd_service").triangular(0.7, 1.3, 1)
        pallet["check_factor"] = rng(seed, identity, "physical_inspection").triangular(0.7, 1.3, 1)
    if out is not None:
        with gzip.open(out / "input_manifest.json.gz", "wt", encoding="utf-8") as stream:
            json.dump(events, stream, separators=(",", ":"))
    return events

def train_forecasters(x):
    """Only the historical training partition, with the same case-level lane assignment law."""
    days = source()[1]["partitions"]["train"]
    assert not (set(days) & set(x["days"]))
    table = sampled(x["load"], pilot.SEED)
    rows = table[table.direction.eq("delivery") & table.day.isin(days)].copy()
    rows["hour"] = rows.tod // 3600
    n_bins = len(days) * 48
    counts = {z: np.zeros(n_bins) for z in "AB"}
    assert list(days) == list(range(days[0], days[-1] + 1))
    for key, group in rows.groupby(["day", "source_sku", "dock", "hour"]):
        stamps = sorted(group.tod.astype(int))
        for i in range(0, len(stamps), 8):
            identity = "train-" + "-".join(map(str, key)) + "-" + str(i // 8)
            zone = "A" if rng(pilot.SEED, identity, "entry_lane").random() < 0.7 else "B"
            for t in stamps[i:i+8]:
                j = ((int(key[0])-days[0])*DAY + t + 30)//1800
                if j < n_bins:
                    counts[zone][j] += 1
    models = {}
    for z, count in counts.items():
        mean = float(count.mean())
        a = [features(count, i, mean) for i in range(48, n_bins)]
        fitted = Ridge(alpha=10).fit(a, count[48:])
        models[z] = dict(w=fitted.coef_.tolist(), b=float(fitted.intercept_), mean=mean)
    return models, days

class BufferPlaces:
    """A level change becomes available to the dispatcher after two equal samples and transport."""
    def __init__(self, model):
        self.m = model
        self.physical = {z: [False]*20 for z in "AB"}
        self.book = {z: [False]*20 for z in "AB"}
        self.report = {z: [False]*20 for z in "AB"}
        self.version = Counter()
        self.pallet_slot = {}
        self.changed = model.e.event()

    def wake(self):
        previous = self.changed
        self.changed = self.m.e.event()
        previous.succeed()
        self.m.signal()

    def write_sensor(self, z, i, value, version, pallet, case):
        now = self.m.e.now
        phase = (i % 8)/4
        first = math.ceil((now-phase)/POLL)*POLL + phase
        stable_at = first + POLL + TRANSPORT
        if rng(pilot.SEED, pallet, "beam_delay", int(value)).random() < SENSOR_DELAY_PROB:
            stable_at += SENSOR_DELAY
            self.m.counts["delayed_slot_reports"] += 1
        yield self.m.at(stable_at)
        if self.version[z,i] == version:
            self.report[z][i] = value
            self.wake()

    def check_disagreement(self,z,i,version,pallet,case):
        yield self.m.e.timeout(3*POLL)
        if self.version[z,i]!=version or self.report[z][i]==self.book[z][i]:
            return
        m=self.m
        v=m.new(case,26,pallet,"receiver")
        a,b,w,sw,fat=m.crew.reserve("receiver",m.e.now,8,case,26,v["id"])
        v.update(grant=a,service_start=a,finish=b,manual_start=a,manual_end=b,worker=w,base=8,switch=sw,fatigue=fat)
        yield m.at(b)
        if self.version[z,i]==version:
            self.report[z][i]=self.physical[z][i]
            self.wake()
        m.counts["manual_slot_checks"]+=1

    def change(self,z,i,value,pallet,case):
        assert self.physical[z][i] != value
        self.physical[z][i] = value
        self.book[z][i] = value  # local transaction: claim or driver confirmation at pickup
        self.version[z,i]+=1
        v=self.version[z,i]
        self.m.e.process(self.write_sensor(z,i,value,v,pallet,case))
        self.m.e.process(self.check_disagreement(z,i,v,pallet,case))
        self.wake()

    def admit(self,z,pallet,case):
        while True:
            for i,(book,bit) in enumerate(zip(self.book[z],self.report[z])):
                if not book and not bit:
                    self.pallet_slot[pallet]=i
                    self.change(z,i,True,pallet,case)
                    return
            yield self.changed

    def pickup(self,z,pallet,case):
        i=self.pallet_slot.pop(pallet)
        self.change(z,i,False,pallet,case)

    def occupancy(self,z):
        return sum(a or b for a,b in zip(self.book[z],self.report[z]))

class Warehouse(pilot.Pilot):
    def __init__(self,x,level,recheck,ml,sref):
        super().__init__(x,pilot.CONTROL)
        self.level=level
        self.recheck=recheck
        self.ml=ml
        self.sref=sref
        self.z_of={p:c["lane"] for c in x["receipts"] for p in c["members"]}
        self.gates={z:SimpleNamespace(e=self.e,gate=simpy.Resource(self.e,1)) for z in "AB"}
        self.slots=BufferPlaces(self) if level>=2 else None
        self.reg_history={z:np.zeros(34*48+1) for z in "AB"}
        self.out_history={z:np.zeros(34*48+1) for z in "AB"}
        self.f_simple={z:0.0 for z in "AB"}
        self.f_ridge=self.f_simple.copy()
        self.outlook=self.f_simple.copy()
        self.issues=[]
        self.decision_rows=[]
        if level:
            self.fork=simpy.FilterStore(self.e,capacity=2)
            for i in range(2):
                self.fork.put((i,0.))

    def cancel_pending(self,p):
        job=self.jobs.pop(p)
        v=self.flights.pop(p)
        v.update(grant=self.e.now,service_start=self.e.now,finish=self.e.now,branch="CANCELLED_AFTER_RELEASE")
        job[5].succeed(None)
        self.counts["quarantine_jobs_cancelled"]+=1
        self.signal()

    def submit(self,c,p,sku,stage,token):
        event=super().submit(c,p,sku,stage,token)
        if self.recheck and stage==10 and self.registered_quality.get(self.x["pallets"][p]["q"])=="released":
            self.cancel_pending(p)
        return event

    def record(self,kind,c="",p="",**data):
        super().record(kind,c,p,**data)
        if kind=="pickup" and data["source"]=="BUFFER":
            z=self.lane[c]
            self.out_history[z][int(self.e.now//1800)]+=1
            if self.slots:
                self.slots.pickup(z,p,c)
        if self.recheck and kind=="quality_delivered" and data["decision"]=="released":
            for pallet, job in list(self.jobs.items()):
                if job[3]==10 and self.x["pallets"][pallet]["q"]==data["quality"]:
                    self.cancel_pending(pallet)

    def identify(self,c,raw):
        if self.level<2:
            token=yield from super().identify(c,raw)
            self.reg_history[self.lane[c]][int(self.e.now//1800)]+=1
            return token
        p,z=raw["pallet"],self.lane[c]
        yield self.at(raw["arrival"])
        s=dict(case=c,lane=z,pallet=p,arrival_s=raw["arrival"],buffer_admit_s=None,id_correct_s=None,
               p02_done_s=None,route="waiting_buffer",attempts=0,raw_decoded_sscc=None,verified_sscc=None)
        self.scans[p]=s
        resource=self.buffer.zones[z]
        token=resource.request()
        yield token
        yield from self.slots.admit(z,p,c)
        self.in_buffer.add(p)
        self.max_lane_count[z]=max(self.max_lane_count[z],resource.count)
        self.record("enter",c,p,capacity=self.capacity,lane=z)
        self.e.process(self.balance_event(p,True))
        s["buffer_admit_s"]=self.e.now
        v=self.new(c,2,p,"receiver",raw_arrival=raw["arrival"])
        v["ready"]=raw["arrival"]
        if raw["bypass"]<P["chance_bypass"]:
            s["route"]="bypass_tsd"
            yield self.e.timeout(P["bypass_to_next_checkpoint_s"])
        else:
            yield from pilot.Pilot.gate_window(self.gates[z],raw,s)
            if not s["route"].startswith("read_"):
                yield self.e.timeout(P["walk_to_fallback_s"])
        auto=s["route"] in ("read_1","read_2")
        if auto:
            assert s["raw_decoded_sscc"]==raw["sscc"]
        correct=self.e.now
        scan=0 if auto else raw["scan_factor"]*P["tsd_damaged_mode_s" if raw["label_bad"] else "tsd_normal_mode_s"]
        check=raw["check_factor"]*P["physical_check_mode_s"]
        begin,end,w,sw,fat=self.crew.reserve("receiver",self.e.now,scan+check,c,2,v["id"])
        if not auto:
            correct=begin+sw+scan*fat+TRANSPORT
        finish=max(end,correct)
        v.update(grant=begin,service_start=begin,manual_start=begin,manual_end=end,finish=finish,worker=w,base=scan+check,switch=sw,fatigue=fat)
        s.update(id_correct_s=correct,p02_done_s=finish,verified_sscc=raw["sscc"])
        yield self.at(finish)
        self.reg_history[z][int(self.e.now//1800)]+=1
        return token

    def grouped(self,c,steps,**fields):
        """One card, same worker and same per-step service draws; no duplicate hand-offs."""
        role=pilot.engine.ops[steps[0]]["primary_resource"]
        visits=[self.new(c,n,"",role,**fields) for n in steps]
        work=[self.draw(v,pilot.engine.ops[v["op"]]["manual_labor_s"],"manual") for v in visits]
        begin,end,w,sw,fat=self.crew.reserve(role,self.e.now,sum(work),c,steps[0],visits[0]["id"])
        cursor=begin
        for j,(v,d) in enumerate(zip(visits,work)):
            setup=sw if j==0 else 0
            if j: v.update(ready=cursor,request=cursor)
            finish=cursor+setup+d*fat
            v.update(grant=cursor,service_start=cursor,finish=finish,manual_start=cursor,manual_end=finish,
                     worker=w,base=d,switch=setup,fatigue=fat)
            cursor=finish
        assert abs(cursor-end)<1e-6
        return visits

    def quality(self,c,raw):
        if self.level==0:
            yield from super().quality(c,raw)
            return
        q=raw["q"]
        yield from self.op(c,6,quality=q)
        yield from self.op(c,7,quality=q,skip=None if raw["held"] else "prior_authorized_result")
        if raw["held"]:yield self.at(self.qready[q])
        signed,checked=self.grouped(c,[8,9],quality=q)
        status="failed" if raw["failed"] else "released"
        yield self.at(signed["finish"])
        self.record("quality_decision",c,quality=q,authority="quality",decision=status)
        yield self.at(checked["finish"])
        yield self.network()
        self.registered_quality[q]=status
        self.record("quality_delivered",c,quality=q,decision=status)
        yield from self.op(c,11,quality=q,skip=None if raw["failed"] else "released")

    def receipt(self,case):
        if self.level==0:
            yield from super().receipt(case)
            return
        c=case["case"]
        yield self.at(max(0,case["ready"]-DRAFT_LEAD)+TRANSPORT)
        draft=self.e.process(self.op(c,12))  # human prep charged even before physical readiness
        yield self.at(case["ready"])
        raw=[self.x["pallets"][p] for p in case["members"]]
        scans=[self.e.process(self.identify(c,r)) for r in raw]
        yield from self.op(c,1)
        yield self.network()
        for r in raw:
            if r["q"] not in self.quality_known:
                self.quality_known[r["q"]]=self.e.process(self.quality(c,r))
        yield from self.op(c,3)
        if case["defect"]:
            yield from self.op(c,3,duration=30,result_detail="document_clarification")
        for n in (4,5):
            yield from self.op(c,n)
        yield draft
        merged=self.grouped(c,[13,14])
        yield self.at(merged[-1]["finish"])
        yield from self.op(c,15,skip=None if case["defect"] else "valid")
        if case["defect"]:yield from self.op(c,14)
        yield self.e.all_of([self.e.process(self.pallet(c,r,s)) for r,s in zip(raw,scans)])
        merged=self.grouped(c,[22,23])
        yield self.at(merged[-1]["finish"])
        yield self.network()
        self.record("exchange_ack",c,version=1)
        for n in (24,25):yield from self.op(c,n)
        self.closed[c]=self.e.now

    def forecasts(self):
        if self.level==0:
            yield from super().forecasts()
            return
        for idx in range(34*48):
            yield self.at(idx*1800)
            for z in "AB":
                model=self.ml[z]
                feat=features(self.reg_history[z],idx,model["mean"])
                self.f_simple[z]=float(np.mean(feat[:4]))
                self.f_ridge[z]=max(0,float(np.dot(model["w"],feat)+model["b"]))
                prev=self.out_history[z][max(0,idx-4):idx]
                self.outlook[z]=(prev.sum()+max(0,4-len(prev))*model["mean"])/4
                self.issues.append((idx,z,self.f_simple[z],self.f_ridge[z]))
            self.signal()

    def dispatch(self):
        if self.level==0:
            yield from super().dispatch()
            return
        while True:
            while self.jobs and self.fork.items:
                count=(dict((z,self.slots.occupancy(z)) for z in "AB") if self.slots
                       else Counter(self.z_of[p] for p in self.known_buffer))
                pressure=lambda f,z: min(1.,max(0.,(count.get(z,0)+f[z]-self.outlook[z]-20)/20))
                simple=[];learned=[]
                for p,job in self.jobs.items():
                    c,_,sku,stage,token,ev,reg=job
                    g=2 if stage==10 else sku//10
                    if not self.pools[g]: continue
                    dest=self.pools[g][0][1]
                    src=self.location.get(p)
                    origin=self.addr[src].distance if src else 0.
                    z=self.lane[c]
                    for tr,pos in self.fork.items:
                        service=180+abs(pos-origin)+abs(self.addr[dest].distance-origin)
                        score=service-(self.e.now-reg)
                        for f,listing in ((self.f_simple,simple),(self.f_ridge,learned)):
                            penalty=self.sref*pressure(f,z) if not src else 0.
                            listing.append((score-penalty,reg,p,dest,tr))
                if not simple:break
                s,l=min(simple),min(learned)
                _,_,p,dest,tr=l if self.level==3 else s
                self.decision_rows.append((self.e.now,p,dest,tr,s[2],l[2],s[4],l[4]))
                c,_,sku,stage,token,ev,reg=self.jobs.pop(p)
                g=2 if stage==10 else sku//10
                assert heapq.heappop(self.pools[g])[1]==dest
                self.record("reserve",c,p,address=dest,stage=stage,sku=sku,quality=self.x["pallets"][p]["q"])
                truck=yield self.fork.get(lambda t:t[0]==tr)
                self.e.process(self.move(c,p,sku,stage,token,ev,reg,truck,dest))
            if self.wake.triggered:self.wake=self.e.event()
            yield self.wake

def case_report(m):
    inputs=[c for c in m.x["receipts"] if pilot.COHORT_FROM<=c["ready"]<pilot.COHORT_TO]
    cohorts={c["case"] for c in inputs}
    assert all(c["ready"]+pilot.H<=m.end for c in inputs)
    by_case=defaultdict(list)
    for v in m.visits:
        if v["case"] in cohorts:by_case[v["case"]].append(v)
    qdone={d["quality"]:(t,d["decision"]) for kind,t,c,p,d in m.events if kind=="quality_delivered"}
    rows=[]
    for c in inputs:
        cid=c["case"]; t0=c["ready"]; close=m.closed.get(cid)
        success=close is not None and close<=t0+pilot.H
        stop=close if success else t0+pilot.H
        readings=[m.scans[p] for p in c["members"]]
        visits=by_case[cid]
        by_op=defaultdict(list)
        labor=defaultdict(float)
        for v in visits:
            if v["branch"]=="APPLIED" and v["finish"] is not None and v["finish"]<=stop:by_op[v["op"]].append(v)
            a,b=v["manual_start"],v["manual_end"]
            if a is not None and b is not None:
                # Includes any work on this receipt before t0 and stops at the measurement horizon.
                labor[v["role"]]+=max(0,min(stop,b)-a)
        qs={m.x["pallets"][p]["q"] for p in c["members"]}
        rejected=any(q in qdone and qdone[q][1]=="failed" and qdone[q][0]<=stop for q in qs)
        assert not (success and rejected)
        row=dict(case=cid,lane=c["lane"],pallets=len(readings),ready_s=t0,closed_s=close,
                 success72=success,y72_s=stop-t0,failed_quality=rejected,
                 people_s=sum(labor.values()),receiver_s=labor["receiver"],driver_s=labor["forklift_driver"],
                 admitted=sum(s["buffer_admit_s"] is not None and s["buffer_admit_s"]<=stop for s in readings))
        for n in (2,5,16,20,25):
            row[f"p{n:02}_last_s"]=max((v["finish"] for v in by_op[n]),default=None)
        if success:
            assert set(c["members"])=={v["pallet"] for v in by_op[16]}=={v["pallet"] for v in by_op[20]}
            for v in by_op[16]:
                s=m.scans[v["pallet"]]
                assert s["verified_sscc"]==m.x["pallets"][v["pallet"]]["sscc"] and s["p02_done_s"]<=v["finish"]
            assert all(qdone[q][1]=="released" for q in qs)
            t1=max(row["p02_last_s"],row["p05_last_s"])
            q=max(qdone[k][0] for k in qs)
            row["last_quality_s"]=q
            t2=max(t1,row["p16_last_s"],q)
            t3=max(t2,row["p20_last_s"])
            assert t0<=t1<=t2<=t3<=close
            for i,(lo,hi) in enumerate(zip([t0,t1,t2,t3],[t1,t2,t3,close]),1):
                row[f"stage{i}_s"]=hi-lo
        rows.append(row)
    return pd.DataFrame(rows).set_index("case").sort_index()

def summarize(m,frame):
    audit=invariants(m)
    assert not audit["violations"],audit["violations"][:3]
    assert all(v<=20 for v in m.max_lane_count.values())
    closed=frame[frame.success72]
    recs=[s for s in m.scans.values() if s["case"] in frame.index]
    assert len(recs)==int(frame.pallets.sum())
    assert len({s["pallet"] for s in recs})==len(recs)
    last=pilot.end_state(m)
    forecasts={}
    if m.level:
        actual=Counter((int(raw["arrival"]//1800),m.z_of[p]) for p,raw in m.x["pallets"].items())
        issues=[i for i in m.issues if pilot.COHORT_FROM<=i[0]*1800<pilot.COHORT_TO]
        for col,label in [(2,"simple_mae"),(3,"ridge_mae")]:
            forecasts[label]=float(np.mean([abs(v[col]-actual[v[0],v[1]]) for v in issues]))
    choices=[row for row in m.decision_rows if pilot.COHORT_FROM<=row[0]<pilot.COHORT_TO]
    return dict(cases=len(frame),pallets=int(frame.pallets.sum()),closed72=len(closed),
                q72_pct=100*len(closed)/len(frame),mean_y72_min=float(frame.y72_s.mean()/60),
                mean_closed_min=float(closed.y72_s.mean()/60),median_closed_min=float(closed.y72_s.median()/60),
                p90_closed_min=float(closed.y72_s.quantile(.9)/60),quality_negative=int(frame.failed_quality.sum()),
                total_labor_h=float(frame.people_s.sum()/3600),receiver_h=float(frame.receiver_s.sum()/3600),
                driver_h=float(frame.driver_s.sum()/3600),admitted_within_case_window=int(frame.admitted.sum()),
                route_counts_through_sim_end=dict(Counter(s["route"] for s in recs)),
                forecast_diagnostics=forecasts,dispatch_decisions_in_cohort_period=len(choices),
                different_simple_vs_ridge_choices=sum(row[4]!=row[5] or row[6]!=row[7] for row in choices),
                additional_checks=dict(m.counts),end_state=last,integrity=audit)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("out",type=Path)
    parser.add_argument("--load", choices=list(NOMINAL_PALLETS_PER_DAY), default="L2")
    args=parser.parse_args()
    out=args.out
    out.mkdir(parents=True,exist_ok=True)
    x=input_for_load(args.load, out)
    if args.load=="L2":
        previous=json.loads((PREVIOUS/"results/result.json").read_bytes())
        assert digest(x)==previous["design"]["input_sha256"]
    models,days=train_forecasters(x)
    baseline={k:dict(v) for k,v in pilot.ops.items()}
    for label,(_,mode) in pilot.settings["retained_manual_modes_s"].items():
        baseline[int(label[1:])]["manual_labor_s"]=mode
    summary={};frames={}
    sources=[Path(__file__).resolve(),PREVIOUS/"run.py",pilot.V3/"src/process.py",pilot.V3/"src/inputs.py",
             pilot.V3/"v3_2/engine.py",pilot.V3/"v3_2/crew.py",pilot.V3/"v3_2/ledger.py",pilot.V3/"v3_2/settings.json",
             pilot.V3/"v3_2/design/protocol/ASIS_BASELINE_PARAMETER_FREEZE.json",pilot.V3/"odoo_addons/wms_research_v3/policy.py",
             pilot.PROJECT/"series_py312/data/normalized/source.parquet",pilot.PROJECT/"series_py312/data/manifests/wepastacks.json"]
    design=dict(input_sha256=digest(x),seed=pilot.SEED,scenario=x["scenario"],load=x["load"],
                nominal_pallets_per_day=NOMINAL_PALLETS_PER_DAY[args.load],training_days=days,test_source_days=x["days"],
                models=models,sku_data="WEPAStacks",early_notice_s=DRAFT_LEAD,slot_poll_s=POLL,transport_s=TRANSPORT,
                delayed_report_probability=SENSOR_DELAY_PROB,delay_duration_s=SENSOR_DELAY,
                slot_recheck_work_s=8,slot_filter_samples=2,reader_profile=P,
                resource_assumptions=pilot.params["staffing_reference_assumption"],cohort_days=30,followup_hours=72,
                stage_units="elapsed seconds on the critical milestone path",source_sha256={str(s.relative_to(pilot.PROJECT)):pilot.sha(s) for s in sources})
    sref=0.
    treatments=[(False,[0,1,2,3])]
    pairs=[("R0","R3")]
    if args.load=="L2":
        treatments.append((True,[0,3]))
        pairs.append(("R0_recheck","R3_recheck"))
    with patch.object(pilot.engine,"ops",baseline):
        for recheck,levels in treatments:
            for level in levels:
                label=f"R{level}"+("_recheck" if recheck else "")
                m=Warehouse(x,level,recheck,models,sref).execute()
                frame=case_report(m)
                if label=="R0":
                    trips=[v["base"] for v in m.visits if v["op"] in (10,18) and v["machine_start"] is not None and v["machine_start"]<DAY]
                    sref=float(np.median(trips))
                    design["sref_s"]=sref
                if label=="R0" and args.load=="L2":
                    old=pd.read_parquet(PREVIOUS/f"results/cases_{pilot.CONTROL}.parquet").set_index("case").sort_index()
                    for col in ["ready_s","pallets","y72_s","receiver_s"]:
                        assert np.allclose(frame[col],old[col],rtol=0,atol=1e-6),col
                    assert frame.success72.equals(old.success72)
                frames[label]=frame
                summary[label]=summarize(m,frame)
                frame.to_parquet(out/f"{label}_cases.parquet")
                if level:
                    pd.DataFrame(m.decision_rows,columns=["model_s","pallet","address","truck","simple_choice","ridge_choice","simple_truck","ridge_truck"]).to_parquet(out/f"{label}_dispatch.parquet",index=False)
                print(args.load,label,round(summary[label]["mean_y72_min"],3),summary[label]["closed72"],round(summary[label]["total_labor_h"],2),flush=True)
                del m
                gc.collect()
    joined=frames["R0"][["ready_s","lane","pallets"]].copy()
    for key,df in frames.items():
        assert joined[["ready_s","lane","pallets"]].equals(df[["ready_s","lane","pallets"]])
        for col in ["success72","y72_s","people_s","receiver_s"]:
            joined[f"{key}_{col}"]=df[col]
    joined.to_csv(out/"all_cases.csv",float_format="%.7f")
    paired={}
    for a,b in pairs:
        left,right=frames[a],frames[b]
        common=left.success72 & right.success72
        stats={}
        for label,df in [(a,left),(b,right)]:
            stages=df.loc[common,[f"stage{i}_s" for i in range(1,5)]]
            assert np.allclose(stages.sum(axis=1),df.loc[common,"y72_s"])
            stats[label]=(stages.mean()/60).to_dict()
        paired[f"{a}_vs_{b}"]=dict(common_closed=int(common.sum()),gained=int((~left.success72 & right.success72).sum()),
            lost=int((left.success72 & ~right.success72).sum()),mean_saved_min=float((left.y72_s-right.y72_s).mean()/60),
            earlier_percent=float(100*(left.y72_s-right.y72_s).sum()/left.y72_s.sum()),stage_means_min=stats)
    assert digest(x)==design["input_sha256"]
    pilot.write_json(out/"summary.json",dict(design=design,arms=summary,paired=paired))
    print(json.dumps(paired),flush=True)

if __name__=="__main__":
    main()
