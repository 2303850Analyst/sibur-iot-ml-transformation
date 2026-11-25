"""Read archived pilot outputs only. Run inside the original experiment_v3 tree."""
from pathlib import Path
from collections import defaultdict, Counter
from functools import lru_cache
import csv,gzip,json,hashlib,zipfile
import numpy as np
import pandas as pd

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
RUNS={'C000':'S1-L1-2900-C000-7b2d7f1d','C127':'S1-L1-2900-C127-bnz890pl'}
OPS=json.loads((ROOT/'protocol.json').read_bytes())['operations']
def save(name,data):
    pd.DataFrame(data).to_csv(OUT/name,index=False,encoding='utf-8-sig',float_format='%.10g')
def frame(rows):
    return pd.DataFrame(rows)
def critical(directory,case_table,units,events):
    by={};quality={};fail=defaultdict(list)
    for r in events:
        if 'op' in r:
            k=(r['case'],r['pallet'],r['op'])
            assert k not in by or r.get('quality'),k
            by[k]=r
            if r.get('quality'):quality[r['quality'],r['op']]=r
        elif r.get('kind')=='open_nonconformity':fail[r['case']].append(r)
    member=units.groupby('case').pallet.agg(list).to_dict()
    qmap=units.set_index('pallet').quality_case.to_dict()
    split=[];checks=[];attribution=Counter()
    def trace(r,c,start,end):
        pieces=[]
        def add(a,b,label,op='',p='',owner=''):
            a=max(a,start);b=min(b,end)
            if b>a+1e-9:pieces.append((a,b,label,op,p,owner))
        def walk(r):
            if r['finished']<=start:return
            n=r['op'];who=r['case'];p=r['pallet'];entry=r['entered']
            label=f'P{n:02d}';qual=r.get('quality') or qmap.get(p)
            prev=None; gap='переход'
            def get(i,unit=''):return by[who,unit,i]
            if n==1:pass
            elif n==2:
                add(start,r['raw_arrival']-30,'готовность выбранной паллеты',p=p,owner=who)
                add(r['raw_arrival']-30,r['raw_arrival'],'внешняя доставка',p=p,owner=who)
                add(r['raw_arrival'],entry,'допуск в буфер',p=p,owner=who)
            elif n in (3,6):prev=get(1);gap='доставка сведений'
            elif n in (7,8,9,11):
                prev=quality[qual,{7:6,8:7,9:8,11:9}[n]];gap={8:'готовность решения качества',9:'доставка решения качества'}.get(n,'переход')
            elif n==16:
                reg=get(15);scan=get(2,p)
                if scan['finished']+.3>reg['finished']:prev=scan;gap='доставка идентификации'
                else:prev=reg
            elif n==10:prev=get(16,p)
            elif n==17:prev=max([get(10,p),quality[qual,11]],key=lambda x:x['finished'])
            elif n in (18,19,20,21):
                prev=get(n-1,p)
                if n==19:gap='подтверждение перемещения'
            elif n==22:prev=max((get(21,u) for u in sorted(member[who])),key=lambda x:x['finished'])
            else:
                prev=get({12:5}.get(n,n-1))
                if n==24:gap='подтверждение обмена'
            if prev:
                assert prev['finished']<=entry+1e-6,(r,prev)
                walk(prev)
                add(prev['finished'],entry,gap,label,p,who)
            add(entry,r['started'],'очередь операции',label,p,who)
            add(r['started'],r['finished'],r['mode'],label,p,who)
        if r:walk(r)
        return pieces
    for c,meas in case_table.iterrows():
        t0=meas.t0; censored=not meas.success72
        end=t0+259200 if censored else meas.t25
        if censored:
            f=min(fail[c],key=lambda x:(x['time'],x['id']))
            p=f['pallet']
            anchor=max([by[c,p,10],quality[qmap[p],11]],key=lambda x:x['finished'])
            assert abs(anchor['finished']-f['time'])<1e-6
            path=trace(anchor,c,t0,end)+[(f['time'],end,'открытое несоответствие до 72 ч','',p,c)]
        else:path=trace(by[c,'',25],c,t0,end)
        cursor=t0
        for a,b,reason,op,p,who in path:
            assert abs(a-cursor)<1e-6,(c,a,cursor,reason)
            cursor=b
            split.append((c,a,b,b-a,reason,op,p,who))
            attribution[reason,op]+=b-a
        assert abs(cursor-end)<1e-6,(c,cursor,end)
        total=sum(b-a for a,b,*_ in path)
        residual=total-60*meas.T72_min
        assert abs(residual)<1e-6
        checks.append((c,censored,meas.T72_min,total/60,residual))
    df=pd.DataFrame(split,columns=['case','from_s','to_s','seconds','segment','operation','pallet','owner_case'])
    return df,checks,attribution

