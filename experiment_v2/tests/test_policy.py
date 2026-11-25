import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import run
from engine import Warehouse
import pytest


def bare(variant='B2'):
    return Warehouse([(0.,0,1),(0.,10,2)],[],'S1',1900,variant,[],False)


def test_sensor_and_expiry_change_source():
    warehouse=bare()
    warehouse.last_count=40
    warehouse.last_stamp=0
    warehouse.wms_buffer=0
    choices=[(0,0,0,0,40,100,True),(2,1,2600,0,80,-10,False)]
    assert warehouse.choice(choices,False,[0,0])[1]==1
    assert warehouse.choice(choices,True,[0,0])[1]==0
    warehouse.last_stamp=-121
    assert warehouse.choice(choices,True,[0,0])[1]==1


def test_forecast_and_fifo():
    warehouse=bare()
    choices=[(0,0,0,10,40,0,True),(2,1,2600,10,80,-1,True)]
    assert warehouse.choice(choices,False,[1,1])[1]==0
    assert warehouse.choice(choices,False,[1,6])[1]==1
    warehouse.adaptive=False
    assert warehouse.choice(choices,False,[1,1])[1]==1


def test_reserved_capacity_and_relocation_distance():
    warehouse=bare()
    pallet=warehouse.pallets[0]
    pallet.state='ready'
    warehouse.registration[0]=0
    warehouse.queue(pallet)
    warehouse.pools=[[521],[1],[],[],[]]
    assert warehouse.candidates(80)[0][2:4]==(521,80)
    warehouse.dynamic=False
    assert warehouse.candidates(80)[0][2]==1
    pallet.source_row=5
    warehouse.jobs=[[],[(0,0)],[],[],[]]
    assert warehouse.candidates(80)[0][2]==1


def test_delayed_replayed_sensor():
    warehouse=bare()
    warehouse.mail=[(0,2,0,39,0,True),(0,2,0,39,0,True),(5,3,5,41,1,True)]
    warehouse.deliver()
    assert warehouse.last_count==39 and warehouse.stats['discarded_replay']==1
    warehouse.env.run(until=6)
    warehouse.deliver()
    assert warehouse.last_count is None


def test_pilot_ml_fallback():
    folder=ROOT/'results/pilot'
    for original in folder.glob('*-B2.json'):
        left=json.loads(original.read_text())
        right=json.loads(original.with_name(original.name.replace('-B2','-FAIL')).read_text())
        for metric in ('cycle_s','empty_m','placed','input_hash','audit_pass'):
            assert left[metric]==right[metric]
