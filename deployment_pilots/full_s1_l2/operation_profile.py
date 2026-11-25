"""Replay the published S1/L2 comparison and measure each applied warehouse operation."""
import argparse
import gc
import json
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import compare


RESULTS = Path(__file__).resolve().parent / "results"
PILOT = compare.pilot
KEY = ["step", "entity", "attempt"]
UNIT_NAMES = {
    "case": "поступление", "pallet": "паллета", "quality_case": "объект качества",
    "quality_case_if_required": "объект качества, требующий проверки",
    "pallet_if_hold": "паллета, направленная в карантин",
    "quality_case_if_nonconformance": "объект качества с несоответствием",
    "case_if_error": "поступление с ошибкой в документах",
    "pallet_if_discrepancy": "паллета с расхождением по адресу",
}
TITLES = {
    1: ("Подтверждение готовности и передача основания", "Предварительное уведомление запускает P12. Производство отдельно подтверждает фактическую готовность."),
    2: ("Стационарное чтение этикетки и физическая приёмка паллеты", "Matrix 220 на обоих входах читает код. При исключении кладовщик применяет ТСД. Физическую сверку выполняют при каждом приёме."),
    3: (None, "Кладовщик сверяет состав и реквизиты документов."),
    4: (None, "Кладовщик оформляет результат приёмки."),
    5: (None, "Кладовщик передаёт сведения о приёмке."),
    6: (None, "Инспектор идентифицирует объект качества один раз для связанных с ним паллет."),
    7: (None, "Для удержанного объекта учитываются работа инспектора, лабораторная часть и ожидание допустимого момента решения."),
    8: ("Утверждение решения в общем задании качества", "Инспектор принимает решение и в том же задании переходит к P09."),
    9: ("Проверка и передача утверждённого статуса в WMS", "После P08 тот же инспектор проверяет статус. Стенд регистрирует его доставку в WMS."),
    10: ("Карантинное размещение по заданию WMS", "При ограничении диспетчер выбирает рейс и машину с учётом очереди, занятости буфера и прогноза."),
    11: (None, "Инспектор разбирает подтверждённое несоответствие качества."),
    12: ("Подготовка черновика WMS по предварительному уведомлению", "Производство посылает уведомление за 60 с до готовности первой паллеты."),
    13: ("Создание карточки поступления в WMS", "Один оператор получает общее задание на создание и проверку карточки P13 и P14."),
    14: ("Проверка карточки тем же оператором", "Оператор продолжает P13 без новой очереди и переключения на другое задание."),
    15: (None, "Условная операция при ошибке в документах."),
    16: (None, "Оператор проводит каждую физически подтверждённую паллету."),
    17: ("Подтверждение условий адресного размещения", "Оператор подтверждает задание. Диспетчер выбирает и резервирует адрес при выдаче рейса P18."),
    18: ("Перемещение по заданию с учётом занятости буфера и прогноза", "Диспетчер выбирает пару задание и погрузчик по длине рейса, времени в очереди, занятости по датчикам и прогнозу Ridge."),
    19: (None, "Водитель устанавливает паллету и подтверждает адрес через ТСД."),
    20: (None, "WMS и оператор сверяют подтверждённое место с ожидаемым."),
    21: (None, "Условная операция при расхождении подтверждённого адреса."),
    22: ("Сверка полного поступления в общей карточке", "Оператор сверяет состав и переходит к итоговым документам P23 в том же задании."),
    23: ("Подготовка итоговых документов в той же карточке", "После P22 оператор оформляет документы без повторного назначения."),
    24: (None, "Учитываются подтверждение обмена с ERP и проверка результата оператором."),
    25: (None, "Оператор фиксирует закрытие после подтверждения всех обязательных результатов."),
}


