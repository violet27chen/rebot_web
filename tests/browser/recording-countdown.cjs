/* All APIs and WebSockets are intercepted. No hardware or running server. */
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..');
const fixture=fs.readFileSync(path.join(__dirname,'global-controls.cjs'),'utf8').split('const fixture=String.raw`')[1].split('`;')[0];
const cfg=JSON.parse(execFileSync('python',['-c',fixture],{cwd:root,encoding:'utf8'}));
cfg.cameras=[];Object.assign(cfg.dataset,{num_episodes:3,episode_time_s:30,reset_time_s:5});
cfg.ui_preferences={dataset:{chkDsAutoScanWaybill:false,chkDsIdleScanWaybill:false}};
let revision=1,lastJob=null;
let progress={phase:'recording',sequence:1,episode:1,raw_episode:20,total:3,repo_id:'qa/countdown',episode_time_s:30,reset_time_s:5,elapsed_s:7,duration_s:30};
let job={job_id:'record-1',kind:'record',name:'fixture',started_at:Date.now()/1000-20,recording:progress};
const sockets=new Set(),errors=[];
function emit(value){for(const socket of sockets)socket.send(JSON.stringify({ts:Date.now()/1000,...value}));}
function state(){emit(job?{type:'state',running:true,...job,job:job.name}:{type:'state',running:false});}
function setProgress(patch){progress={...progress,...patch};job.recording=progress;state();}
function merge(a,b){for(const[k,v]of Object.entries(b))a[k]=v&&typeof v==='object'&&!Array.isArray(v)?merge(a[k]||{},v):v;return a;}
(async()=>{const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});try{
 const context=await browser.newContext({viewport:{width:1440,height:1000}});context.setDefaultTimeout(8000);
 await context.routeWebSocket('**/ws/logs',socket=>{sockets.add(socket);socket.onClose(()=>sockets.delete(socket));});
 await context.routeWebSocket('**/ws/monitor',()=>{});
 await context.route('**/*',async route=>{
  const req=route.request(),p=new URL(req.url()).pathname,json=value=>route.fulfill({json:value});
  if(p==='/static/js/arm3d.js')return route.fulfill({contentType:'text/javascript',body:'window.ARM3D={initWorkspace:()=>({set(){},layout(){},onLoad(f){f()},onError(){}})};'});
  if(p.startsWith('/api/')){
   if(p==='/api/config'){if(req.method()==='POST'){merge(cfg,req.postDataJSON());revision++;return json({ok:true,config:cfg});}return json(cfg);}
   if(p==='/api/config/revision')return json({revision});
   if(p==='/api/status')return json({status_ts:Date.now()/1000,running:!!job,job,last_job:lastJob,env_ready:true,platform:'Linux',lerobot_installed:true,dual_arm:cfg.dual_arm});
   if(p==='/api/record/start'){
    progress={...progress,phase:'recording',sequence:1,episode:1,elapsed_s:1};
    job={job_id:'record-2',kind:'record',name:'fixture',started_at:Date.now()/1000,recording:progress};state();
    await new Promise(resolve=>setTimeout(resolve,250));
    return json({ok:true,job_id:job.job_id,kind:'record',started_at:job.started_at});
   }
   if(p==='/api/datasets')return json({datasets:[],root:'/tmp/qa'});
   if(p==='/api/logs')return json({events:[]});
   if(p==='/api/waybill/scan')return json({ok:true,found:false,codes:[],scanned_cameras:[]});
   if(p==='/api/runtime')return json({server:{platform:'Linux'},client:{local:true}});
   if(p==='/api/monitor/status')return json({arms:{},released:true,cameras:{sessions:[]}});
   if(p==='/api/env/status')return json({ready:true});
   return json({ok:true,devices:[],sessions:[],ports:[]});
  }
  const file=p.startsWith('/static/')?path.join(root,'frontend',p):p.startsWith('/p/')?req.frame().parentFrame()?path.join(root,'frontend/pages',p.split('/').pop()+'.html'):path.join(root,'frontend/console.html'):null;
  if(!file||!file.startsWith(root+path.sep)||!fs.existsSync(file))return route.abort();
  return route.fulfill({contentType:{'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
 });
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://rebot.test/p/dataset');const f=page.frameLocator('#workspaceFrame');
 const remain=()=>page.locator('#recEpRemain').textContent();
 const seconds=async()=>{const parts=(await remain()).split(':').map(Number);return parts[0]*60+parts[1];};
 await page.locator('#recModalBadge').filter({hasText:'正在采集第 1 集'}).waitFor();
 assert.ok(await seconds()<=23&&await seconds()>=22,'opening active task must recover backend elapsed time');
 for(let i=0;i<3;i++){
  emit({type:'log',line:'[进度] 数据采集 · 最近输出：INFO Recording episode 20'});
  emit({type:'log',line:'INFO Recording episode 20'});state();await page.waitForTimeout(250);
 }
 assert.ok(await seconds()<=23,'duplicate logs and status polls must not restart timer');
 const before=await seconds();
 await f.locator('body').evaluate(()=>{const original=Date.now.bind(Date);Date.now=()=>original()+3600000;});
 setProgress({elapsed_s:1});await page.waitForTimeout(300);
 assert.ok(await seconds()<=before,'stale elapsed snapshots and wall clock changes cannot increase remaining time');
 setProgress({elapsed_s:35});await page.waitForTimeout(250);
 assert.equal(await remain(),'00:00');assert.match(await page.locator('#recModalBadge').textContent(),/正在采集/);
 assert.match(await page.locator('#recModalHint').textContent(),/尚未开始重置/);
 setProgress({phase:'reset',sequence:2,elapsed_s:2,duration_s:5});await page.waitForTimeout(250);
 const reset=await page.locator('#recResetRemain').textContent();assert.equal(reset,'00:03');
 emit({type:'log',line:'INFO Reset the environment'});emit({type:'log',line:'[进度] 最近输出：Reset the environment'});
 await page.locator('[data-rec-key="right"]').click();await page.waitForTimeout(250);
 assert.equal(await page.locator('#recResetRemain').textContent(),reset,'a requested key is not yet an actual phase transition');
 setProgress({phase:'saving',sequence:3,elapsed_s:0,duration_s:null});await page.waitForTimeout(250);
 assert.match(await page.locator('#recModalBadge').textContent(),/保存第 1 集/);
 assert.match(await page.locator('#recModalTotalInfo').textContent(),/尚有 2 集/);
 emit({type:'state',...job,running:true,recording:{...progress,phase:'recording',sequence:1,elapsed_s:0}});
 await page.waitForTimeout(200);assert.match(await page.locator('#recModalBadge').textContent(),/保存第 1 集/);
 setProgress({phase:'recording',sequence:4,episode:2,elapsed_s:0,duration_s:30});await page.waitForTimeout(200);
 assert.match(await page.locator('#recModalEpInfo').textContent(),/剧集 2 \/ 3/);
 setProgress({phase:'preparing_next',sequence:5,elapsed_s:0});await page.waitForTimeout(100);
 assert.match(await page.locator('#recModalBadge').textContent(),/重录第 2 集/);
 setProgress({phase:'recording',sequence:6,elapsed_s:12});await page.reload();
 await page.locator('#recModalBadge').filter({hasText:'正在采集第 2 集'}).waitFor();
 assert.ok(await seconds()<=18&&await seconds()>=17,'reload must retain the current attempt and elapsed time');
 setProgress({phase:'reset',sequence:7,reset_time_s:0,elapsed_s:0,duration_s:0});await page.waitForTimeout(200);
 assert.equal(await page.locator('#recResetRemain').textContent(),'00:00','zero reset must remain zero');
 const first=job.job_id;job=null;emit({type:'state',running:false,job_id:first});
 await f.locator('body').evaluate(async()=>{await APP.refreshStatus();});
 await page.locator('#recModalBadge').waitFor({state:'hidden'});
 await f.locator('#btnRecord').click();
 await page.locator('#recModalBadge').filter({hasText:'正在采集第 1 集'}).waitFor();
 await page.waitForTimeout(450);assert.match(await page.locator('#recModalBadge').textContent(),/正在采集/,'late start HTTP response cannot reset a WebSocket-started countdown');
 assert.ok(await seconds()<=29);
 emit({type:'state',running:false,job_id:first});await page.waitForTimeout(100);
 assert.equal(await page.locator('#recModalBadge').count(),1,'old job completion must not close current modal');
 progress={...progress,phase:'init',sequence:0,raw_episode:null,has_started:false,elapsed_s:0,startup_error:''};
 job={job_id:'record-startup-failure',kind:'record',name:'fixture',started_at:Date.now()/1000,recording:progress};
 await page.reload();await page.locator('#recModalBadge').filter({hasText:'准备中'}).waitFor();
 setProgress({phase:'closing',sequence:1,startup_error:'主臂连接失败：/dev/ttyUSB0 · ID 0 未响应'});await page.waitForTimeout(200);
 assert.equal(await remain(),'未开始采集');assert.match(await page.locator('#recModalBadge').textContent(),/启动失败/);
 assert.equal(await page.locator('#recModalBar').evaluate(n=>n.style.width),'0%');
 // An idle response without an exit code must not imply success.
 const failedJob=job;job=null;await f.locator('body').evaluate(async()=>{await APP.refreshStatus();});
 assert.equal(await f.locator('.job-progress.done').count(),0);
 lastJob={...failedJob,exit_code:1};await f.locator('body').evaluate(async()=>{await APP.refreshStatus();});
 await f.locator('.job-progress.failed .jp-title').filter({hasText:'后台任务失败'}).waitFor();
 await page.waitForTimeout(1000);await f.locator('body').evaluate(async()=>{await APP.refreshStatus();});
 assert.equal(await f.locator('.job-progress.done').count(),0,'later polls must not replace failure with completion');
 assert.deepEqual(errors,[]);
 console.log('PASS countdown stability/recovery, HTTP/WebSocket ordering, startup failure remains not-started/0%, unknown exit is not success, terminal exit code survives later polls; all APIs mocked');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
