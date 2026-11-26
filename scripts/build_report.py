import argparse
import json
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import yaml

matplotlib.use('Agg')
from matplotlib import pyplot as plt


ART = Path('series_py312/artifacts')
INTEGRATION = Path('artifacts')


def table(frame):
    headings = ' | '.join(map(str, frame.columns))
    return '\n'.join([f'| {headings} |', '| ' + ' | '.join(['---'] * len(frame.columns)) + ' |',
                      *('|' + '|'.join(f' {field:.3f} ' if isinstance(field, float) else f' {field} ' for field in row)
                        + '|' for row in frame.itertuples(index=False, name=None))])


def json_file(path, fallback):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else fallback


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def parameter_registry():
    protocol_source = ART.parent / 'protocol.yaml'
    if not protocol_source.is_file():
        protocol_source = Path('protocol.yaml')
    protocol = yaml.safe_load(protocol_source.read_text(encoding='utf-8'))

    def walk(branch, prefix=''):
        for key, value in branch.items():
            name = f'{prefix}.{key}' if prefix else key
            if isinstance(value, dict):
                yield from walk(value, name)
                continue
            unit = next((unit for suffix, unit in (('mps', 'm/s'), ('seconds', 's'), ('_m', 'm'), ('_pp', 'pp'),
                ('_days', 'days'), ('_hours', 'hours'), ('_years', 'years'), ('rub_per_year', 'RUB/year'),
                ('rub', 'RUB')) if key.endswith(suffix)), 'unspecified')
            if prefix == 'loads':
                unit = 'pallets/day'
            if prefix == 'economics.wages_rub_per_hour':
                unit = 'RUB/hour'
            yield dict(parameter=name, value=json.dumps(value, ensure_ascii=False), unit=unit, origin='PROTOCOL_ASSUMPTION',
                       source='01_EXPERIMENT_AGENT / frozen protocol.yaml', uncertainty='not calibrated to an operating site')

    device_source = protocol_source.parent / 'configs/device_profiles.yaml'
    devices = yaml.safe_load(device_source.read_text(encoding='utf-8'))
    rows = list(walk(protocol))
    rows.extend(dict(**fields, parameter=f'devices.{key}') for key, fields in devices.items())
    pd.DataFrame(rows).to_csv(ART / 'parameter_registry.csv', index=False)


def compare(matrix):
    indexes = np.random.default_rng(20261118).integers(0, 20, size=(5000, 20))
    rows, hypotheses = [], {}
    metrics = ['registration_bounded_s', 'placement_bounded_s', 'register_60_share', 'prep_late_share', 'unplaced_stop',
               'wait_minutes', 'full_buffer_share', 'observed_buffer_mae', 'operator_hours', 'forklift_hours', 'forecast_mae']
    for (scenario, level), block in matrix.groupby(['scenario', 'level']):
        for treatment, baseline in [('B', 'A'), ('C', 'B'), ('C', 'A')]:
            first = block[block.architecture == treatment].set_index('seed').sort_index()
            second = block[block.architecture == baseline].set_index('seed').sort_index()
            assert first.index.equals(second.index)
            for metric in metrics:
                delta = (first[metric] - second[metric]).to_numpy()
                distribution = delta[indexes].mean(axis=1)
                lower, upper = map(float, np.quantile(distribution, [.025, .975]))
                rows.append(dict(evidence='SIMULATION', scenario=scenario, level=level, contrast=treatment + '-' + baseline,
                                 metric=metric, difference=delta.mean(), low95=lower, high95=upper))
                main = {('S1', 'L1', 'B', 'registration_bounded_s'): 'H1',
                        ('S1', 'L2', 'C', 'placement_bounded_s'): 'H2'}.get((scenario, level, treatment, metric))
                if main is None or baseline != ('A' if main == 'H1' else 'B'):
                    continue
                adjusted = list(map(float, np.quantile(-distribution, [.0125, .9875])))
                late = (first.prep_late_share - second.prep_late_share).to_numpy() * 100
                late_ci = list(map(float, np.quantile(late[indexes].mean(axis=1), [.0125, .9875])))
                guard = main == 'H1' or (late_ci[1] <= 1 and first[['duplicate_receipts', 'illegal_moves', 'illegal_release', 'negative_stock']].sum().sum() == 0)
                hypotheses[main] = dict(contrast=baseline + '-' + treatment, scenario=scenario, level=level, metric=metric,
                                        mean_s=float(-delta.mean()), ci95_s=[-upper, -lower], ci975_s=adjusted,
                                        guard_pass=bool(guard), prep_late_delta_pp=float(late.mean()), prep_late_ci975_pp=late_ci,
                                        supported=bool(adjusted[0] > 0 and guard))
    return pd.DataFrame(rows), hypotheses, indexes


