"""Finite crews with staggered shift windows, deterministic FIFO reservation."""
from common import params
from collections import defaultdict,deque
from math import floor
SHIFT=28800
class Crew:
    def __init__(self,env):
        self.env=env;self.slots={};self.history=defaultdict(deque);self.context={};self.allocations=[]
        for rid,(role,count) in enumerate(params['staffing_reference_assumption']['workers_per_shift'].items()):
            self.slots[role]=[0.]*count
        self.roles=list(self.slots)
    def windows(self,role,slot,shift):
        base=shift*SHIFT;offset=180*self.roles.index(role)+600*slot
        a,b,c=base+7200+offset,base+14400+offset,base+21600+offset
        return [(base,a),(a+600,b),(b+1800,c),(c+600,base+SHIFT)]
    def proposal(self,role,slot,ready,base,context,physical):
        at=max(ready,self.slots[role][slot]);shift=int(at//SHIFT)
        while True:
            wid=f'{role}/{shift%3}/{slot}'
            for lo,hi in self.windows(role,slot,shift):
                t=max(at,lo)
                if t>=hi:continue
                hist=self.history[wid]
                # Candidate evaluation is read-only. Different future alternatives can
                # be inspected before another request gets this worker.
                busy=0.
                for a,b in reversed(hist):
                    if b<=t-7200:break
                    busy+=max(0,min(t,b)-max(a,t-7200))
                fatigue=1.1 if not physical and busy>6120 else 1.
                switch=8 if wid in self.context and self.context[wid]!=context else 0
                end=t+switch+base*fatigue
                if end<=hi:return t,end,slot,wid,switch,fatigue
            shift+=1
    def reserve(self,role,ready,base,case,n,v,slot=None,physical=False):
        opts=range(len(self.slots[role])) if slot is None else [slot]
        t,end,i,wid,sw,fat=min(self.proposal(role,j,ready,base,(case,n),physical) for j in opts)
        self.history[wid].append((t,end))
        self.context[wid]=(case,n);self.slots[role][i]=end
        self.allocations.append((wid,role,t,end,ready,sw,fat,case,n,v))
        return t,end,wid,sw,fat