def observation_frame(model, cases):
    limits = (cases.ready_s + cases.y72_s).to_dict()
    first_document = {}
    issued = {}
    delivered = {}
    last_move = {}
    last_document = {}
    for v in model.visits:
        if v["case"] not in limits or v["branch"] != "APPLIED":
            continue
        if v["op"] == 3 and v["attempt"] == 1:
            first_document[v["case"]] = v["ready"]
        elif v["op"] == 8:
            issued[v["quality"]] = v["finish"]
        elif v["op"] == 18:
            last_move[v["pallet"]] = v["finish"]
        elif v["op"] == 23:
            last_document[v["case"]] = v["finish"]
    for kind, time, case, _, data in model.events:
        if kind == "quality_delivered":
            delivered[data["quality"]] = time

    rows = []
    scanned = set()
    for v in model.visits:
        case, step = v["case"], v["op"]
        if case not in limits or v["ready"] > limits[case]:
            continue
        if step == 2:
            scanned.add(v["pallet"])
        entity = v["pallet"] or v.get("quality") or case
        begin, end = v["ready"], v["finish"]
        row = dict(step=step, entity=entity, attempt=v["attempt"], case=case,
                   ready_s=begin, stop_s=limits[case], manual_observed_s=0.0)
        if v["branch"].startswith("NA:"):
            rows.append(dict(row, state="skipped"))
            continue
        assert v["branch"] == "APPLIED"
        human_start, human_end = v["manual_start"], v["manual_end"]
        if human_start is not None and human_end is not None:
            row["manual_observed_s"] = max(0.0, min(human_end, limits[case]) - human_start)
        if end is None:
            rows.append(dict(row, state="open"))
            continue

        device = 0.0
        lab = float(v.get("external_lab", 0.0))
        release_wait = 0.0
        if step == 1:
            # The first documentary check starts when the production message has arrived.
            device = first_document[case] - end
            end = first_document[case]
        elif step == 2:
            s = model.scans[v["pallet"]]
            if s.get("gate_start_s") is not None:
                device = s["gate_end_s"] - s["gate_start_s"] + compare.TRANSPORT
                if not s["route"].startswith("read_"):
                    device += compare.P["walk_to_fallback_s"]
            elif s["route"] == "bypass_tsd":
                device = compare.P["bypass_to_next_checkpoint_s"]
            device += max(0.0, end - human_end)
        elif step == 7:
            # The model can keep a held lot waiting after its laboratory work is finished.
            release_wait = max(0.0, model.qready[v["quality"]] - end)
            end += release_wait
        elif step == 9:
            begin = issued[v["quality"]]
            end = delivered[v["quality"]]
            device = (v["ready"] - begin) + (end - v["finish"])
        elif step == 12 and model.level == 3:
            begin = max(0, cases.at[case, "ready_s"] - compare.DRAFT_LEAD)
            device = v["ready"] - begin
        elif step in (19, 24):
            begin = last_move[v["pallet"]] if step == 19 else last_document[case]
            device = v["ready"] - begin

        row.update(ready_s=begin, finish_s=end)
        if end > limits[case] + 1e-7:
            rows.append(dict(row, state="open"))
            continue
        manual = human_end - human_start
        elapsed = end - begin
        work = manual + device + lab
        queue = elapsed - work - release_wait
        assert min(manual, device, lab, queue) >= -1e-6, (step, v["id"], queue)
        rows.append(dict(row, state="completed", manual_s=manual, device_s=max(0,device),
                         lab_s=lab, release_wait_s=release_wait, queue_s=max(0,queue),
                         work_s=work, wait_s=max(0,queue) + release_wait, elapsed_s=elapsed))

    # A pallet waiting outside a full lane has a physical arrival but no WMS visit yet.
    for pallet, scan in model.scans.items():
        case = scan["case"]
        if case in limits and pallet not in scanned and scan["arrival_s"] <= limits[case]:
            rows.append(dict(step=2, entity=pallet, attempt=1, case=case, ready_s=scan["arrival_s"],
                             stop_s=limits[case], state="open", manual_observed_s=0.0))
    frame = pd.DataFrame(rows).set_index(KEY).sort_index()
    assert frame.index.is_unique
    labor = frame.groupby("case").manual_observed_s.sum().reindex(cases.index, fill_value=0.0)
    np.testing.assert_allclose(labor, cases.people_s, rtol=0, atol=1e-6)
    assert len(frame.xs(2)) == cases.pallets.sum()
    return frame


