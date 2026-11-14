const {chromium}=require('playwright');const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const ctx=await browser.newContext({viewport:{width:1440,height:1000}});const errors=[],calls=[];let delayed=false,failSave=false,delayAction=false;
 await ctx.route('**/api/**',async route=>{const r=route.request(),path=new URL(r.url()).pathname;
   if(r.method()==='POST'&&path==='/api/config'){if(failSave)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,error:'QA simulated save outage'})});if(delayed)await new Promise(r=>setTimeout(r,1400));return route.continue();}
   if(['POST','PUT','DELETE'].includes(r.method())&&path!=='/api/dual/mode'){calls.push({path,data:r.postDataJSON()});if(delayAction)await new Promise(r=>setTimeout(r,1800));return route.fulfill({status:200,contentType:'application/json',body:'{"ok":true}'});}
   return route.continue();
 });
 const page=await ctx.newPage(),other=await ctx.newPage();[page,other].forEach(p=>p.on('pageerror',e=>errors.push(e.message)));
 const base=process.env.REBOT_TEST_URL||'http://127.0.0.1:8012';const config=async()=> (await(await page.request.get(base+'/api/config')).json());
 const wait=async fn=>{const deadline=Date.now()+20000;while(Date.now()<deadline){if(await fn())return;await page.waitForTimeout(180);}throw Error('Condition did not converge');};
 await page.goto(base+'/p/dataset');await other.goto(base+'/p/dataset');await page.waitForFunction(()=>APP.autosave.state().some(s=>s.id.startsWith('dataset:')));await other.waitForFunction(()=>APP.autosave.state().some(s=>s.id.startsWith('dataset:')));
 delayed=true;await page.locator('#recordForm [name=single_task]').fill('QA first');await page.waitForTimeout(750);await page.locator('#recordForm [name=single_task]').fill('QA latest task');await other.locator('#recordForm [name=num_episodes]').fill('4');
 await wait(async()=>{const c=await config();return c.dataset.single_task==='QA latest task'&&c.dataset.num_episodes===4;});delayed=false;await wait(async()=>await other.locator('#recordForm [name=single_task]').inputValue()==='QA latest task');
 console.log('PASS rapid edits during save, disjoint fields from two pages, live cross-page refresh');
 failSave=true;await page.locator('#recordForm [name=single_task]').fill('QA retained after failure');await page.waitForFunction(()=>document.querySelector('#autosaveState').classList.contains('err'));assert.ok(await page.locator('#autosaveError').isVisible());
 page.once('dialog',d=>d.accept());await page.reload();failSave=false;await wait(async()=>(await config()).dataset.single_task==='QA retained after failure');console.log('PASS visible save failure, retained draft, reload and recovery');
 delayAction=true;await page.locator('#recordForm [name=single_task]').fill('QA save before start');await page.locator('#btnRecord').click();await page.waitForFunction(()=>document.querySelector('#btnRecord').getAttribute('aria-busy')==='true');await wait(async()=>calls.some(c=>c.path==='/api/record/start'));assert.equal((await config()).dataset.single_task,'QA save before start');await page.waitForTimeout(2000);delayAction=false;console.log('PASS task start flushes config; button shows pending feedback; hardware request intercepted');
 await page.goto(base+'/p/connect');await page.waitForFunction(()=>APP.autosave.state().some(s=>s.id==='devices'));assert.equal(await page.locator('.nav a[href="/p/dual"]').count(),0);assert.equal(await page.locator('#left_follower').isVisible(),true);
 await page.locator('#robotMode').selectOption('single');await wait(async()=>!(await config()).dual_arm.enabled);await wait(async()=>await page.locator('#leaderPort').isVisible());
 await page.locator('#robotMode').selectOption('dual');await wait(async()=>(await config()).dual_arm.enabled);await wait(async()=>await page.locator('#left_follower').isVisible());
 assert.equal(await page.locator('[data-mode="dual"] [data-calibrate]').count(),4);console.log('PASS device page switches single/dual with autosave and exposes all four calibrations');
 await page.goto(base+'/p/dual');assert.ok(page.url().endsWith('/p/connect'));
 await page.goto(base+'/p/train');await page.waitForFunction(()=>APP.autosave.state().some(s=>s.id==='training'));await page.locator('#l_steps').fill('4567');await wait(async()=>(await config()).train.steps===4567);console.log('PASS training autosave and legacy page redirect');
 assert.deepEqual(errors,[]);await browser.close();
})().catch(e=>{console.error(e);process.exit(1);});
