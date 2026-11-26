import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import {Workbook,SpreadsheetFile} from '@oai/artifact-tool';
const dir=path.dirname(fileURLToPath(import.meta.url));
const [matrixFile,outFile]=process.argv.slice(2);
const rows=(await fs.readFile(matrixFile,'utf8')).trim().split(/\r?\n/).slice(1);
const num=s=>Number(s.replace(',','.').replace('(','-').replace(')',''));
const configs=rows.map(l=>{const [code,type,...v]=l.split('\t');const [z,f,s,x,paid,h,cap,op,sal,loss]=v.map(num);return {code,type,z,f,s,x,paid,h,cap,op,sal,loss};});
const byCode=new Map(configs.map((o,i)=>[o.code,i+5]));
const loads=[320,640,960], data={}, group={}, base={}, packList=[];
const avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
const pvCost=[1,2,3,4,5].reduce((s,t)=>s+1.07**(t-1)/1.2591**t,0),pvLoss=pvCost-.2/1.2591;
for(const l of loads){
 data[l]=JSON.parse(await fs.readFile(path.join(dir,'flow'+l+'.json')));
 const map=new Map();
 for(const r of data[l]){if(!map.has(r.code))map.set(r.code,[]);map.get(r.code).push(r);}
 base[l]=map.get('As-is');
 group[l]=configs.map(cfg=>{
   const main=map.get(cfg.code);assert.equal(main.length,3);
   const worst=Math.max(...main.map(r=>r.p95??Infinity)), stable=main.every(r=>r.stable);
   const npv=-cfg.cap+(cfg.sal-cfg.op)*pvCost+cfg.loss*pvLoss;
   return {cfg, main, worst,stable,npv,load:l};
 }).sort((a,b)=>a.worst-b.worst||a.cfg.code.localeCompare(b.cfg.code,'en'));
 packList.push(...group[l]);
}
const wb=Workbook.create();
const names=['Выбор парка','Матрица 444','Комплектации','Допущения','Прогоны'];
const [sum,mat,fleet,ass,raw]=names.map(n=>wb.worksheets.add(n));
const ncol=i=>{let s='';for(;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;};
const put=(s,r,c,v)=>{s.getRangeByIndexes(r-1,c-1,v.length,v[0].length).values=v};
const ff=(s,r,c,v)=>{s.getRangeByIndexes(r-1,c-1,v.length,v[0].length).formulas=v};
const val=(s,r,c,v)=>put(s,r,c,[[v]]);
const dec='#,##0.00;(#,##0.00);0.00';
const timeFmt='#,##0.0;(#,##0.0);0.0';
const pct='0.0%';
const title=(s,t)=>{s.showGridLines=false;val(s,2,1,t);const f=s.getRange('A2').format;f.font={name:'Arial',size:16,bold:true,color:'#14364D'};f.rowHeight=29;};
function header(s,row,labels){
 put(s,row,1,[labels]);const z=s.getRangeByIndexes(row-1,0,1,labels.length);
 z.format.fill='#20465E';z.format.font={name:'Arial',bold:true,color:'#FFFFFF',size:10};
 z.format.rowHeight=44;z.format.wrapText=true;z.format.verticalAlignment='center';z.format.horizontalAlignment='center';
}
function widths(s,items){items.forEach((w,i)=>{s.getRange(ncol(i+1)+'1').format.columnWidth=w;});}
function table(s,row,last,cols,name){const t=s.tables.add('A'+row+':'+ncol(cols)+last,true,name);t.style='TableStyleMedium2';s.freezePanes.freezeRows(row);}
function headerStyle(s,row,cols){const z=s.getRangeByIndexes(row-1,0,1,cols);z.format.fill='#20465E';z.format.font={name:'Arial',bold:true,color:'#FFFFFF',size:10};z.format.wrapText=true;z.format.rowHeight=46;z.format.verticalAlignment='center';}
function sub(s,r,t){val(s,r,1,t);s.getRange('A'+r).format.font={bold:true,name:'Arial',size:13,color:'#146456'};}
title(ass,'Вводные для сопоставления вариантов');
header(ass,4,['Показатель','Значение','Источник и использование']);
const assumptions=[
['Номинальная ставка дисконта',.2591,'Финансовые условия проекта, книга «Zhengdu - смены, тариф и NPV». Платежи в конце года.'],
['Годовая индексация',.07,'Со второго года для тарифа, предотвращённого ущерба и эксплуатации.'],
['Освоение эффекта потерь в год 1',.8,'Финансовые условия проекта. Зарплатный эффект и эксплуатация действуют весь первый год.'],
['Доля изменения ФОТ в тарифе',1,'Полная передача изменения платы инвестору. При неизменном тарифе поставить 0.'],
['Доля эффекта потерь у инвестора',1,'Учитывать только собственный невозмещённый ущерб. При полном возмещении поставить 0.'],
['Доля выгод в годы 4 и 5',1,'Модель предполагает продление условий подрядчика. OPEX начисляется все пять лет.'],
['Потери, млн ₽ за полный год при 320',1.36,'Присланная матрица для IoT и Ex. Для SAP эффект равен нулю.'],
['Эластичность потерь по объёму',0,'0 сохраняет 1,36 млн ₽ на каждом потоке. 1 масштабирует пропорционально объёму.'],
['Базовый поток, паллет/сут',320,'Объём, для которого задан денежный эффект по продукции.'],
['Оплаченные часы водителей в базе за год',22200,'Восстановлено из всех строк вставки: снятые часы + часы оплаченного нового штата.'],
['Один круглосуточный пост, ч/год',8760,'365 дней × 24 часа. Резерв учитывается в оплате даже при отсутствии регулярных заданий.'],
['Час водителя с начислениями, ₽',569.5740365111562,'Проектная оценка Перми: 72 000 × 12 × 1,30 / 1 972. Округление эффекта до 0,01 млн ₽ как во вставке.'],
['Цифровая приёмка IoT, млн ₽',.85,'Комплект сверх существующих SAP и ТСД. По присланной матрице.'],
['Пост и интеграция Ex сверх IoT, млн ₽',.78,'Общая часть Ex за вычетом 0,85 млн ₽ на IoT. Разбивка получена из цен комплектаций.'],
['Один ричтрак Zhengdu Ex, млн ₽',6.99,'Сметная цена из приращения соседних строк матрицы. Предложение на поставку в Пермь не представлено.'],
['IoT, годовое содержание, млн ₽',.02,'Присланная матрица. Для SAP с имеющимися ТСД дополнительные инвестиции и OPEX нулевые.'],
['Ex, постоянное содержание сверх IoT, млн ₽/год',.08,'Разница составов из присланной матрицы.'],
['Один Zhengdu, содержание, млн ₽/год',.09,'Дельта эксплуатационных затрат на следующую машину.'],
['Дополнительный ориентир P95, мин',60,'Используется только для отдельного выбора с ограничением времени.'],
];
put(ass,5,1,assumptions);ass.getRange('B5:B10').setNumberFormat(pct);ass.getRange('B11:B22').setNumberFormat(dec);ass.getRange('B5:B23').format.font={name:'Arial',color:'#205493'};ass.getRange('B5:B23').format.fill='#FFF3D8';
header(ass,26,['Год','Индекс цен','Дисконт-фактор','Доля выгод','Освоение потерь']);
for(let k=0;k<5;k++){const i=27+k;val(ass,i,1,k+1);ff(ass,i,2,[[`=(1+$B$6)^(A${i}-1)`,`=1/(1+$B$5)^A${i}`,`=IF(A${i}<=3,1,$B$10)`,`=IF(A${i}=1,$B$7,1)`]]);}
ass.getRange('B27:E31').setNumberFormat('0.0000');
val(ass,33,1,'PV полной оплаты / тарифа');ff(ass,33,2,[['=SUMPRODUCT(B27:B31,C27:C31,D27:D31)']]);
val(ass,34,1,'PV эффекта потерь');ff(ass,34,2,[['=SUMPRODUCT(B27:B31,C27:C31,D27:D31,E27:E31)']]);ass.getRange('B33:B34').setNumberFormat('0.0000');
sub(ass,36,'Метод расчёта');
const notes=[
['Процесс','Правила сравнения As-is и To-be, редакция 5, P01–P25. Файл «Парк приёмки ПСВ - 148 вариантов» использован только как образец структуры отчёта.'],
['Исходные деньги','Вставленная таблица содержит 148 кодов, число оплаченных водителей, часы, инвестиции, содержание, эффект оплаты и потерь. Стоимость комплекта одинакова для всех строк одного состава.'],
['Поток','5 паллет в поставке. Соседние паллеты выходят через 120 с. Начала поставок через 1 350 / 675 / 450 с соответственно. Вход круглосуточный, равномерный.'],
['Отсчёт','Сутки прогрева, затем 30 суток выпуска. После месяца до 72 ч на завершение. Когорта определяется по выпуску первой паллеты.'],
['Контур SAP','Ручное чтение ТСД, 150 с на паллету + 150 с на документ и 600 с на одно уточнение паспорта. Контроль каждой паллеты. Итоговая сверка приёмщиком 300 с.'],
['Контуры IoT / Ex','Предварительные записи готовы. На посту 30 с, включая параллельный осмотр 20 с. При направлении на ОТК 25 с на входе и 5 с на итоговый статус, отдельный рейс 300 с и проверка 600 с.'],
['Исключения','Индивидуальный отбор 1% по одинаковым номерам паллет для всех цифровых парков одного повтора. Все отобранные паллеты годны. Групповой отказ паспорта, брак и повторное чтение отключены.'],
['Ресурсы','Один приёмщик, один последовательный входной пост, три места ОТК. Для SAP закрытие также занимает приёмщика. Для IoT и Ex SAP закрывает поставку за 30 с.'],
['Водители и техника','Подвоз от фасовки 120 с. К ОТК ездят подвозчики, универсалы и роботы. На укладке водитель 300 с, Zhengdu 240 с. Одновременно одна машина перевозит одну паллету.'],
['Назначение заданий','Сначала самое раннее готовое совместимое задание. При равенстве стартов предпочтение специализированному водителю, затем роботу, затем универсалу. Универсал тратит 60 с при смене вида техники.'],
['Оплаченный резерв','Если в коде нет ни одного водителя, но по матрице оплачивается один, это дежурный резерв. Для плановых рейсов он недоступен.'],
['Доступность техники','По завершении рейса машина сразу может принять следующее совместимое задание. Время перевозки, укладки и ожидания свободных ресурсов входит в длительность поставки.'],
['Границы испытания','Площадь буфера, блокировки общих проходов, перерывы, поломки и конкуренция с отгрузкой отдельно не моделируются. Остаток описывает расчётную потребность, а не ёмкость реального буфера.'],
['Устойчивость','В каждом из трёх повторов все поставки должны закрыться за дополнительный период. Прирост открытых поставок к концу относительно старта и середины не превышает одного часа входа, округлённого вверх.'],
['Время на листе матрицы','Среднее по закрытым поставкам, взвешенное по их числу в трёх повторах. P95 - худший из трёх месячных P95 (метод ближайшего ранга). Для перегрузки время закрытых поставок смещено.'],
['Остаток','Незакрытая к концу месяца поставка может находиться в обычной обработке. На листе прогонов есть отдельный остаток после 72 ч и динамика общей очереди.'],
['Как считать NPV','Номинальный рублёвый поток до налога. CAPEX в момент 0. Зарплата, потери и OPEX показаны отдельно. Ликвидационная стоимость, финансирование и доход от дополнительных продаж не заданы.'],
['Сопоставимость базы','Операционный As-is выделяет двух водителей на подвоз и одного на штабелер. Договорная база 22 200 часов/год взята из вложения; три круглосуточных поста не считаются фактическим ФОТ.'],
['Сравнимый объём','Верхний ручной As-is с одним приёмщиком перегружен уже при 320. NPV является оценкой при указанной скидке подрядчика и обеспечении сравнимого объёма; деньги на расширение ручной базы не начислены.'],
['Рост потока','По умолчанию ФОТ и годовые потери из матрицы фиксированы при ×2 и ×3. Отдельная чувствительность показывает рост потерь пропорционально потоку. Тариф и потери требуют подтверждения.'],
['Исполнение Ex','Цена закупки, монтаж, взрывозащита всей комплектации и работа на требуемых ярусах требуют документального подтверждения площадки и поставщика.'],
['Проверка выбора','Максимум NPV среди устойчивых в трёх повторах. При равном NPV выбирается меньший худший P95, затем код. Часовой ориентир и результат без потерь показаны отдельно.']
];
notes.forEach((r,k)=>{val(ass,37+k,1,r[0]);ass.getRange('B'+(37+k)+':F'+(37+k)).merge();val(ass,37+k,2,r[1]);});
ass.getRange('B37:F'+(36+notes.length)).format.wrapText=true;ass.getRange('A37:F'+(36+notes.length)).format.rowHeight=33;ass.getRange('C5:C23').format.wrapText=true;ass.getRange('A5:C23').format.rowHeight=31;widths(ass,[49,19,76,14,17,8]);
title(fleet,'Комплектации и единая смета');header(fleet,4,['Код парка','Контур','Zhengdu, шт.','Только подвоз','Только укладка','Два допуска','Оплачено в смену','Снято ч/год','Оплата с взносами, млн ₽/год','CAPEX, млн ₽','OPEX, млн ₽/год','Потери при 320, млн ₽/год']);
put(fleet,5,1,configs.map(c=>[c.code,c.type,c.z,c.f,c.s,c.x,c.paid]));
const a="'Допущения'!", fl="'Комплектации'!", rr="'Прогоны'!", mm="'Матрица 444'!";
ff(fleet,5,8,configs.map((c,k)=>{const i=k+5;return [`=${a}$B$14-G${i}*${a}$B$15`,`=ROUND(H${i}*${a}$B$16/1000000,2)`,`=IF(B${i}="SAP",0,${a}$B$17+IF(C${i}>0,${a}$B$18+C${i}*${a}$B$19,0))`,`=IF(B${i}="SAP",0,${a}$B$20+IF(C${i}>0,${a}$B$21+C${i}*${a}$B$22,0))`,`=IF(B${i}="SAP",0,${a}$B$11)`]}));
table(fleet,4,152,12,'FleetCosts');headerStyle(fleet,4,12);fleet.getRange('I5:L152').setNumberFormat(dec);widths(fleet,[21,11,12,12,13,12,13,16,18,16,16,16]);
title(raw,'Результаты независимых прогонов');header(raw,4,['Код парка','Паллет/сут','Номер потока','Режим','Поступило поставок','Закрыто за месяц','Закрыто с +72 ч','Остаток месяца','Остаток +72 ч','Открыто в начале¹','В середине¹','В конце¹','Устойчив, 1/0','Среднее P25, мин','P95, мин','Проверок ОТК²','Водители, активн. ч/мес','Роботы, активн. ч/мес','Приёмщик, ч/мес','Места ОТК, ч/мес','Ожидание укладки, мин','Паллет с P20 за месяц','Закрытых позже 60 мин']);
const rawRows=[];
function addRaw(r){
 rawRows.push([r.code,r.load,r.seed,'Основной',r.groups,r.month,r.closed,r.residue,r.undrained,r.openedStart,r.openedMid,r.openedEnd,+r.stable,r.mean,r.p95,r.checks,r.driverH,r.robotH,r.receiverH,r.labH,r.waitRack,r.placed30,r.over60]);return rawRows.length+4;
}
for(const p of packList){p.rawStart=addRaw(p.main[0]);addRaw(p.main[1]);p.rawEnd=addRaw(p.main[2]);}
const baseStart={};for(const l of loads)baseStart[l]=addRaw(base[l][0]),addRaw(base[l][1]),addRaw(base[l][2]);
put(raw,5,1,rawRows);table(raw,4,4+rawRows.length,23,'SimulationRuns');headerStyle(raw,4,23);
raw.getRange('N5:O'+(4+rawRows.length)).setNumberFormat(timeFmt);raw.getRange('Q5:U'+(4+rawRows.length)).setNumberFormat(timeFmt);
widths(raw,[20,12,15,15,13,13,13,12,12,13,12,12,12,15,13,13,17,16,15,15,15,15,15]);
val(raw,3,1,'¹ Все открытые поставки на границе, включая прогрев.  ² Число направлений из когорты, оформленных до конца наблюдения.');
title(mat,'Операции и экономика · 444 сочетания');val(mat,3,1,'Деньги в млн ₽. Устойчивость требует 3 из 3 повторов. Срок P25 в минутах. Пустой NPV допуска означает, что плановый объём не подтверждён.');
const headers=['Паллет/сут','Код парка','Контур','Устойчивых из 3','Среднее P25, мин','Худший P95, мин','Остаток месяца, макс.','Остаток +72 ч, макс.','Направлений ОТК, ср.','Водители, активн. ч/мес','Роботы, активн. ч/мес','CAPEX, млн ₽','Снято ч/год','Экономия оплаты, млн ₽/год','Ущерб предотвращён, млн ₽/год','OPEX, млн ₽/год','Поток год 1','Поток год 2','Поток год 3','Поток год 4','Поток год 5','NPV плана, 5 лет','NPV при допуске','NPV без потерь¹','NPV потерь × объём¹','NPV при P95 ≤ цели','Порог потерь, млн ₽/год²','Ожидание укладки, мин','Поставок дольше часа, %','NPV без роботов при допуске'];
header(mat,4,headers);
put(mat,5,1,packList.map(p=>[p.load,p.cfg.code,p.cfg.type]));
const formulas=packList.map((p,k)=>{
 const i=k+5, lo=p.rawStart,hi=p.rawEnd,c=byCode.get(p.cfg.code);const q=col=>rr+col+lo+':'+col+hi;
 const f=[`=SUM(${q('M')})`,`=IFERROR(SUMPRODUCT(${q('N')},${q('G')})/SUM(${q('G')}),"")`,`=MAX(${q('O')})`,`=MAX(${q('H')})`,`=MAX(${q('I')})`,`=AVERAGE(${q('P')})`,`=AVERAGE(${q('Q')})`,`=AVERAGE(${q('R')})`,`=${fl}J${c}`,`=${fl}H${c}`,`=${fl}I${c}*${a}$B$8`,`=${fl}L${c}*(A${i}/${a}$B$13)^${a}$B$12*${a}$B$9`,`=${fl}K${c}`];
 for(let j=27;j<=31;j++)f.push(`=((N${i}+O${i}*${a}$E$${j})*${a}$D$${j}-P${i})*${a}$B$${j}`);
 f.push(`=NPV(${a}$B$5,Q${i}:U${i})-L${i}`,`=IF(D${i}=3,V${i},"")`,`=V${i}-O${i}*${a}$B$34`,`=X${i}+${fl}L${c}*(A${i}/${a}$B$13)*${a}$B$9*${a}$B$34`,`=IF(AND(D${i}=3,F${i}<=${a}$B$23),V${i},"")`,`=IFERROR(MAX(0,-X${i}/${a}$B$34),"")`,`=AVERAGE(${q('U')})`,`=IFERROR(SUM(${q('W')})/SUM(${q('G')}),0)`);
 f.push(`=IF(AND(D${i}=3,C${i}<>"Ex"),V${i},"")`);
 return f;
});
ff(mat,5,4,formulas);table(mat,4,448,headers.length,'DecisionMatrix');headerStyle(mat,4,headers.length);
widths(mat,[13,20,11,13,17,15,15,15,15,15,15,14,14,17,17,15,15,15,15,15,15,17,17,17,17,17,17,16,15]);
mat.getRange('E5:F448').setNumberFormat(timeFmt);mat.getRange('I5:K448').setNumberFormat(timeFmt);mat.getRange('L5:L448').setNumberFormat(dec);mat.getRange('N5:AA448').setNumberFormat(dec);mat.getRange('AB5:AB448').setNumberFormat(timeFmt);mat.getRange('AD5:AD448').setNumberFormat(dec);mat.getRange('AD1').format.columnWidth=18;mat.getRange('AC5:AC448').setNumberFormat(pct);
val(mat,450,1,'¹ Финансовая чувствительность при полном выполнении заказа.  ² Минимальный годовой предотвращённый ущерб, остающийся у инвестора, для NPV ≥ 0 при действующей экономии оплаты.');
title(sum,'Склад ПСВ · выбор парка на пять лет');val(sum,3,1,'148 парков × 3 нагрузки. В каждой строке выбран максимум NPV среди вариантов, выдержавших поток во всех трёх повторах.');
header(sum,5,['Паллет/сут','Парк','Среднее P25, мин','Худший P95, мин','CAPEX, млн ₽','Снято ч/год','Экономия оплаты, млн ₽/год','Экономия потерь, млн ₽/год','OPEX, млн ₽/год','NPV, млн ₽','NPV без потерь','NPV потерь × объём','Порог потерь, млн ₽/год','Устойчиво из 148']);
sub(sum,11,'Выбор с ограничением по P95');val(sum,12,1,'Ориентир задаётся в допущениях. Для отбора используется худший P95 из трёх повторов.');
header(sum,13,['Паллет/сут','Парк','Среднее P25, мин','Худший P95, мин','CAPEX, млн ₽','Снято ч/год','Экономия оплаты, млн ₽/год','Экономия потерь, млн ₽/год','OPEX, млн ₽/год','NPV, млн ₽','NPV без потерь','NPV потерь × объём','Порог потерь, млн ₽/год','Прошли по P95']);
function summary(r,idx,key){
 const lo=5+148*idx,hi=lo+147;
 const area=c=>mm+'$'+c+'$'+lo+':$'+c+'$'+hi;
 const choice=`MATCH(MAX(${area(key)}),${area(key)},0)`;
 const look=c=>`=IF(COUNT(${area(key)})=0,"",INDEX(${area(c)},${choice}))`;
 val(sum,r,1,loads[idx]);ff(sum,r,2,[[...['B','E','F','L','M','N','O','P','V','X','Y','AA'].map(look),`=COUNT(${area(key)})`]]);
}
for(let k=0;k<3;k++){summary(6+k,k,'W');summary(14+k,k,'Z');}
for(const [lo,hi] of [[6,8],[14,16]]){
 for(const c of ['C','D'])sum.getRange(c+lo+':'+c+hi).setNumberFormat(timeFmt);
 for(const c of ['E','G','H','I','J','K','L','M'])sum.getRange(c+lo+':'+c+hi).setNumberFormat(dec);
 sum.getRange('J'+lo+':J'+hi).format.font={bold:true,color:'#155C54',size:12,name:'Arial'};
}
sub(sum,19,'Ручная база As-is');
header(sum,20,['Паллет/сут','Поступило поставок','Закрыто за месяц','Остаток месяца','Не закрыто за +72 ч','Закрыто с +72 ч']);
loads.forEach((l,k)=>{let r=21+k,j=baseStart[l];val(sum,r,1,l);ff(sum,r,2,[['E','F','H','I','G'].map(c=>'='+rr+c+j)]);});
val(sum,25,1,'База As-is дана для одного приёмщика, двух водителей на подвоз и одного на штабелере. Её значения одинаковы во всех трёх повторах.');
val(sum,27,1,'Дополнительные продажи и экономия зарплаты приёмщика в NPV не включены. Затраты водителей дают деньги, когда меняется оплата подрядчика.');
val(sum,28,1,'Ставка 25,91%, горизонт 5 лет. По умолчанию годовой эффект по продукции и договорный ФОТ одинаковы для трёх потоков; отдельный столбец проверяет пропорциональный рост потерь.');
val(sum,29,1,'В коде по порядку указаны контур, число роботов, водители на подвозе, на укладке и универсалы. Время дано в минутах.');
widths(sum,[15,22,16,16,15,15,16,16,15,16,16,18,17,16]);
sub(sum,32,'Решение без закупки роботов');
header(sum,33,['Паллет/сут','Парк','Среднее P25, мин','Худший P95, мин','CAPEX, млн ₽','Снято ч/год','Экономия оплаты, млн ₽/год','Экономия потерь, млн ₽/год','OPEX, млн ₽/год','NPV, млн ₽']);
loads.forEach((l,k)=>{
 const lo=5+148*k,hi=lo+147,row=34+k;
 val(sum,row,1,l);
 const range=c=>mm+c+lo+':'+c+hi;
 const look=col=>`=IF(COUNT(${range('AD')})=0,${col==='B'?'"Нет устойчивого"':'""'},INDEX(${range(col)},MATCH(MAX(${range('AD')}),${range('AD')},0)))`;
 ff(sum,row,2,[['B','E','F','L','M','N','O','P','V'].map(look)]);
});
sum.getRange('C34:D36').setNumberFormat(timeFmt);sum.getRange('E34:E36').setNumberFormat(dec);sum.getRange('G34:J36').setNumberFormat(dec);
sub(sum,39,'Из чего складывается NPV выбранного парка');
header(sum,40,['Паллет/сут','Код парка','PV оплаты, млн ₽','PV потерь, млн ₽','PV расходов, млн ₽','CAPEX, млн ₽','NPV, млн ₽','Ожидание укладки, мин','Поставок дольше часа, %']);
loads.forEach((l,k)=>{
 const i=41+k,s=6+k,lo=5+k*148,hi=lo+147,block=group[l];

 const rawOf=col=>`INDEX(${mm}$${col}$${lo}:$${col}$${hi},MATCH(B${i},${mm}$B$${lo}:$B$${hi},0))`;
 ff(sum,i,1,[[`=A${s}`,`=B${s}`,`=G${s}*${a}$B$33`,`=H${s}*${a}$B$34`,`=-I${s}*SUMPRODUCT(${a}$B$27:$B$31,${a}$C$27:$C$31)`,`=-E${s}`,`=SUM(C${i}:F${i})`,`=${rawOf('AB')}`,`=${rawOf('AC')}`]]);
});
sum.getRange('C41:G43').setNumberFormat(dec);sum.getRange('H41:H43').setNumberFormat(timeFmt);sum.getRange('I41:I43').setNumberFormat(pct);
val(sum,45,1,'Ожидание укладки отсчитывается от готовности задания до начала рейса. Доля поставок дольше часа посчитана по всем закрытым поставкам трёх повторов.');
const preview=await wb.render({sheetName:'Выбор парка',range:'A2:J16',scale:.95,format:'png'});await fs.writeFile(path.join(dir,'report-preview.png'),new Uint8Array(await preview.arrayBuffer()));
console.log('preview complete',JSON.stringify(sum.getRange('A6:N8').values));
const check=await wb.inspect({kind:'region',sheetId:'Выбор парка',range:'A6:J8',maxChars:1000});console.log(JSON.stringify(check).slice(0,300));
const out=await SpreadsheetFile.exportXlsx(wb);await out.save(outFile);console.log(outFile);