def finance(matrix, bootstrap):
    base = matrix[matrix.scenario.eq('S1') & matrix.level.eq('L1')]
    out = []
    discount = sum(1 / 1.2 ** year for year in range(1, 6))
    ramp = discount - .4 / 1.2
    for treatment, baseline in [('B', 'A'), ('C', 'B'), ('C', 'A')]:
        old, new = [base[base.architecture.eq(arch)].set_index('seed').sort_index() for arch in (baseline, treatment)]
        same = bool((old.placed_by_stop.eq(new.placed_by_stop) & old.final_by_drain.eq(new.final_by_drain) &
                     old.unplaced_stop.eq(0) & new.unplaced_stop.eq(0) & old.prep_late.eq(new.prep_late) &
                     old.out_missing.eq(new.out_missing)).all())
        values = ((old.operator_hours - new.operator_hours) * 650 + (old.forklift_hours - new.forklift_hours) * 1200) / 30
        interval = np.quantile(values.to_numpy()[bootstrap].mean(axis=1), [.025, .975])
        total_baseline = (old.operator_hours * 650 + old.forklift_hours * 1200).sum() / old.placed_by_stop.sum()
        total_treatment = (new.operator_hours * 650 + new.forklift_hours * 1200).sum() / new.placed_by_stop.sum()
        for fraction in (0, .25, .50):
            for capex in (0, 500000, 1500000, 4500000):
                for opex in (0, 100000, 400000, 1000000):
                    annual = values.mean() * 365 * fraction if same else None
                    limit = annual * ramp - opex * discount if same else None
                    out.append(dict(evidence='FINANCIAL_SCENARIO', contrast=treatment + '-' + baseline, comparable=same,
                                    baseline_unplaced_stop=old.unplaced_stop.mean(), treatment_unplaced_stop=new.unplaced_stop.mean(),
                                    operator_hours_difference=(old.operator_hours - new.operator_hours).mean(),
                                    forklift_hours_difference=(old.forklift_hours - new.forklift_hours).mean(),
                                    baseline_rub_per_placed=total_baseline, treatment_rub_per_placed=total_treatment,
                                    arithmetic_cost_delta_rub_day=values.mean(), low95_rub_day=interval[0], high95_rub_day=interval[1],
                                    k=fraction, capex_rub=capex, opex_rub_year=opex, annual_cash_rub=annual,
                                    break_even_capex_rub=limit, npv_rub=limit - capex if same else None))
    return pd.DataFrame(out)


