from core_revision import *
from bench import csvfile
import pyarrow.parquet as pq
import itertools,shutil,statistics
def comparison(part,baseline,target):
    a=pq.read_table(baseline/'operation_aggregate.parquet').to_pylist();b=pq.read_table(part/'operation_aggregate.parquet').to_pylist()
    res=[]
    for i,(l,r) in enumerate(zip(a,b),1):
        row={'operation':f'P{i:02d}','name':ops[i]['operation'],'scope':ops[i]['scope'],'control_run':l['run_id'],'variant_run':r['run_id']}
        for key in ('visits','skips','worker_ms','machine_ms','service_ms','wait_ms','critical_ms'):
            for obj,label in ((l,'ASIS'),(r,'variant')):
                row[key+'_'+label]=obj[key] if key in ('visits','skips') else obj[key]/60000/obj['cases']
            base=row[key+'_ASIS'];change=row[key+'_variant']-base;row[key+'_delta']=change;row[key+'_change_pct']=None if not base else 100*change/base
        for key in ('service_ms','wait_ms'):
            for obj,label in ((l,'ASIS'),(r,'variant')):
                row[key+'_per_visit_'+label]=None if not obj['visits'] else obj[key]/obj['visits']
        res.append(row)
    csvfile(target,res)
    blocks=[]
    for indices in [range(0,5),range(5,11),range(11,16),range(16,21),range(21,25)]:
        d={'block':'B'+str(len(blocks)+1)}
        for k in ('service_ms','wait_ms','worker_ms','machine_ms','critical_ms'):
            for label in ('ASIS','variant','delta'):d[k+'_'+label]=sum(res[i][k+'_'+label] for i in indices)
            base=d[k+'_ASIS'];d[k+'_change_pct']=100*d[k+'_delta']/base if base else None
        blocks.append(d)
    csvfile(target.with_name(target.name.replace('operation','block')),blocks)
def ready():
    from case_replay_tool import verify
    assert json.loads((HOME/'preflight/pilot_signature.json').read_bytes())==signatures()
    bench=json.loads((DEST/'preflight/selection.json').read_bytes())
    assert bench['version']==fingerprint()
    prefix=bench['series'];rows=[]
    for s,l,m in [('S1','L1',0),('S1','L1',127),('S4','L2',0),('S4','L2',127),('S6','L3',0),('S6','L3',127),('S4','L2',63),('S1','L1',63),('S6','L3',63)]:
        part=DEST/prefix/s/l/'2900'/f'C{m:03d}'
        rows+=verify(part)
    csvfile(DEST/'preflight/replay_verification.csv',rows)
    base=DEST/prefix/'S1/L1/2900/C000'
    comparison(base.with_name('C127'),base,DEST/'preflight/comparisons_by_operation.csv')
    contract=json.loads((HOME/'design/protocol/RESULTS_DATA_CONTRACT_V3_2_CANONICAL.json').read_bytes())
    for name,section in [('cases','case_ledger'),('resources','resource_ledger'),('visits','diagnostic_operation_visit_trace'),('runs','run_manifest')]:
        schema=pq.read_schema(base/(name+'.parquet'))
        assert set(contract[section])<=set(schema.names),(name,set(contract[section])-set(schema.names))
    stats=json.loads((DEST/'preflight/contrast.json').read_bytes())['records']
    assert not any(r['audit_violations'] for r in stats)
    bytes_per_run=sum(sum(f.stat().st_size for f in (DEST/prefix/r['scenario_id']/r['load_id']/str(r['seed'])/r['config_id']).glob('*')) for r in stats)/len(stats)
    disk=shutil.disk_usage(DEST)
    forecast=bytes_per_run*46080
    capacity={'mean_pilot_bytes':bytes_per_run,'projected_bytes':forecast,'free_before':disk.free,'reserve_bytes':100_000_000_000}
    save(DEST/'preflight/capacity.json',capacity)
    assert disk.free>100_000_000_000
    plans=json.loads((HOME/'preflight/design.json').read_bytes())
    table=['# Допуск к серии A','',f"Проверены {len(rows)} выбранных записей из девяти запусков, а также все строки поступлений и диагностических визитов при полном повторе этих запусков. Файлы replay_verification.csv и comparisons_by_operation.csv содержат результаты проверок.",'',
        f"Выбрано {bench['workers']} процессов по измеренному тесту. Сведения о памяти, диске и Docker приведены в environment_before.json и capacity.json. Ожидаемый объём рассчитан по смешанной пилотной выборке и уточняется по фактическому расходу.",'',
        'Источник AS-IS - согласованные сценарные режимы из JSON. Значения остаточных действий хранятся в settings.json. При отсутствии численного остатка в спецификации принята отдельная сценарная величина до запуска пилотов. Сканирование сохраняет 20 с физической сверки. На рейсе водитель занимает тот же интервал, что и погрузчик, с отдельным временем переключения. На P19 водитель подтверждает установку, погрузчик удерживается без активного движения. M6 меняет доступную оценку буфера, M7 меняет прогноз на том же наборе допустимых действий.', '',
        '| Код | Операция | Роль | Базовая мода, с | Остаток при включении |','|---|---|---|---|---|']
    for n,o in ops.items():
        key=o['operation_id'];rule=settings['retained_manual_modes_s'].get(key)
        rest=f'M{rule[0]}: {rule[1]} с' if rule else {2:'20 с + фактическое считывание или ручной резерв',7:'180 с инспектора + 720 с внешнего процесса',10:'водитель и машина',18:'водитель и машина'}.get(n,'та же ручная операция или неприменимая ветка')
        table.append(f"| {key} | {o['operation']} | {o['primary_resource']} | {o.get('base_mode_s') or 'рейс'} | {rest} |")
    table+=['','Распределение ручного базового времени треугольное, границы 70% и 130% моды. На P07 компоненты разыграны независимо. Хеши трёх планов:','',js(plans['manifest_sha']),'','Файл содержит допуск симуляции. Контрактный replay и нагрузка Odoo имеют отдельные результаты допуска.']
    (DEST/'preflight/FREEZE_READY_RU.md').write_text('\n'.join(table)+'\n',encoding='utf8')
    return bench

