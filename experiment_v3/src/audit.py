"""Independent replay of application evidence and mutation controls."""
from collections import Counter
from copy import deepcopy
import json
from inputs import ROOT

def inspect(rows):
    issues=Counter()
    receipts={}; placed={}; ack=set(); releases={}; occupancy={}; reservations={}; postings=set(); versions={}; expected={}
    for r in rows:
        if r['status']!=200:continue
        n=r['operation'];data=r['data'];case=r['case'];p=data.get('pallet')
        ver=r.get('version',1)
        if case in versions and ver!=versions[case]:issues['stale_version']+=1
        if n==1:
            expected[case]=set(data['members'])
            versions[case]=ver
        if p is not None and p not in expected.get(case,set()):issues['foreign_pallet']+=1
        result=r['result']
        if 'picking' in result:
            pk=result['picking']
            if pk in postings:issues['repeated_posting']+=1
            postings.add(pk)
        if n==16:
            if p in receipts:issues['double_receipt']+=1
            receipts[p]=case
        if n==8:
            if r['role']!='quality':issues['unauthorized_quality']+=1
            elif data.get('decision')=='released':releases[case]=True
        if n==17:
            a=result['address']
            if a in reservations or a in occupancy:issues['duplicate_reservation']+=1
            reservations[a]=p
        if n==19:
            a=data['address']
            if reservations.get(a)!=p:issues['foreign_cell']+=1
            if p not in receipts or p in placed:issues['repeated_stock_issue']+=1
            if case not in releases:issues['missing_quality']+=1
            placed[p]=a;occupancy[a]=p;reservations.pop(a,None)
        if n==24 and data.get('ack')==f'{case}/{ver}':ack.add(case)
        if n==25:
            if not expected.get(case) or not expected[case].issubset(placed):issues['early_close']+=1
            if case not in ack:issues['missing_ack']+=1
    return dict(issues)
def controls(rows):
    passed={}
    for name,n,mutation in [('double_receipt',16,'duplicate'),('foreign_cell',19,'address'),('unauthorized_quality',8,'role'),
                            ('repeated_stock_issue',19,'duplicate'),('early_close',19,'remove'),('missing_ack',24,'remove'),
                            ('stale_version',19,'version'),('duplicate_reservation',17,'duplicate')]:
        cooked=deepcopy(rows)
        i=next(i for i,r in enumerate(cooked) if r['status']==200 and r['operation']==n)
        if mutation=='duplicate':cooked.insert(i,deepcopy(cooked[i]))
        if mutation=='address':cooked[i]['data']['address']='FOREIGN'
        if mutation=='role':cooked[i]['role']='operator'
        if mutation=='remove':cooked.pop(i)
        if mutation=='version':cooked[i]['version']=0
        passed[name]=inspect(cooked).get(name,0)>0
    return passed
def main():
    folder=ROOT/'results/audit'
    folder.mkdir(parents=True,exist_ok=True)
    for f in (ROOT/'results/functional').glob('*.json'):
        rows=json.loads(f.read_bytes())['rows']
        result={'file':f.name,'scope':'HTTP functional observations only; no DB inventory reconstruction','rows':len(rows),
                'detected':inspect(rows),'mutations':controls(rows)}
        assert not result['detected'] and all(result['mutations'].values()),result
        (folder/f.name).write_text(json.dumps(result,indent=2))
        print(f.name,len(rows),'8/8')
if __name__=='__main__':main()

