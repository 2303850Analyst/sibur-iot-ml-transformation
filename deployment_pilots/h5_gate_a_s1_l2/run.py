"""One paired S1/L2 replication: a fixed reader on A versus the same warehouse with TSDs.

Run in the project's Python runner with the project root mounted at /lab.
Only the output directory is written. A single frozen event manifest feeds both arms.
"""
import argparse
import gc
import gzip
import heapq
import json
import platform
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import simpy

PROJECT = Path(__file__).resolve().parents[2]
V3 = PROJECT / "experiment_v3"
sys.path.insert(0, str(V3 / "v3_2"))
import engine
from common import ops, params, settings, sha
from inputs import DAY, digest, fixtures, rng
from ledger import invariants

SEED = 20261121
H = 72 * 3600
COHORT_FROM, COHORT_TO = DAY, 31 * DAY
CONTROL = "000-D0-G0-B0"
GATE_A = "000-D0-GA-B0"
PROFILE = {
    "gate_setup_s": 2, "success_decode_s": 0.8, "no_read_timeout_s": 2,
    "retry_wait_s": 2, "delivery_s": 0.3, "walk_to_fallback_s": 5,
    "bypass_to_next_checkpoint_s": 30, "chance_bypass": 0.002,
    "chance_ambiguous_after_decode": 0.003, "chance_other_valid_label_after_decode": 0.001,
    "p_read_first_clean_bad": [0.97, 0.25], "p_read_retry_clean_bad": [0.75, 0.20],
    "tsd_normal_mode_s": 15, "tsd_damaged_mode_s": 120, "physical_check_mode_s": 20,
    "triangular_low_high_scale": [0.7, 1.3], "lane_A_case_probability": 0.70,
    "buffer_slots_per_lane": 20, "id_cap_s": 600,
}

def synthetic_sscc(sequence):
    base = f"9{sequence:016d}"
    check = -sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(base)) % 10
    return base + str(check)

def fixed_inputs():
    x = fixtures("S1", "L2", SEED)
    for case in x["receipts"]:
        case["lane"] = "A" if rng(SEED, case["case"], "entry_lane").random() < PROFILE["lane_A_case_probability"] else "B"
    for index, raw in enumerate(x["pallets"].values()):
        p = raw["pallet"]
        raw["sscc"] = synthetic_sscc(index + 1)
        raw["wrong_sscc"] = synthetic_sscc(9000000 + index)
        for key in ("bypass", "read1", "read2", "decode_type"):
            raw[key] = rng(SEED, p, "gate_" + key).random()
        raw["scan_factor"] = rng(SEED, p, "tsd_service").triangular(0.7, 1.3, 1)
        raw["check_factor"] = rng(SEED, p, "physical_inspection").triangular(0.7, 1.3, 1)
    return x

class ZoneBuffers:
    def __init__(self, env):
        self.zones = {z: simpy.Resource(env, PROFILE["buffer_slots_per_lane"]) for z in ("A", "B")}

    def release(self, token):
        return token.resource.release(token)

