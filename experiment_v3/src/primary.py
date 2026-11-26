"""Commit a simulated run only after independent replay. Reads official run IDs."""
import csv,hashlib,json,os,sys,tempfile
from pathlib import Path
from inputs import ROOT,fixtures
from preflight import shafile,write
from process import Process
from replay import replay

def fingerprint():
    paths=[ROOT/'protocol.json',ROOT/'data/ridge.json',ROOT/'data/input_contract.json',ROOT/'parameter_registry.csv',ROOT/'assumptions.csv']
    for directory in ['src','odoo_addons','live','configs','plans','tests']:
        paths.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and p.suffix in ('.py','.json','.yaml','.csv','.xml','.ps1','.html') and p.name!='coverage.json')
    return hashlib.sha256(json.dumps([(p.relative_to(ROOT).as_posix(),shafile(p)) for p in sorted(set(paths))]).encode()).hexdigest()
def freeze():
    # Evidence for every live condition is a prerequisite of the user-specified pilot gate.
    # The supplied design digest is never used as a substitute for a code freeze.
    evidence=ROOT/'results/qualification.json'
    if not evidence.exists():raise SystemExit('PARTIAL: no qualification of S2-S6 and all independent live modules. See report/COMPLETENESS.md.')
    q=json.loads(evidence.read_bytes())
    if q.get('source_fingerprint')!=fingerprint() or q.get('passed') is not True:raise SystemExit('Qualification does not match the current sources.')
    frozen=ROOT/'protocol.sha256'
    if frozen.exists() and frozen.read_text().strip()!=fingerprint():raise SystemExit('Existing freeze belongs to another version.')
    frozen.write_text(fingerprint()+'\n')
def run():
    f=ROOT/'protocol.sha256'
    if not f.exists() or f.read_text().strip()!=fingerprint():raise SystemExit('Runtime protocol is not frozen. Use resume.')
    base=ROOT/'results/primary';base.mkdir(exist_ok=True,parents=True)
    previous=None;x=None
    with (ROOT/'plans/run_manifest.csv').open(encoding='utf-8-sig',newline='') as manifest:
        for r in csv.DictReader(manifest):
            id_=r['run_id'];final=base/id_
            if (final/'commit.json').exists():continue
            key=(r['scenario'],r['load'],int(r['seed']))
            if previous!=key:x=fixtures(*key);previous=key
            # Private staging and a same-volume atomic directory rename allow safe restart.
            tmp=Path(tempfile.mkdtemp(prefix='staging-',dir=base))
            summary=Process(x,int(r['config_id'][1:]),tmp).run()
            audit=replay(tmp,x)
            write(tmp/'commit.json',dict(run_id=id_,fingerprint=f.read_text().strip(),status='FAILED' if audit['findings'] else 'COMPLETED',summary=summary,audit=audit))
            try:tmp.rename(final)
            except OSError:
                if not (final/'commit.json').exists():raise
    print('Simulation manifest exhausted; live coverage is tracked separately.')
if __name__=='__main__':{'freeze':freeze,'run':run}[sys.argv[1]]()

