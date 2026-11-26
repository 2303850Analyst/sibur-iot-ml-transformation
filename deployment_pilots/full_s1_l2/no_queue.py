"""Controlled S1/L2 replay with immediate resources and paired physical trip profiles."""
import argparse
import gc
import heapq
import json
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import simpy

import compare
import operation_profile as operations
from policy import Address


PILOT = compare.pilot
RESULTS = operations.RESULTS


def visit_key(visit):
    return (visit["op"], visit["pallet"] or visit.get("quality") or visit["case"], visit["attempt"])


class ServiceReference:
    """Paired exogenous work factors and travel legs, calibrated on baseline operations."""

    def __init__(self):
        self.preparations = {}
        self.trips = {}

    def add_trace(self, warehouse):
        pickups = {}
        for kind, time, _, pallet, fields in warehouse.events:
            if kind == "pickup":
                pickups[pallet, fields["destination"]] = (time, fields["source"])
        for visit in warehouse.visits:
            if visit["branch"] != "APPLIED" or visit["manual_end"] is None:
                continue
            self.preparations.setdefault(visit_key(visit), (visit["switch"], visit["fatigue"]))
            if visit["op"] not in (10, 18):
                continue
            pallet = visit["pallet"]
            pickup = pickups.get((pallet, visit["address"]))
            if pickup is None:
                continue
            time, source = pickup
            group = warehouse.x["pallets"][pallet]["sku"] // 10
            family = (visit["op"], source != "BUFFER", group)
            empty_leg = max(0.0, time - visit["machine_start"])
            loaded_leg = max(0.0, visit["base"] - 180.0 - empty_leg)
            assert np.isclose(visit["base"], 180.0 + empty_leg + loaded_leg, atol=1.e-6, rtol=0)
            self.trips.setdefault((family, pallet), (empty_leg, loaded_leg))

    def freeze(self):
        by_step = defaultdict(list)
        for (step, _, _), factors in self.preparations.items():
            by_step[step].append(factors)
        self.step_medians = {key: tuple(np.median(v, axis=0)) for key, v in by_step.items()}
        # Auxiliary sensor checks use the observed work from the previous hardware arm.
        original = pd.read_parquet(RESULTS/"operation_observations_s1_l2.parquet")
        checks = original.loc[("R3", 26)].query('state == "completed"')
        self.step_medians[26] = (float(checks.manual_s.median()) - 8.0, 1.0)
        by_family = defaultdict(list)
        for (family, _), legs in self.trips.items():
            by_family[family].append(legs)
        self.trip_medians = {key: tuple(np.median(v, axis=0)) for key, v in by_family.items()}


class ImmediateCrew:
    def __init__(self, model, reference):
        self.model = model
        self.reference = reference

    def reserve(self, role, ready, base, case, step, visit_id, slot=None, physical=False):
        visit = self.model.visits[visit_id - 1]
        key = visit_key(visit)
        factors = self.reference.preparations.get(key)
        if factors is None:
            factors = self.reference.step_medians[step]
            if case in self.model.cohort:
                self.model.imputed_work[step] += 1
        setup, fatigue = factors
        assert not physical or fatigue == 1
        worker = f"{role}/{slot if slot is not None else visit_id}"
        return ready, ready + setup + base * fatigue, worker, setup, fatigue


class AvailablePlaces(compare.BufferPlaces):
    def admit(self, zone, pallet, case):
        if all(a or b for a, b in zip(self.book[zone], self.report[zone])):
            for states in (self.physical, self.book, self.report):
                states[zone].append(False)
        yield from super().admit(zone, pallet, case)


