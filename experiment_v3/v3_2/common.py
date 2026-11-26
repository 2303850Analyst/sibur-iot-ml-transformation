import os
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key]='1'
import csv,hashlib,json,sys
from pathlib import Path
HOME=Path(__file__).resolve().parent
V3=HOME.parent
sys.path.insert(0,str(V3/'src'))
DEST=Path(os.environ.get('WMS32_OUTPUT',str(HOME/'results')))
PARAM=HOME/'design/protocol/ASIS_BASELINE_PARAMETER_FREEZE.json'
params=json.loads(PARAM.read_bytes())
ops={int(x['operation_id'][1:]):x for x in params['operations']}
settings=json.loads((HOME/'settings.json').read_bytes())
def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def js(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def save(p,obj):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.tmp-'+str(os.getpid()))
    tmp.write_text(js(obj),encoding='utf8');tmp.replace(p)
def fingerprint():
    inputs=[PARAM,HOME/'settings.json',V3/'src/process.py',V3/'src/inputs.py',V3/'data/ridge.json',V3/'odoo_addons/wms_research_v3/policy.py',V3.parent/'series_py312/data/normalized/source.parquet',V3.parent/'series_py312/data/manifests/wepastacks.json',*sorted((HOME/'design').rglob('*.csv')),*sorted((HOME/'design/protocol').glob('*.json')),*sorted(HOME.glob('*.py'))]
    return hashlib.sha256(js([(str(p.relative_to(V3.parent)).replace(chr(92),'/'),sha(p)) for p in inputs]).encode()).hexdigest()