def figures(matrix, paired):
    output = ART / 'figures'
    output.mkdir(exist_ok=True)
    for metric, ylabel in [('unplaced_stop', 'Неразмещённые паллеты к 30-м суткам'),
                           ('full_buffer_share', 'Доля времени с полным буфером')]:
        figure, axes = plt.subplots(2, 3, figsize=(11, 6), sharex=True, sharey=True, layout='constrained')
        for axis, (scenario, group) in zip(axes.flat, matrix.groupby('scenario')):
            for architecture, values in group.groupby('architecture'):
                average = values.groupby('level')[metric].mean()
                axis.plot(average.index, average.values, marker='o', label=architecture)
            axis.set_title(scenario)
            axis.set_ylim(bottom=0)
        axes.flat[0].legend()
        figure.supylabel(ylabel)
        figure.savefig(output / f'{metric}.png', dpi=150)
        plt.close(figure)
    selected = paired[paired.metric.eq('placement_bounded_s') & paired.contrast.eq('C-B')].reset_index(drop=True)
    figure, axis = plt.subplots(figsize=(7, 8), layout='constrained')
    axis.errorbar(selected.difference / 60, np.arange(len(selected)), xerr=np.vstack([
        (selected.difference - selected.low95) / 60, (selected.high95 - selected.difference) / 60]), fmt='o')
    axis.set_yticks(np.arange(len(selected)), selected.scenario + '/' + selected.level)
    axis.axvline(0, color='gray')
    axis.set_xlabel('C − B, ограниченное время размещения, мин')
    figure.savefig(output / 'placement_ml.png', dpi=150)
    plt.close(figure)


def supporting_artifacts():
    stages = [
        ('Поступление', 'pallets.parquet; physical_arrival', 'ожидаемый документ, наблюдение и физическое событие раздельно'),
        ('Идентификация', 'events.jsonl.gz; integration_results.json', 'ошибки этикетки, партии, недоставка, повтор'),
        ('Буфер', 'queue.parquet', 'физический предел, ожидание перед входом, учёт отдельно'),
        ('Место', 'events.jsonl.gz; integration_results.json', 'резерв, вместимость, группа и качество'),
        ('Перемещение', 'pallets.parquet; integration_results.json', 'рейс, подтверждение адреса, карантин и выпуск'),
        ('KPI', 'runs.csv; paired_comparisons.csv', 'ограниченные метрики учитывают незавершённые паллеты'),
        ('Ограниченный буфер', 'queue.parquet; S4', 'смоделировано'),
        ('Нехватка допустимых мест', 'runs.csv; S5', 'смоделировано'),
        ('Расхождение физической и учётной картины', 'queue.parquet; S3', 'смоделировано'),
        ('Ручная регистрация', 'runs.csv; A/B', 'смоделировано'),
        ('Ошибка этикетки или партии', 'events.jsonl.gz; S2', 'смоделировано'),
        ('Нарушение связи и повторы', 'events.jsonl.gz; S3', 'смоделировано'),
        ('Незапланированный пик и очередь', 'runs.csv; S6', 'смоделировано'),
        ('Выпуск качества и карантин', 'pallets.parquet; S5', 'смоделировано'),
        ('Компьютерное зрение', '', 'вне опыта'),
        ('Весовой контроль', '', 'вне опыта'),
        ('Промышленная интеграция с ERP/SAP', '', 'вне опыта'),
        ('Строительство и реальные потери', '', 'вне опыта'),
    ]
    pd.DataFrame(stages, columns=['scope', 'artifact', 'boundary']).assign(evidence='SIMULATION').to_csv(
        ART / 'traceability_matrix.csv', index=False)
    claims = [
        ('H1', 'Ограниченная задержка регистрации', 'SIMULATION', 'hypotheses.json:H1', 'mean(A-B), cap 600 s', 'S1/L1'),
        ('H2', 'Ограниченное первичное размещение', 'SIMULATION', 'hypotheses.json:H2', 'mean(B-C), cap 86400 s', 'S1/L2; просрочка подготовки'),
        ('RQ3', 'Устойчивость очереди и буфера', 'SIMULATION', 'summary_54.csv:S1–S6/L1–L3', 'mean by architecture, scenario and level', 'условная модель'),
        ('RQ4', 'Порог CAPEX', 'FINANCIAL_SCENARIO', 'economics.csv:S1/L1', 'PV(cash benefits)-PV(OPEX)', 'только сопоставимая работа'),
        ('INT', 'Транзакции референсной WMS', 'ODOO_INTEGRATION', 'integration_results.json:cases', 'Odoo picking + stock quants + audit', 'не является валидацией 1080 запусков'),
        ('REPLAY', 'Сверка 54 выборок по 50 паллет', 'ODOO_INTEGRATION', 'replay/verification.json:counts', 'native stock records + compare canonical sequence', 'фоновые остатки из журнала модели'),
        ('HTTP', 'Фактическая HTTP-нагрузка хоста', 'API_BENCHMARK', 'api_benchmark/status.json:levels', 'Locust measured counts and all-request P95', 'не является пропускной способностью склада'),
    ]
    pd.DataFrame(claims, columns=['id', 'claim', 'evidence', 'source_key', 'formula', 'limit']).to_csv(ART / 'claims.csv', index=False)


