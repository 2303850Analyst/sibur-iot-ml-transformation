from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import *
CORE=['bench.py','common.py','crew.py','engine.py','job.py','ledger.py','measure_io.py','validate_design.py']
def signatures():
    files=[HOME/p for p in CORE]+[HOME/'io_profile.json',HOME/'settings.json',PARAM,V3/'src/process.py',V3/'src/inputs.py',V3/'odoo_addons/wms_research_v3/policy.py',V3/'data/ridge.json',V3.parent/'series_py312/data/normalized/source.parquet']
    return {p.relative_to(V3.parent).as_posix():sha(p) for p in files}
if __name__=='__main__':save(HOME/'preflight/pilot_signature.json',signatures())

