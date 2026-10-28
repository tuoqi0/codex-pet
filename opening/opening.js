(()=>{'use strict';
const seed=document.getElementById('codex-pet-opening');
if(!seed)return;
if(!document.querySelector('meta[name="codex-pet-sidebar"]')){seed.remove();return}
const sprite=new Image();
sprite.src=new URL('./cp-dragon.png',document.currentScript.src).href;
const spriteReady=new Promise(resolve=>{
 const deadline=setTimeout(resolve,8000);
 sprite.decode().then(()=>{clearTimeout(deadline);resolve()},()=>{clearTimeout(deadline);resolve()});
});
const template=seed.cloneNode(true);let current=null,finishTimer=null,removeTimer=null,frameTimer=null;seed.remove();
function start(){
 clearTimeout(finishTimer);clearTimeout(removeTimer);clearInterval(frameTimer);
 if(current)current.remove();
 const opening=template.cloneNode(true);current=opening;opening.classList.add('cp-loading');document.body.appendChild(opening);opening.style.display='grid';opening.addEventListener('click',end,{once:true});
 spriteReady.then(()=>{
  if(current!==opening||opening.classList.contains('cp-fade'))return;
  opening.classList.remove('cp-loading');
  const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const portrait=opening.querySelector('.cp-portrait');const started=performance.now();
  function frame(){const i=Math.min(31,Math.floor((performance.now()-started)/100));portrait.style.backgroundPosition=`${(i%8)*100/7}% ${Math.floor(i/8)*100/3}%`}
  frame();if(!reduced)frameTimer=setInterval(frame,50);finishTimer=setTimeout(end,reduced?300:3300);
 });
}
function end(){if(!current||current.classList.contains('cp-fade'))return;clearTimeout(finishTimer);clearInterval(frameTimer);current.classList.add('cp-fade');const old=current;removeTimer=setTimeout(()=>{old.remove();if(current===old)current=null},500)}
window.addEventListener('keydown',e=>{if(e.key==='Escape')end()});window.addEventListener('message',e=>{if(e.data&&e.data.type==='codex-pet-opening-show')start()});start();
})();
