import assert from 'node:assert/strict';
import fs from 'node:fs';
import {simulate} from './decision_matrix_p25.mjs';

const output=process.argv[2];
if(!output)throw Error('Укажите путь к результатам CSV.');
const loads=[320,640,960];
const cases=[];
function add(name,load,feed,store,flex,documentDesk=false,lastStart=null,robots=0){
  cases.push({name,load,config:{contour:robots?'Ex':'SAP',feed,store,flex,robots},documentDesk,lastStart});
}
for(const load of loads)add('Исходный As-is',load,2,1,0);
add('Только сотрудник по документам',320,2,1,0,true);
add('Только совмещение рейсов',320,1,1,1);
add('Разделение документов и совмещение рейсов',320,1,1,1,true);
for(const minutes of [23*60,23*60+5,23*60+10]){
  add('Ручной сценарий с окном выпуска',320,1,1,1,true,minutes*60);
}
add('Цифровая приёмка при том же выпуске',320,0,0,1,false,(23*60+5)*60,1);

const rows=[['Сценарий','Паллет в сутки','Подвоз','Укладка','Универсалы','Роботы','Сотрудник по документам','Выпуск первой паллеты последней поставки','Реализация','Сутки наблюдения','Поставок выпущено','Из них закрыто в свои сутки','Из них осталось в полночь','Последнее закрытие когорты (ч от начала её суток)','Среднее P25 за 30 суток по завершённым, мин','P95 за 30 суток по завершённым, мин']];
const decimal=n=>Number.isFinite(n)?String(Math.round(n*10000)/10000).replace('.',','):'';
const clock=s=>[Math.floor(s/3600),Math.floor(s%3600/60),s%60].map(t=>String(t).padStart(2,'0')).join(':');
for(const item of cases){
  const {name,load,config,documentDesk,lastStart}=item;
  const count=load/5,release=lastStart??86400-86400/count;
  for(const seed of config.robots?[241126,241127,241128]:[241126]){
    const r=simulate(config,load,seed,{documentDesk,lastStart,dailyStats:true});
    for(const day of r.days){
      rows.push([name,load,config.feed,config.store,config.flex,config.robots,+documentDesk,clock(release),seed,day.day,count,day.onTime,count-day.onTime,day.lastSeconds===null?'':clock(day.lastSeconds),decimal(r.mean),decimal(r.p95)]);
    }
    if(name==='Исходный As-is'&&load===320)assert.deepEqual([r.openedStart,r.openedMid,r.openedEnd],[26,404,782]);
    if(name==='Ручной сценарий с окном выпуска'&&lastStart===83100){
      assert(r.days.every(day=>day.onTime===64&&day.lastSeconds===86281));
      assert.equal(r.undrained,0);assert.equal(r.checks,9600);
    }
    console.log(name,load,clock(release),seed,'суток закрыто',r.days.filter(day=>day.onTime===count).length,'среднее',decimal(r.mean),'P95',decimal(r.p95));
  }
}
fs.writeFileSync(output,'\uFEFF'+rows.map(row=>row.join(';')).join('\r\n')+'\r\n','utf8');
