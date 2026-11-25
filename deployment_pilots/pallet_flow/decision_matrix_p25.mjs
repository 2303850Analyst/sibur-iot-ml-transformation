import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import assert from 'node:assert/strict';

const DAY=86400, N=5, WARM=DAY, END=31*DAY, LIMIT=END+3*DAY;
const SEEDS=[241126,241127,241128];
const FEED=0, QC_TRIP=1, STORE=2, REGISTER=3, CLOSE=4;
const at=(a,b)=>a.time-b.time || a.id-b.id || a.type-b.type;

class Heap {
  constructor(){this.a=[];}
  push(event){
    const a=this.a; let i=a.length; a.push(event);
    while(i){const p=(i-1)>>1;if(at(a[p],event)<=0)break;a[i]=a[p];i=p;}
    a[i]=event;
  }
  pop(){
    const a=this.a, first=a[0], last=a.pop();
    if(a.length){let i=0;
      while(i*2+1<a.length){let c=i*2+1;if(c+1<a.length&&at(a[c+1],a[c])<0)c++;if(at(last,a[c])<=0)break;a[i]=a[c];i=c;}
      a[i]=last;
    }return first;
  }
}
class Queue {
  constructor(){this.arr=[];this.head=0;}
  add(x){this.arr.push(x);}
  get first(){return this.arr[this.head];}
  take(){const x=this.arr[this.head++];if(this.head>2048&&this.head*2>this.arr.length){this.arr=this.arr.slice(this.head);this.head=0;}return x;}
}
function rand01(i,seed){
  let h=Math.imul(i+1,0x9e3779b1)^Math.imul(seed,0x85ebca6b);
  h=Math.imul(h^(h>>>16),0x85ebca6b);
  h=Math.imul(h^(h>>>13),0xc2b2ae35);
  return ((h^(h>>>16))>>>0)/4294967296;
}
const manifestCache=new Map();
function manifest(load,seed,isolated,lastStart){
  const key=[load,seed,isolated,lastStart].join('-');
  if(manifestCache.has(key))return manifestCache.get(key);
  const cases=isolated?1:31*load/N, gap=DAY/(load/N);
  const groupsPerDay=load/N;
  const caseStart=c=>isolated?WARM:lastStart==null?c*gap:
    Math.floor(c/groupsPerDay)*DAY+Math.round((c%groupsPerDay)*lastStart/(groupsPerDay-1));
  const releases=[], flags=new Uint8Array(cases*N);
  for(let c=0;c<cases;c++)for(let j=0;j<N;j++){
    const id=c*N+j; releases.push({id,time:caseStart(c)+120*j,type:0});
    flags[id]=isolated?0:Number(rand01(id,seed)<.01);
  }
  releases.sort(at);const payload={cases,caseStart,releases,flags};manifestCache.set(key,payload);return payload;
}
// One resource per eligible person or robot. Dedicated assets are preferred at equal readiness.
export function simulate(cfg,load,seed,{isolated=false,lastStart=null,documentDesk=false,dailyStats=false,manualTimes={}}={}){
  const {cases,caseStart,releases,flags}=manifest(load,seed,isolated,lastStart);
  const sap=cfg.contour==='SAP'; const first=isolated?0:load/N;
  const {passport=600,close:manualClose=300,putaway=300}=manualTimes;
  assert([passport,manualClose,putaway].every(seconds=>Number.isInteger(seconds)&&seconds>0));
  assert(sap||Object.keys(manualTimes).length===0);
  const groups=cases-first, total=groups*N;
  const state=new Uint8Array(cases*N), count=new Uint8Array(cases), doc=new Uint8Array(cases);
  const ends=new Float64Array(cases).fill(Infinity);
  const readyTimes=Array.from({length:5},()=>new Queue());
  const lab=new Queue(), heap=new Heap(), units=[];
  for(const [type,n] of [['feed',cfg.feed],['store',cfg.store],['robot',cfg.robots],['flex',cfg.flex]]){
    for(let i=0;i<n;i++)units.push({type,busy:false,last:-1});
  }
  let time=0, release=0, receiver=false,labs=0,placed30=0,checks=0,arrivals=0,closes=0;
  let recvSeconds=0, driverSeconds=0,robotSeconds=0,labSeconds=0,stageWait=0, waits=0;
  let nextMark=0;const marks=[WARM,16*DAY,END],backs=[];
  const documents=new Queue();
  let deskBusy=false,deskSeconds=0;
  const overlap=(a,b)=>Math.max(0,Math.min(END,b)-Math.max(WARM,a));
  function event(atTime,type,id,machine=-1){heap.push({time:atTime,type,id,machine});}
  function job(stage,id){readyTimes[stage].add({time,id,type:stage});}
  function move(id,from,to){assert.equal(state[id],from);state[id]=to;}
  function registerOrClose(){
    if(receiver)return;
    const reg=readyTimes[REGISTER].first, close=readyTimes[CLOSE].first;
    if(!reg&&!close)return;
    const isClose=close&&(!reg||at(close,reg)<0);
    const j=readyTimes[isClose?CLOSE:REGISTER].take(), c=Math.floor(j.id/N);
    if(isClose){
      receiver=true;recvSeconds+=overlap(time,time+manualClose);event(time+manualClose,8,j.id);
    }else{
      const needs=sap||flags[j.id];let seconds;
      if(sap){seconds=150+(doc[c]?0:150)+(!documentDesk&&j.id%N===N-1?passport:0);doc[c]=1;}
      else seconds=needs?25:30;
      receiver=true;recvSeconds+=overlap(time,time+(sap?seconds:20));
      event(time+seconds,3,j.id);
    }
  }
  function startLab(){
    while(labs<3&&lab.first){
      const {id}=lab.take();labs++;labSeconds+=overlap(time,time+600);event(time+600,5,id);
    }
  }
  function startDocuments(){
    if(deskBusy||!documents.first)return;
    const {id,isClose}=documents.take(), seconds=isClose?manualClose:passport;
    deskBusy=true;deskSeconds+=overlap(time,time+seconds);
    event(time+seconds,isClose?11:10,id);
  }
  function available(u,stage){
    return u.type==='robot'||u.type==='flex'||(stage===STORE?u.type==='store':u.type==='feed');
  }
  function select(stage){
    let best=-1,rank=Infinity;
    for(let i=0;i<units.length;i++){
      const u=units[i];if(u.busy||!available(u,stage))continue;
      const leg=(stage===STORE?1:0), sw=u.type==='flex'&&u.last!==-1&&u.last!==leg;
      const priority=(sw?10:0)+(u.type==='flex'?2:u.type==='robot'?1:0);
      if(priority<rank){best=i;rank=priority;}
    }return best;
  }
  function dispatch(){
    for(;;){
      let next=null,chosen=-1;
      for(let stage=FEED;stage<=STORE;stage++){
        const j=readyTimes[stage].first;
        if(j&&(!next||at(j,next)<0)){const k=select(stage);if(k>=0){next=j;chosen=k;}}
      }
      if(!next)return;
      const {type:stage,id,time:queued}=readyTimes[next.type].take();
      const u=units[chosen];const leg=Number(stage===STORE);
      const change=u.type==='flex'&&u.last!==-1&&u.last!==leg?60:0;
      const dur=change+(stage===FEED?120:stage===QC_TRIP?300:u.type==='robot'?240:putaway);
      u.last=leg;u.busy=true;
      if(u.type==='robot')robotSeconds+=overlap(time,time+dur);else driverSeconds+=overlap(time,time+dur);
      if(id>=first*N&&stage===STORE){stageWait+=time-queued;waits++;}
      event(time+dur,[1,2,4][stage],id,chosen);
    }
  }
  while(heap.a.length||release<releases.length){
    time=Math.min(heap.a[0]?.time??Infinity,releases[release]?.time??Infinity);
    while(nextMark<marks.length&&marks[nextMark]<=time){backs.push(arrivals-closes);nextMark++;}
    if(time>LIMIT)break;
    while(release<releases.length&&releases[release].time===time){
      const {id}=releases[release++];if(id%N===0)arrivals++;
      job(FEED,id);
    }
    while(heap.a.length&&heap.a[0].time===time){
      const e=heap.pop(),id=e.id,c=Math.floor(id/N);
      if(e.type===1){units[e.machine].busy=false;move(id,0,1);job(REGISTER,id);}
      else if(e.type===2){units[e.machine].busy=false;move(id,3,4);lab.add({id});}
      else if(e.type===3){
        receiver=false;
        if(documentDesk&&sap&&id%N===N-1){documents.add({id,isClose:false});}
        else if(sap||flags[id]){move(id,1,3);if(c>=first)checks++;job(QC_TRIP,id);}
        else {move(id,1,2);job(STORE,id);}
      }else if(e.type===4){
        units[e.machine].busy=false;move(id,2,5);count[c]++;
        if(c>=first&&time<END)placed30++;
        if(count[c]===N){
          if(sap&&documentDesk)documents.add({id,isClose:true});
          else if(sap)job(CLOSE,id);else event(time+30,9,id);
        }
      }else if(e.type===5){
        labs--;move(id,4,6);event(time+(sap?0:5),6,id);
      }else if(e.type===6){move(id,6,2);job(STORE,id);}
      else if(e.type===10){
        deskBusy=false;move(id,1,3);if(c>=first)checks++;job(QC_TRIP,id);
      }
      else if(e.type===8||e.type===9||e.type===11){
        assert.equal(count[c],N);assert(!Number.isFinite(ends[c]));
        ends[c]=time;closes++;if(e.type===8)receiver=false;if(e.type===11)deskBusy=false;
      }
    }
    registerOrClose();if(documentDesk)startDocuments();startLab();dispatch();
  }
  while(nextMark++<marks.length)backs.push(arrivals-closes);
  assert.equal(backs.length,3);
  const lead=[];let month=0,late=0;
  for(let c=first;c<cases;c++)if(Number.isFinite(ends[c])){
    const dt=ends[c]-caseStart(c);lead.push(dt);if(ends[c]<END)month++;if(dt>3600)late++;
  }
  lead.sort((a,b)=>a-b);
  const rateThreshold=Math.ceil(load/(N*24));
  const stable=lead.length===groups&&Math.max(backs[2]-backs[0],backs[2]-backs[1])<=rateThreshold;
  if(lead.length===groups)assert.equal(checks,sap?total:flags.subarray(first*N).reduce((a,b)=>a+b,0));
  const p95=lead.length?lead[Math.ceil(.95*lead.length)-1]/60:null;
  const result={groups,closed:lead.length,month,residue:groups-month,undrained:groups-lead.length,placed30,
    openedStart:backs[0],openedMid:backs[1],openedEnd:backs[2],stable,checks,
    mean:lead.length?lead.reduce((a,b)=>a+b,0)/lead.length/60:null,p95,
    over60:late,driverH:driverSeconds/3600,robotH:robotSeconds/3600,
    receiverH:recvSeconds/3600,labH:labSeconds/3600,waitRack:waits?stageWait/waits/60:null};
  if(dailyStats){
    assert(!isolated);result.deskH=deskSeconds/3600;
    result.days=Array.from({length:30},(_,k)=>{
      const start=(k+1)*DAY, deadline=start+DAY, offset=(k+1)*load/N;
      const cohort=ends.subarray(offset,offset+load/N);
      let onTime=0,last=0;
      for(const t of cohort){if(t<=deadline)onTime++;last=Math.max(last,t);}
      return {day:k+1,onTime,lastSeconds:Number.isFinite(last)?last-start:null};
    });
  }
  return result;
}
const [input,output,loadText]=process.argv.slice(2);
if(input&&output&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const matrix=fs.readFileSync(input,'utf8').trim().split(/\r?\n/).slice(1).map(line=>{
    const [code,contour,z,f,r,x]=line.split('\t');return {code,contour,robots:+z,feed:+f,store:+r,flex:+x};
  });
  assert.equal(matrix.length,148);assert.equal(new Set(matrix.map(c=>c.code)).size,148);
  const baseline={code:'As-is',contour:'SAP',robots:0,feed:2,store:1,flex:0};
  const clean=simulate(baseline,320,241126,{isolated:true});
  const cleanRobot=simulate({contour:'Ex',robots:2,feed:1,store:0,flex:0},320,241126,{isolated:true});
  assert.equal(clean.mean,52);assert.equal(cleanRobot.mean,15);
  const load=Number(loadText), rows=[];
  for(const cfg of [baseline,...matrix]){
    for(const seed of SEEDS)rows.push({code:cfg.code,load,seed,profile:'main',...simulate(cfg,load,seed)});
  }
  fs.writeFileSync(output,JSON.stringify(rows));console.log('load',load,'runs',rows.length);
}