class Pilot(engine.Engine):
    def __init__(self, x, config):
        super().__init__(x, 0)  # Shared priority/FIFO policy. H5 treatment is local to one gate.
        self.config = config
        self.lane = {c["case"]: c["lane"] for c in x["receipts"]}
        self.buffer = ZoneBuffers(self.e)
        self.gate = simpy.Resource(self.e, 1)
        self.scans = {}
        self.max_lane_count = Counter()

    def free(self, name, notify=True):
        # Nearest eligible address within the existing storage class in both arms.
        address = self.addr[name]
        heapq.heappush(self.pools[address.group], ((address.distance, address.name), name))
        if notify:
            self.free_event.succeed()
            self.free_event = self.e.event()
            self.signal()

    def gate_window(self, raw, row):
        bad = int(raw["label_bad"])
        with self.gate.request() as request:
            yield request
            row["gate_start_s"] = self.e.now
            yield self.e.timeout(PROFILE["gate_setup_s"])
            for attempt, key, chances in (
                (1, "read1", "p_read_first_clean_bad"), (2, "read2", "p_read_retry_clean_bad")
            ):
                row["attempts"] = attempt
                if raw[key] < PROFILE[chances][bad]:
                    yield self.e.timeout(PROFILE["success_decode_s"])
                    outcome = raw["decode_type"]
                    if outcome < PROFILE["chance_ambiguous_after_decode"]:
                        row["route"] = "ambiguous_tsd"
                    elif outcome < PROFILE["chance_ambiguous_after_decode"] + PROFILE["chance_other_valid_label_after_decode"]:
                        row["raw_decoded_sscc"] = raw["wrong_sscc"]
                        row["route"] = "foreign_tsd"
                    else:
                        row["raw_decoded_sscc"] = raw["sscc"]
                        row["route"] = f"read_{attempt}"
                    break
                yield self.e.timeout(PROFILE["no_read_timeout_s"])
                if attempt == 1:
                    yield self.e.timeout(PROFILE["retry_wait_s"])
            else:
                row["route"] = "no_read_tsd"
            row["gate_end_s"] = self.e.now
        yield self.e.timeout(PROFILE["delivery_s"])

    def identify(self, c, raw):
        pallet, zone = raw["pallet"], self.lane[c]
        yield self.at(raw["arrival"])
        row = dict(case=c, pallet=pallet, sscc=raw["sscc"], lane=zone, bad_label=raw["label_bad"],
                   arrival_s=raw["arrival"], buffer_admit_s=None, attempts=0, route="waiting_buffer",
                   gate_start_s=None, gate_end_s=None, raw_decoded_sscc=None, receiver_request_s=None,
                   receiver_start_s=None, receiver_end_s=None, worker=None, switch_s=0.0,
                   fatigue=None, hand_scan_s=0.0, inspection_s=0.0, id_correct_s=None,
                   p02_done_s=None, verified_sscc=None)
        self.scans[pallet] = row
        resource = self.buffer.zones[zone]
        token = resource.request()
        yield token
        self.in_buffer.add(pallet)
        self.max_lane_count[zone] = max(self.max_lane_count[zone], resource.count)
        self.record("enter", c, pallet, capacity=self.capacity, lane=zone)
        self.e.process(self.balance_event(pallet, True))
        row.update(buffer_admit_s=self.e.now, route="tsd")
        v = self.new(c, 2, pallet, "receiver", raw_arrival=raw["arrival"])
        v["ready"] = raw["arrival"]
        if self.config == GATE_A and zone == "A":
            if raw["bypass"] < PROFILE["chance_bypass"]:
                # At the next ordinary physical checkpoint the operator discovers the unmatched unit.
                row["route"] = "bypass_tsd"
                yield self.e.timeout(PROFILE["bypass_to_next_checkpoint_s"])
            else:
                yield from self.gate_window(raw, row)
                if not row["route"].startswith("read_"):
                    yield self.e.timeout(PROFILE["walk_to_fallback_s"])

        automatic = row["route"] in ("read_1", "read_2")
        if automatic:
            assert row["raw_decoded_sscc"] == raw["sscc"]
            id_ready = self.e.now
        scan = 0 if automatic else raw["scan_factor"] * PROFILE["tsd_damaged_mode_s" if raw["label_bad"] else "tsd_normal_mode_s"]
        check = raw["check_factor"] * PROFILE["physical_check_mode_s"]
        asked = self.e.now
        begin, end, worker, switch, fatigue = self.crew.reserve("receiver", asked, scan + check, c, 2, v["id"])
        if not automatic:
            id_ready = begin + switch + scan * fatigue + PROFILE["delivery_s"]
        finish = max(id_ready, end)
        v.update(grant=begin, service_start=begin, manual_start=begin, manual_end=end, finish=finish,
                 base=scan + check, switch=switch, fatigue=fatigue, worker=worker, system_s=0)
        row.update(receiver_request_s=asked, receiver_start_s=begin, receiver_end_s=end, worker=worker,
                   switch_s=switch, fatigue=fatigue, hand_scan_s=scan*fatigue, inspection_s=check*fatigue,
                   id_correct_s=id_ready, p02_done_s=finish, verified_sscc=raw["sscc"])
        yield self.at(finish)
        return token

def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

