import csv,json
from collections import defaultdict
import pandas as pd
from inputs import ROOT
from coverage import main as coverage
from preflight import write

def main():
    cp=coverage()
    out=ROOT/'report';out.mkdir(exist_ok=True)
    names={int(r['mask']):r['name_ru'] for r in csv.DictReader((ROOT/'plans/configurations.csv').open(encoding='utf-8-sig'))}
    matched=defaultdict(dict);live=[];invalid=[]
    for f in sorted((ROOT/'results/pilot').glob('*/summary.json')):
        p=f.parent;item=json.loads(f.read_bytes())
        if (p/'reliability.json').exists():
            invalid.append(p.name);continue
        if 'clock' in item:live.append(item);continue
        if not (p/'audit.json').exists():continue
        s,l,seed,*_=p.name.split('-')
        df=pd.read_parquet(p/'case_metrics.parquet').set_index('case').sort_index()
        matched[(s,l,seed,item['input_sha256'])][item['mask']]=(df,item,p)
    summaries=[];audit=[]
    for (s,l,seed,h),variants in sorted(matched.items()):
        for mask,(df,item,p) in sorted(variants.items()):
            a=json.loads((p/'audit.json').read_bytes())
            audit.append([p.name,mask,len(df),a['events_checked'],a['max_buffer'],sum(a['findings'].values()),'Технический пилот'])
            if 0 not in variants or mask==0:continue
            base=variants[0][0];assert base.index.equals(df.index)
            for field,label,unit,mult in [('T72_min','Время до стандартного закрытия, ограниченное 72 ч','мин/поступление',1),
                    ('labor_min','Активный труд по зарегистрированным операциям','чел.-мин/поступление',1),('machine_min','Работа погрузчиков','маш.-мин/поступление',1),
                    ('success72','Стандартное закрытие за 72 ч','% поступлений',100)]:
                av=float(base[field].mean()*mult);bv=float(df[field].mean()*mult)
                summaries.append(dict(mask=mask,name=names[mask],scenario=s,load=l,seed=int(seed),metric=label,unit=unit,
                         cases=len(df),before=av,after=bv,delta=bv-av,change_pct=100*(bv-av)/av if av else None,
                         status='Технический пилот, один парный seed',limitation='Нет ДИ основной серии; учёт незавершённых активных действий требует проверки'))
    write(out/'tables.json',dict(comparisons=summaries,audit=audit,live=live,missing=cp['counts'],configurations=list(names.items())))
    pd.DataFrame(summaries).to_csv(out/'pilot_comparisons.csv',index=False,encoding='utf-8-sig')
    lines=['# WMS V3. Промежуточные результаты','',
        'Статус PARTIAL. Проверен исходный дизайн с 128 масками. В основной серии пока нет принятых измерений. Числа ниже относятся к техническому пилоту на сценарных данных WEPAStacks.','',
        '| Контур | Запланировано | Выполнено в основной серии | Осталось |','|---|---:|---:|---:|',
        '| Модель полного поступления | 46 080 | 0 | 46 080 |','| Odoo, 20 пользователей | 6 912 | 0 | 6 912 |','',
        '## Сравнение на одинаковых входах','',
        'T72 измеряет минуты от готовности первой паллеты до стандартного закрытия всего поступления. Каждое поступление без стандартного успеха за 72 часа получает 4 320 минут. В таблице одна пара запусков на строку. Процент рассчитан от среднего базового варианта.','',
        '| Сценарий | Нагрузка | Seed | Вариант | Поступлений | C000, мин | Вариант, мин | Разница, мин | Изменение |',
        '|---|---|---:|---|---:|---:|---:|---:|---:|']
    for r in summaries:
        if r['metric'].startswith('Время'):
            lines.append(f"| {r['scenario']} | {r['load']} | {r['seed']} | C{r['mask']:03d} | {r['cases']} | {r['before']:.2f} | {r['after']:.2f} | {r['delta']:+.2f} | {r['change_pct']:+.2f}% |")
    lines+=['','Полный состав каждого варианта, труд и машинное время приведены в pilot_comparisons.csv. Для доли завершения абсолютная разница выражена в процентных пунктах.','',
        '## Проверки приложения','',
        'На отдельном стенде Odoo 19.0 проведены приёмка и складские перемещения по двум паллетам на документ. Функциональные проверки охватывают карантин, повтор сообщения, проверку адреса, полномочия ОТК, отказ при неполном составе и подтверждение обязательного обмена. В журнале доступа видны запросы к ТСД. Технические сообщения во второй проверке прошли через Mosquitto.']
    lines+=['',f"Функциональную проверку 25 операций на отдельном двухпаллетном поступлении прошли {cp['functional_odoo_masks']['successful']} из 128 масок Odoo. Для каждой маски проверены остатки и завершённые проводки. Транзакция откатывалась после проверки, чтобы у следующей маски был тот же начальный запас."]
    for t in live:
        lines+=['',f"Пилот C000/S1/L1 зарегистрировал {t['session_count']} ролевых сессий. В основном окне предложено {t['offered']} поступлений, за индивидуальный горизонт в 180 секунд закрыто {t['success_180s']}. Журнал содержит {t['requests']} обращений. Единица времени здесь - секунда стенда."]
    lines+=['','## Ограничения','',
        f'В {len(audit)} текущих имитационных пилотах отдельный обработчик проверил {sum(r[3] for r in audit):,} событий. Перечень проверок и найденные нарушения сохранены в audit.json каждого запуска. Четыре ранних технических прогона оставлены для диагностики с отметкой INVALID_FOR_COMPARISON.',
        '', 'На одном хосте плановая длительность окон Odoo составляет 6 912 × (20 + 60 + 180 + 180) = 3 041 280 секунд, или 35 суток 4 часа 48 минут. Восстановление снимков и сбор доказательств потребуют дополнительного времени.',
        '', 'Выводы о пользе каждого модуля, ДИ, проверка H1-H7, экономическая оценка и Pareto остаются открытыми. Для них нужна основная серия на едином зафиксированном исполнителе. Стоимость ожидания в заработную плату не переводилась.']
    (out/'RESULTS_RU.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    remain=json.loads((ROOT/'configs/remaining.json').read_bytes())
    (out/'COMPLETENESS.md').write_text('# Покрытие V3\n\nОсновная серия имеет статус PARTIAL.\n\n'+'\n'.join('- '+r['remaining'] for r in remain)+'\n\nТочные идентификаторы оставшихся испытаний сохранены в results/coverage/missing_run_manifest.csv и missing_live_manifest.csv.\n',encoding='utf8')
    print('Paired pilot comparisons',len(summaries),'audited pilots',len(audit),'live pilots',len(live))
if __name__=='__main__':main()

