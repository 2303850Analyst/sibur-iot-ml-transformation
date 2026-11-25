import argparse,hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    files=sorted((ROOT/'odoo_addons').rglob('*.py'))+[ROOT/'live'/s for s in ('fixtures_db.py','orm_matrix.py','forecast_coefficients.json')]
    fingerprint=hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()
    out=ROOT/'results/functional_masks'/fingerprint
    out.mkdir(parents=True,exist_ok=True)
    missing=[n for n in range(128) if not (out/f'C{n:03d}.json').exists() or not json.loads((out/f'C{n:03d}.json').read_bytes())['complete']]
    if not missing:
        print('C000-C127 already checked on this revision');return
    spans=[]
    for n in missing:
        if spans and n==spans[-1][-1]+1:spans[-1].append(n)
        else:spans.append([n])
    for span in spans:
        cmd=['docker','compose','--env-file','../.env','exec','-T','-e',f'V3_FIRST_MASK={span[0]}','-e',f'V3_LAST_MASK={span[-1]}','odoo','sh','-c',
             'odoo shell -d wms_v3 --db_host=db --db_user=odoo --db_password="$PASSWORD" --addons-path=/mnt/legacy,/mnt/extra-addons --no-http --log-level=error < /lab/orm_matrix.py']
        with subprocess.Popen(cmd,cwd=ROOT,stdout=subprocess.PIPE,text=True,encoding='utf8') as proc:
            for line in proc.stdout:
                if not line.startswith('V3_PROOF '):continue
                item=json.loads(line[9:]);n=item['mask'];item['source_fingerprint']=fingerprint
                f=out/f'C{n:03d}.json'
                tmp=f.with_suffix('.tmp');tmp.write_text(json.dumps(item,indent=2));tmp.replace(f)
                if not item['complete']:raise RuntimeError(item['error'])
                if n%16==15:print('Functional ORM masks through C%03d'%n,flush=True)
            if proc.wait():raise RuntimeError('Odoo functional batch failed')
    if len(list(out.glob('C*.json')))!=128:raise RuntimeError('Incomplete functional batch')
if __name__=='__main__':main()

