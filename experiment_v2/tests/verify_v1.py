from pathlib import Path
import hashlib
import json

root=Path(__file__).resolve().parents[1]
series=root.parent/'series_py312'
count=0
markers=list((series/'artifacts/runs').glob('*/manifest.json'))
assert len(markers)==1080
for marker in markers:
    for name,expected in json.loads(marker.read_text())['files'].items():
        with (marker.parent/name).open('rb') as stream:
            assert hashlib.file_digest(stream,'sha256').hexdigest()==expected,(marker,name)
        count+=1
(root/'results/v1_integrity.json').write_text(json.dumps(dict(manifests=len(markers),hashes=count,status='PASS')))
print('Verified original V1 artifacts:',count)