def end_state(model):
    capacity = sum(a.quarantine for a in model.addr.values())
    free = capacity
    exhausted_at = None
    for k,t,c,p,d in model.events:
        if k == "reserve" and d["stage"] == 10:
            free -= 1
            if free == 0:
                exhausted_at = t
        if k == "pickup" and d["source"] != "BUFFER" and model.addr[d["source"]].quarantine:
            free += 1
            exhausted_at = None
    assert free == len(model.pools[2])
    held = Counter(model.registered_quality.get(model.x["pallets"][p]["q"],"pending")
                   for p, a in model.location.items() if model.addr[a].quarantine)
    waiting = Counter(f"P{job[3]}_{model.registered_quality.get(model.x['pallets'][p]['q'],'pending')}"
                      for p,job in model.jobs.items())
    return dict(quarantine_capacity=capacity,quarantine_free=free,last_continuous_zero_s=exhausted_at,
                pallets_in_quarantine_by_status=dict(held),waiting_dispatch=dict(waiting),
                ordinary_free=[len(a) for a in model.pools[:2]],trucks_available=len(model.fork.items),
                buffer_occupied={k:v.count for k,v in model.buffer.zones.items()},
                queue_before_buffer={k:len(v.queue) for k,v in model.buffer.zones.items()},
                last_standard_close_s=max(model.closed.values(),default=None))

def arm(x, config, out):
    model = Pilot(x, config).execute()
    checks = invariants(model)
    assert not checks["violations"], checks["violations"][:5]
    cohort = {c["case"]: c for c in x["receipts"] if COHORT_FROM <= c["ready"] < COHORT_TO}
    visits = defaultdict(list)
    for v in model.visits:
        if v["case"] in cohort:
            visits[v["case"]].append(v)
    scan_rows = [r for r in model.scans.values() if r["case"] in cohort]
    case_rows, trace_rows = [], []
    rejected = {d["quality"]:t for k,t,c,p,d in model.events if k=="quality_delivered" and d["decision"]=="failed"}
    for cid, case in cohort.items():
        t0 = case["ready"]
        assert t0 + H <= model.end
        closed = model.closed.get(cid)
        ok = closed is not None and closed <= t0 + H
        stop = closed if ok else t0 + H
        group = visits[cid]
        received = {v["pallet"]:v["finish"] for v in group if v["op"]==16 and v["finish"] is not None and v["finish"]<=stop}
        if ok:
            assert set(case["members"]) == set(received)
        for p, t in received.items():
            assert model.scans[p]["verified_sscc"] == x["pallets"][p]["sscc"] and model.scans[p]["p02_done_s"] <= t
        failed = any(rejected.get(x["pallets"][p]["q"], float("inf"))<=stop for p in case["members"])
        assert not (ok and failed)
        active = defaultdict(float)
        for v in group:
            lo, hi = v["manual_start"], v["manual_end"]
            if lo is not None and hi is not None:
                active[v["role"]] += max(0, min(hi, stop)-max(t0, lo))
            trace_rows.append((cid, v["pallet"], v.get("quality",""), v["op"], v["branch"], v["ready"],
                               v["grant"], v["finish"], v["worker"], v.get("address",""), v["truck"]))
        case_rows.append(dict(case=cid, lane=case["lane"], pallets=len(case["members"]), ready_s=t0,
                              closed_s=closed, y72_s=stop-t0, success72=ok, quality_nonconformity=failed,
                              receiver_s=active["receiver"], worker_s=sum(active.values())))
    scans = pd.DataFrame(scan_rows).sort_values(["arrival_s","pallet"])
    cases = pd.DataFrame(case_rows).sort_values(["ready_s","case"])
    if len(scans) != cases.pallets.sum() or scans.pallet.duplicated().any():
        missing = {p for c in cohort.values() for p in c["members"]} - set(scans.pallet)
        print("Coverage diagnostic:", dict(expected=int(cases.pallets.sum()), observed=len(scans),
              missing_sample=sorted(missing)[:10], open_jobs=len(model.jobs), closed=len(model.closed),
              free_addresses=[len(h) for h in model.pools], trucks_ready=len(model.fork.items),
              lanes={k:(v.count,len(v.queue)) for k,v in model.buffer.zones.items()}), flush=True)
        raise RuntimeError("Incomplete cohort scan coverage")
    known = scans[scans.id_correct_s.notna() & scans.id_correct_s.le(model.end)]
    assert (known.p02_done_s >= known.id_correct_s).all() and (known.id_correct_s >= known.arrival_s).all()
    scans.loc[scans.id_correct_s.gt(model.end), ["id_correct_s","verified_sscc"]] = None
    scans.loc[scans.p02_done_s.gt(model.end), "p02_done_s"] = None
    assert scans.arrival_s.max() + PROFILE["id_cap_s"] < model.end
    stop = scans["case"].map(cases.set_index("case").eval("ready_s + y72_s"))
    # Compute time actually worked inside each receipt's measurement window.
    switch_end = scans.receiver_start_s + scans.switch_s
    scan_end = switch_end + scans.hand_scan_s
    for col,lo,hi in (("p02_switch_work_s",scans.receiver_start_s,switch_end),
                      ("p02_scan_work_s",switch_end,scan_end),
                      ("p02_check_work_s",scan_end,scans.receiver_end_s)):
        scans[col]=(hi.clip(upper=stop)-lo).clip(lower=0).fillna(0)
    assert all(n <= PROFILE["buffer_slots_per_lane"] for n in model.max_lane_count.values())
    cases.to_parquet(out/f"cases_{config}.parquet", index=False)
    scans.to_parquet(out/f"scans_{config}.parquet", index=False)
    pd.DataFrame(trace_rows, columns=["case","pallet","quality","op","branch","ready_s","start_s","finish_s","worker","address","truck"]).to_parquet(out/f"operations_{config}.parquet", index=False)
    deliveries = [(k,t,c,p,d) for k,t,c,p,d in model.events if k in ("quality_delivered","pickup","deposit","exchange_ack") and c in cohort]
    with gzip.open(out/f"milestones_{config}.jsonl.gz","wt",encoding="utf-8") as stream:
        for a in deliveries:
            stream.write(json.dumps(a,separators=(",",":"))+"\n")
    checks["max_buffer_per_lane"] = dict(model.max_lane_count)
    checks["verified_cohort_pallets"] = len(scans)
    checks["verified_cohort_cases"] = len(cases)
    checks["observed_at_end"] = end_state(model)
    return checks

