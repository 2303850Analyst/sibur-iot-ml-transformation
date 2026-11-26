import json
import pandas as pd
from inputs import ROOT
from preflight import write

def main():
    for f in (ROOT/'results/pilot').glob('live-*/summary.json'):
        d=f.parent
        trace=pd.DataFrame(json.loads((d/'operations.json').read_bytes()))
        trace['latency_ms']=1000*(trace.end-trace.start)
        trace['client_queue_ms']=1000*(trace.start-trace.queued)
        by=[]
        for (method,n),g in trace.groupby(['method','operation'],dropna=False):
            by.append(dict(method=method,operation=n,n=len(g),p50_ms=g.latency_ms.quantile(.50),p95_ms=g.latency_ms.quantile(.95),p99_ms=g.latency_ms.quantile(.99),
                queue_p95_ms=g.client_queue_ms.quantile(.95),errors=int((g.status>=400).sum())))
        pd.DataFrame(by).to_csv(d/'latencies.csv',index=False)
        main=[r for r in json.loads((d/'cases.json').read_bytes()) if r['cohort']]
        observed=[min(180,r['end']-r['planned']) if r['outcome']=='standard' else 180 for r in main]
        summary=json.loads(f.read_bytes())
        assert len(main)==summary['offered']
        write(d/'measurement.json',dict(evidence='LIVE_SOFTWARE_TEST',scope='technical pilot',unit='wall_seconds_per_offered_receipt',
                 denominator=len(main),mean_restricted_T180_s=sum(observed)/len(observed),accepted=len(set(trace['case'].dropna())&{r['case'] for r in main}),
                 offered=len(main),standard_180=summary['success_180s'],failed_http=int((trace.status>=400).sum())))
if __name__=='__main__':main()

