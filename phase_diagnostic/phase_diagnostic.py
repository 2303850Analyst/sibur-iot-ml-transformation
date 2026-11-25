import gzip
import hashlib
import io
import json
import os
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

ARCHIVE = Path(os.getenv("ARCHIVE", Path.home() / "Downloads/WMS_20261118_223420_agent.zip"))
OUTPUT = Path(__file__).resolve().parent
START, STOP, END = 86400, 2678400, 2764800
EVENTS = ("physical_arrival", "physical_buffer", "register", "reserve_and_start", "physical_move_end", "confirm_place")
LABELS = ("До входа в приёмочный буфер", "От входа в буфер до регистрации",
          "От регистрации до начала перемещения", "Физическое перемещение", "Ожидание подтверждения")


def analyze(archive, variant):
    folder = f"series_py312/artifacts/runs/S1-L2-1000-{variant}"
    raw = archive.read(f"{folder}/events.jsonl.gz")
    manifest = json.loads(archive.read(f"{folder}/manifest.json"))
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest["files"]["events.jsonl.gz"]:
        raise ValueError(f"{variant}: event hash mismatch")
    first = defaultdict(dict)
    with gzip.open(io.BytesIO(raw), "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line, parse_float=Decimal)
            kind = row["event"]
            if kind not in EVENTS or (kind == "confirm_place" and not row["primary"]):
                continue
            if row["time"] >= END:
                raise ValueError("Event outside observation window")
            first[row["pallet"]].setdefault(kind, Decimal(row["time"]))
    cohort = {pid for pid, times in first.items() if START <= times["physical_arrival"] < STOP}
    totals = [Decimal(0) for _ in LABELS]
    completed = set()
    for pid in cohort:
        times = first[pid]
        if "confirm_place" not in times:
            continue
        stamps = [times[kind] for kind in EVENTS]
        durations = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
        if min(durations) < 0 or sum(durations) != stamps[-1] - stamps[0]:
            raise ValueError(f"{variant}/{pid}: phase order or sum mismatch")
        completed.add(pid)
        totals = [old + value for old, value in zip(totals, durations)]
    counts = (len(cohort), len(completed))
    if counts != (manifest["metrics"]["inbound"], manifest["metrics"]["placed_by_drain"]):
        raise ValueError(f"{variant}: cohort differs from manifest")
    total = sum(totals)
    return dict(cohort=counts[0], completed=counts[1], unfinished=counts[0]-counts[1],
                mean_cycle_s=float(total/counts[1]), events_sha256=digest,
                input_hash=manifest["metrics"]["input_hash"], fingerprint=manifest["fingerprint"],
                phases=[dict(label=label, mean_s=float(value/counts[1]), share_pct=float(100*value/total))
                        for label, value in zip(LABELS, totals)]), cohort, completed


def main():
    with ZipFile(ARCHIVE) as archive:
        base, cohort_b, completed_b = analyze(archive, "B")
        candidate, cohort_c, completed_c = analyze(archive, "C")
    if cohort_b != cohort_c or any(base[key] != candidate[key] for key in ("input_hash","fingerprint")):
        raise ValueError("Incomparable runs")
    report = dict(scope="S1/L2 seed 1000", source_archive=ARCHIVE.name,
                  cohort_start_s=START, cohort_end_exclusive_s=STOP, observation_end_exclusive_s=END,
                  completed_in_both=len(completed_b & completed_c), B=base, C=candidate)
    (OUTPUT/"phase_diagnostic.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    num = lambda value: f"{value:.3f}".replace(".",",")
    rows = [f"| {left['label']} | {num(left['mean_s'])} | {num(right['mean_s'])} | {num(left['share_pct'])}% |"
            for left,right in zip(base["phases"],candidate["phases"])]
    doc = f"""# Из чего складывается время первичного размещения

По журналам S1/L2 для seed 1000 в основную когорту каждого варианта вошла {base['cohort']} паллета. Первичное размещение завершено для {base['completed']} в B и {candidate['completed']} в C. В обоих вариантах успели завершить размещение {report['completed_in_both']} одних и тех же паллет.

| Время на завершённую паллету | B, с | C, с | Доля цикла B |
|---|---:|---:|---:|
{chr(10).join(rows)}
| Полный цикл, среднее | {num(base['mean_cycle_s'])} | {num(candidate['mean_cycle_s'])} | 100% |

Вход в буфер зарегистрирован физическим событием. Последняя отметка - подтверждение первичного размещения, в том числе в карантине. В расчёт вошли поступления с 86 400 с включительно до 2 678 400 с исключительно. Их завершение отслеживалось до конца запуска на 2 764 800 с. Для каждой завершённой паллеты сумма пяти фаз совпала с интервалом между прибытием и подтверждением. Отрицательных длительностей среди этих фаз нет. Считывание кода может идти во время ожидания входа в буфер, поэтому отдельное слагаемое для него не вводилось.

В обоих вариантах основная часть времени прошла до входа в буфер. Для установления причины нужны данные о доступности мест, занятости исполнителей и решениях диспетчера в каждый интервал ожидания.

К концу наблюдения в B оставались {base['unfinished']} паллет без первичного размещения, в C - {candidate['unfinished']}. Средние в таблице относятся только к завершённым паллетам этого повтора. Гипотеза H2 оценивает другую величину - ограниченное время по всем прибытиям в 20 парных повторах. Перенос этих длительностей на промышленный склад требует натурных замеров.

Запуск из каталога со скриптом: `python phase_diagnostic.py`. По умолчанию исходный архив ищется в Downloads. Переменная среды ARCHIVE задаёт иной путь. Контрольные суммы сжатых журналов сверены с исходными манифестами и сохранены в phase_diagnostic.json.
"""
    (OUTPUT/"PHASE_DIAGNOSTIC_RU.md").write_text(doc,encoding="utf-8")


if __name__ == "__main__":
    main()
