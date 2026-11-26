"""Report actual remaining qualification work without starting an invalid main series."""
import json
from inputs import ROOT
for r in json.loads((ROOT/'configs/remaining.json').read_bytes()):
    print(r['id'],r['state'],r['remaining'])
print('PARTIAL. Technical artifacts and remaining run IDs are in results/checkpoints and results/coverage.')
raise SystemExit(2)

