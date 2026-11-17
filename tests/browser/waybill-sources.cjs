/* All camera, serial and scan APIs are intercepted. */
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..'),fixture=fs.readFileSync(path.join(__dirname,'global-controls.cjs'),'utf8').split('const fixture=String.raw`')[1].split('`;')[0];
const cfg=JSON.parse(execFileSync('python',['-c',fixture],{cwd:root,encoding:'utf8'}));
const right='/dev/v4l/by-path/qa-right';cfg.cameras=[{name:'左',index_or_path:0},{name:'右',index_or_path:right}];
cfg.ui_preferences={dataset:{dsWaybillCamSelect:'右',chkDsAutoScanWaybill:false,chkDsIdleScanWaybill:true},eval:{waybillCamSelect:'右',chkAutoScanWaybill:false,chkIdleScanWaybill:true}};
let running=true,kind='record',revision=1;const scans=[],errors=[],streams=[];
function merge(a,b){for(const[k,v]of Object.entries(b))a[k]=v&&typeof v==='object'&&!Array.isArray(v)?merge(a[k]||{},v):v;return a;}
(async()=>{const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});try{
const context=await browser.newContext({viewport:{width:1400,height:1000}});await context.routeWebSocket('**/ws/**',()=>{});
await context.route('**/*',async route=>{const req=route.request(),url=new URL(req.url()),p=url.pathname,json=v=>route.fulfill({json:v});
 if(p==='/api/camera/stream'){streams.push(url.searchParams.get('device'));return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60"><rect width="80" height="60" fill="green"/></svg>'});}
 if(p==='/static/js/arm3d.js')return route.fulfill({contentType:'text/javascript',body:'window.ARM3D={initWorkspace:()=>({set(){},layout(){},onLoad(f){f()},onError(){}})};'});
 if(p.startsWith('/api/')){
  if(p==='/api/config'){if(req.method()==='POST'){merge(cfg,req.postDataJSON());revision++;return json({ok:true,config:cfg});}return json(cfg);}
  if(p==='/api/config/revision')return json({revision});
  if(p==='/api/status')return json({status_ts:Date.now()/1000,running,job:running?{kind,job_id:'qa-job',name:'fixture'}:null,env_ready:true,platform:'Linux',lerobot_installed:true,dual_arm:cfg.dual_arm});
  if(p==='/api/waybill/scan'){const data=req.postDataJSON();scans.push(data);assert.ok(['all','0',right].includes(data.camera));return json({ok:true,found:false,count:0,codes:[],scanned_cameras:[{name:'左',source:data.camera,ok:true,sharpness:10,found:0}]});}
  if(p==='/api/waybill/history')return json({count:0,latest:null});
  if(p==='/api/checkpoints')return json({checkpoints:[]});if(p==='/api/datasets')return json({datasets:[]});if(p==='/api/logs')return json({events:[]});
  if(p==='/api/runtime')return json({server:{platform:'Linux'},client:{local:true}});
  if(p==='/api/monitor/status')return json({arms:{},released:true,cameras:{sessions:[]}});
  if(p==='/api/env/status')return json({ready:true});
  if(p==='/api/version')return json({version:'3.5.5'});
  return json({ok:true,devices:[],sessions:[],ports:[]});
 }
const file=p.startsWith('/static/')?path.join(root,'frontend',p):p.startsWith('/p/')?req.frame().parentFrame()?path.join(root,'frontend/pages',p.split('/').pop()+'.html'):path.join(root,'frontend/console.html'):null;
if(!file||!file.startsWith(root+path.sep)||!fs.existsSync(file))return route.abort();
return route.fulfill({contentType:{'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
});
await context.addInitScript(()=>{Object.defineProperty(HTMLImageElement.prototype,'onload',{configurable:true,set(fn){this.__testLoad=fn;},get(){return this.__testLoad;}});});
const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
for(const entry of [{page:'dataset',select:'dsWaybillCamSelect',button:'btnDsScanWaybill',auto:'chkDsAutoScanWaybill',save:false},{page:'eval',select:'waybillCamSelect',button:'btnScanWaybill',auto:'chkAutoScanWaybill',save:true}]){
 running=false;kind=entry.page==='dataset'?'record':'eval';await page.goto('http://rebot.test/p/'+entry.page);const f=page.frameLocator('#workspaceFrame');
 await f.locator('#'+entry.select+' option[value="'+right+'"]').waitFor({state:'attached'});
 await page.waitForFunction(({id,value})=>document.querySelector('#workspaceFrame').contentDocument?.getElementById(id)?.value===value,{id:entry.select,value:right});
 await f.locator('#'+entry.button).click();for(let i=0;i<40&&!scans.length;i++)await page.waitForTimeout(20);
 assert.equal(scans.at(-1).camera,right);
 await f.locator('#'+entry.select).selectOption('0');running=true;
 await f.locator('body').evaluate(async taskKind=>{await APP.refreshStatus();document.dispatchEvent(new CustomEvent('arm:log',{detail:{type:'state',running:true,kind:taskKind,job_id:'qa-job',ts:Date.now()/1000}}));},kind);
 const before=scans.length;
 if(entry.page==='dataset'&&await page.locator('#recModalScanBtn').count())await page.locator('#recModalScanBtn').click();else if(entry.page==='dataset'&&await f.locator('#recModalScanBtn').count())await f.locator('#recModalScanBtn').click();else await f.locator('#'+entry.button).click();
 for(let i=0;i<40&&scans.length===before;i++)await page.waitForTimeout(20);
 assert.equal(scans[before].camera,'0');assert.equal(scans[before].save,entry.save);
 await page.waitForTimeout(1500);assert.equal(scans.length,before+1,'idle checkbox must not override disabled scanning during a device task');
 if(entry.page==='dataset'){
  await f.locator('[name="video_resolution"]').evaluate(n=>{n.value='1280x720';n.dispatchEvent(new Event('change',{bubbles:true}));});
  await f.locator('body').evaluate(async()=>{await APP.autosave.flush();});
  assert.equal(cfg.dataset.video_resolution,'1280x720','recording resolution must autosave');
  await f.locator('body').evaluate(()=>document.querySelector('#btnRecord').click());await page.locator('#recModalBadge').waitFor({state:'visible',timeout:5000});await f.locator('#'+entry.auto).evaluate(n=>{n.checked=true;n.dispatchEvent(new Event('change',{bubbles:true}));});
  await f.locator('body').evaluate(()=>document.dispatchEvent(new CustomEvent('arm:log',{detail:{type:'log',line:'[剧集保存] 等待采集图片写盘 · 已用 2.0s'}})));
  const atSave=scans.length;await page.waitForTimeout(1500);assert.equal(scans.length,atSave,'automatic decoding must pause during episode saving');
  assert.match(await f.locator('#dsWaybillScanState').textContent(),/自动扫码暂缓/);
  await f.locator('body').evaluate(()=>document.dispatchEvent(new CustomEvent('arm:log',{detail:{type:'log',line:'Recording episode 1'}})));
  await page.waitForTimeout(1500);assert.ok(scans.length>atSave,'scanning must resume when the next real episode starts');
 }else{
  await f.locator('[name="async_inference"]').uncheck();await f.locator('body').evaluate(async()=>{await APP.autosave.flush();});
  assert.equal(cfg.eval.async_inference,false,'async option must autosave');
 }

}
running=false;cfg.cameras=[{name:'左',index_or_path:0,preview:false,width:640,height:480,fps:30},{name:'右',index_or_path:right,preview:false,width:640,height:480,fps:30},{name:'顶部',index_or_path:6,preview:false,width:640,height:480,fps:30}];
await page.goto('http://rebot.test/p/camera');const f=page.frameLocator('#workspaceFrame');await f.locator('.camera-tile').nth(2).waitFor();await f.locator('#btnPreviewAll').click();
await page.waitForFunction(()=>Array.from(document.querySelector('#workspaceFrame').contentDocument.querySelectorAll('.camera-preview canvas')).filter(c=>!c.hidden&&c.width===80&&c.height===60).length===3);
assert.equal(streams.length,3);await f.locator('#btnPreviewAll').click();await page.waitForTimeout(200);assert.equal(streams.length,3);assert.equal(await f.locator('.camera-placeholder:visible').count(),0);
assert.deepEqual(errors,[]);console.log('PASS three shared camera previews resume with no onload event and no duplicate stream requests; named preference migration, physical source dropdown values incl zero/stable path, manual scans, autosaved recording/async options, decoding paused during save and resumed next episode; all APIs mocked');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
