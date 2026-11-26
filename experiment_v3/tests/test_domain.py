import sys,unittest
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
sys.path.insert(0,str(root/'odoo_addons/wms_research_v3'))
from policy import Address,choose_address,enabled,observed_count,pick_job
from effects import adjust,pair
import pandas as pd

class Design(unittest.TestCase):
    def test_all_masks(self):
        for mask in range(128):
            self.assertEqual(sum(1<<(i-1) for i in range(1,8) if enabled(mask,i)),mask)
            pool=[Address('A',0,False,80),Address('B',0,False,40),Address('Q',0,True,15),Address('C',1,False,8)]
            self.assertEqual(choose_address(pool,0,True,mask).name,'B' if mask&8 else 'A')
            self.assertEqual(choose_address(pool,0,False,mask).name,'Q')
    def test_heartbeat(self):
        self.assertEqual(observed_count(100,(80,3),5,10),(3,'sensor'))
        for snapshot in [(0,3),(100,11),(101,2),None]:
            self.assertEqual(observed_count(100,snapshot,5,10),(5,'delivered_movements'))
    def test_forecast_independence(self):
        jobs=[(0,'a',0,200),(0,'b',1,200)]
        for mask in range(128):
            expected='b' if mask&8 else 'a'
            self.assertEqual(pick_job(jobs,0,9,10,[20,0],mask)[1],expected)
    def test_percent(self):
        x=pd.Series([0.]*20);y=pd.Series([2.]*20)
        self.assertIsNone(pair(x,y,'T72_min')['change_pct'])
        self.assertEqual(pair(y,y/2,'T72_min')['change_pct'],-50)
        self.assertAlmostEqual(adjust([.01,.04,.03],'holm')[1],.06)

