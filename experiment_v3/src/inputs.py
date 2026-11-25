"""External evaluator fixtures. Policies must never import this module."""
from functools import lru_cache
import hashlib,json,math,random
from pathlib import Path
from collections import defaultdict
import pandas as pd
import numpy as np
from sklearn.linear_model import Ridge

ROOT=Path(__file__).resolve().parents[1]
DAY=86400
def rng(seed,entity,op,attempt=0,source='external'):
    key=json.dumps([seed,entity,op,attempt,source],separators=(',',':')).encode()
    return random.Random(int.from_bytes(hashlib.blake2b(key,digest_size=8).digest(),'big'))
def digest(value):
    return hashlib.sha256(json.dumps(value,separators=(',',':'),sort_keys=True).encode()).hexdigest()
@lru_cache(maxsize=1)
def source():
    p=ROOT.parent/'series_py312/data'
    return pd.read_parquet(p/'normalized/source.parquet'),json.loads((p/'manifests/wepastacks.json').read_bytes())
@lru_cache(maxsize=6)
def sampled(load,seed):
    df,meta=source()
    nominal={'L1':300,'L2':450,'L3':900}[load]
    # Fixed per source row; no mask, no change in ordering when filtering.
    keys=pd.util.hash_pandas_object(df.source_row.astype('uint64')+np.uint64(seed)*np.uint64(10000019),index=False).to_numpy(dtype='uint64')
    fraction=keys.astype(float)/2**64
    rate=df.direction.map(meta['train_pallets_per_day']).to_numpy()
    return df.loc[fraction<nominal/rate]
def fixtures(scenario,load,seed):
    df=sampled(load,seed)
    periods=source()[1]['partitions']['test']
    blocks=[periods[i:i+7] for i in range(0,len(periods)-6,7)]
    days=sum((rng(seed,i,'block').choice(blocks) for i in range(5)),[])[:34]
    members=defaultdict(list)
    departures=[]
    pallets={}
    for day,src in enumerate(days):
        for row in df[df.day.eq(src)].itertuples():
            t=day*DAY+int(row.tod)
            if row.direction=='retrieval':
                departures.append((t,int(row.sku)))
                continue
            p=f'{day}-{row.source_row}'
            if scenario=='S6' and rng(seed,p,'burst').random()<.60:
                t=day*DAY+rng(seed,day,'peak').randint(28800,64800)+rng(seed,p,'peak_offset').randint(0,7200)
            sku=int(row.sku)
            key=(day,int(row.source_sku),int(row.dock),int(t%DAY)//3600)
            members[key].append((t,p))
            lot=f'{day}-{row.source_sku}-{row.batch}'
            q='Q-'+lot
            held=rng(seed,q,'hold').random()<(.20 if scenario=='S5' else .05)
            pallets[p]=dict(pallet=p,ready=t,arrival=t+30,sku=sku,lot=lot,q=q,held=held,
                             failed=rng(seed,q,'nonconformity').random()<(.02 if scenario=='S5' else .005),
                             placement_mismatch=scenario=='S2' and rng(seed,p,'verification_scan').random()<.02,
                             label_bad=rng(seed,p,'label').random()<(.05 if scenario=='S2' else .005))
    receipts=[]
    for key,items in sorted(members.items()):
        items.sort()
        for start in range(0,len(items),8):
            chunk=items[start:start+8]
            cid='R-'+'-'.join(map(str,key))+f'-{start//8}'
            receipts.append(dict(case=cid,ready=chunk[0][0],last_ready=chunk[-1][0],members=[p for _,p in chunk],
                                 defect=scenario=='S2' and rng(seed,cid,'document').random()<.02))
    return dict(days=days,receipts=sorted(receipts,key=lambda c:(c['ready'],c['case'])),pallets=pallets,
                departures=sorted(departures),scenario=scenario,load=load,seed=seed)

def features(hist,idx,mean):
    lag=lambda n: hist[idx-n] if idx>=n else mean
    angle=2*math.pi*(idx%48)/48
    return [lag(1),lag(2),lag(3),lag(4),lag(48),math.sin(angle),math.cos(angle)]
def train():
    output={}
    scores=[]
    for load in ('L1','L2','L3'):
        df=sampled(load,20261119)
        for group in (0,1):
            subset=df[df.direction.eq('delivery') & (df.sku//10).eq(group)]
            days=source()[1]['partitions']
            train_days=days['train']
            counts=np.bincount(subset.seconds.to_numpy()//1800,minlength=(days['test'][-1]+1)*48)
            a,b=48*train_days[0],48*(train_days[-1]+1)
            mean=float(counts[a:b].mean())
            x=[features(counts,i,mean) for i in range(a+48,b)]
            model=Ridge(alpha=10).fit(x,counts[a+48:b])
            output[f'{load}/{group}']=[model.coef_.tolist(),float(model.intercept_),mean]
            for part in ('validation','test'):
                for i in range(48*days[part][0],48*(days[part][-1]+1)):
                    row=features(counts,i,mean)
                    scores.append([load,group,part,i,int(counts[i]),np.mean(row[:4]),max(0,float(np.dot(row,model.coef_)+model.intercept_))])
    dest=ROOT/'data'
    dest.mkdir(exist_ok=True)
    (dest/'ridge.json').write_text(json.dumps(output))
    pd.DataFrame(scores,columns=['load','group','partition','bin','actual','F0','F1']).to_parquet(dest/'forecast_holdout.parquet')
if __name__=='__main__':
    train()

