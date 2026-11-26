import numpy as np
import pandas as pd
from engine import WARM,STOP,END,allowed

REASONS = ('registration','buffer','cell','forklift','dispatch','quality','confirmation','transport')


def measure(sim,detail):
    frame = pd.DataFrame({field:[getattr(p,field) for p in sim.pallets] for field in
        ('id','arrival','sku','source_row','buffer_at','capture_at','registered_at','released_at','started_at',
         'physical_end_at','placed_at','final_at')})
    frame = frame[frame.arrival.between(WARM,STOP,inclusive='left')].copy()
    arrival = frame.arrival.to_numpy()
    stage = [np.minimum(frame[name].fillna(END).to_numpy(),END) for name in
             ('buffer_at','registered_at','started_at','physical_end_at','placed_at')]
    buffer,ready,start,finish,placed = stage
    cycle = placed-arrival
    move = finish-start
    assert np.all(np.diff(np.vstack([arrival,*stage]),axis=0)>=-1e-8)
    times = np.array([item[0] for item in sim.states]+[END])
    signatures = np.array([item[1] for item in sim.states]+[sim.states[-1][1]],dtype=int)
    masks = (len(frame),)
    block = {name:np.zeros(masks) for name in REASONS}
    active = {name:np.zeros(masks) for name in REASONS}
    block['buffer'] = buffer-arrival
    block['registration'] = ready-buffer
    block['transport'] = move
    block['confirmation'] = placed-finish
    active.update(buffer=buffer-arrival,registration=ready-arrival,transport=move,confirmation=placed-finish)
    both_busy = signatures[:,0]>=2
    def integrate(mask,lo,hi):
        acc = np.r_[0,np.cumsum(np.diff(times)*mask[:-1])]
        def integral(at):
            idx = times.searchsorted(at,side='right')-1
            return acc[idx]+(at-times[idx])*mask[idx]
        return integral(hi)-integral(lo)
    kind = 2*(frame.sku.to_numpy()//10)+(frame.source_row.to_numpy()%5==0)
    released = frame.released_at.fillna(END).to_numpy()
    first_cell = np.full(len(frame),np.inf)
    overlap = buffer-arrival
    for group in range(5):
        idx = np.ones(len(frame),dtype=bool) if group==4 else kind==group
        lo = ready[idx] if group==4 else np.maximum(ready[idx],np.minimum(start[idx],released[idx]))
        hi = np.maximum(lo,np.minimum(start[idx],released[idx])) if group==4 else start[idx]
        free = signatures[:,np.array(allowed(group))+1].any(axis=1)
        no_cell = integrate(~free,lo,hi)
        busy = integrate(both_busy,lo,hi)
        waiting = integrate(free & both_busy,lo,hi)
        policy = hi-lo-no_cell-waiting
        block['cell'][idx] += no_cell if group!=4 else 0
        block['quality'][idx] += no_cell if group==4 else 0
        block['forklift'][idx] += waiting
        block['dispatch'][idx] += policy
        active['cell'][idx] += no_cell
        active['quality'][idx] += no_cell if group==4 else 0
        active['forklift'][idx] += busy
        active['dispatch'][idx] += policy
        overlap[idx] += no_cell if group==4 else busy-waiting
        starts = times[free]
        if len(starts):
            current = free[times.searchsorted(lo,side='right')-1]
            next_idx = starts.searchsorted(lo)
            next_free = np.where(current,lo,np.r_[starts,np.inf][next_idx])
            use = np.where((next_free<=hi)&(hi>lo),next_free,np.inf)
            first_cell[idx] = np.minimum(first_cell[idx],use)
    first_cell = np.minimum(first_cell,np.where(frame.started_at.notna(),start,np.inf))
    assert np.allclose(sum(block.values()),cycle)
    sums = {f'{prefix}_{name}_s':float(values.mean()) for prefix,table in
            [('primary',block),('active',active)] for name,values in table.items()}
    unfinished = frame.placed_at.isna().to_numpy()
    at_stop = placed>=STOP
    frame['first_eligible_at'] = np.where(np.isfinite(first_cell),first_cell,np.nan)
    frame['ready_at'] = frame.registered_at
    frame['assigned_at'] = frame.started_at
    if detail:
        for name in REASONS:
            frame['primary_'+name] = block[name]
            frame['active_'+name] = active[name]
    result = dict(**sums,input_hash=sim.input_hash,inbound=len(frame),actual_day=len(frame)/30,
        cycle_s=cycle.mean(),wait_s=(cycle-move).mean(),p90_wait_s=np.quantile(cycle-move,.9),
        p95_wait_s=np.quantile(cycle-move,.95),timely_4h=(cycle<=14400).mean(),
        unfinished_stop=int(sum(at_stop)),unfinished_end=int(sum(unfinished)),unfinished_share=unfinished.mean(),
        unfinished_age_s=(END-arrival[unfinished]).mean() if unfinished.any() else 0,
        unfinished_age_sum_s=(END-arrival[unfinished]).sum(),placed=int(sum(~unfinished)),
        placed_stop=int(sum(~at_stop)),final=int(frame.final_at.notna().sum()),full_buffer=sim.full_seconds/(STOP-WARM),
        operator_h=sim.operator_seconds/3600,empty_m=sim.empty,loaded_m=sim.loaded,
        travel_h=(sim.empty+sim.loaded)/3600,fork_h=sum(sim.work)/3600,
        util_0=sim.work[0]/(STOP-WARM),util_1=sim.work[1]/(STOP-WARM),mae=np.mean(sim.error),
        no_cell_pallets=int((active['cell']>1e-6).sum()),multiple_s=overlap.mean(),
        repeat_moves=sim.stats['repeat_moves'],corrections=sim.stats['corrections'],
        assignments=sim.stats['assignments'],completed_jobs=sim.stats['completed_jobs'],
        changed_iot=sim.stats['iot_changed'],changed_forecast=sim.stats['forecast_changed'],
        stale_share=sim.stats['stale_threshold']/max(1,sim.stats['assignments']),
        fallback=sim.stats['fallback'],drops=sim.stats['miss'],false_observations=sim.stats['false'],
        duplicates_ignored=sim.stats['discarded_replay'])
    return result,frame
