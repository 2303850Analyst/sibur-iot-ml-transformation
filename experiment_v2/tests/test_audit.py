import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from run import Check
from engine import Warehouse
from ledger import Finding


def auditor(scenario='S1'):
    model=Warehouse([(0.,1,5)],[],scenario,2000,'A2',[],False)
    return Check(model)


def emit(ledger,op,**fields):
    ledger.apply(dict(event=op,pallet=0,time=0,**fields))


def admitted():
    ledger=auditor()
    ledger.held.clear()
    emit(ledger,'physical_arrival',sku=1,source_row=5)
    emit(ledger,'physical_buffer')
    emit(ledger,'observation_received')
    emit(ledger,'register',quality='released',quantity=1,lot='LOT-0')
    return ledger


@pytest.mark.parametrize('kind,event,fields',[
    ('duplicate_receipts','register',dict(quality='released',quantity=1,lot='LOT-0')),
    ('illegal_moves','reserve_and_start',dict(cell=700,quality='released')),
    ('negative_stock','physical_move_end',dict(cell=3)),
    ('illegal_release','external_release',{})])
def test_detect_mutation(kind,event,fields):
    ledger=admitted()
    with pytest.raises(Finding) as problem:
        emit(ledger,event,**fields)
    assert problem.value.kind==kind


def test_long_quarantine_uses_s5_schedule():
    ledger=auditor('S5')
    ledger.held.add(0)
    emit(ledger,'physical_arrival',sku=1,source_row=5)
    with pytest.raises(Finding):
        ledger.apply(dict(time=7200,event='external_release',pallet=0))
    ledger.apply(dict(time=86400,event='external_release',pallet=0))
    assert 0 not in ledger.held