rules={1:'22 с вручную / 0,3 с автоматически (M1)',2:'12 с вручную; повреждённая метка 120 с; M5: 0,3 с либо 60 с при пропуске считывателя',3:'24 с вручную; дополнительное уточнение 30 с только при дефекте документа',4:'18 с вручную',5:'10 с вручную',6:'12 с на общий объект качества',7:'15 с при удержании; иначе ранее принятое решение, 0 с; ожидание готовности учтено между P07 и P08',8:'12 с на общий объект качества',9:'18 с вручную / 0,3 с автоматически (M2)',10:'Только при удержании или несоответствии; рейс по формуле P18, иначе 0 с',11:'35 с при несоответствии, иначе 0 с',12:'18 с / 0,3 с (M3)',13:'22 с / 0,3 с (M3)',14:'15 с / 0,3 с (M3)',15:'30 с при исправлении, иначе 0 с',16:'12 с / 0,3 с (M3) на паллету',17:'14 с / 0,3 с (M4)',18:'180 с обработки + порожний путь / 1 м/с + гружёный путь / 1 м/с',19:'12 с вручную в обоих вариантах',20:'12 с / 0,3 с (M3)',21:'35 с при расхождении, иначе 0 с',22:'20 с / 0,3 с (M3)',23:'18 с / 0,3 с (M3)',24:'14 с вручную; сетевое подтверждение перед входом в P24',25:'10 с / 0,3 с (M3)'}
grains={**dict.fromkeys([1,3,4,5,12,13,14,15,22,23,24,25],'поступление'),**dict.fromkeys([6,7,8,9,11],'общий объект качества'),**dict.fromkeys([2,10,16,17,18,19,20,21],'паллета')}
def main():
    ops=[{'код':o['id'],'операция':o['name_ru'],'учёт':grains[i],'правило модели':rules[i]} for i,o in enumerate(OPS,1)]
    blocks=[{'блок':'B'+str(i)} for i in range(1,6)]
    summaries=[];checkrows=[];critrows=[];paired=None;input_hash=None
    for tag,run in RUNS.items():
        d=ROOT/'results/pilot'/run
        summary=json.loads((d/'summary.json').read_bytes())
        assert not (d/'reliability.json').exists()
        if input_hash:assert input_hash==summary['input_sha256']
        input_hash=summary['input_sha256']
        cases=pd.read_parquet(d/'case_metrics.parquet').set_index('case').sort_index();N=len(cases)
        members=pd.read_parquet(d/'pallet_metrics.parquet')
        cohort=members[members['case'].isin(cases.index)][['case','pallet','quality_case']].sort_values(['case','pallet']).reset_index(drop=True)
        if paired is not None:pd.testing.assert_frame_equal(paired,cohort)
        paired=cohort
        with gzip.open(d/'events.jsonl.gz','rt') as f:events=[json.loads(l) for l in f]
        path,checks,attribution=critical(d,cases,members,events)
        path.to_csv(OUT/f'critical_path_{tag}.csv.gz',index=False,compression='gzip')
        for (reason,op),sec in sorted(attribution.items()):critrows.append([tag,reason,op,sec/N/60])
        checkrows.extend([[tag,*r] for r in checks])
        rows=[r for r in events if 'op' in r and r['case'] in cases.index]
        ev=pd.DataFrame(rows).merge(cases[['t0','T72_min']],left_on='case',right_index=True)
        end=ev.t0+60*ev.T72_min
        ev=ev.loc[ev.entered<end].copy()
        end=end.loc[ev.index]
        ev['service']=(np.minimum(ev.finished,end)-ev.started).clip(lower=0)
        ev['queue']=(np.minimum(ev.started,end)-ev.entered).clip(lower=0)
        ev['labor_s']=np.minimum(ev.labor,ev.service);ev['machine_s']=np.minimum(ev.machine,ev.service)
        for n,record in enumerate(ops,1):
            subset=ev[ev.op.eq(n)];active=subset[subset['mode'].ne('branch')]
            counts=active['mode'].value_counts()
            obs=[]
            for mode,g in active.groupby('mode'):
                z=np.round(g.finished-g.started,6)
                if len(z.unique())<=6:val='/'.join(f'{v:g}' for v in sorted(z.unique()))
                else:val=f'{z.min():g}..{z.max():g}'
                obs.append(f'{mode} {val} с ({len(z)})')
            record.update({tag+' режим и фактические длительности (число визитов)':', '.join(obs) or 'только неприменимая ветка',
                tag+' выполненных визитов':len(active),tag+' пропусков ветки':len(subset)-len(active),
                tag+' активно с/выполненный визит':active.service.mean(),tag+' очередь с/выполненный визит':active.queue.mean(),
                tag+' активно мин/поступление':active.service.sum()/60/N,tag+' очередь мин/поступление':active.queue.sum()/60/N,
                tag+' чел.-мин/поступление':active.labor_s.sum()/60/N,tag+' маш.-мин/поступление':active.machine_s.sum()/60/N,
                tag+' активность и очередь на критическом пути мин/поступление':sum(attribution.get((reason,f'P{n:02d}'),0) for reason in ('manual','automatic','machine','очередь операции'))/60/N})
        bm=pd.read_parquet(d/'block_metrics.parquet')
        for n,record in enumerate(blocks,1):
            b=bm[bm.block.eq(n)];assert len(b)==N
            record.update({tag+' поступлений с отметками':int(b.lifetime_s.notna().sum()),tag+' размах мин/поступление с отметками':b.lifetime_s.mean()/60,
              tag+' размах мин/все поступления (нет отметок=0)':b.lifetime_s.fillna(0).mean()/60,
              tag+' активность и очередь на критическом пути мин/поступление':sum(o[tag+' активность и очередь на критическом пути мин/поступление'] for o,meta in zip(ops,OPS) if meta['block']=='B'+str(n))})
        for fld,col in [('labor_min','labor_s'),('machine_min','machine_s')]:
            sums=ev.groupby('case')[col].sum().reindex(cases.index,fill_value=0)/60
            assert np.allclose(sums,cases[fld])
        summaries.append(dict(вариант=tag,поступлений=N,успех_72ч=int(cases.success72.sum()),T72_мин=cases.T72_min.mean(),чел_мин=cases.labor_min.mean(),маш_мин=cases.machine_min.mean(),max_невязка_с=max(abs(r[-1]) for r in checks),input_sha256=input_hash))
    save('25_operations.csv',ops);save('5_blocks.csv',blocks);save('summary.csv',summaries)
    save('calendar_check.csv',pd.DataFrame(checkrows,columns=['mask','case','censored','T72_min','path_min','residual_seconds']))
    save('critical_contributions.csv',pd.DataFrame(critrows,columns=['mask','segment','operation','minutes_per_receipt']))
    print(frame(summaries).to_string(index=False))
if __name__=='__main__':main()
