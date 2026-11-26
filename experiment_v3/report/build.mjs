import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';
const here=process.argv[2] || path.dirname(fileURLToPath(import.meta.url));
const data=JSON.parse(await fs.readFile(path.join(here,'tables.json'),'utf8'));
const wb=Workbook.create();
function table(name,headers,rows,widths) {
  const sh=wb.worksheets.add(name);
  const v=[headers,...rows];
  sh.getRangeByIndexes(0,0,v.length,headers.length).values=v;
  const header=sh.getRangeByIndexes(0,0,1,headers.length);
  header.format.fill='#174653';header.format.font.color='#ffffff';header.format.font.bold=true;
  header.format.rowHeight=37; header.format.wrapText=true;
  widths.forEach((w,i)=>{sh.getRangeByIndexes(0,i,v.length,1).format.columnWidth=w;});
  sh.freezePanes.freezeRows(1);
  return sh;
}
const rows=data.comparisons.map(r=>['Технический пилот',r.name,r.scenario,r.load,r.seed,r.metric,r.unit,r.cases,r.before,r.after,null,null,'Одна пара, ДИ отсутствует']);
const s=table('Свод пилота',['Статус','Функции','Сценарий','Нагрузка','Seed','Показатель','Единица','Поступлений','C000','Вариант','Разница','Изменение, %','Ограничение'],rows,[20,64,12,12,10,50,24,15,16,16,16,20,32]);
for(let i=2;i<=rows.length+1;i++){
  s.getRange('K'+i).formulas=[['=J'+i+'-I'+i]];
  s.getRange('L'+i).formulas=[['=IF(I'+i+'=0,"",K'+i+'/I'+i+')']];
}
if(rows.length){s.getRange('I2:K'+(rows.length+1)).setNumberFormat('0.00');s.getRange('L2:L'+(rows.length+1)).setNumberFormat('0.00%');}
table('Основная серия',['Контур','План','Выполнено','Осталось'],Object.entries(data.missing).map(([k,v])=>[k,v.planned,v.planned-v.missing,v.missing]),[30,16,16,16]);
table('Odoo, пилот',['Сценарий','Нагрузка','Маска','Пользователей','Предложено','Закрыто за 180 с','Обращений'],data.live.map(t=>[t.scenario,t.load,t.mask,t.session_count,t.offered,t.success_180s,t.requests]),[15,15,12,20,18,23,19]);
table('Аудит пилота',['Прогон','Маска','Поступлений','Событий проверено','Максимум в буфере','Замечаний','Область'],data.audit,[45,14,18,24,25,18,28]);
table('Конфигурации',['Маска','Включённые функции'],data.configurations,[16,110]);
const book=await SpreadsheetFile.exportXlsx(wb);
await book.save(path.join(here,'UNIFIED_COMPARISON.xlsx'));

