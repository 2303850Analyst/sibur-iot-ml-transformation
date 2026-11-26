"""Independent stream consumer. Inventory is reconstructed without importing Process."""
import gzip,json
from collections import Counter,defaultdict
import numpy as np
import pandas as pd
from inputs import DAY

HORIZON=3*DAY
def replay(directory,x):
    cases={r['case']:r for r in x['receipts']}
    primary={k for k,v in cases.items() if DAY<=v['ready']<31*DAY}
    own={p:c for c,v in cases.items() for p in v['members']}
    issues=Counter();examples=[]
    warehouse={};reserved={};carried={};present=set();placed={};received=set();qa={};delivered={};ack=set();closed={};verified=set()
    activities=defaultdict(list)
    pallet_state=defaultdict(dict)
    done_times={};flow=Counter()
    def check(test,rule,row):
        if not test:
            issues[rule]+=1
            if len(examples)<24:examples.append([rule,row['id']])
    seen=0;last=0;max_buffer=0
    with gzip.open(directory/'events.jsonl.gz','rt',encoding='utf8') as stream:
        for line in stream:
            row=json.loads(line);seen+=1
            check(row['id']==last+1,'event_sequence',row);last=row['id']
            kind=row.get('kind','operation');c=row['case'];p=row['pallet']
            if p:check(own.get(p)==c,'manifest_membership',row)
            if kind=='initial_stock':
                warehouse={a:'initial' for cells in row['stock'].values() for a in cells}
            elif kind=='enter':
                check(p not in present,'double_buffer',row);present.add(p)
                max_buffer=max(max_buffer,len(present))
                check(len(present)<=row['capacity'],'buffer_capacity',row)
            elif kind=='quality_decision':
                check(row['authority']=='quality','quality_authority',row);qa[row['quality']]=row['decision']
            elif kind=='quality_delivered':
                check(qa.get(row['quality'])==row['decision'],'unapproved_quality',row);delivered[row['quality']]=row['decision']
            elif kind=='reserve':
                a=row['address'];number=int(a.split('-')[-1])
                check(p in received,'movement_before_receipt',row)
                check(a not in warehouse and a not in reserved,'foreign_or_occupied_reserve',row)
                if row['stage']==18:
                    check(number//2600==row['sku']//10 and delivered.get(row['quality'])=='released','storage_eligibility',row)
                else:check(number>=5200,'quarantine_eligibility',row)
                reserved[a]=p
            elif kind=='pickup':
                a=row['source'];truck=row['vehicle']
                check(truck not in carried,'vehicle_busy',row)
                check(reserved.get(row['destination'])==p,'foreign_destination',row)
                if a=='BUFFER':
                    check(p in present,'empty_pickup',row);present.discard(p)
                else:check(warehouse.pop(a,None)==p,'foreign_pickup',row)
                carried[truck]=p
            elif kind=='deposit':
                a=row['address']
                check(reserved.pop(a,None)==p and a not in warehouse,'unreserved_deposit',row)
                truck=next((i for i,v in carried.items() if v==p),None)
                check(truck is not None,'missing_transit',row)
                carried.pop(truck,None);warehouse[a]=p
                if row['stage']==18:placed[p]=a
            elif kind=='departure':
                check(warehouse.pop(row['address'],None) is not None,'negative_stock',row)
            elif kind=='exchange_ack':ack.add(c)
            elif kind=='operation':
                n=row['op'];t=row['finished']
                check(row['entered']<=row['started']<=t,'clock_order',row)
                if n==16:
                    check(p not in received,'double_receipt',row);received.add(p)
                if n==20:check(placed.get(p)==row['address'],'wrong_confirmation',row);verified.add(p)
                if n==25:
                    check(c in ack and set(cases[c]['members']).issubset(verified),'early_close',row)
                    closed[c]=t
                if p:pallet_state[p][f'P{n:02d}']=t
                if c in primary and row['entered']<cases[c]['ready']+HORIZON:
                    activities[c].append(row)
    case_rows=[];op_rows=[];block_rows=[]
    for c in sorted(primary):
        cfg=cases[c];a=cfg['ready'];z=a+HORIZON;success=closed.get(c)
        last=min(z,success) if success is not None else z
        vals=defaultdict(list)
        for r in activities[c]:vals[r['op']].append(r)
        total_labor=total_machine=0.
        for n in range(1,26):
            group=vals[n];block=1 if n<=5 else 2 if n<=11 else 3 if n<=16 else 4 if n<=21 else 5
            def active(r,field):
                return min(r[field],max(0,last-r['started']))
            labor=sum(active(r,'labor') for r in group)
            machine=sum(active(r,'machine') for r in group)
            total_labor+=labor;total_machine+=machine
            op_rows.append(dict(case=c,operation=n,block=block,visits=len(group),manual=sum(r['mode']=='manual' for r in group),
                automatic=sum(r['mode']=='automatic' for r in group),not_applicable=sum(r['mode']=='branch' for r in group),
                labor_s=labor,machine_s=machine,waiting_s=sum(max(0,min(last,r['started'])-r['entered']) for r in group),
                first=min((r['entered'] for r in group),default=None),last=max((min(last,r['finished']) for r in group),default=None)))
        elapsed=success-a if success is not None else float('inf')
        case_rows.append(dict(case=c,t0=a,t25=success,last_ready=cfg['last_ready'],members=len(cfg['members']),
            T72_min=(last-a)/60,completed_cycle_min=elapsed/60 if np.isfinite(elapsed) else None,
            labor_min=total_labor/60,machine_min=total_machine/60,
            open_at_cohort_end=success is None or success>=31*DAY,
            **{f'success{h}':elapsed<=h*3600 for h in (2,4,8,24,72)}))
    ops=pd.DataFrame(op_rows);cases_df=pd.DataFrame(case_rows)
    blocks=ops.groupby(['case','block'],as_index=False).agg(first=('first','min'),last=('last','max'),labor_s=('labor_s','sum'),machine_s=('machine_s','sum'),visits=('visits','sum'))
    blocks['lifetime_s']=blocks['last']-blocks['first']
    units=[dict(pallet=p,case=c,lot=x['pallets'][p]['lot'],quality_case=x['pallets'][p]['q'],**pallet_state[p]) for p,c in own.items()]
    for name,df in [('case_metrics',cases_df),('pallet_metrics',pd.DataFrame(units)),('operation_metrics',ops),('block_metrics',blocks)]:
        df.to_parquet(directory/(name+'.parquet'),index=False)
    raw=pd.read_parquet(directory/'cases.parquet').set_index('case').sort_index()
    rebuilt=cases_df.set_index('case').sort_index()
    if not raw.index.equals(rebuilt.index) or not np.allclose(raw.T72_min,rebuilt.T72_min):issues['independent_T72_disagreement']+=1
    result=dict(events_checked=seen,primary_cases=len(primary),max_buffer=max_buffer,findings=dict(issues),examples=examples,
                scope='quantity one per pallet, stock by address, receipt, dispatch, authority, closure, primary T72')
    (directory/'audit.json').write_text(json.dumps(result,indent=2))
    return result

