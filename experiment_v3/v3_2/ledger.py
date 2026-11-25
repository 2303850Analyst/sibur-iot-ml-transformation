"""Independent accounting from simulator timestamps, with a causal predecessor per join."""
from common import *
from inputs import DAY
from crew import SHIFT
import math,collections
import pyarrow as pa
import pyarrow.parquet as pq
from collections import Counter,defaultdict
def ms(t):return None if t is None else round(1000*t)
def interval(a,b,left,right):
    return 0 if a is None or b is None else max(0,min(b,right)-max(a,left))
def block(i):
    return 0 if i<=5 else 1 if i<=11 else 2 if i<=16 else 3 if i<=21 else 4
def write_table(path,items):
    pq.write_table(pa.Table.from_pylist(items),path,compression='zstd',compression_level=4)
def causal(model):
    byc=defaultdict(list);units=model.x['pallets'];cases={c['case']:c for c in model.x['receipts']}
    cp={};pp={};qq={};first={};prev={};wait={}
    for v in model.visits:
        c,n,p,i=v['case'],v['op'],v['pallet'],v['id'];q=v.get('quality') or (units[p]['q'] if p else '')
        preds=[]; gap='handoff'
        if n==1:first[c]=v
        elif n==2:gap='arrival_and_buffer'
        elif n==6:preds=[first[c]];gap='document_delivery'
        elif q and n in (7,8,9,11):preds=[qq[q]];gap='quality_external' if n==8 else 'quality_handoff'
        elif n==16:preds=[cp[c],pp[p]];gap='scan_delivery'
        elif n==17:preds=[pp[p],qq[q]];gap='quality_gate'
        elif n==22:preds=[pp[k] for k in cases[c]['members']];gap='manifest_join'
        elif p:preds=[pp[p]];gap='physical_confirmation' if n==19 else 'pallet_handoff'
        else:preds=[cp[c]];gap='exchange_ack' if n==24 else 'document_delivery' if n==3 else 'case_handoff'
        if preds:
            def eligible(a):
                end=a['finish']
                return end+(.3 if n==16 and a['op']==2 else 0)
            parent=max(preds,key=eligible)
            assert parent['finish']<=v['ready']+.00001,(n,v['ready'],parent)
            prev[i]=parent['id']
        else:prev[i]=0
        wait[i]=gap;byc[c].append(v)
        if n in (6,7,8,9,11):qq[q]=v
        elif p:pp[p]=v
        else:cp[c]=v
    return byc,prev,wait,qq
