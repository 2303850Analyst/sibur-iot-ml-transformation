"""Read-only database reconciliation independent of Odoo workflow code."""
import csv,io,json,subprocess
from inputs import ROOT
from preflight import write

def db_rows(database,query):
    assert database in ('wms_v3','wms_v3_trial')
    args=['docker','exec','-i','wms-inbound-v3-db-1','psql','-U','odoo','-d',database,'-v','ON_ERROR_STOP=1','--csv','-c',query]
    raw=subprocess.run(args,check=True,capture_output=True,encoding='utf8')
    return list(csv.DictReader(io.StringIO(raw.stdout)))
def audit(database):
    events=db_rows(database,"""SELECT c.name AS receipt,c.mask,e.operation,e.name AS event_id,e.member,e.role,
    e.create_date,e.result,e.actor_id,e.version FROM wms_v3_event e JOIN wms_v3_case c ON c.id=e.case_id ORDER BY e.id""")
    cases=db_rows(database,'SELECT id,name,mask,closed FROM wms_v3_case ORDER BY id')
    stock=db_rows(database,"""SELECT p.pallet_id,p.quantity,p.quality,r.id AS receipt_id,r.state AS receipt_state,
        m.id AS transfer_id,m.state AS transfer_state,c.zone,c.location_id,
        coalesce((select sum(q.quantity) from stock_quant q where q.package_id=p.package_id and q.lot_id=p.lot_id
          and q.location_id=c.location_id),0) AS actual_at_address
        FROM wms_research_pallet p JOIN stock_picking r ON r.id=p.receipt_id LEFT JOIN stock_picking m ON m.id=p.transfer_id
        LEFT JOIN wms_research_cell c ON c.id=p.cell_id ORDER BY p.id""")
    findings=[];bycase={}
    for ev in events:
        bycase.setdefault(ev['receipt'],[]).append(ev)
    for c in cases:
        route=bycase.get(c['name'],[])
        if c['closed']=='t':
            if {int(e['operation']) for e in route}!=set(range(1,26)):findings.append([c['name'],'missing_operation'])
            ack=[i for i,e in enumerate(route) if e['operation']=='24']
            ends=[i for i,e in enumerate(route) if e['operation']=='25']
            if not ack or not ends or ack[0]>=ends[0]:findings.append([c['name'],'unacknowledged_close'])
    ids=set()
    for p in stock:
        if p['receipt_state']=='done':
            if p['receipt_id'] in ids:findings.append([p['pallet_id'],'duplicate_receipt'])
            ids.add(p['receipt_id'])
        if p['transfer_state']=='done' and (float(p['quantity'])!=float(p['actual_at_address']) or
            (p['zone']=='storage' and p['quality']!='released')):findings.append([p['pallet_id'],'stock_or_quality'])
    result={'database':database,'case_count':len(cases),'closed':sum(c['closed']=='t' for c in cases),'events':len(events),
            'pallets':len(stock),'receipts_done':len(ids),'findings':findings,'scope':'current stock location and lot, receipt uniqueness, full closed routes and exchange ordering'}
    write(ROOT/'results/audit'/(database+'_records.json'),dict(events=events,cases=cases,pallets=stock))
    write(ROOT/'results/audit'/(database+'_audit.json'),result)
    print(json.dumps(result))
if __name__=='__main__':
    import sys
    audit(sys.argv[1] if len(sys.argv)>1 else 'wms_v3')

