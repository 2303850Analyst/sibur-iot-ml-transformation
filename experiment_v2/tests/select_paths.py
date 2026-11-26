import gzip
import json
from pathlib import Path
from uuid import uuid4

root=Path('/lab/experiment_v2/results')
cases=[]
for variant in ('A2','B2','C2'):
    folder=root/'traces'/f'S4-L2-2000-{variant}'
    rows=[json.loads(line) for line in gzip.open(folder/'events.jsonl.gz','rt')]
    arrivals={row['pallet']:row for row in rows if row['event']=='physical_arrival' and 86400<=row['time']<2678400}
    by_pallet={}
    for row in rows:
        if row['pallet'] in arrivals and row['event'] in ('register','reserve_and_start','external_release','confirm_place'):
            by_pallet.setdefault(row['pallet'],[]).append(row)
    for hold in (False,True):
        for key,events in by_pallet.items():
            places=[row for row in events if row['event']=='confirm_place']
            if len(places)!=(2 if hold else 1) or (places[0]['cell']>=5200)!=hold:
                continue
            cases.append(dict(run='V2-'+uuid4().hex[:12],variant=variant,pallet='P'+str(key),
                              sku=arrivals[key]['sku'],cells=[row['cell'] for row in places],
                              events=events,source_run=folder.name))
            break
assert len(cases)==6
(root/'odoo_fixtures.json').write_text(json.dumps(cases,indent=2))
