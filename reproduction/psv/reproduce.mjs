import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const reference = path.join(here, 'reference');
const scripts = path.join(root, 'deployment_pilots', 'pallet_flow');
const reports = path.join(root, 'writing','Финальная оценка Ex');
const work = path.join(root,'reproduced');
fs.mkdirSync(work,{recursive:true});
const output = fs.mkdtempSync(path.join(work,'psv-'));
function sha(file) {
  return createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}
function run(name,...args) {
  const result = spawnSync(process.execPath,[path.join(scripts,name),...args],{stdio:'inherit'});
  if(result.error) throw result.error;
  assert.equal(result.status,0,name);
}
const manifest = JSON.parse(fs.readFileSync(path.join(here,'checksums.json'),'utf8'));
for(const [name,hash] of Object.entries(manifest)) assert.equal(sha(path.join(root,name)),hash,name);
const {simulate} = await import('../../deployment_pilots/pallet_flow/decision_matrix_p25.mjs');
const manual = {contour:'SAP',robots:0,feed:2,store:1,flex:0};
const robot = {contour:'Ex',robots:2,feed:1,store:0,flex:0};
assert.equal(simulate(manual,320,241126,{isolated:true}).mean*60,3120);
assert.equal(simulate(robot,320,241126,{isolated:true}).mean*60,122*5+290);
const calibrated = simulate(manual,320,241126,{lastStart:83100,dailyStats:true,manualTimes:{passport:250,close:180,putaway:255}});
assert(calibrated.days.length===30 && calibrated.days.every(d=>d.onTime===64 && d.lastSeconds===86390));
assert.equal(Math.round(calibrated.mean*60),3354);
assert.equal(Math.round(calibrated.p95*60),3860);

const fleets = path.join(reference,'fleets.tsv');
const rows = fs.readFileSync(fleets,'utf8').trim().split(/\r?\n/).slice(1).map(line=>line.split('\t'));
const number = x=>Number(x.replace(',','.').replace('(','-').replace(')',''));
const results=[];
for(const [load,expected,expectedNpv] of [[320,127,17.16],[640,110,9.91],[960,84,-4.60]]) {
  const name='flow'+load+'.json';
  run('decision_matrix_p25.mjs',fleets,path.join(output,name),String(load));
  const trials=JSON.parse(fs.readFileSync(path.join(output,name)));
  assert.deepEqual(trials,JSON.parse(fs.readFileSync(path.join(reference,name))));
  const eligible=[];
  for(const row of rows) {
    const runs=trials.filter(r=>r.code===row[0]);
    assert.equal(runs.length,3);
    if(!runs.every(r=>r.stable)) continue;
    const [capex,opex,payroll,loss]=row.slice(8,12).map(number);
    let npv=-capex;
    for(let year=1;year<=5;year++){
      const cash=payroll-opex+loss*(year===1?.8:1);
      npv+=cash*1.07**(year-1)/1.2591**year;
    }
    eligible.push({code:row[0],npv,p95:Math.max(...runs.map(r=>r.p95))});
  }
  assert.equal(eligible.length,expected);
  eligible.sort((a,b)=>b.npv-a.npv||a.p95-b.p95||a.code.localeCompare(b.code,'en'));
  assert.equal(Number(eligible[0].npv.toFixed(2)),expectedNpv);
  results.push({load,eligible:eligible.length,winner:eligible[0]});
}
for(const [script,name] of [['asis_day_balance.mjs','As-is ПСВ - закрытие по суткам.csv'],['asis_duration_sweep.mjs','As-is ПСВ - подбор ручных длительностей.csv']]){
  run(script,path.join(output,name));
  assert.equal(sha(path.join(output,name)),sha(path.join(reports,name)),name);
}
fs.writeFileSync(path.join(output,'verified.json'),JSON.stringify(results,null,2));
console.log('PASS · P01-P25, As-is 5.2, 444 сочетания и NPV',output);