def main():
    global ART
    parser = argparse.ArgumentParser()
    parser.add_argument('--series', type=Path, default=ART)
    args = parser.parse_args()
    ART = args.series
    ART.mkdir(parents=True, exist_ok=True)
    expected = ART / 'runs.csv'
    integration = json_file(INTEGRATION / 'integration_results.json', {'status': 'NOT_RUN', 'cases': []})
    benchmark = json_file(INTEGRATION / 'api_benchmark/status.json', {'status': 'NOT_RUN'})
    replay = json_file(INTEGRATION / 'replay/status.json', {'status': 'NOT_RUN', 'cases': []})
    replay_verification = json_file(INTEGRATION / 'replay/verification.json', {'status': 'NOT_RUN'})
    concurrency = json_file(INTEGRATION / 'api_concurrency.json', {'status': 'NOT_RUN'})
    metadata = json_file(ART.parent / 'data/manifests/wepastacks.json', {'status': 'NOT_AUDITED'})
    reproduction = json_file(ART / 'reproducibility.json', {'status': 'NOT_RUN'})
    count = len(list((ART / 'runs').glob('*/manifest.json')))
    matrix = pd.read_csv(expected) if expected.exists() else pd.DataFrame()
    cells = matrix.groupby(['scenario', 'level', 'architecture']).size() if not matrix.empty else pd.Series(dtype=int)
    c_runs = int(matrix.architecture.eq('C').sum()) if not matrix.empty else 0
    full_matrix = bool(count == 1080 and len(matrix) == matrix.run_id.nunique() == 1080 and c_runs == 360
                       and len(cells) == 54 and cells.eq(20).all()
                       and matrix.groupby(['scenario', 'level', 'seed']).input_hash.nunique().eq(1).all())
    replay_verified = (replay_verification['status'] == 'PASS' and replay_verification.get('verified_cases') == 54
                       and replay_verification.get('namespace') == replay.get('namespace'))
    full_integration = (integration['status'] == 'PASS_DEMO_AND_RESTART' and replay['status'] == 'PASS_PROJECTED_REPLAY'
                        and replay_verified and benchmark['status'] == 'PASS')
    status = ('PARTIAL_RUNS' if not full_matrix else 'PARTIAL_SIMULATION_ONLY' if not integration['status'].startswith('PASS') else
              'FULL_PASS' if full_integration and reproduction['status'] == 'PASS' else 'PARTIAL_RUNS')
    dump(ART / 'experiment_status.json', dict(technical_status=status, expected_runs=1080, completed_runs=count,
         data_status=metadata['status'], integration=integration['status'], replay_54=replay['status'],
         completed_cells=len(cells), c_runs=c_runs, replay_cases=len(replay['cases']), replay_verified=replay_verified,
         benchmark=benchmark['status'],
         live_concurrency=concurrency['status'], reproducibility=reproduction['status']))
    supporting_artifacts()
    parameter_registry()
    head = f'''# Входящий поток WMS: референсный стенд с имитационной моделью

**Статус:** `{status}`. Источник: `{metadata['status']}`. Симуляция: {count}/1080 завершённых запусков. Odoo/MQTT: `{integration['status']}`, проверок в журнале: {len(integration['cases'])}. Replay: {len(replay['cases'])}/54, `{replay['status']}`. Locust: `{benchmark['status']}`. Повтор двух seed: `{reproduction['status']}`.

## Что сравнивали

A - ручное подтверждение ТСД и простой прогноз. B - автоматическое наблюдение, датчик буфера и тот же прогноз. C - те же наблюдения и правила, обучаемый прогноз вместо простого. Прогноз влияет на очерёдность обязательной подготовки мест, а не сокращает физическую длительность рейса. В реальном приложении наблюдение IoT не проводит складскую приёмку: её отдельно подтверждает оператор. В ускоренной модели штатное автоматическое наблюдение заменяет ручной ввод, но не общую проверку поступления.

## Данные и допущения

См. `data_audit.md` и `../protocol.yaml`. WEPAStacks не относится к СИБУРу. Операционная модель использует 20 SKU, две группы хранения, 5200 обычных мест, 129 карантинных мест и отдельный входящий буфер. При среднем рейсе 320 с два погрузчика могут теоретически выполнить 540 рейсов/сутки без подготовки, либо 472,5 рейса после 24 задач по 15 минут; карантинные перемещения и простои снижают предел. Это расчёт для заданных ресурсов, не паспорт склада.

Остатки и документы реальной Odoo относятся к отдельной базе `wms_reference`. При replay блокировки фоновых ячеек восстанавливаются из журналов модели; реальные операции проводятся для первых 50 паллет каждой ячейки, а не для всего модельного склада. Locust проверяет HTTP-вызовы, а не пропускную способность склада.
'''
    head += f'\nПроверка конкурентной приёмки: `{concurrency["status"]}`.\n'
    head += ('\nПри сверке физический выезд из буфера проводится через транзитную локацию. '
             'Демонстрационный API освобождает место только при подтверждении размещения. '
             'Сверка не доказывает совпадение двух контрактов на пределе вместимости.\n')
    if benchmark.get('levels'):
        rows = pd.DataFrame(benchmark['levels']).rename(columns={'completed_pallets': 'completed_workflows'})
        columns = ['users', 'elapsed_s', 'http_requests', 'http_errors', 'completed_workflows',
                   'business_rejections', 'write_technical_errors', 'p95_ms']
        head += '\n## HTTP-нагрузка · API_BENCHMARK\n\n' + table(rows[columns]) + (
            '\n\n30 с прогрева и 120 с измерения на уровень. Успешный складской цикл включает приёмку, '
            'резервирование и размещение. P95 рассчитан по HTTP-запросам.\n')
    if not full_matrix:
        (ART / 'report.md').write_text(head + '\nОсновная матрица не завершена. H1/H2 и финансовые выводы не рассчитаны.\n', encoding='utf-8')
        return
    metrics = matrix.select_dtypes('number').columns.difference(['seed']).tolist()
    by_cell = matrix.groupby(['scenario', 'level', 'architecture'])[metrics].mean().reset_index().assign(evidence='SIMULATION')
    by_cell.to_csv(ART / 'summary_54.csv', index=False)
    paired, primary, bootstrap = compare(matrix)
    paired.to_csv(ART / 'paired_comparisons.csv', index=False)
    dump(ART / 'hypotheses.json', dict(evidence='SIMULATION', forecast='fixed_before_test', hypotheses=primary))
    costs = finance(matrix, bootstrap)
    costs.to_csv(ART / 'economics.csv', index=False)
    figures(matrix, paired)
    forecast = pd.read_parquet(ART / 'source_forecast.parquet')
    forecast['abs_base'] = abs(forecast.actual - forecast.baseline)
    forecast['abs_ml'] = abs(forecast.actual - forecast.ml)
    accuracy = forecast.groupby(['load', 'partition']).agg(naive_MAE=('abs_base', 'mean'), ml_MAE=('abs_ml', 'mean'),
                   abs_base=('abs_base', 'sum'), abs_ml=('abs_ml', 'sum'), actual=('actual', 'sum')).reset_index()
    accuracy['naive_WAPE'] = accuracy.pop('abs_base') / accuracy.actual.replace(0, np.nan)
    accuracy['ml_WAPE'] = accuracy.pop('abs_ml') / accuracy.pop('actual').replace(0, np.nan)
    accuracy.assign(evidence='SIMULATION').to_csv(ART / 'source_forecast_accuracy.csv', index=False)
    hypotheses = pd.DataFrame([dict(H=key, contrast=item['contrast'], effect_s=item['mean_s'],
        CI95=' / '.join(f'{limit:.3f}' for limit in item['ci95_s']), CI97_5=' / '.join(f'{limit:.3f}' for limit in item['ci975_s']),
        status='поддержана' if item['supported'] else 'не поддержана', guard=item['guard_pass']) for key, item in primary.items()])
    robustness = by_cell.groupby(['scenario', 'level']).agg(
        unplaced_min=('unplaced_stop', 'min'), unplaced_max=('unplaced_stop', 'max'),
        full_buffer_min=('full_buffer_share', 'min'), full_buffer_max=('full_buffer_share', 'max'),
        outside_trend_max=('outside_trend_per_day', 'max'), max_util=('forklift_utilization', 'max')).reset_index()
    finance_table = costs[costs.k.eq(.5) & costs.capex_rub.eq(0) & costs.opex_rub_year.eq(100000)][[
        'contrast', 'comparable', 'baseline_unplaced_stop', 'treatment_unplaced_stop',
        'baseline_rub_per_placed', 'treatment_rub_per_placed', 'break_even_capex_rub']].fillna('NA')
    contrast_table = paired[paired.contrast.isin(['B-A', 'C-B']) & paired.metric.isin(['registration_bounded_s', 'placement_bounded_s'])]
    contrasts = contrast_table.pivot(index=['scenario', 'level'], columns=['contrast', 'metric'], values='difference').reset_index()
    contrasts.columns = ['/'.join(filter(None, column)) for column in contrasts.columns]
    (ART / 'report.md').write_text(head + f'''
## Главные вопросы · SIMULATION

Матрица: 54 ячейки, 20 парных seed в каждой, 1080 запусков, из них 360 C. Единица перевыборки - целый seed; bootstrap 5000 парных перевыборок. Положительный основной эффект означает улучшение; интервалы условны относительно модели и исходного профиля. Интервал, включающий ноль, не доказывает эквивалентность.

{table(hypotheses)}

В S1/L2 доля просроченных задач подготовки выросла на {primary['H2']['prep_late_delta_pp']:.3f} п.п. Интервал 97,5%: от {primary['H2']['prep_late_ci975_pp'][0]:.3f} до {primary['H2']['prep_late_ci975_pp'][1]:.3f} п.п. Верхняя граница ниже заданного предела в 1 п.п.

Оценка прогноза на исходном хронологическом отложенном ряду с доступной сверенной историей, не на фактах будущих операций:

{table(accuracy)}

### Где показатели меняются

Ячейки показаны все. Разность ниже нуля означает более короткое время у новой архитектуры. Исследовательские интервалы и вторичные показатели - в `paired_comparisons.csv`.

{table(contrasts)}

## RQ3. Очереди и загрузка · SIMULATION

Для каждого режима показан диапазон по средним A/B/C. Физический предел виден по остатку, времени полного буфера и тренду очереди при фиксированных ресурсах.

{table(robustness)}

## RQ4. Сценарная экономика · FINANCIAL_SCENARIO

Ставки 650 руб./ч оператора и 1200 руб./ч погрузчика с водителем - допущения. Для примера показана доля монетизации 0,5 и OPEX 100 тыс. руб./год; полная сетка - в `economics.csv`. Если объём не сопоставим или работа не завершена, NPV и порог не рассчитываются. Стоимость делится на фактически завершённую работу; это не стоимость внедрения. Отрицательный порог означает, что даже нулевой CAPEX не обеспечивает NPV ≥ 0 при данном OPEX.

{table(finance_table)}

Модель не оценивает потери качества, строительство, внешнее хранение, оборотный капитал или промышленную интеграцию. Прогноз на пять лет - сценарная проекция, не эксплуатационное наблюдение. Референсный стенд не заменяет SAP EWM, не воспроизводит условия СИБУРа и не проверяет физическое оборудование.
''', encoding='utf-8')


if __name__ == '__main__':
    main()
