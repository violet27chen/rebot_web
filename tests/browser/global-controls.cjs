/* Fully intercepted browser regression: no server or physical devices needed. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..');
const fixture=String.raw`import ast,json,pathlib
t=ast.parse(pathlib.Path('backend/main.py').read_text())
n=next(x for x in t.body if isinstance(x,ast.AnnAssign) and getattr(x.target,'id','')=='DEFAULT_CONFIG')
class Values(ast.NodeTransformer):
 def visit_Name(self,n):return ast.copy_location(ast.Constant({'FOLLOWER_TYPE':'seeed_b601_dm_follower','LEADER_TYPE':'rebot_arm_102_leader','CAN_ADAPTER':'damiao'}.get(n.id,'')),n)
print(json.dumps(ast.literal_eval(Values().visit(n.value))))`;
const cfg=JSON.parse(execFileSync('python',['-c',fixture],{cwd:root,encoding:'utf8'}));
cfg.dual_arm.enabled=true;cfg.dual_arm.robot_type=cfg.dual_arm.teleop_type='seeedstudiob601dm';
cfg.dual_arm.left.follower_port='/dev/qa-left';cfg.dual_arm.right.follower_port='/dev/qa-right';
cfg.cameras=[];
let revision=1,job=null,logTime=Date.now()/1000,zeroServiceMissing=false,zeroStartMissing=false,zeroResultMissing=false;
const sockets=new Set(),inputs=[],motion=[],zero=[];
function merge(a,b){if(!b||typeof b!=='object'||Array.isArray(b))return b;for(const k of Object.keys(b))a[k]=merge(a[k]||{},b[k]);return a;}
function event(state){logTime=Math.max(Date.now()/1000,logTime+.001);const data={type:'state',ts:logTime,...state};for(const s of sockets)s.send(JSON.stringify(data));}
function updateJob(j){job=j;event(j?{...j,job:j.name,running:true}:{running:false,action:'finish',exit_code:0});}
function status(){return {status_ts:Date.now()/1000,running:!!job,job:job?{...job}:null,platform:'Linux',is_windows:false,env_ready:true,active_env:'test',lerobot_installed:true,connected:false,dual_connected:false,dual_arm:{...cfg.dual_arm,connected:false,ports:{},sides:{left:{},right:{}}}};}
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000}}),errors=[];
  context.setDefaultTimeout(15000);
  await context.routeWebSocket('**/ws/logs',socket=>{sockets.add(socket);socket.onClose(()=>sockets.delete(socket));});
  await context.routeWebSocket('**/ws/monitor',()=>{});
  await context.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url()),p=url.pathname,method=request.method();
   const json=data=>route.fulfill({json:data});
   // URDF rendering is unchanged and covered separately; keep this interaction
   // test independent of software-OpenGL load on the control host.
   if(p==='/static/js/arm3d.js')return route.fulfill({contentType:'text/javascript',body:'window.ARM3D={initWorkspace:function(){return {set:function(){},resize:function(){},layout:function(){},fit:function(){},onLoad:function(cb){queueMicrotask(cb);},onError:function(){}};}};'});
   if(p.startsWith('/api/')){
    if(p==='/api/config'){if(method==='POST'){merge(cfg,request.postDataJSON());revision++;event({type:'config',revision,sections:[]});return json({ok:true,config:cfg});}return json(cfg);}
    if(p==='/api/config/revision')return json({revision});
    if(p==='/api/status')return json(status());
    if(p==='/api/version')return json({version:'3.4.1',build:'20261114.2',platform:'Linux'});
    if(p==='/api/env/status')return json({ready:true});
    if(p==='/api/runtime')return json({server:{platform:'Linux'},client:{local:true}});
    if(p==='/api/ports')return json({ports:[]});
    if(p==='/api/system/permissions')return json({devices:[],requires_chmod:true});
    if(p==='/api/checkpoints')return json({checkpoints:[]});
    if(p==='/api/datasets')return json({datasets:[{repo_id:'demo/three',episodes:1}]});
    if(p==='/api/dataset/view/episodes'){
     const names=['observation.images.left','observation.images.right','observation.images.depth_rgb'];
     return json({cameras:names,episodes:[{episode_index:0,length:60,tasks:['fixture'],cameras:Object.fromEntries(names.map(name=>[name,{from:0,to:2,chunk:0,file:0,available:true}]))}]});
    }
    if(p==='/api/dataset/view/episode')return json({fps:30,state:[[0,0],[1,1]],action:[[0,0],[1,1]]});
    if(p==='/api/dataset/view/clip'){
     const body=fs.readFileSync('/tmp/rebot-viewer-fixture-20261114.mp4'),range=/bytes=(\d+)-(\d*)/.exec(request.headers().range||'');
     if(range){const from=Number(range[1]),to=range[2]?Math.min(Number(range[2]),body.length-1):body.length-1;return route.fulfill({status:206,contentType:'video/mp4',headers:{'accept-ranges':'bytes','content-range':`bytes ${from}-${to}/${body.length}`},body:body.subarray(from,to+1)});}
     return route.fulfill({contentType:'video/mp4',headers:{'accept-ranges':'bytes'},body});
    }
    if(p==='/api/logs')return json({events:[]});
    if(p==='/api/commands/preview')return json({enabled:cfg.dual_arm.enabled,command:'offline browser fixture'});
    if(p==='/api/dual/types')return json({dual_robots:['bi_seeed_b601_dm_follower'],dual_teleops:['bi_rebot_arm_102_leader'],error:''});
    if(p==='/api/monitor/status')return json({arms:{},released:false,cameras:{sessions:[]},depth:{available:false,active:false}});
    if(p==='/api/job/input'){
     const payload=request.postDataJSON();inputs.push(payload);
     if(!job||payload.job_id!==job.job_id)return route.fulfill({status:409,json:{error:'stale task'}});
     updateJob({...job,awaiting_input:false});return json({ok:true});
    }
    if(p==='/api/devices/home-and-disable'){
     motion.push(p);updateJob({job_id:'home-test',prompt_id:0,name:'收工归零并失能',kind:'home-and-disable',awaiting_input:false});
     return json({ok:true,name:'收工归零并失能'});
    }
    if(p==='/api/devices/zero/capabilities')return zeroServiceMissing?route.fulfill({status:404,json:{detail:'Not Found'}}):json({api_version:1,version:'3.4.1',build:'20261114.2',worker_available:true,actions:['read','release','set-zero']});
    if(p==='/api/devices/zero/start'){if(zeroStartMissing)return route.fulfill({status:404,json:{detail:'Not Found'}});zero.push(request.postDataJSON());return json({ok:true,name:'zero fixture',job_id:'zero-fixture'});}
    if(p==='/api/devices/zero/result')return zeroResultMissing?route.fulfill({status:404,json:{error:'零点任务结果已过期，请重新读取'}}):json({ok:true,finished:true,exit_code:0,result:{side:'left',port:'/dev/qa-left',timestamp:Date.now()/1000,joints:[{id:7,degrees:0,disabled:true}]}});
    // Every API request stays local to this fixture; no route.continue().
    return json({ok:true,devices:[],sessions:[]});
   }
   let file;
   if(p.startsWith('/static/'))file=path.join(root,'frontend',p);
   else if(p==='/'||p.startsWith('/p/')){
    file=path.join(root,'frontend','console.html');
    if(request.frame().parentFrame())file=path.join(root,'frontend/pages',p.split('/').pop()+'.html');
   }else return route.abort();
   if(!file.startsWith(root+path.sep)||!fs.existsSync(file))return route.fulfill({status:404,body:'Not found'});
   const ext=path.extname(file);const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png'};
   return route.fulfill({contentType:types[ext]||'application/octet-stream',body:fs.readFileSync(file)});
  });
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://rebot.test/p/connect');
  const frame=page.frameLocator('#workspaceFrame');
  await frame.locator('#btnProbeTypes').waitFor({state:'visible'});
  assert.equal(await frame.locator('#btnProbeTypes').evaluate(n=>!!n.closest('details')),false);
  await frame.locator('#btnProbeTypes').click();await frame.locator('#btnApplyTypes').waitFor({state:'visible'});await frame.locator('#btnApplyTypes').click();
  await page.waitForFunction(()=>document.querySelector('#btnHomeDisable'));
  for(let n=0;n<50&&cfg.dual_arm.robot_type!=='bi_seeed_b601_dm_follower';n++)await page.waitForTimeout(20);
  assert.equal(cfg.dual_arm.robot_type,'bi_seeed_b601_dm_follower');assert.equal(cfg.dual_arm.teleop_type,'bi_rebot_arm_102_leader');
  console.log('PASS detected and applied registered types');
  await frame.locator('#zeroSet').click();await page.locator('.notify-modal.confirm').waitFor();
  assert.match(await page.locator('.notify-modal.confirm').innerText(),/关节 7/);
  await page.locator('.notify-modal.confirm [data-cancel]').click();assert.equal(zero.length,0);
  await frame.locator('#zeroSet').click();await page.locator('.notify-modal.confirm [data-ok]').click();
  await frame.locator('#zeroAlert').filter({hasText:'零点已写入'}).waitFor();assert.equal(zero[0].confirm,true);assert.deepEqual(zero[0].joints,[7]);
  console.log('PASS zero write confirmation and cancel');
  zeroStartMissing=true;
  await frame.locator('#zeroRead').click();await frame.locator('#zeroAlert').filter({hasText:'零点调整接口未加载'}).waitFor();
  assert.equal(await frame.locator('#zeroRead').isDisabled(),true);
  zeroStartMissing=false;zeroServiceMissing=true;
  await frame.locator('#zeroRetryService').click();await frame.locator('#zeroService').filter({hasText:'未就绪'}).waitFor();
  assert.equal(await frame.locator('#zeroSet').isDisabled(),true);
  assert.equal(await frame.locator('#zeroAlert').innerText().then(s=>s.includes('Not Found')),false);
  zeroServiceMissing=false;
  await frame.locator('#zeroRetryService').click();await frame.locator('#zeroService').filter({hasText:'3.4.1'}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('#workspaceFrame').contentDocument.querySelector('#zeroRead').disabled);
  zeroResultMissing=true;
  await frame.locator('#zeroRead').click();await frame.locator('#zeroAlert').filter({hasText:'零点任务结果已失效'}).waitFor();
  assert.equal(await frame.locator('#zeroRead').isDisabled(),false);zeroResultMissing=false;
  console.log('PASS missing zero API is diagnosed without hardware retries; service recovery and expired-result errors');
  assert.equal(await page.locator('.nav a[href="/p/grasp"],.nav a[href="/p/arm"]').count(),0);
  await page.locator('.nav a[href="/p/home"]').click();await page.waitForURL('**/p/home');await frame.locator('body[data-page="home"]').waitFor();
  await page.locator('#jobInputDialog').evaluate(n=>n.dataset.identity='global');
  const calibration={name:'左从臂校准',job_id:'calibration-test',prompt_id:1,kind:'calibrate-left-follower',awaiting_input:true,prompt:'Press ENTER when ready:'};
  updateJob(calibration);await page.locator('#jobInputDialog[open]').waitFor();
  assert.equal(await frame.locator('#jobInputDialog').count(),0);
  assert.equal(await page.locator('#btnHomeDisable').isDisabled(),true);
  await page.locator('#jobInputSend').click();await page.locator('#jobInputDialog[open]').waitFor({state:'hidden'});
  assert.equal(inputs[0].text,'');assert.equal(inputs[0].prompt_id,1);assert.equal(inputs[0].job_id,'calibration-test');
  updateJob({...calibration,prompt_id:2});await page.locator('#jobInputDialog[open]').waitFor();
  await page.locator('#jobInputLater').click();event({...job,running:true});
  await page.waitForTimeout(100);assert.equal(await page.locator('#jobInputDialog[open]').count(),0);
  await page.locator('.nav a[href="/p/teleop"]').click();await page.waitForURL('**/p/teleop');await frame.locator('#btnTeleop').waitFor();
  await page.locator('#btnJobInput').click();await page.locator('#jobInputDialog[open]').waitFor();
  assert.equal(await page.locator('#jobInputDialog').getAttribute('data-identity'),'global');
  await page.locator('#jobInputValue').fill('c');await page.locator('#jobInputSend').click();await page.locator('#jobInputDialog[open]').waitFor({state:'hidden'});
  assert.equal(inputs[1].text,'c');assert.equal(inputs[1].prompt_id,2);
  updateJob({...calibration,prompt_id:3});await page.locator('#jobInputDialog[open]').waitFor();
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:'/tmp/rebot-global-input-390.png'});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  updateJob({...calibration,prompt_id:3,awaiting_input:false});
  await page.locator('#jobInputDialog[open]').waitFor({state:'hidden'});
  assert.equal(await page.locator('#btnHomeDisable').isDisabled(),true);
  updateJob(null);await page.locator('#jobInputDialog[open]').waitFor({state:'hidden'});
  await page.locator('#btnHomeDisable').click();
  await page.locator('.notify-modal.confirm').waitFor();assert.equal(motion.length,0);
  await page.locator('.notify-modal.confirm [data-ok]').click();
  for(let n=0;n<50&&!motion.length;n++)await page.waitForTimeout(20);
  assert.deepEqual(motion,['/api/devices/home-and-disable']);assert.equal(await page.locator('#btnHomeDisable').isDisabled(),true);
  updateJob(null);await page.waitForFunction(()=>!document.querySelector('#btnHomeDisable').disabled,null,{timeout:10000});
  console.log('PASS global prompts and confirmed homing');
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('.nav a[href="/p/viewer"]').click();await page.waitForURL('**/p/viewer');
  await frame.locator('#videos video').nth(2).waitFor();
  console.log('PASS rendered three video elements');
  await page.waitForFunction(()=>Array.from(document.querySelector('#workspaceFrame').contentDocument.querySelectorAll('#videos video')).every(v=>v.readyState>=1),null,{timeout:15000});
  assert.equal(await frame.locator('#videos video').count(),3);assert.match(await frame.locator('#vCameraCount').innerText(),/3 路/);
  await frame.locator('#btnPlay').click();await page.waitForTimeout(350);
  const times=await frame.locator('#videos video').evaluateAll(videos=>videos.map(v=>v.currentTime));assert.ok(times.every(t=>t>.1));assert.ok(Math.max(...times)-Math.min(...times)<.2);
  await frame.locator('#btnPause').click();
  await frame.locator('#vSeek').evaluate(n=>{n.value='500';n.dispatchEvent(new Event('input'));});
  await page.waitForFunction(()=>Array.from(document.querySelector('#workspaceFrame').contentDocument.querySelectorAll('#videos video')).every(v=>Math.abs(v.currentTime-1)<.15),null,{timeout:5000}).catch(async error=>{
   console.log('seek diagnostics',await frame.locator('#videos video').evaluateAll(vs=>vs.map(v=>({time:v.currentTime,duration:v.duration,ready:v.readyState,seekable:Array.from({length:v.seekable.length},(_,i)=>[v.seekable.start(i),v.seekable.end(i)]),error:v.error&&v.error.message}))));throw error;
  });
  await frame.locator('.viewer-camera button').first().click();await page.locator('.viewer-preview-dialog[open] video').waitFor();
  await page.locator('.viewer-preview-dialog button').click();await frame.locator('#videos video').nth(2).waitFor();assert.equal(await frame.locator('#videos video').count(),3);
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:'/tmp/rebot-viewer-3-cameras-390.png'});
  assert.ok(await frame.locator('html').evaluate(n=>n.scrollWidth<=innerWidth));
  await frame.locator('body').evaluate(()=>{window.dispatchEvent(new Event('pagehide'));APP.toast('stale-notification-test','err');});
  assert.equal((await page.locator('body').innerText()).includes('stale-notification-test'),false);
  assert.deepEqual(errors,[]);
  console.log('PASS plugin detection; global and repeated prompts; confirmed homing and selected zero; three synchronized videos, shared seek, enlargement, mobile layout; all requests intercepted');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
