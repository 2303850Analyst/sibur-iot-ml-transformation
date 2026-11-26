import copy
import gzip
import json
from pathlib import Path

import pytest

from ledger import Finding, Ledger


def fresh(hold=False):
    return Ledger(dict(inbound=[[0,1,42]],outbound=[],cells=[],held=[0] if hold else []))


def step(ledger,event,**details):
    row = dict(time=max(0,ledger.time),pallet=0,event=event)
    row.update(details)
    ledger.apply(row)
    return row


def received(hold=False):
    ledger = fresh(hold)
    step(ledger,'physical_arrival',source_row=42,sku=1)
    step(ledger,'physical_buffer')
    step(ledger,'observation_received')
    step(ledger,'idempotent_duplicate')
    step(ledger,'register',lot='LOT-0',quantity=1,quality='hold' if hold else 'released')
    return ledger


def check_error(ledger,kind,event,**details):
    with pytest.raises(Finding) as error:
        step(ledger,event,**details)
    assert error.value.kind == kind


def test_duplicate_transport_single_receipt():
    ledger = received()
    check_error(ledger,'duplicate_receipts','register',lot='LOT-0',quantity=1)


@pytest.mark.parametrize('cell',[-1,2600,5200,5329])
def test_wrong_address(cell):
    check_error(received(),'illegal_moves','reserve_and_start',cell=cell)


def test_occupied_address():
    ledger = received()
    ledger.cells[1] = (None,1)
    check_error(ledger,'illegal_moves','reserve_and_start',cell=1)


def test_transit_source():
    check_error(received(),'negative_stock','physical_move_end',cell=0)


def test_wrong_reservation():
    ledger = received()
    step(ledger,'reserve_and_start',cell=1)
    check_error(ledger,'illegal_moves','physical_move_end',cell=2)


def test_premature_quality_release():
    check_error(received(True),'illegal_release','external_release',time=7199)


def test_silent_release():
    check_error(received(True),'illegal_release','reserve_and_start',quality='released',cell=1)


def test_quarantine_transit_release_and_outbound():
    ledger = received(True)
    step(ledger,'reserve_and_start',cell=5201,quality='hold')
    step(ledger,'external_release',time=7200)
    step(ledger,'physical_move_end',cell=5201)
    step(ledger,'confirm_place',cell=5201,quality='released',primary=True)
    step(ledger,'reserve_and_start',cell=1,quality='released')
    step(ledger,'physical_move_end',cell=1)
    step(ledger,'confirm_place',cell=1,quality='released',primary=False)
    ledger.outbound = [[7200,1],[7200,1]]
    step(ledger,'outbound',cell=1,sku=1)
    step(ledger,'unfilled_outbound',pallet=None,sku=1)
    ledger.finish()


def test_initial_stock_then_shortage():
    ledger = Ledger(dict(inbound=[],outbound=[[0,1],[0,1]],cells=[[0,1]],held=[]))
    step(ledger,'outbound',cell=0,pallet=None,sku=1)
    step(ledger,'unfilled_outbound',pallet=None,sku=1)
    ledger.finish()
    check_error(ledger,'negative_stock','outbound',cell=0,pallet=None,sku=1)


@pytest.mark.parametrize('trigger,kind',[
    ('register','duplicate_receipts'),('reserve_and_start','illegal_moves'),
    ('external_release','illegal_release'),('outbound','negative_stock')])
def test_control_trace_mutations(trigger,kind):
    folder = Path(__file__).parent/'runs/S1-L2-1000-B'
    with gzip.open(folder/'initial.json.gz') as stream:
        ledger = Ledger(json.load(stream))
    with gzip.open(folder/'events.jsonl.gz') as stream:
        for line in stream:
            row = json.loads(line)
            if row['event'] == trigger:
                altered = copy.copy(row)
                if trigger == 'reserve_and_start':
                    altered['cell'] = next(iter(ledger.cells))
                else:
                    ledger.apply(row)
                with pytest.raises(Finding) as error:
                    ledger.apply(altered)
                assert error.value.kind == kind
                return
            ledger.apply(row)
    pytest.fail('Control event missing')