def results(out):
    cs, ps = {}, {}
    for config in (CONTROL,GATE_A):
        cs[config] = pd.read_parquet(out/f"cases_{config}.parquet").set_index("case")
        ps[config] = pd.read_parquet(out/f"scans_{config}.parquet").set_index("pallet")
    a, b = cs[CONTROL], cs[GATE_A]
    assert a.index.equals(b.index) and a[["lane","pallets","ready_s"]].equals(b[["lane","pallets","ready_s"]])
    u, v = ps[CONTROL], ps[GATE_A]
    assert u.index.equals(v.index) and u[["case","lane","sscc","bad_label","arrival_s"]].equals(v[["case","lane","sscc","bad_label","arrival_s"]])
    case_pair = a[["ready_s","lane","pallets"]].copy()
    for col in ("closed_s","success72","y72_s","quality_nonconformity","receiver_s","worker_s"):
        case_pair[f"r0_{col}"],case_pair[f"h5_{col}"] = a[col], b[col]
    case_pair["saved_s"] = a.y72_s-b.y72_s
    case_pair.to_csv(out/"paired_cases.csv",float_format="%.6f")
    summary = {}
    for name, cases, scans in (("R0",a,u),("H5_A",b,v)):
        summary[name]={}
        for zone in ("all","A","B"):
            c=cases if zone=="all" else cases[cases.lane.eq(zone)]
            s=scans if zone=="all" else scans[scans.lane.eq(zone)]
            completed=c[c.success72]
            raw_id=s.id_correct_s-s.arrival_s
            presented=s[s.buffer_admit_s.notna() & (s.buffer_admit_s + PROFILE["id_cap_s"] <= 34*DAY)]
            station_id=presented.id_correct_s-presented.buffer_admit_s
            summary[name][zone]=dict(cases=len(c),pallets=len(s),success72=int(c.success72.sum()),
                 quality_nonconformity=int(c.quality_nonconformity.sum()),sum_y_s=float(c.y72_s.sum()),
                 mean_y_min=float(c.y72_s.mean()/60),sum_receiver_h=float(c.receiver_s.sum()/3600),
                 sum_worker_h=float(c.worker_s.sum()/3600),median_closed_min=float(completed.y72_s.median()/60),
                 p90_closed_min=float(completed.y72_s.quantile(.9)/60),mean_id600_s=float(raw_id.fillna(600).clip(upper=600).mean()),
                 id_within600=int(raw_id.le(600).sum()),mean_id_uncapped_s=float(raw_id.mean()),
                 station_eligible=len(presented),mean_station_id600_s=float(station_id.fillna(600).clip(upper=600).mean()),
                 identified=int(s.id_correct_s.notna().sum()),
                 p02_labor_h=float(s[["p02_switch_work_s","p02_scan_work_s","p02_check_work_s"]].sum().sum()/3600),
                 p02_check_h=float(s.p02_check_work_s.sum()/3600),p02_scan_h=float(s.p02_scan_work_s.sum()/3600),
                 p02_switch_h=float(s.p02_switch_work_s.sum()/3600),
                 mean_p02_s=float((s.p02_done_s-s.arrival_s).mean()),bad_labels=int(s.bad_label.sum()),
                 routes={str(k):int(n) for k,n in s.route.value_counts().items()})
    gain=case_pair.saved_s
    summary["paired_counts"]={"faster":int((gain>.001).sum()),"slower":int((gain<-.001).sum()),"same_1ms":int((gain.abs()<=.001).sum())}
    # Fixed selection rule: earliest eligible single-pallet receipt, no selection by effect size or sign.
    direct_cases=set(v.loc[v.route.eq("read_1"),"case"])
    candidates=case_pair[case_pair.r0_success72 & case_pair.h5_success72 & case_pair.pallets.eq(1) & case_pair.index.isin(direct_cases)]
    summary["example_case"]=str(candidates.index[0])
    fallback=v[(v.lane=="A") & v.route.str.endswith("_tsd") & v.id_correct_s.notna()]
    summary["first_fallback_pallet"]=str(fallback.index[0]) if not fallback.empty else None
    return summary

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("output",type=Path)
    out=ap.parse_args().output
    out.mkdir(exist_ok=True,parents=True)
    x=fixed_inputs()
    input_hash=digest(x)
    with gzip.open(out/"input_manifest.json.gz","wt",encoding="utf-8") as f:
        json.dump(x,f,separators=(",",":"),sort_keys=True)
    # No duplicate manual entry charged at R0. Use the project's residual manual primitives
    # as explicit shared scenario inputs. The same existing EWM/terminal checks apply to H5.
    baseline={n:dict(spec) for n,spec in ops.items()}
    for label, (_,mode) in settings["retained_manual_modes_s"].items():
        baseline[int(label[1:])]["manual_labor_s"]=mode
    meta=dict(seed=SEED,scenario="S1",load="L2",nominal_pallets_day=450,profile=PROFILE,
              input_sha256=input_hash,observed_days=x["days"],python=platform.python_version(),simpy=simpy.__version__,
              roster=params["staffing_reference_assumption"],
              manual_modes_s={n:v["manual_labor_s"] for n,v in baseline.items() if v.get("manual_labor_s") is not None},
              code_hashes={str(p.relative_to(PROJECT)):sha(p) for p in (Path(__file__).resolve(), V3/"v3_2/engine.py", V3/"v3_2/crew.py", V3/"v3_2/ledger.py", V3/"src/inputs.py", V3/"src/process.py", V3/"odoo_addons/wms_research_v3/policy.py",V3/"v3_2/settings.json",V3/"v3_2/design/protocol/ASIS_BASELINE_PARAMETER_FREEZE.json",PROJECT/"series_py312/data/normalized/source.parquet",PROJECT/"series_py312/data/manifests/wepastacks.json")})
    print("Fixed input:",input_hash,flush=True)
    audits={}
    with patch.object(engine, "ops", baseline):
        for config in (CONTROL,GATE_A):
            started=time.monotonic()
            audits[config]=arm(x,config,out)
            assert digest(x)==input_hash
            print(config,"completed in",round(time.monotonic()-started,1),"s",flush=True)
            gc.collect()
    summary=results(out)
    write_json(out/"result.json",dict(design=meta,audits=audits,summary=summary))
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=="__main__":
    main()
