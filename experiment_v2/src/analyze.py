import json
from itertools import product
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
RESULT=ROOT/'results'
INDEX=np.random.default_rng(20261119).integers(0,20,size=(5000,20))
PRIMARY=['A2','B2','C2']
PAIRS=[('A2','B2'),('B2','C2'),('BASE','A2'),('FIFO','A2'),('NEAR','A2'),('IGN','B2'),('C2','IDEAL')]
METRICS=['wait_s','cycle_s','empty_m','fork_h','operator_h','unfinished_share','timely_4h','placed','final',
         'full_buffer','mae','p95_wait_s','repeat_moves','corrections','stale_share','unfinished_stop','unfinished_end']


def effects(frame):
    rows=[]
    for (scenario,level),cell in frame.groupby(['scenario','level']):
        variants={key:data.set_index('seed').sort_index() for key,data in cell.groupby('variant')}
        for old,new in PAIRS:
            if old not in variants or new not in variants: continue
            first,last=variants[old],variants[new]
            assert list(first.index)==list(last.index)==list(range(2000,2020))
            assert first.input_hash.equals(last.input_hash)
            for metric in METRICS:
                delta=(first[metric]-last[metric]).to_numpy()
                low,high=np.quantile(delta[INDEX].mean(axis=1),[.025,.975])
                mean=delta.mean()
                baseline=first[metric].mean()
                rows.append(dict(scenario=scenario,level=level,baseline=old,treatment=new,metric=metric,
                       baseline_mean=baseline,treatment_mean=last[metric].mean(),difference=mean,low95=low,high95=high,
                       percent=100*mean/baseline if baseline else None,n=20))
    return pd.DataFrame(rows)


def economics(frame):
    rows=[]
    pv=sum(1/1.2**year for year in range(1,6))
    for (scenario,level),cell in frame.groupby(['scenario','level']):
        for old,new in [('BASE','A2'),('A2','B2'),('B2','C2')]:
            ref=cell[cell.variant==old].set_index('seed').sort_index()
            newdata=cell[cell.variant==new].set_index('seed').sort_index()
            if ref.empty: continue
            matched=bool(((ref.placed_stop==newdata.placed_stop)&(ref.unfinished_stop==0)&(newdata.unfinished_stop==0)
                          &(ref.final==newdata.final)).all())
            for wage_factor in (.75,1.,1.25):
                before=(650*ref.operator_h+1200*ref.fork_h)*wage_factor
                after=(650*newdata.operator_h+1200*newdata.fork_h)*wage_factor
                unit_before=before.sum()/ref.placed_stop.sum()
                unit_after=after.sum()/newdata.placed_stop.sum()
                common=min(ref.placed_stop.mean(),newdata.placed_stop.mean())
                resource=(before-after).mean()*365/30
                equivalent=(unit_before-unit_after)*common*365/30
                for k,capex,opex in product((0,.25,.5),(0,500000,1500000,4500000),(0,100000,400000,1000000)):
                    threshold=(resource if matched else equivalent)*k*(pv-.4/1.2)-opex*pv
                    rows.append(dict(scenario=scenario,level=level,baseline=old,treatment=new,wage_factor=wage_factor,
                       operator_hours_delta=(ref.operator_h-newdata.operator_h).mean(),fork_hours_delta=(ref.fork_h-newdata.fork_h).mean(),
                       repeat_delta=(ref.repeat_moves-newdata.repeat_moves).mean(),correction_delta=(ref.corrections-newdata.corrections).mean(),
                       backlog_delta=(ref.unfinished_stop-newdata.unfinished_stop).mean(),common_pallets_30d=common,
                       before_rub_per_placed=unit_before,after_rub_per_placed=unit_after,raw_resources_rub_year=resource,
                       resource_at_equal_unit_cost_assumption=equivalent,matched=matched,model='direct' if matched else 'conditional_unit_cost',
                       k=k,capex=capex,opex=opex,conditional_cash_year=(resource if matched else equivalent)*k-opex,
                       conditional_capex_threshold=threshold,conditional_npv=threshold-capex,
                       matched_npv=threshold-capex if matched else None))
    return pd.DataFrame(rows)


