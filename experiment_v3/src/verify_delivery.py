"""Verify coverage, archive provenance, workbook formulas and the current checkpoint."""
from pathlib import Path
import csv,json,zipfile
from xml.etree import ElementTree as ET
from inputs import ROOT
def main():
    cp=json.loads((ROOT/'results/checkpoints/latest.json').read_bytes())
    for k,fn in [('LIVE_ODOO','missing_live_manifest.csv'),('PROCESS_SIMULATION','missing_run_manifest.csv')]:
        with (ROOT/'results/coverage'/fn).open(encoding='utf-8-sig',newline='') as f:
            ids=[r['run_id'] for r in csv.DictReader(f)]
        assert len(set(ids))==len(ids)==cp['counts'][k]['missing']
    assert cp['functional_odoo_masks']['successful']==128
    xlsx=ROOT/'report/UNIFIED_COMPARISON.xlsx'
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(xlsx) as book:
        assert book.testzip() is None
        sh=ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
        formulas=sh.findall('.//s:f',ns)
        assert len(formulas)==2*len(json.loads((ROOT/'report/tables.json').read_bytes())['comparisons'])
        assert formulas[0].text=='J2-I2'
        errors=[c for c in sh.findall('.//s:c',ns) if c.get('t')=='e']
        assert not errors
    print('PASS coverage, 128 functional masks, workbook, formulas:',len(formulas))
if __name__=='__main__':main()

