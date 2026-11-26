import argparse
from pathlib import Path
import json
import gzip

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
END=32*86400


def intervals(folder,pallet_id):
    frame=pd.read_parquet(folder/'pallets.parquet').set_index('id')
    pallet=frame.loc[pallet_id]
    capacity=pd.read_parquet(folder/'capacity.parquet').drop_duplicates('time',keep='last').set_index('time')
    ceiling=lambda value: END if pd.isna(value) else value
    entered,ready,start,finish,placed=[ceiling(pallet[field]) for field in
        ('buffer_at','registered_at','started_at','physical_end_at','placed_at')]
    release=ceiling(pallet.released_at)
    bounds=sorted({pallet.arrival,entered,ready,start,finish,placed,min(placed,max(pallet.arrival,release)),
                   *capacity.index[(capacity.index>ready)&(capacity.index<start)]})
    result=[]
    for lo,hi in zip(bounds,bounds[1:]):
        if lo>=placed or hi<=lo: continue
        if lo<entered: active=['buffer','registration']
        elif lo<ready: active=['registration']
        elif lo<start:
            state=capacity.iloc[capacity.index.searchsorted(lo,side='right')-1]
            group=int(pallet.sku)//10
            required=int(pallet.source_row)%5==0
            free=state['free4'] if lo<release else (state[f'free{2*group+1}'] or
                                                       (not required and state[f'free{2*group}']))
            active=[]
            if not free:
                if lo<release: active.append('quality')
                active.append('cell')
            if state.busy==2: active.append('forklift')
            if not active: active.append('dispatch')
        elif lo<finish: active=['transport']
        else: active=['confirmation']
        signature='+'.join(active)
        if result and result[-1]['active']==signature: result[-1]['end_s']=hi
        else: result.append(dict(start_s=lo,end_s=hi,active=signature,primary=active[0]))
    for row in result: row['duration_s']=row['end_s']-row['start_s']
    by_reason={name:sum(row['duration_s'] for row in result if row['primary']==name)
               for name in ('buffer','registration','cell','quality','forklift','dispatch','transport','confirmation')}
    assert all(np.isclose(value,pallet['primary_'+key],atol=1e-5) for key,value in by_reason.items())
    return result


def stocks(folder):
    moves=pd.read_parquet(folder/'actions.parquet')
    events=[(r.time+r.empty_m,-1,0) for r in moves.itertuples() if r.origin<0 and r.time+r.empty_m<END]
    with gzip.open(folder/'events.jsonl.gz','rt') as src:
        for line in src:
            row=json.loads(line)
            if row['event']=='physical_buffer': events.append((row['time'],1,0))
            if row['event']=='register': events.append((row['time'],0,1))
            if row['event']=='confirm_place' and row['primary']: events.append((row['time'],0,-1))
    values=pd.DataFrame(events,columns=['time','physical','registered_unconfirmed'])
    values.time=values.time.round(6)
    levels=values.groupby('time').sum().cumsum()
    assert levels.min().min()>=0
    levels.to_parquet(folder/'buffer_states.parquet')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--run')
    parser.add_argument('--pallet',type=int)
    args=parser.parse_args()
    if args.run:
        print(pd.DataFrame(intervals(ROOT/'results/traces'/args.run,args.pallet)).to_csv(index=False))
    else:
        for folder in sorted((ROOT/'results/traces').glob('*-2000-*')):
            stocks(folder)
            pallets=pd.read_parquet(folder/'pallets.parquet')
            chosen=pallets.sort_values('primary_cell',ascending=False).iloc[0]
            info=intervals(folder,int(chosen.id))
            pd.DataFrame(info).assign(pallet=int(chosen.id)).to_csv(folder/'reason_example.csv',index=False)
