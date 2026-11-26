import argparse,json
from pathlib import Path
from inputs import ROOT,fixtures
from process import Process
def main():
    p=argparse.ArgumentParser()
    p.add_argument('--seed',type=int,choices=[2900,2901,2902],default=2900)
    p.add_argument('--scenario',choices=[f'S{i}' for i in range(1,7)],default='S1')
    p.add_argument('--load',choices=['L1','L2','L3'],default='L1')
    p.add_argument('--masks',nargs='+',type=int,default=[0,127])
    args=p.parse_args()
    x=fixtures(args.scenario,args.load,args.seed)
    for mask in args.masks:
        assert 0<=mask<128
        import tempfile,uuid
        parent=ROOT/'results/pilot'
        parent.mkdir(parents=True,exist_ok=True)
        dest=Path(tempfile.mkdtemp(prefix=f'{args.scenario}-{args.load}-{args.seed}-C{mask:03d}-',dir=parent))
        result=Process(x,mask,dest).run()
        from replay import replay
        audit=replay(dest,x)
        result['audit_findings']=audit['findings']
        result['status']='TECHNICAL_PILOT_ONLY'
        (dest/'summary.json').write_text(json.dumps(result,indent=2))
        print(dest.name, result['cases'], result['standard72'],flush=True)
if __name__=='__main__': main()

