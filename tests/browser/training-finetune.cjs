/* All HTTP/WS is intercepted. Never starts training or opens physical devices. */
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..');
const fixture=fs.readFileSync(path.join(__dirname,'global-controls.cjs'),'utf8').split('const fixture=String.raw`')[1].split('`;')[0];
const cfg=JSON.parse(execFileSync('python',['-c',fixture],{cwd:root,encoding:'utf8'}));
cfg.cameras=[];cfg.train.repo_id='demo/flip_box';cfg.train.dataset_dir='/qa/datasets/flip_box';
cfg.train.training_mode='scratch';cfg.train.base_model='';cfg.train.learning_rate=0;
const checkpoint='outputs/train/flip_box/checkpoints/last/pretrained_model';
const starts=[],inspections=[],writes=[],errors=[],evaluations=[];let revision=1;
function merge(a,b){for(const[k,v]of Object.entries(b))a[k]=v&&typeof v==='object'&&!Array.isArray(v)?merge(a[k]||{},v):v;return a;}
(async()=>{const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});try{
 const context=await browser.newContext({viewport:{width:1440,height:1000}});context.setDefaultTimeout(12000);
 await context.routeWebSocket('**/ws/**',()=>{});
 await context.route('**/*',async route=>{const req=route.request(),url=new URL(req.url()),p=url.pathname;const json=v=>route.fulfill({json:v});
  if(p==='/static/js/arm3d.js')return route.fulfill({contentType:'text/javascript',body:'window.ARM3D={initWorkspace:()=>({set(){},layout(){},resize(){},fit(){},onLoad(f){f()},onError(){}})};'});
  if(p.startsWith('/api/')){
   if(p==='/api/config'){if(req.method()==='POST'){const patch=req.postDataJSON();writes.push(patch);merge(cfg,patch);revision++;return json({ok:true,config:cfg});}return json(cfg);}
   if(p==='/api/config/revision')return json({revision});
   if(p==='/api/status')return json({status_ts:Date.now()/1000,running:false,job:null,env_ready:true,lerobot_installed:true,platform:'Linux',connected:false,dual_arm:cfg.dual_arm});
   if(p==='/api/version')return json({version:'3.5.3',build:'20261114.19',platform:'Linux'});
   if(p==='/api/runtime')return json({server:{platform:'Linux'},client:{local:true}});
   if(p==='/api/env/status')return json({ready:true});
   if(p==='/api/logs')return json({events:[]});
   if(p==='/api/monitor/status')return json({arms:{},released:true,cameras:{sessions:[]},depth:{active:false}});
   if(p==='/api/model-eval/start'){const v=req.postDataJSON();evaluations.push(v);return json({ok:true,evaluation_id:'a'.repeat(24),job_id:'qa-offline'});}
   if(p==='/api/model-eval/result')return json({ok:true,evaluation_id:evaluations.length?'a'.repeat(24):'',finished:true,result:evaluations.length?{samples:10,model_path:checkpoint,mae:.2,rmse:.3,max_error:.6,normalized_l1:.1,inference_ms:12,per_joint_mae:[.1,.3],per_joint_rmse:[.2,.4],action_names:['left_joint','right_joint'],note:'fixture saved-data only'}:null});
   if(p==='/api/eval')throw new Error('Offline evaluation must not call hardware eval');
   if(p==='/api/train/backends')return json({backends:['lerobot','yolo','opencv'].map(key=>({key,label:key,engine:key,desc:'Fixture '+key,available:true,problems:[]}))});
   if(p==='/api/datasets')return json({datasets:[{repo_id:'demo/flip_box',path:'/qa/datasets/flip_box',episodes:20}]});
   if(p==='/api/dataset/root')return json({root:'/qa/datasets'});
   if(p==='/api/datasets/features')return json({found:true,action_dim:14,state_dim:14,total_episodes:20,cameras:['top','wrist']});
   if(p==='/api/checkpoints')return json({checkpoints:[{path:checkpoint,config:checkpoint+'/train_config.json',policy_type:'act',can_finetune:true},{path:'outputs/yolo/best.pt'}]});
   if(p==='/api/commands/preview')return json({command:'fixture',enabled:false});
   if(p==='/api/model/cache')return json({cached:true});
   if(p==='/api/train/finetune/inspect'){
    const payload=req.postDataJSON();inspections.push(payload);
    if(payload.base_model==='/bad/model')return route.fulfill({status:400,json:{ok:false,error:'微调模型路径不存在'}});
    assert.equal(payload.policy_type,'act');assert.equal(payload.training_mode,'finetune');
    return json({ok:true,base_model:payload.base_model,note:'已有权重与 14 维双臂、两路相机兼容。'});
   }
   if(p==='/api/train/start'){const payload=req.postDataJSON();starts.push(payload);return json({ok:true,name:'fixture only',job_name:'flip_box_finetune',output_dir:'/qa/new-run',training_mode:payload.params.training_mode});}
   return json({ok:true,ports:[],devices:[],sessions:[],events:[]});
  }
  const file=p.startsWith('/static/')?path.join(root,'frontend',p):p==='/'||p.startsWith('/p/')?req.frame().parentFrame()?path.join(root,'frontend/pages',p.split('/').pop()+'.html'):path.join(root,'frontend/console.html'):null;
  if(!file||!file.startsWith(root+path.sep)||!fs.existsSync(file))return route.abort();
  return route.fulfill({contentType:{'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
 });
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));await page.goto('http://rebot.test/p/train');let frame=page.frameLocator('#workspaceFrame');
 await frame.locator('[data-ckpt-finetune]').waitFor();
 assert.equal(await frame.locator('#baseModelRow').isHidden(),true);
 await frame.locator('[data-ckpt-finetune]').click();
 await frame.locator('#finetuneStatus').filter({hasText:'检查通过'}).waitFor();
 assert.equal(await frame.locator('#l_training_mode').inputValue(),'finetune');
 assert.equal(await frame.locator('#l_base_model').inputValue(),checkpoint);
 await frame.locator('#l_steps').fill('2000');await frame.locator('#l_learning_rate').fill('0.000001');await frame.locator('#l_job_name').click();
 await page.waitForFunction(()=>document.querySelector('#workspaceFrame').contentWindow.APP.autosave.state().every(b=>!b.dirty&&!b.saving));
 assert.equal(cfg.train.training_mode,'finetune');assert.equal(cfg.train.learning_rate,.000001);assert.ok(writes.length);
 await frame.locator('#btnTrain').click();await frame.locator('#trainAlert').filter({hasText:'微调已启动'}).waitFor();
 assert.equal(starts.length,1);assert.equal(starts[0].params.base_model,checkpoint);assert.equal(starts[0].params.steps,2000);assert.equal(starts[0].params.learning_rate,1e-6);assert.match(await frame.locator('#cmdPreview').innerText(),/--policy.path=/);
 await frame.locator('#btnTrain:not([data-action-busy])').waitFor();await frame.locator('#l_base_model').fill('/bad/model');await frame.locator('#l_base_model').press('Tab');await page.waitForFunction(()=>document.querySelector('#workspaceFrame').contentWindow.APP.autosave.state().every(b=>!b.dirty&&!b.saving));await frame.locator('#btnTrain').click();try{await frame.locator('#finetuneStatus').filter({hasText:'检查未通过'}).waitFor();}catch(e){console.error({status:await frame.locator('#finetuneStatus').innerText(),alert:await frame.locator('#trainAlert').innerText(),inspections,starts});throw e;}assert.equal(starts.length,1);
 await frame.locator('#l_training_mode').selectOption('scratch');assert.equal(await frame.locator('#baseModelRow').isHidden(),true);
 await frame.locator('#btnTrain').click();await page.waitForFunction(()=>document.querySelector('#workspaceFrame').contentWindow.document.querySelector('#trainAlert').textContent.includes('训练已启动'));
 assert.equal(starts.length,2);assert.equal(starts[1].params.base_model,'');assert.equal(starts[1].params.training_mode,'scratch');

 // Exact x coordinates survive retention, repeated steps and history replay.
 const chartState=await frame.locator('#lossChart').evaluate(canvas=>{
  const A=window.APP,s=A.lossSeries(600);
  for(let i=1;i<=650;i++)s.add({type:'loss',step:i*200,value:1/i,job_id:'run-a',ts:i});
  const before=s.points();if(before.length!==600||before[0].step!==10200||before.at(-1).step!==130000)throw new Error('retention reindexed training steps');
  s.add({type:'loss',step:130000,value:.5,job_id:'run-a',ts:651});
  s.add({type:'loss',step:1000,value:8,job_id:'run-a',ts:652});
  if(s.points().at(-1).step!==130000||s.points().length!==600)throw new Error('duplicate/replay changed x range');
  A.chart(canvas,s.points(),A.lossAxes('step'));const label=canvas.getAttribute('aria-label');
  s.add({type:'loss',step:0,value:.2,job_id:'run-b',ts:653});
  s.add({type:'loss',step:130200,value:99,job_id:'run-a',ts:654});
  if(s.points().length!==1||s.points()[0].step!==0)throw new Error('old run leaked into new run');
  return label;
 });
 assert.match(chartState,/X 轴：训练步数（Step）/);assert.match(chartState,/Y 轴：损失值（Loss）/);assert.match(chartState,/10200 至 130000/);
 await frame.locator('#lossChart').evaluate(()=>document.dispatchEvent(new CustomEvent('arm:log',{detail:{type:'loss',step:10200,value:.15,job_id:'qa-visible',ts:Date.now()/1000}})));
 assert.match(await frame.locator('#lossAxes').innerText(),/训练步数/);assert.match(await frame.locator('#lossChart').getAttribute('aria-label'),/10200 至 10200/);
 // Offline model selection fills the path and only submits the dedicated worker API.
 await frame.locator('[data-model-eval]').click();await page.locator('#modelEvalDialog[open] #me_model').selectOption(checkpoint);
 assert.equal(await page.locator('#me_path').inputValue(),checkpoint);
 await page.locator('#me_episodes').fill('0, 2');await page.locator('#me_samples').fill('10');await page.locator('#me_start').click();
 await frame.locator('#modelEvalState').filter({hasText:'评估完成'}).waitFor();
 assert.equal(evaluations.length,1);assert.equal(evaluations[0].base_model,checkpoint);assert.equal(evaluations[0].episodes,'0, 2');assert.match(await frame.locator('#modelEvalMetrics').innerText(),/0.200000/);
 await frame.locator('#btnModelEval').click();await page.locator('#modelEvalDialog[open]').waitFor();await page.locator('#me_close').click();await page.waitForFunction(()=>!document.querySelector('#modelEvalDialog').open);
 // Grow then shrink the SAME document: an old iframe height cannot be its minimum.
 await frame.locator('#page').evaluate(n=>{const b=document.createElement('div');b.id='height-fixture';b.style.height='3000px';n.appendChild(b);});
 await page.waitForFunction(()=>document.querySelector('#workspaceFrame').getBoundingClientRect().height>5000);
 await frame.locator('#height-fixture').evaluate(n=>n.remove());
 await page.waitForFunction(()=>{const f=document.querySelector('#workspaceFrame'),app=f.contentDocument.querySelector('.app');return Math.abs(f.getBoundingClientRect().height-app.getBoundingClientRect().height)<3;});
 await page.evaluate(()=>{window.__monitorNode=document.querySelector('#monitorArmScene');window.scrollTo(0,document.body.scrollHeight);});
 assert.ok(await page.evaluate(()=>scrollY>1000));
 await page.locator('.nav a[href="/p/logs"]').evaluate(n=>n.click());await page.waitForFunction(()=>location.pathname==='/p/logs'&&document.querySelector('#workspaceFrame').contentDocument?.body?.dataset.page==='logs');
 await page.waitForFunction(()=>scrollY===0);assert.ok(await page.evaluate(()=>document.querySelector('#monitorArmScene')===__monitorNode));
 await page.waitForFunction(()=>document.querySelector('#workspaceFrame').getBoundingClientRect().height<3000);
 await page.evaluate(()=>history.back());await page.waitForFunction(()=>location.pathname==='/p/train'&&document.querySelector('#workspaceFrame').contentDocument?.body?.dataset.page==='train');
 await frame.locator('#l_training_mode').waitFor();await page.waitForFunction(()=>scrollY===0);
 await page.setViewportSize({width:390,height:844});await frame.locator('#l_training_mode').selectOption('finetune');
 await page.waitForTimeout(150);if(!await frame.locator('html').evaluate(n=>n.scrollWidth<=innerWidth))console.error(await frame.locator('body').evaluate(()=>({viewport:innerWidth,html:document.documentElement.scrollWidth,body:document.body.scrollWidth,boxes:Array.from(document.querySelectorAll('*')).filter(n=>n.getBoundingClientRect().right>innerWidth+1).slice(0,12).map(n=>({tag:n.tagName,id:n.id,cls:n.className,right:n.getBoundingClientRect().right,width:n.getBoundingClientRect().width})),scrolling:Array.from(document.querySelectorAll('*')).filter(n=>n.scrollWidth>n.clientWidth+1).slice(0,10).map(n=>({tag:n.tagName,id:n.id,cls:n.className,cw:n.clientWidth,sw:n.scrollWidth}))})));assert.ok(await frame.locator('html').evaluate(n=>n.scrollWidth<=innerWidth));assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);console.log('PASS loss axes, absolute step retention and replay; offline evaluation dropdown, metrics and modal; ACT checkpoint selection, compatibility failure blocks start, finetune and scratch commands, autosave, intrinsic frame shrink, navigation/back scroll reset, persistent monitor, mobile width; all APIs mocked');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
