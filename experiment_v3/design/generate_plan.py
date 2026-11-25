#!/usr/bin/env python3
"""Generate and validate V3 plans. Does not execute the WMS experiment."""
from __future__ import annotations
import argparse
import csv
import hashlib
import itertools
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=Path(__file__).with_name("V3_PROTOCOL.json"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("plans"))
    args = parser.parse_args()
    protocol = load_json(args.protocol)
    if protocol["status"] != "DESIGN_NOT_EXECUTED":
        raise ValueError("Expected a design protocol, not experimental results")
    modules = protocol["modules"]
    names = [entry["id"] for entry in modules]
    if names != [f"M{i}" for i in range(1, 8)]:
        raise ValueError("Expected exactly M1..M7 in that order")
    configs = []
    for mask in range(1 << len(names)):
        flags = [(mask >> index) & 1 for index in range(len(names))]
        active_names = [entry["name_ru"] for entry, flag in zip(modules, flags) if flag]
        configs.append({
            "config_id": f"C{mask:03d}", "mask": mask,
            **dict(zip(names, flags)), "n_modules": sum(flags),
            "modules": "+".join(name for name, flag in zip(names, flags) if flag) or "NONE",
            "name_ru": "; ".join(active_names) or "AS-IS: WMS + ТСД, без новых модулей",
        })
    configs.sort(key=lambda row: (row["M7"], row["n_modules"], row["mask"]))
    distribution = Counter(row["n_modules"] for row in configs)
    expected = {k: math.comb(7, k) for k in range(8)}
    if dict(distribution) != expected or len(configs) != 128:
        raise AssertionError("Incomplete factorial design")
    if sum(protocol["live_odoo"]["live_role_accounts"].values()) != 20:
        raise AssertionError("Live role allocation must sum to 20")
    if len(protocol["operations"]) != 25:
        raise AssertionError("Expected all 25 process operations")
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "configurations.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(configs[0]))
        writer.writeheader()
        writer.writerows(configs)
    summary: dict[str, Any] = {
        "status": "PLAN_VALIDATED_NOT_EXECUTED", "configs": 128,
        "counts_by_size": expected, "modules": names,
        "all_combinations_with_ml": 64, "operations": 25,
        "live_user_count": protocol["live_odoo"]["concurrent_users"],
        "engines": {},
    }
    scenario_ids = protocol["design"]["scenarios"]
    loads = protocol["design"]["loads"]
    for engine, section, filename in [
        ("PROCESS_SIMULATION", "simulation", "run_manifest.csv"),
        ("LIVE_ODOO", "live_odoo", "live_manifest.csv"),
    ]:
        setting = protocol[section]
        seeds = setting["seeds"]
        fields = ["run_id", "engine", "config_id", "modules", "has_ml", "scenario", "load", "load_multiplier", "seed", "paired_input_key", "concurrent_users", "status"]
        total = 0
        with_ml = 0
        per_condition: Counter[tuple[str, str, str]] = Counter()
        per_input: Counter[tuple[str, str, int]] = Counter()
        seen: set[str] = set()
        with (args.output / filename).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for scenario, load, seed, config in itertools.product(scenario_ids, loads, seeds, configs):
                run_id = f"V3-{engine}-{scenario}-{load}-{seed}-{config['config_id']}"
                if run_id in seen:
                    raise AssertionError(f"Duplicate run: {run_id}")
                seen.add(run_id)
                writer.writerow({
                    "run_id": run_id, "engine": engine, "config_id": config["config_id"],
                    "modules": config["modules"], "has_ml": config["M7"],
                    "scenario": scenario, "load": load, "load_multiplier": loads[load]["multiplier"],
                    "seed": seed, "paired_input_key": f"{engine}|{scenario}|{load}|{seed}",
                    "concurrent_users": 20 if engine == "LIVE_ODOO" else "NA_PHYSICAL_RESOURCES_ARE_SEPARATE",
                    "status": "PLANNED_NOT_EXECUTED",
                })
                total += 1
                with_ml += config["M7"]
                per_condition[(config["config_id"], scenario, load)] += 1
                per_input[(scenario, load, seed)] += 1
        if total != setting["target_runs"]:
            raise AssertionError(f"{engine}: {total} != expected {setting['target_runs']}")
        if set(per_condition.values()) != {setting["repetitions_per_condition"]}:
            raise AssertionError(f"{engine}: unbalanced replication")
        if set(per_input.values()) != {128}:
            raise AssertionError(f"{engine}: missing within-input configurations")
        summary["engines"][engine] = {
            "planned_runs": total, "planned_with_ml": with_ml,
            "conditions": len(per_condition), "replications_each": setting["repetitions_per_condition"],
            "paired_input_blocks": len(per_input), "configs_per_input_block": 128,
            "unique_ids": len(seen), "all_balanced": True,
        }
    summary["protocol_sha256"] = hashlib.sha256(args.protocol.read_bytes()).hexdigest()
    summary["warning"] = "Validated only the planned coverage. No Odoo or simulation experiments were executed."
    write_json(args.output / "plan_validation.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