def main():
    rows=[json.loads(path.read_text()) for path in sorted((RESULT/'jobs').glob('*.json'))]
    data=pd.DataFrame(rows)
    assert len(data)==1480 and data.run_id.nunique()==1480 and data.audit_pass.all()
    assert data.fingerprint.nunique()==1 and data.fingerprint.iloc[0]==json.loads((RESULT/'freeze.json').read_text())['fingerprint']
    core=data[data.variant.isin(PRIMARY)].copy()
    other=data[~data.variant.isin(PRIMARY)].copy()
    groups=core.groupby(['scenario','level','variant'])
    assert len(groups)==54 and groups.size().eq(20).all()
    assert data.groupby(['scenario','level','seed']).input_hash.nunique().eq(1).all()
    assert np.allclose(core[[col for col in core if col.startswith('primary_')]].sum(axis=1),core.cycle_s)
    core.to_csv(RESULT/'runs_v2.csv',index=False)
    other.to_csv(RESULT/'ablations.csv',index=False)
    pairs=effects(data)
    pairs.to_csv(RESULT/'paired_effects.csv',index=False)
    summary=groups.mean(numeric_only=True).reset_index()
    summary.to_csv(RESULT/'summary_54.csv',index=False)
    names=[key for key in data if key.startswith(('primary_','active_')) or key in ('multiple_s','full_buffer','no_cell_pallets')]
    data[['run_id','scenario','level','seed','variant','input_hash',*names]].to_csv(RESULT/'bottleneck_diagnostic.csv',index=False)
    data[['run_id','scenario','level','seed','variant','mae','changed_forecast','changed_iot','assignments','stale_share','fallback']].to_csv(
           RESULT/'forecast_comparison.csv',index=False)
    finance=economics(data)
    finance.to_csv(RESULT/'economics.csv',index=False)
    prior=[]
    discount=sum(1/1.2**year for year in range(1,6))
    for name,old,new,capex,opex in [('IoT','A2','B2',4200000,750000),
              ('ML additional, purchased platform','B2','C2',4500000,1000000),
              ('ML additional, shared platform','B2','C2',1200000,400000)]:
        pick=finance[(finance.baseline==old)&(finance.treatment==new)&(finance.capex==0)&(finance.opex==0)].copy()
        pick['budget_case']=name
        pick['capex']=capex
        pick['opex']=opex
        pick['conditional_cash_year']-=opex
        pick['conditional_capex_threshold']-=opex*discount
        pick['conditional_npv']=pick.conditional_capex_threshold-capex
        pick['matched_npv']=pick.conditional_npv.where(pick.matched)
        prior.append(pick)
    pd.concat(prior).to_csv(RESULT/'historical_budget_sensitivity.csv',index=False)
    safety=dict(scope='V2 SimPy all primary and ablation streams',main=len(core),ablation=len(other),
                event_rows=int(data.audited_events.sum()),passed=int(data.audit_pass.sum()),violations={name:0 for name in
                  ('duplicate_receipts','illegal_moves','illegal_release','negative_stock')},
                method='Event subscriber validates source, reserve, transit, confirm, quality schedule and opening stock, then closes balance.',
                fingerprint=data.fingerprint.iloc[0])
    (RESULT/'safety_audit.json').write_text(json.dumps(safety,indent=2))
    figure_dir=ROOT/'figures'
    figure_dir.mkdir(exist_ok=True)
    for metric,label,file in [('wait_s','Накопленное ожидание, часы','waiting'),('unfinished_share','Доля незавершённых','backlog')]:
        fig,axes=plt.subplots(2,3,figsize=(11,6),layout='constrained',sharey=True)
        for axis,(scenario,block) in zip(axes.flat,summary.groupby('scenario')):
            for variant,group in block.groupby('variant'):
                axis.plot(group.level,group[metric]/(3600 if metric=='wait_s' else 1),marker='o',label=variant)
            axis.set_title(scenario)
        axes.flat[0].legend()
        fig.supylabel(label)
        fig.savefig(figure_dir/(file+'.png'),dpi=150)
        plt.close(fig)
    subset=pairs[(pairs.metric=='cycle_s') & (pairs.baseline=='B2') & (pairs.treatment=='C2')]
    fig,axis=plt.subplots(figsize=(7,7),layout='constrained')
    axis.errorbar(subset.difference,np.arange(len(subset)),xerr=np.maximum(0,np.vstack([subset.difference-subset.low95,subset.high95-subset.difference])),fmt='o')
    axis.set_yticks(np.arange(len(subset)),subset.scenario+'/'+subset.level)
    axis.axvline(0,color='grey')
    axis.set_xlabel('Сокращение времени при переходе к ML, с')
    fig.savefig(figure_dir/'forecast_effect.png',dpi=150)
    plt.close(fig)

if __name__=='__main__':
    main()