def invariants(e):
    bins=set();cargo={};cells={};reserve={};physical={};quality={};placed={};recv=set();ack=set();checks=Counter();fail=[]
    entries=[]
    for v in e.visits:
        if v['finish'] is not None and v['finish']<=e.end and v['op'] in (16,25):
            entries.append((v['finish'],0,'receive' if v['op']==16 else 'close',v['case'],v['pallet'],{}))
    entries.extend((t,1,k,c,p,d) for k,t,c,p,d in e.events)
    manifest={c['case']:c for c in e.x['receipts']}
    def check(k,ok,c,p):
        checks[k]+=1
        if not ok:fail.append((k,c,p))
    for t,_,k,c,p,d in sorted(entries,key=lambda a:a[:2]):
        if k=='initial_stock':cells={a:'initial' for l in d['stock'].values() for a in l}
        elif k=='receive':check('unique_receipt',p not in recv,c,p);recv.add(p)
        elif k=='quality_decision':quality[d['quality']]=d['decision'] if d['authority']=='quality' else 'unauthorized'
        elif k=='enter':bins.add(p);check('capacity',len(bins)<=d['capacity'],c,p)
        elif k=='reserve':
            a=d['address'];check('reservation',a not in reserve and a not in cells and p in recv,c,p);reserve[a]=p
            group=int(a.split('-')[-1])//2600
            check('quality_address',group==2 if d['stage']==10 else group==d['sku']//10 and quality.get(d['quality'])=='released',c,p)
        elif k=='pickup':
            a=d['source'];truck=d['vehicle'];check('source',p in bins if a=='BUFFER' else cells.get(a)==p,c,p)
            if a=='BUFFER':bins.discard(p)
            else:cells.pop(a,None)
            check('single_vehicle',truck not in cargo,c,p);cargo[truck]=p
        elif k=='deposit':
            a=d['address'];tr=next((key for key,val in cargo.items() if val==p),None)
            check('balance',p==reserve.pop(a,None) and tr is not None and a not in cells,c,p);cargo.pop(tr,None);cells[a]=p
            if d['stage']==18:placed[p]=a
        elif k=='release_at_address':
            a=d['address']
            check('release_in_place',a in e.dual_slots and cells.get(a)==p and p in recv
                  and p not in placed and p not in cargo.values() and a not in reserve
                  and d['quality']==e.x['pallets'][p]['q'] and quality.get(d['quality'])=='released',c,p)
            placed[p]=a
        elif k=='reject_pickup':
            a=d['address']; tr=d['vehicle']
            check('segregated_reject_pickup',cells.get(a)==p and a not in reserve and tr not in cargo
                  and p not in placed and e.addr[a].quarantine and d['quality']==e.x['pallets'][p]['q']
                  and quality.get(d['quality'])=='failed',c,p)
            cells.pop(a,None); cargo[tr]=p
        elif k=='reject_handover':
            check('segregated_reject_handover',cargo.pop(d['vehicle'],None)==p,c,p)
        elif k=='departure':check('positive_balance',cells.pop(d['address'],None) is not None,c,p)
        elif k=='exchange_ack':ack.add(c)
        elif k=='close':check('complete_document',c in ack and all(p in placed for p in manifest[c]['members']),c,p)
    return dict(checks=dict(checks),violations=fail)
def output(e,folder,rid):
    byc,parents,gaps,qq=causal(e);vis=e.visits
    failures=defaultdict(list)
    for k,t,c,p,d in e.events:
        if k=='open_nonconformity':failures[c].append((t,p))
    audit=invariants(e);cases=[];agg=Counter();diag=[]
    sample=set()
    for c in e.x['receipts']:
        if int(hashlib.sha256(c['case'].encode()).hexdigest()[:8],16)%199==0:sample.add(c['case'])
    for c in e.x['receipts']:
        cid=c['case'];t0=ms(c['ready']);closed=ms(e.closed.get(cid))
        limit=min(t0+259200000,ms(e.end),closed if closed is not None else ms(e.end))
        z=[0]*25;visits=z.copy();svc=z.copy();queue=z.copy();workers=z.copy();machines=z.copy();holds=z.copy();cp=z.copy();skips=z.copy();external=z.copy();system=z.copy()
        first=[None]*25;last=[None]*25;reasons=[Counter() for i in z];na=[set() for i in z];roles=Counter()
        path=Counter();detail=[];ids=[]
        for v in byc[cid]:
            ready=ms(v['ready'])
            if ready>limit:continue
            n=v['op']-1;grant=ms(v['grant']);finish=ms(v['finish']);worker=ms(v['manual_start']);wend=ms(v['manual_end'])
            end=min(limit,finish if finish is not None else limit)
            qtime=max(0,min(limit,grant if grant is not None else limit)-ready)
            reason='driver_vehicle_address' if v['op'] in (10,18) else 'worker_calendar_or_busy'
            worked=interval(worker,wend,t0,limit);moved=interval(ms(v['machine_start']),ms(v['machine_end']),t0,limit)
            reserved=interval(ms(v['reserve_start']),ms(v['reserve_end']),t0,limit)
            wait=interval(ready,min(grant,limit) if grant is not None else limit,t0,limit)
            if v['branch'].startswith('NA:'):skips[n]+=1;na[n].add(v['branch'][3:])
            else:visits[n]+=1
            first[n]=ready if first[n] is None else min(first[n],ready)
            if finish is not None and finish<=limit:last[n]=finish if last[n] is None else max(last[n],finish)
            busy=interval(grant,end,t0,limit)
            svc[n]+=busy;queue[n]+=wait;workers[n]+=worked;machines[n]+=moved;holds[n]+=reserved
            if wait:
                resource_request=ms(v['request'])
                if n==1:
                    buf=interval(ready,resource_request,t0,limit);reasons[n]['physical_buffer_admission']+=buf
                    reasons[n][reason]+=wait-buf
                elif n in (9,17) and v['reserve_start'] is not None:
                    before=interval(ready,ms(v['reserve_start']),t0,limit);reasons[n]['wait_for_eligible_trip']+=before
                    reasons[n]['driver_calendar']+=wait-before
                else:reasons[n][reason]+=wait
            if wend is not None and finish is not None:
                lab_end=wend+ms(v.get('external_lab',0))
                external[n]+=interval(wend,lab_end,t0,limit)
                system[n]+=interval(lab_end,finish,t0,limit)
            roles[v['role']]+=worked
            row=(v['id'],v['op'],v['pallet'],v.get('quality',''),v['attempt'],ready,grant,finish,v['worker'],v['truck'],ms(v['base']),v['fatigue'],ms(v['switch']),worked,moved,reserved,wait,reason,v['branch'],parents[v['id']])
            detail.append(row);ids.append(v['id'])
        cohort=DAY<=c['ready']<31*DAY
        unitset=c['members']
        closed_ok=closed is not None and closed<=limit
        # Backtrack the latest prerequisite at every join. For an open case follow
        # the observed blocking branch to its observation boundary.
        end_id=None;tail='pending_observation'
        if closed is not None:
            end_id=next(v['id'] for v in reversed(byc[cid]) if v['op']==25)
        elif failures[cid]:
            stamp,p=min(failures[cid]);q=e.x['pallets'][p]['q']
            candidates=[v for v in byc[cid] if v['pallet']==p and v['op']==10]+[qq[q]]
            end_id=max(candidates,key=lambda v:v['finish'] or 0)['id'];tail='open_nonconformity'
        elif ids:
            pending=[vis[i-1] for i in ids if vis[i-1]['finish'] is None or ms(vis[i-1]['finish'])>=limit]
            end_id=(min(pending,key=lambda v:v['id'])['id'] if pending else ids[-1])
            tail='pending_join_or_external'
        def add(a,b,label,op=None):
            val=interval(a,b,t0,limit)
            if val:
                if op is None:path[label]+=val
                else:cp[op-1]+=val
        if end_id:
            lastv=vis[end_id-1]
            add(ms(lastv['finish']) if lastv['finish'] is not None else limit,limit,tail)
            i=end_id
            while i:
                v=vis[i-1];j=parents[i]
                a=ms(vis[j-1]['finish']) if j else t0;b=ms(v['ready'])
                add(a,b,gaps[i])
                add(b,ms(v['finish']) if v['finish'] is not None else limit,'operation',v['op'])
                i=j
        else:add(t0,limit,'not_started')
        assert sum(cp)+sum(path.values())==limit-t0,(cid,cp,path,limit-t0)
        def blocks(a):return [sum(x for i,x in enumerate(a,1) if block(i)==b) for b in range(5)]
        states=['APPLIED' if count else 'NOT_APPLICABLE' if skip else 'NOT_REACHED' for count,skip in zip(visits,skips)]
        record=dict(run_id=rid,case_id=cid,cohort=cohort,t_first_ready_ms=t0,t_last_ready_ms=ms(c['last_ready']),t_last_pallet_arrival_ms=ms(max(e.x['pallets'][p]['arrival'] for p in unitset)),
            t_standard_close_ms_nullable=closed,t_alternative_close_ms_nullable=None,t_observed_end_ms=limit,T72_ms=limit-t0,standard_success_72h=closed_ok,
            outcome='standard' if closed_ok else tail,num_pallets=len(unitset),quality_case_refs=sorted({e.x['pallets'][p]['q'] for p in unitset}),
            worker_busy_ms_by_role=dict(roles),forklift_active_ms=sum(machines),forklift_reserved_ms=sum(holds),block_active_ms_vector_B1_B5=blocks(svc),
            block_queue_ms_vector_B1_B5=blocks(queue),operation_visit_count_vector_P01_P25=visits,operation_service_ms_vector_P01_P25=svc,
            operation_wait_ms_vector_P01_P25=queue,operation_worker_ms_vector_P01_P25=workers,operation_machine_ms_vector_P01_P25=machines,
            operation_branch_state_vector_P01_P25=states,critical_path_ms=limit-t0,sum_critical_path_components_ms=sum(cp)+sum(path.values()),
            exception_count=sum(visits[i-1] for i in [11,15,21]),rework_count=sum(v['attempt']>1 for v in byc[cid] if ms(v['ready'])<=limit),
            operation_first_ready_ms_vector_P01_P25=first,operation_last_completed_ms_vector_P01_P25=last,operation_worker_busy_ms_vector_P01_P25=workers,
            operation_equipment_active_ms_vector_P01_P25=machines,operation_equipment_reserved_ms_vector_P01_P25=holds,
            operation_wait_by_reason_map_P01_P25=[dict(k) for k in reasons],operation_nonapplicable_reason_vector_P01_P25=[sorted(s) for s in na],
            block_critical_path_contribution_ms_vector_B1_B5=blocks(cp),operation_critical_ms=cp,operation_external_lab_ms=external,operation_system_elapsed_ms=system,external_critical_ms=dict(path),skipped=skips,
            trace_sha256=hashlib.sha256(js(detail).encode()).hexdigest())
        record['ledger_sha256']=hashlib.sha256(js({k:v for k,v in record.items() if k!='run_id'}).encode()).hexdigest();cases.append(record)
        if cid in sample or (not closed_ok and cohort):
            for r in detail:diag.append((cid,*r))
    # Each allocated millisecond is checked against the same case observation window.
    limits={r['case_id']:(r['t_first_ready_ms'],r['t_observed_end_ms']) for r in cases}
    resources=defaultdict(Counter);role_check=Counter()
    for wid,role,a,b,ready,sw,fat,c,n,v in e.crew.allocations:
        if a>=e.end:continue
        shift=int(a//SHIFT);base=resources[wid,role,shift];duration=ms(min(b,e.end))-ms(a)
        base['busy_ms']+=duration;base['queue_requests']+=1;base['queue_wait_ms']+=ms(a)-ms(ready)
        base['fatigue_trigger_count']+=int(fat>1);base['context_switch_count']+=int(sw>0)
        role_check[role]+=interval(ms(a),ms(b),*limits[c])
    for role in e.crew.roles:
        assert role_check[role]==sum(r['worker_busy_ms_by_role'].get(role,0) for r in cases),role
    rr=[]
    for shift in range(int(e.end//SHIFT)):
        for role,slots in e.crew.slots.items():
            for slot in range(len(slots)):
                wid=f'{role}/{shift%3}/{slot}';cnt=resources[wid,role,shift]
                avail=SHIFT*1000-3000000
                assert cnt['busy_ms']<=avail
                rr.append(dict(run_id=rid,resource_id=wid,role=role,time_bucket_id=shift,shift_available_ms=avail,busy_ms=cnt['busy_ms'],
                    standby_ms=avail-cnt['busy_ms'],break_ms=3000000,overtime_ms=0,queue_requests=cnt['queue_requests'],queue_wait_ms=cnt['queue_wait_ms'],
                    break_count=3,fatigue_trigger_count=cnt['fatigue_trigger_count'],context_switch_count=cnt['context_switch_count']))
    # Independent equipment occupancy per truck and shift, including retained P19.
    truck_bins=defaultdict(Counter)
    intervals=defaultdict(list)
    for v in vis:
        if v['truck'] is None:continue
        a=v['reserve_start'];b=min(v['reserve_end'],e.end)
        if a>=b:continue
        intervals[v['truck']].append((a,b))
        for shift in range(int(a//SHIFT),int((b-1e-8)//SHIFT)+1):
            lo=ms(shift*SHIFT);hi=ms((shift+1)*SHIFT);cnt=truck_bins[v['truck'],shift]
            cnt['reserved']+=interval(ms(a),ms(b),lo,hi)
            cnt['active']+=interval(ms(v['machine_start']),ms(v['machine_end']),lo,hi)
    for truck in (0,1):
        spans=sorted(intervals[truck])
        assert all(a[1]<=b[0]+1e-7 for a,b in zip(spans,spans[1:]))
        for shift in range(int(e.end//SHIFT)):
            cnt=truck_bins[truck,shift];assert cnt['active']<=cnt['reserved']<=SHIFT*1000
            rr.append(dict(run_id=rid,resource_id='truck/'+str(truck),role='forklift',time_bucket_id=shift,
                shift_available_ms=SHIFT*1000,busy_ms=cnt['active'],standby_ms=SHIFT*1000-cnt['reserved'],break_ms=0,overtime_ms=0,
                queue_requests=0,queue_wait_ms=0,break_count=0,fatigue_trigger_count=0,context_switch_count=0,reserved_ms=cnt['reserved']))
    for r in rr:r.setdefault('reserved_ms',r['busy_ms'])
    primary=[r for r in cases if r['cohort']]
    aggregate=[]
    for n in range(25):
        row={'run_id':rid,'operation':n+1,'cases':len(primary)}
        for col,key in [('visits','operation_visit_count_vector_P01_P25'),('service_ms','operation_service_ms_vector_P01_P25'),('wait_ms','operation_wait_ms_vector_P01_P25'),('worker_ms','operation_worker_ms_vector_P01_P25'),('machine_ms','operation_machine_ms_vector_P01_P25'),('critical_ms','operation_critical_ms'),('skips','skipped')]:
            row[col]=sum(r[key][n] for r in primary)
        aggregate.append(row)
    # Explicit types for nested variable-key maps preserve keys absent in the first row.
    for r in cases:
        for field in ('external_critical_ms','worker_busy_ms_by_role','operation_wait_by_reason_map_P01_P25'):
            r[field]=js(r[field])
    write_table(folder/'cases.parquet',cases);write_table(folder/'resources.parquet',rr);write_table(folder/'operation_aggregate.parquet',aggregate)
    names='case_id event_id operation_id pallet_id_nullable quality_case_id_nullable attempt_id ready_ms grant_ms service_end_ms worker_id_nullable equipment_id_nullable base_draw_ms fatigue_multiplier context_switch_ms manual_active_ms equipment_active_ms equipment_reserved_ms wait_ms wait_reason branch_status causation_id'.split()
    sampled=[]
    for d in diag:
        r=dict(zip(names,d));r.update(run_id=rid,visit_id=r['event_id'],request_ms=r['ready_ms'],service_start_ms=r['grant_ms'],resource_id=r['worker_id_nullable']);sampled.append(r)
    write_table(folder/'visits.parquet',sampled)
    save(folder/'audit.json',audit)
    return dict(offered_cases=len(cases),cohort_cases=len(primary),closed_cases=len(e.closed),open_cases=len(cases)-len(e.closed),other_outcomes=0,
                standard_72=sum(r['standard_success_72h'] for r in primary),mean_T72_ms=sum(r['T72_ms'] for r in primary)/len(primary),
                event_count=len(e.events)+len(e.visits),ledger_sha256=hashlib.sha256(''.join(r['ledger_sha256'] for r in cases).encode()).hexdigest(),audit_violations=len(audit['violations']))


