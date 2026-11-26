"""Resume missing predeclared technical cells. Superseded directories never count."""
import json,subprocess,sys
from inputs import ROOT
SCHEDULE=[('S1','L1',2900,[0,127]),('S2','L1',2902,[0,127]),('S3','L1',2902,[0,127]),
          ('S4','L2',2901,[0,127]),('S5','L2',2902,[0,2,127]),('S6','L3',2902,[0,127])]
def main():
    for s,l,seed,masks in SCHEDULE:
        for mask in masks:
            runs=(ROOT/'results/pilot').glob(f'{s}-{l}-{seed}-C{mask:03d}-*/summary.json')
            if any((f.parent/'audit.json').exists() and not (f.parent/'reliability.json').exists()
                and not json.loads((f.parent/'audit.json').read_bytes())['findings'] for f in runs):continue
            subprocess.run([sys.executable,str(ROOT/'src/pilot.py'),'--scenario',s,'--load',l,'--seed',str(seed),'--masks',str(mask)],check=True)
if __name__=='__main__':main()

