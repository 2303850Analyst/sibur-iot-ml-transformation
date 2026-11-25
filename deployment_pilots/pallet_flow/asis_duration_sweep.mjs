import fs from 'node:fs';
import assert from 'node:assert/strict';
import {simulate} from './decision_matrix_p25.mjs';

const output=process.argv[2];
if(!output)throw Error('Укажите путь для сохранения CSV.');
const fleet={contour:'SAP',feed:2,store:1,flex:0,robots:0};
const plannedLastStart=23*3600+5*60;
const chosen={passport:250,close:180,putaway:255};
const clock=s=>[Math.floor(s/3600),Math.floor(s%3600/60),s%60].map(x=>String(x).padStart(2,'0')).join(':');
const decimal=v=>v===null?'':String(Number(v.toFixed(3))).replace('.',',');
const rows=[['Сценарий','Выпуск первой паллеты последней поставки','Регистрация 5 паллет, с','Уточнение паспорта, с/поставка','Закрытие SAP, с/поставка','Укладка, с/паллета','Суток с полным закрытием из 30','Минимум закрытых в свои сутки из 64','Максимум закрытых в свои сутки из 64','Всего закрыто к полуночи дня выпуска из 1920','Самое позднее закрытие когорты от начала её суток, ч:мин:с','Минимальный запас к полуночи, с','Среднее P25 по завершённым, мин','P95 по завершённым, мин','Загрузка приёмщика, %','Незавершено после дополнительных 72 ч']];
function trial(label,times,lastStart=plannedLastStart){
  const r=simulate(fleet,320,241126,{manualTimes:times,lastStart,dailyStats:true});
  const latest=r.days.every(d=>d.lastSeconds!==null)?Math.max(...r.days.map(d=>d.lastSeconds)):null;
  const sameDay=r.days.map(d=>d.onTime);
  const fullDays=sameDay.filter(x=>x===64).length;
  const total=sameDay.reduce((a,b)=>a+b,0);
  const margin=latest===null?null:86400-latest;
  rows.push([label,clock(lastStart??85050),900,times.passport,times.close,times.putaway,fullDays,Math.min(...sameDay),Math.max(...sameDay),total,latest===null?'':clock(latest),margin??'',decimal(r.mean),decimal(r.p95),decimal(r.receiverH/720*100),r.undrained]);
  return {latest,margin,fullDays,total,r};
}
let tested=0,passed=0;
for(const close of [180,240,300]){
  for(let extra=390;extra<=465;extra+=5){
    for(const putaway of [250,255,260,262,264,265,266,268,270]){
      const passport=extra-close;
      const selected=passport===chosen.passport&&close===chosen.close&&putaway===chosen.putaway;
      const t=trial(selected?'Выбранный As-is 5.2':'Подбор длительностей',{passport,close,putaway});
      tested++;if(t.fullDays===30)passed++;
      if(selected){
        assert.equal(t.total,1920);assert.equal(t.r.checks,9600);assert.equal(t.margin,10);
        console.log('Выбранный',JSON.stringify({min:Math.min(...t.r.days.map(d=>d.lastSeconds)),max:t.latest,mean:t.r.mean,p95:t.r.p95,receiver:t.r.receiverH/720,store:chosen.putaway*320/86400}));
      }
    }
  }
}
const original={passport:600,close:300,putaway:300};
trial('Верхний исходный As-is при окне до 23:05',original);
const regression=trial('Верхний исходный As-is при круглосуточном выпуске',original,null);
assert.deepEqual([regression.r.openedStart,regression.r.openedMid,regression.r.openedEnd],[26,404,782]);
const continuous=trial('Выбранные длительности при круглосуточном выпуске',chosen,null);
console.log('Круглосуточный выпуск',JSON.stringify({last:continuous.latest,days:continuous.fullDays,onTime:[...new Set(continuous.r.days.map(d=>d.onTime))],atStart:continuous.r.openedStart,atEnd:continuous.r.openedEnd}));
console.log('Комбинаций',tested,'прошли',passed);
fs.writeFileSync(output,'\uFEFF'+rows.map(row=>row.join(';')).join('\r\n')+'\r\n','utf8');
