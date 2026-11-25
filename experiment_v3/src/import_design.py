"""Verify the supplied ZIP and independently reproduce every planned row."""
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'WMS_V3_PROMPT/'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def install(archive):
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        sums = z.read(PREFIX+'SHA256SUMS.txt').decode().splitlines()
        listed = {name: digest for digest, name in (line.split('  ', 1) for line in sums)}
        assert set(z.namelist()) == {PREFIX+x for x in listed} | {PREFIX+'SHA256SUMS.txt'}
        for name, expected in listed.items():
            data = z.read(PREFIX+name)
            if sha(data) != expected:
                raise ValueError(name)
            dest = (ROOT/'design'/name).resolve()
            assert dest.is_relative_to(ROOT/'design')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        (ROOT/'design/SHA256SUMS.txt').write_bytes(z.read(PREFIX+'SHA256SUMS.txt'))
    with tempfile.TemporaryDirectory() as scratch:
        subprocess.run([sys.executable, str(ROOT/'design/generate_plan.py'), '--output', scratch], check=True, stdout=subprocess.DEVNULL)
        for p in (ROOT/'design/plans').iterdir():
            generated = Path(scratch, p.name)
            if p.suffix == '.json':
                assert json.loads(p.read_bytes()) == json.loads(generated.read_bytes())
            else:
                read = lambda f: list(csv.reader(io.StringIO(f.read_text(encoding='utf-8-sig'))))
                assert read(p) == read(generated)
            (ROOT/'plans').mkdir(exist_ok=True)
            (ROOT/'plans'/p.name).write_bytes(p.read_bytes())
    (ROOT/'protocol.json').write_bytes((ROOT/'design/V3_PROTOCOL.json').read_bytes())
    (ROOT/'protocol.md').write_bytes((ROOT/'design/01_PROMPT_V3_FULL_RU.md').read_bytes())
    return {'archive_sha256':sha(Path(archive).read_bytes()),'verified_members':len(listed),'planned_files_reproduced':4}
if __name__=='__main__':
    print(json.dumps(install(Path(sys.argv[1]))))