def export_table(frames, out):
    base, after = frames["R0"], frames["R3"]
    paired = base[base.state.eq("completed")].join(
        after[after.state.eq("completed")], how="inner", lsuffix="_0", rsuffix="_3")
    rows = []
    for step in range(1, 26):
        spec = PILOT.ops[step]
        subset = paired[paired.index.get_level_values("step") == step]
        title, change = TITLES[step]
        row = {"Шаг": f"P{step:02}", "Было": spec["operation"], "Стало": title or spec["operation"],
               "Как выполняется после внедрения": change, "Единица": UNIT_NAMES[spec["scope"]],
               "Парных выполнений": len(subset)}
        for arm, frame in frames.items():
            states = Counter(frame.loc[frame.index.get_level_values("step") == step, "state"])
            for state, label in [("completed", "Завершено"), ("open", "В ожидании или работе на границе"), ("skipped", "Ветвь пропущена")]:
                row[f"{label}, {arm}"] = states[state]
        for k, name in [("work_s","Выполнение"), ("wait_s","Ожидание"), ("elapsed_s","Всего"),
                        ("manual_s","Активный труд"), ("queue_s","Очередь"),
                        ("lab_s","Лабораторная часть"), ("release_wait_s","Ожидание срока качества"),
                        ("device_s","Устройство, обмен и переход к резервному посту")]:
            for suffix, arm in [("0","R0"), ("3","R3")]:
                row[f"{name}, с, {arm}"] = subset[f"{k}_{suffix}"].mean()
        row["Изменение всего, с"] = (subset.elapsed_s_3 - subset.elapsed_s_0).mean()
        rows.append(row)
    table = pd.DataFrame(rows)
    table.to_csv(out/"operations_s1_l2.csv", sep=";", decimal=",", encoding="utf-8-sig",
                 float_format="%.6f", index=False)
    combined = pd.concat(frames, names=["arm"])
    combined.to_parquet(out/"operation_observations_s1_l2.parquet")
    return table


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    out = parser.parse_args().out
    out.mkdir(parents=True, exist_ok=True)
    reference = json.loads((RESULTS/"summary.json").read_bytes())
    design = reference["design"]
    for path, expected in design["source_sha256"].items():
        assert PILOT.sha(PILOT.PROJECT/path) == expected, path
    inputs = compare.input_for_load("L2", out)
    assert compare.digest(inputs) == design["input_sha256"]
    assert (inputs["scenario"], inputs["load"]) == ("S1", "L2")
    operations = {k:dict(v) for k,v in PILOT.ops.items()}
    for key, (_, seconds) in PILOT.settings["retained_manual_modes_s"].items():
        operations[int(key[1:])]["manual_labor_s"] = seconds
    frames = {}
    with patch.object(PILOT.engine, "ops", operations):
        for arm, level in [("R0",0), ("R3",3)]:
            model = compare.Warehouse(inputs, level, False, design["models"], design["sref_s"]).execute()
            cases = compare.case_report(model)
            saved = pd.read_parquet(RESULTS/f"{arm}_cases.parquet")
            pd.testing.assert_frame_equal(cases, saved, check_exact=False, rtol=0, atol=1e-6)
            assert not compare.invariants(model)["violations"]
            frames[arm] = observation_frame(model, cases)
            print(arm, len(cases), sum(cases.success72), frames[arm].state.value_counts().to_dict(), flush=True)
            del model
            gc.collect()
    table = export_table(frames, out)
    assert len(table) == 25 and max(table["Парных выполнений"]) > 10000
    print("Verified operation timings for all 25 steps.", flush=True)


if __name__ == "__main__":
    main()
