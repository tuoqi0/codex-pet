const assert=require('assert');const fs=require('fs');const vm=require('vm');
const source=fs.readFileSync(process.argv[2] || require('path').join(__dirname,'../opening/opening.js'),'utf8');
function harness(reduced=false){
 let time=0,id=0,timers=new Map(),events={},decodeResolve,decodeReject,instances=[];
 const defer=new Promise((a,b)=>{decodeResolve=a;decodeReject=b});
 function schedule(fn,ms,repeat){const key=++id;timers.set(key,{fn,at:time+ms,ms,repeat});return key}
 function clear(key){timers.delete(key)}
 function advance(ms){let end=time+ms;while(true){let n=[...timers].filter(([,x])=>x.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!n)break;const [key,item]=n;time=item.at;if(item.repeat)item.at+=item.ms;else timers.delete(key);item.fn()}time=end}
 class Elem{constructor(){this.classes=new Set();this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),contains:x=>this.classes.has(x)};this.style={};this.portrait={style:{}};this.ev={};this.removed=false}cloneNode(){const e=new Elem();instances.push(e);return e}remove(){this.removed=true}addEventListener(n,fn){this.ev[n]=fn}querySelector(){return this.portrait}}
 let seed=new Elem();const document={getElementById:()=>seed,querySelector:()=>true,currentScript:{src:'https://local/assets/cp-opening.js'},body:{appendChild(){}}};
 const window={matchMedia:()=>({matches:reduced}),addEventListener:(k,fn)=>events[k]=fn};
 const context={document,window,URL,performance:{now:()=>time},Image:class{decode(){return defer}},setTimeout:(f,m)=>schedule(f,m,false),setInterval:(f,m)=>schedule(f,m,true),clearTimeout:clear,clearInterval:clear};vm.runInNewContext(source,context);
 return {advance,instances,events,decodeResolve,decodeReject,timers,async flush(){for(let i=0;i<5;i++)await Promise.resolve()},get current(){return instances.at(-1)}}
}
(async()=>{
 {const h=harness();assert(h.current.classes.has('cp-loading'));h.advance(2000);assert(!h.current.classes.has('cp-fade'));h.decodeResolve();await h.flush();assert(!h.current.classes.has('cp-loading'));h.advance(1800);assert.notEqual(h.current.portrait.style.backgroundPosition,'0% 0%');h.advance(1500);assert(h.current.classes.has('cp-fade'));h.advance(500);assert(h.current.removed)}
 {const h=harness();h.decodeReject(Error('decode failed'));await h.flush();assert(!h.current.classes.has('cp-loading'));h.advance(3300);assert(h.current.classes.has('cp-fade'))}
 {const h=harness();h.advance(8000);await h.flush();assert(!h.current.classes.has('cp-loading'));h.advance(3300);assert(h.current.classes.has('cp-fade'))}
 {const h=harness();let old=h.current;old.ev.click();h.decodeResolve();await h.flush();assert(old.classes.has('cp-fade'));h.advance(500);assert(old.removed)}
 {const h=harness();let old=h.current;h.events.keydown({key:'Escape'});h.decodeResolve();await h.flush();assert(old.classes.has('cp-fade'));h.advance(500);assert(old.removed)}
 {const h=harness();let old=h.current;h.events.message({data:{type:'codex-pet-opening-show'}});let now=h.current;assert(old.removed);h.decodeResolve();await h.flush();assert(old.classes.has('cp-loading'));assert(!now.classes.has('cp-loading'));h.advance(3300);assert(now.classes.has('cp-fade'))}
 {const h=harness(true);h.decodeResolve();await h.flush();assert.equal(h.current.portrait.style.backgroundPosition,'0% 0%');h.advance(300);assert(h.current.classes.has('cp-fade'));assert.equal(h.current.portrait.style.backgroundPosition,'0% 0%')}
 console.log('7 opening async behavior tests passed');
})().catch(e=>{console.error(e);process.exitCode=1});
