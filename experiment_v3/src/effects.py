"""Paired comparisons, no substitution of pilot for a missing primary run."""
import numpy as np
import pandas as pd
from scipy.stats import permutation_test
METRICS={'T72_min':'lower','labor_min':'lower','machine_min':'lower','success72':'higher'}
def pair(a,b,metric):
    values=pd.concat([a.rename('a'),b.rename('b')],axis=1).dropna()
    if len(values)!=20:return None
    x=values.a.to_numpy();y=values.b.to_numpy()
    rng=np.random.default_rng(20261119)
    sample=rng.integers(len(x),size=(5000,len(x)))
    av,bv=x.mean(),y.mean();delta=bv-av
    ax=x[sample].mean(axis=1);bx=y[sample].mean(axis=1)
    pct=None if av==0 else delta/av*100
    ratio=np.divide(100*(bx-ax),ax,out=np.full(len(ax),np.nan),where=ax!=0)
    ci=np.nanquantile(ratio,[.025,.975]) if np.isfinite(ratio).any() else (None,None)
    return dict(seeds=len(x),control=av,variant=bv,delta=delta,change_pct=pct,ci_low=ci[0],ci_high=ci[1],
                improvement_pct=None if pct is None else (pct if METRICS[metric]=='higher' else -pct),
                percentage_points=delta*100 if metric=='success72' else None)
def adjust(p,method):
    arr=np.asarray(p,float);idx=np.argsort(arr);v=arr[idx];m=len(v)
    corrected=np.maximum.accumulate(v*(m-np.arange(m))) if method=='holm' else np.minimum.accumulate((v*m/(1+np.arange(m)))[::-1])[::-1]
    ans=np.empty_like(v);ans[idx]=np.minimum(1,corrected)
    return ans
def signflip(diff):
    # Exact one-sided paired randomization p for the sharp exchangeability null.
    d=np.asarray(diff);observed=d.sum()
    if len(d)!=20:raise ValueError('incomplete primary comparison')
    sums=np.array([0.])
    for val in d:sums=np.r_[sums+val,sums-val]
    return float(np.mean(sums>=observed-1e-10))
def compare(df):
    effects=[];marginal=[];interaction=[]
    for (s,l),g in df.groupby(['scenario','load']):
        for metric in METRICS:
            mat=g.pivot(index='seed',columns='mask',values=metric)
            def contrast(a,b,output,**more):
                if a in mat and b in mat:
                    item=pair(mat[a],mat[b],metric)
                    if item:output.append(dict(scenario=s,load=l,metric=metric,control_mask=a,variant_mask=b,**more,**item))
            for mask in mat:
                if mask:contrast(0,mask,effects,comparison='versus_baseline')
                if mask!=127:contrast(mask,127,effects,comparison='versus_full')
                for i in range(7):
                    if mask & (1<<i):continue
                    contrast(mask,mask|(1<<i),marginal,module=i+1)
                    for j in range(i+1,7):
                        if mask & (1<<j):continue
                        ids=[mask,mask|1<<i,mask|1<<j,mask|1<<i|1<<j]
                        if all(k in mat for k in ids):
                            block=mat[ids].dropna()
                            if len(block)==20:
                                values=block.iloc[:,1]+block.iloc[:,2]-block.iloc[:,0]-block.iloc[:,3]
                                if METRICS[metric]=='higher':values=-values
                                interaction.append(dict(scenario=s,load=l,metric=metric,background=mask,i=i+1,j=j+1,interaction=values.mean()))
    return [pd.DataFrame(x) for x in (effects,marginal,interaction)]

