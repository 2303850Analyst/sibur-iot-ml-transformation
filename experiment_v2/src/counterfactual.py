import json
import os
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
import numpy as np
import pandas as pd
import run
from engine import Warehouse, WARM,STOP,END,CONFIG


class Shadow(Warehouse):
    def __init__(self,*args):
        super().__init__(*args)
        self.shadow_total=0
        self.shadow_change=0
        self.bad_signal=0
        self.signal_cost=0
        self.ticks={}

    def log(self,action,pallet=None,**extra):
        super().log(action,pallet,**extra)
        if action=='physical_arrival': self.ticks[pallet.id]=self.env.now
    def candidates(self,position):
        self.first_query=True
        return super().candidates(position)
    def choice(self,options,sensor,pred):
        actual=super().choice(options,sensor,pred)
        if not self.first_query: return actual
        self.first_query=False
        if not WARM<=self.env.now<STOP: return actual
        self.shadow_total+=1
        actual_count=self.last_count if sensor and self.last_count is not None and self.env.now-self.last_stamp<=120 else self.wms_buffer
        truth=len(self.buffer_tokens)
        if (truth>=.75*self.buffer_limit)!=(actual_count>=.75*self.buffer_limit):
            self.bad_signal+=1
            previous=(self.last_count,self.last_stamp,self.received)
            self.last_count,self.last_stamp=truth,self.env.now
            # Hold age input fixed to isolate occupancy from every other decision input.
            self.received=previous[2] if sensor and previous[0] is not None and self.env.now-previous[1]<=120 else {}
            alternative=super().choice(options,True,pred)
            self.last_count,self.last_stamp,self.received=previous
            self.shadow_change+=int(alternative[1]!=actual[1])
        return actual


def task(job):
    level,seed=job
    selected=run.sample(run.source_data,run.manifest,run.LEVELS[level],seed)
    rows=[]
    for scenario in ('S1','S2','S3','S4','S5','S6'):
        inbound,outbound,_=run.campaign(selected,run.manifest,seed,scenario)
        for variant in ('A2','B2','C2'):
            sim=Shadow(inbound,outbound,scenario,seed,variant,run.models[level],False)
            sim.execute()
            saved=json.loads((run.RESULT/'jobs'/f'{scenario}-{level}-{seed}-{variant}.json').read_text())
            assert sim.input_hash==saved['input_hash'] and sim.shadow_total==saved['assignments']
            assert sim.empty==saved['empty_m'] and np.isclose(sim.bad_signal/sim.shadow_total,saved['stale_share'])
            rows.append(dict(run_id=saved['run_id'],scenario=scenario,level=level,variant=variant,seed=seed,
                assignments=sim.shadow_total,wrong_threshold=sim.bad_signal,different_assignment=sim.shadow_change,
                different_share=sim.shadow_change/sim.shadow_total))
    return rows


if __name__=='__main__':
    jobs=[(level,seed) for seed in range(2000,2020) for level in run.LEVELS]
    rows=[]
    with ProcessPoolExecutor(12,initializer=run.initialize) as pool:
        futures=[pool.submit(task,job) for job in jobs]
        for future in as_completed(futures): rows.extend(future.result())
    assert len(rows)==1080 and len({row['run_id'] for row in rows})==1080
    pd.DataFrame(rows).sort_values('run_id').to_csv(run.RESULT/'occupancy_decisions.csv',index=False)
