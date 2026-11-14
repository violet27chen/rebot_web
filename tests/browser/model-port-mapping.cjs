/* Actual official URDF/STL loader and scene math, no GPU/hardware connections. */
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'../..');
(async()=>{const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});try{
 const context=await browser.newContext({viewport:{width:1200,height:900}}),errors=[];
 await context.route('**/*',async route=>{const p=new URL(route.request().url()).pathname;
  if(p==='/model-test')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><link rel="stylesheet" href="/static/css/style.css"></head><body><div class="app"></div></body></html>'});
  if(p.startsWith('/api/'))return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>'});
  const file=path.join(root,'frontend',p);if(!file.startsWith(path.join(root,'frontend')+path.sep)||!fs.existsSync(file))return route.abort();
  return route.fulfill({contentType:{'.js':'text/javascript','.css':'text/css','.urdf':'application/xml','.STL':'application/octet-stream'}[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
 });
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));await page.goto('http://rebot.test/model-test');
 await page.addScriptTag({path:path.join(root,'frontend/static/js/three.min.js')});
 await page.evaluate(()=>{
  THREE.WebGLRenderer=class{constructor(){}setPixelRatio(){}setSize(){}render(){}};
  window.__cfg={dual_arm:{enabled:true,left:{follower_port:'/dev/tty-left'},right:{follower_port:'/dev/tty-right'}},follower:{port:'/dev/tty-right'},monitor_layout:{mode:'coordinates'},cameras:[]};
  window.__bindings={left:{port:'/dev/tty-left',canonical_port:'/dev/tty-left'},right:{port:'/dev/tty-right',canonical_port:'/dev/tty-right'}};
  window.__configListeners=[];
  window.APP={el:id=>document.getElementById(id),q:s=>document.querySelector(s),esc:s=>s,action:fn=>fn(),saveConfig:async()=>{},
   api:{get:async p=>p==='/api/config'?__cfg:{arms:{},bindings:__bindings,binding_generation:1,time:Date.now()/1000,released:false},post:async()=>({ok:true})},
   autosave:{bind:()=>({}),onConfig:fn=>__configListeners.push(fn)}};
  window.WebSocket=class{constructor(){window.__ws=this;setTimeout(()=>{this.onopen&&this.onopen();this.onmessage&&this.onmessage({data:JSON.stringify({type:'snapshot',arms:{},bindings:__bindings,binding_generation:1,time:Date.now()/1000})});},0);}};
  window.__emit=p=>__ws.onmessage({data:JSON.stringify(p)});
  window.__change=c=>{window.__cfg=c;__configListeners.forEach(f=>f(c));};
 });
 for(const name of ['STLLoader.js','URDFLoader.js','arm3d.js'])await page.addScriptTag({path:path.join(root,'frontend/static/js',name)});
 await page.evaluate(()=>{const init=ARM3D.initWorkspace;ARM3D.initWorkspace=(...a)=>window.__world=init(...a);});
 await page.addScriptTag({path:path.join(root,'frontend/static/js/monitor.js')});await page.evaluate(()=>MONITOR.mount());
 await page.waitForFunction(()=>__world.getState('left').modelId&&__world.getState('right').modelId);
 const initial=await page.evaluate(()=>({l:__world.getState('left'),r:__world.getState('right')}));assert.notEqual(initial.l.modelId,initial.r.modelId);
 async function feedback(port,side,q,generation=1){await page.evaluate(({port,side,q,generation})=>__emit({type:'feedback',server_time:Date.now()/1000,arm:{port,side,joints:[q,0,0,0,0,0],timestamp:Date.now()/1000,binding_generation:generation}}),{port,side,q,generation});}
 await feedback('/dev/tty-left','right',.3);let state=await page.evaluate(()=>[__world.getState('left'),__world.getState('right')]);assert.ok(Math.abs(state[0].joints[0]-.3)<1e-8);assert.ok(Math.abs(state[1].joints[0])<1e-8);
 await feedback('/dev/tty-right','left',-.2);state=await page.evaluate(()=>[__world.getState('left'),__world.getState('right')]);assert.ok(Math.abs(state[0].joints[0]-.3)<1e-8);assert.ok(Math.abs(state[1].joints[0]+.2)<1e-8);
 assert.equal(await page.locator('.monitor-model-binding[data-side=left]').getAttribute('data-port'),'/dev/tty-left');
 await page.waitForFunction(()=>!document.querySelector('.monitor-model-binding[data-side=left]').hidden);
 const positions=await page.locator('.monitor-model-binding').evaluateAll(ns=>ns.map(n=>({side:n.dataset.side,x:n.getBoundingClientRect().x})));assert.ok(positions.find(p=>p.side==='left').x<positions.find(p=>p.side==='right').x);
 await page.evaluate(()=>{const c=structuredClone(__cfg);c.dual_arm.left.follower_port='/dev/tty-right';c.dual_arm.right.follower_port='/dev/tty-left';__change(c);__emit({type:'bindings',binding_generation:2,bindings:{left:{port:'/dev/tty-right',canonical_port:'/dev/tty-right'},right:{port:'/dev/tty-left',canonical_port:'/dev/tty-left'}}});});
 state=await page.evaluate(()=>[__world.getState('left'),__world.getState('right')]);assert.equal(state[0].measured,false);assert.equal(state[1].measured,false);
 await feedback('/dev/tty-left','left',.45,2);state=await page.evaluate(()=>[__world.getState('left'),__world.getState('right')]);assert.ok(Math.abs(state[0].joints[0])<1e-8);assert.ok(Math.abs(state[1].joints[0]-.45)<1e-8);
 await feedback('/dev/tty-right','left',.8,1);state=await page.evaluate(()=>__world.getState('left'));assert.ok(Math.abs(state.joints[0])<1e-8);
 await page.evaluate(()=>{const c=structuredClone(__cfg);c.dual_arm.enabled=false;c.follower.port='/dev/tty-right';__change(c);__emit({type:'bindings',binding_generation:3,bindings:{left:{port:'/dev/tty-right',canonical_port:'/dev/tty-right'}}});});
 await feedback('/dev/tty-right','right',.25,3);state=await page.evaluate(()=>__world.getState('left'));assert.equal(state.port,'/dev/tty-right');assert.ok(Math.abs(state.joints[0]-.25)<1e-8);assert.equal(await page.locator('#modelRightStatus').isHidden(),true);
 await page.evaluate(()=>{const c=structuredClone(__cfg);c.follower.port='/dev/serial/by-path/stable';__change(c);__emit({type:'bindings',binding_generation:4,bindings:{left:{port:'/dev/serial/by-path/stable',canonical_port:'/dev/tty-current'}}});});
 await feedback('/dev/tty-current','right',-.12,4);state=await page.evaluate(()=>__world.getState('left'));assert.equal(state.port,'/dev/tty-current');assert.ok(Math.abs(state.joints[0]+.12)<1e-8);
 await page.locator('#monitorArmScene').click({position:{x:40,y:40}});await page.waitForFunction(()=>document.querySelector('#monitorPreview').open);await page.waitForTimeout(50);assert.ok(await page.locator('#monitorPreviewView .monitor-model-binding[data-side=left]').count());await page.locator('#monitorPreview .monitor-preview-close').click();
 assert.deepEqual(errors,[]);console.log('PASS official model instances independent; port-based left/right routing; stale swap feedback rejected; single-arm right port; stable aliases; anchored labels in enlarged view; no hardware requests');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