class QueueFreeWarehouse(compare.Warehouse):
    def __init__(self, inputs, level, design, reference):
        super().__init__(inputs, level, False, design["models"], design["sref_s"])
        self.reference = reference
        self.cohort = {r["case"] for r in inputs["receipts"]
                       if PILOT.COHORT_FROM <= r["ready"] < PILOT.COHORT_TO}
        self.imputed_work = Counter()
        self.trip_counts = Counter()
        self.extra_addresses = Counter()
        self.crew = ImmediateCrew(self, reference)
        self.capacity = len(inputs["pallets"])
        self.buffer.zones = {z: simpy.Resource(self.e, self.capacity) for z in "AB"}
        self.gates = {z: SimpleNamespace(e=self.e, gate=simpy.Resource(self.e, self.capacity))
                      for z in "AB"}
        self.slots = AvailablePlaces(self) if level >= 2 else None
        self.fork = simpy.Store(self.e)
        self.next_truck = 0

    def dispatch(self):
        # Every eligible job is dispatched at submit time. There is no list to prioritise.
        yield self.waitforever

    def submit(self, case, pallet, sku, stage, token):
        group = 2 if stage == 10 else sku // 10
        if self.pools[group]:
            _, destination = heapq.heappop(self.pools[group])
        else:
            self.extra_addresses[group] += 1
            destination = f"AVAILABLE{self.extra_addresses[group]}-{group * 2600:04d}"
            self.addr[destination] = Address(destination, group, group == 2, (40, 80, 30)[group])
        self.record("reserve", case, pallet, address=destination, stage=stage, sku=sku,
                    quality=self.x["pallets"][pallet]["q"])
        self.next_truck += 1
        return self.e.process(self.immediate_move(case, pallet, sku, stage, token,
                                                  self.next_truck, destination))

    def immediate_move(self, case, pallet, sku, stage, token, truck, destination):
        source = self.location.get(pallet)
        family = (stage, source is not None, sku // 10)
        legs = self.reference.trips.get((family, pallet))
        if case in self.cohort:
            self.trip_counts["matched" if legs is not None else "route_class_median"] += 1
        if legs is None:
            legs = self.reference.trip_medians[family]
        empty, loaded = legs
        physical = 180.0 + empty + loaded
        visit = self.new(case, stage, pallet, "forklift_driver")
        t, end, worker, setup, fatigue = self.crew.reserve(
            "forklift_driver", self.e.now, physical, case, stage, visit["id"], slot=truck, physical=True)
        visit.update(grant=t, service_start=t, manual_start=t, manual_end=end, worker=worker,
                     truck=truck, base=physical, switch=setup, fatigue=fatigue, machine_start=t+setup,
                     machine_end=end, reserve_start=t, reserve_end=end, finish=end, address=destination)
        yield self.at(t + setup + empty)
        self.record("pickup", case, pallet, source=source or "BUFFER", destination=destination, vehicle=truck)
        if source is None:
            self.in_buffer.remove(pallet)
            self.buffer.release(token)
            self.e.process(self.balance_event(pallet, False))
        else:
            self.free(source)
        yield self.at(end)
        self.location[pallet] = destination
        self.record("deposit", case, pallet, address=destination, stage=stage, sku=sku)
        if stage == 18:
            self.p19[pallet] = (truck, self.addr[destination].distance, end)
        return destination


def summarize(model, cases, visits):
    complete = visits.loc[visits.state.eq("completed")]
    closed = cases.loc[cases.success72]
    time_s = closed.y72_s
    max_queue = complete.queue_s.max()
    assert max_queue < 1.e-6
    assert not visits.state.eq("open").any()
    assert cases.admitted.equals(cases.pallets)
    assert cases.success72.equals(~cases.failed_quality)
    audit = compare.invariants(model)
    assert not audit["violations"], audit["violations"][:5]
    actual = model.x["pallets"]
    for pallet, scan in model.scans.items():
        if scan["case"] in model.cohort:
            assert abs(scan["buffer_admit_s"] - actual[pallet]["arrival"]) < 1.e-6
            assert scan["verified_sscc"] == actual[pallet]["sscc"]
            if scan.get("gate_start_s") is not None:
                assert abs(scan["gate_start_s"] - actual[pallet]["arrival"]) < 1.e-6
    hold = complete.loc[7].release_wait_s
    return dict(cases=len(cases), pallets=int(cases.pallets.sum()), closed=len(closed),
                quality_rejected=int(cases.failed_quality.sum()),
                mean_closed_s=float(time_s.mean()), mean_all_capped72_s=float(cases.y72_s.mean()),
                median_closed_s=float(time_s.median()), p90_closed_s=float(time_s.quantile(.9)),
                total_staff_h=float(cases.people_s.sum()/3600), staff_s_per_offered=float(cases.people_s.mean()),
                work_s_per_offered=float(complete.work_s.sum()/len(cases)),
                checks=int(complete.index.get_level_values("step").value_counts().get(26, 0)),
                quarantine_trips=int(len(complete.loc[10])), regulated_quality_objects=len(hold),
                mean_hold_s_on_held=float(hold.mean()), max_resource_wait_s=float(max_queue),
                fallback_trip_counts=dict(model.trip_counts), fallback_work=dict(model.imputed_work),
                extra_storage=dict(model.extra_addresses), peak_lane=dict(model.max_lane_count),
                ledger_checks=dict(audit["checks"]))


def reference_from_logs(inputs, level, design):
    ref = ServiceReference()
    for recheck in (False, True):
        tag = f"R{level}" + ("_recheck" if recheck else "")
        recorded = compare.Warehouse(inputs, level, recheck, design["models"], design["sref_s"]).execute()
        trace = compare.case_report(recorded)
        pd.testing.assert_frame_equal(trace, pd.read_parquet(RESULTS/f"{tag}_cases.parquet"),
                                      check_exact=False, rtol=0, atol=1.e-6)
        ref.add_trace(recorded)
        del recorded
        gc.collect()
    ref.freeze()
    return ref


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    out = parser.parse_args().out
    out.mkdir(parents=True, exist_ok=True)
    previous = json.loads((RESULTS/"summary.json").read_bytes())
    design = previous["design"]
    for path, expected in design["source_sha256"].items():
        assert PILOT.sha(PILOT.PROJECT/path) == expected, path
    inputs = compare.input_for_load("L2", out)
    assert compare.digest(inputs) == design["input_sha256"]
    ops = {k: dict(v) for k,v in PILOT.ops.items()}
    for label, (_, seconds) in PILOT.settings["retained_manual_modes_s"].items():
        ops[int(label[1:])]["manual_labor_s"] = seconds
    frames = {}
    observations = {}
    summary = {}
    with patch.object(PILOT.engine, "ops", ops):
        ref = reference_from_logs(inputs, 0, design)
        for arm, level in [("R0",0), ("R1",1), ("R2",2), ("R3",3), ("R3_recorded",3)]:
            if arm == "R3_recorded":
                # Keep the full solution's own observed transport legs and service factors
                # as a second comparison without resource waits.
                ref = reference_from_logs(inputs, 3, design)
            model = QueueFreeWarehouse(inputs, level, design, ref).execute()
            frame = compare.case_report(model)
            visits = operations.observation_frame(model, frame)
            if level in (1, 2):
                # observation_frame's early-notice boundary was originally used only for R3.
                selection = (visits.index.get_level_values("step") == 12) & visits.state.eq("completed")
                visits.loc[selection,"ready_s"] -= compare.TRANSPORT
                visits.loc[selection,["work_s","device_s","elapsed_s"]] += compare.TRANSPORT
            frames[arm] = frame
            summary[arm] = summarize(model, frame, visits)
            if arm in ("R0", "R3"):
                observations[arm] = visits
            if arm == "R3_recorded":
                recorded_visits = visits[visits.state.eq("completed")]
            print(arm, summary[arm]["closed"], summary[arm]["mean_closed_s"],
                  summary[arm]["total_staff_h"], flush=True)
            del model
            gc.collect()

    base = frames["R0"]
    joined = base[["pallets","lane","ready_s","success72","failed_quality"]].copy()
    for arm, frame in frames.items():
        pd.testing.assert_frame_equal(joined.iloc[:,:5], frame[joined.columns[:5]])
        for col in ["closed_s","y72_s","people_s"]:
            joined[f"{arm}_{col}"] = frame[col]
    pd.testing.assert_frame_equal(frames["R2"], frames["R3"], check_exact=True)
    joined.to_csv(out/"receipts.csv", encoding="utf-8-sig", float_format="%.7f")
    table = operations.export_table(observations, out)
    observed_work = pd.read_csv(RESULTS/"operations_s1_l2.csv", sep=";", decimal=",")
    for arm in ("R0","R3"):
        table[f"Прошлый прогон с исключением очереди, с, {arm}"] = observed_work[f"Выполнение, с, {arm}"]
    applied = observations["R0"].loc[observations["R0"].state.eq("completed")]
    paired_recorded = applied[["work_s"]].join(recorded_visits[["work_s"]], how="inner",
                                               lsuffix="_0", rsuffix="_3").groupby(level="step").mean()
    for column, label in [("work_s_0", "R0"), ("work_s_3", "R3")]:
        table[f"С собственными профилями рейсов и труда, с, {label}"] = (
            paired_recorded[column].reindex(range(1, 26)).to_numpy())
    table["Чистый выигрыш, с"] = table["Выполнение, с, R0"] - table["Выполнение, с, R3"]
    order = ["Шаг","Было","Стало","Единица","Парных выполнений","Выполнение, с, R0",
             "Выполнение, с, R3","Чистый выигрыш, с","Ожидание срока качества, с, R0",
             "Ожидание срока качества, с, R3","Очередь, с, R0","Очередь, с, R3"]
    table[order + [k for k in table if k not in order]].to_csv(
        out/"operations_s1_l2.csv", sep=";", decimal=",", encoding="utf-8-sig", index=False, float_format="%.6f")
    (out/"summary.json").write_text(json.dumps(
        dict(design=dict(input_sha256=design["input_sha256"], seed=design["seed"], scenario="S1",load="L2",
                         work_factors="Common per-object setup and fatigue from R0, completed by R0_recheck and within-step medians.",
                         travel="Common per-pallet empty and loaded legs from matched baseline trips; within-route-class medians for unseen trips.",
                         alternate="R3_recorded uses full solution's own R3 and R3_recheck trip lengths and active-work factors, retaining the same zero-wait semantics.",
                         queue="All eligible requests served immediately; quality dates, arrivals, branch logic and precedence remain active."),
             arms=summary), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
