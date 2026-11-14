const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
 const page=await context.newPage(); const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const base='http://127.0.0.1:8000';
 const config=async()=> (await (await page.request.get(base+'/api/config')).json()).cameras;
 const status=async()=> (await (await page.request.get(base+'/api/cameras/status')).json());
 const waitThree=async p=>p.waitForFunction(()=>document.querySelectorAll('.camera-tile .badge.ok').length===3,{timeout:18000});
 const original=await config();
 await page.goto(base+'/p/camera');await waitThree(page);
 const second=await context.newPage();await second.goto(base+'/p/camera');await waitThree(second);
 let previous={};
 for(let n=0;n<(process.env.REBOT_UI_ONLY ? 0 : 9);n++){
   await page.waitForTimeout(5000);const s=await status();
   const live=s.sessions.filter(s=>s.active);assert.equal(live.length,3);
   for(const c of live){assert.equal(c.error,'');assert.equal(c.actual.fourcc,'MJPG');assert.equal(c.clients,2);assert.ok(c.frames>(previous[c.device]||0));previous[c.device]=c.frames;}
   if(n===3)for(const c of live){const r=await page.request.get(base+'/api/camera/snapshot?device='+encodeURIComponent(c.device));assert.equal(r.status(),200);assert.ok((await r.body()).subarray(0,2).equals(Buffer.from([255,216])));}
 }
 if(!process.env.REBOT_UI_ONLY)console.log('PASS: three real cameras, two tabs, 45 seconds of advancing frames, snapshots share ownership');
 await second.close();
 await page.locator('#btnPauseAll').click();await page.waitForTimeout(2500);assert.equal((await status()).sessions.filter(s=>s.active).length,0);
 await page.locator('#btnPreviewAll').click();await waitThree(page);
 console.log('PASS: all devices released after pause and reconnect successfully');
 await page.locator('#btnAddCam').click();await page.locator('#camName').fill('QA-unsaved-camera');await page.locator('#cameraForm button[type=submit]').click();assert.equal(await page.locator('.camera-tile').count(),4);assert.deepEqual(await config(),original);
 await page.locator('.camera-tile').last().locator('[data-remove]').click();assert.equal(await page.locator('[data-cancel]').evaluate(e=>e===document.activeElement),true);await page.keyboard.press('Enter');assert.equal(await page.locator('.camera-tile').count(),4);
 await page.locator('.camera-tile').last().locator('[data-remove]').click();await page.locator('[data-ok]').click();assert.equal(await page.locator('.camera-tile').count(),3);await page.locator('#btnSaveCam').click();await page.waitForFunction(()=>document.querySelector('#btnSaveCam').disabled);await waitThree(page);assert.deepEqual(await config(),original);
 console.log('PASS: add/remove preserve draft, no silent saving, Enter cancels removal, explicit save preserves original configuration');
 const downloadPromise=page.waitForEvent('download');await page.locator('.camera-tile').nth(1).locator('[data-snapshot]').click();const download=await downloadPromise;assert.ok(download.suggestedFilename().endsWith('.jpg'));
 await page.locator('#btnDiscover').click();await page.waitForTimeout(400);assert.equal(await page.locator('.notify-mask').count(),0);
 await page.locator('#btnRailToggle').click();assert.ok(await page.locator('#rail').isVisible());await page.keyboard.press('Escape');assert.ok(await page.locator('#rail').isHidden());
 await page.screenshot({path:'/tmp/rebot-final-camera-desktop.png',fullPage:true});
 await page.goto(base+'/p/home');await page.waitForFunction(()=>document.querySelector('#homeConnectLink').getAttribute('aria-disabled')!== 'true');await page.screenshot({path:'/tmp/rebot-final-home-desktop.png',fullPage:true});assert.equal(await page.locator('#homeConnectLink').getAttribute('href'),'/p/dual');
 const mobile=await browser.newPage({viewport:{width:390,height:844}});
 mobile.on('pageerror',e=>errors.push(e.message));
 const hardwareRequests=[];mobile.on('request',r=>{if(r.url().includes('/api/arm/'))hardwareRequests.push(r.url());});
 for(const name of ['camera','home','dual','connect','dataset','train','eval','teleop','setup','logs','viewer','yolo','arm','grasp']){
   await mobile.goto(base+'/p/'+name);await mobile.waitForTimeout(name==='arm'?2000:500);assert.ok(await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),name+' overflows');
   if(name==='camera'){await waitThree(mobile);await mobile.screenshot({path:'/tmp/rebot-final-camera-mobile.png',fullPage:true});}
   if(name==='home'){await mobile.waitForFunction(()=>!document.querySelector('#homeConnectLink').hasAttribute('aria-disabled'));await mobile.screenshot({path:'/tmp/rebot-final-home-mobile.png',fullPage:true});}
 }
 assert.deepEqual(hardwareRequests,[]);assert.deepEqual(errors,[]);console.log('PASS: snapshot download, non-blocking notifications, log drawer, dashboard, 14 mobile pages, no JavaScript errors, no automatic arm connection');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1);});
