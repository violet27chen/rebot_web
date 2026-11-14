const {chromium}=require('/tmp/rebot-ui-qa/node_modules/playwright');const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage(),errors=[],hardware=[],dialogs=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',async d=>{dialogs.push(d.type());await d.dismiss();});
const base=process.env.REBOT_TEST_URL||'http://127.0.0.1:8012';
const image=fs.readFileSync(require('node:path').resolve(__dirname,'../../frontend/static/img/camera-off.svg'));
await context.route('**/api/**',async route=>{const req=route.request(),path=new URL(req.url()).pathname;
 if(['/api/camera/stream','/api/depth/stream','/api/camera/snapshot'].includes(path))return route.fulfill({contentType:'image/svg+xml',body:image});
 if(path==='/api/checkpoints')return route.fulfill({json:{checkpoints:[{path:'outputs/vision/test.pt'}]}});
 if(path==='/api/grasp/calibration')return route.fulfill({json:{calibration:{H:[[1,0,0],[0,1,0],[0,0,1]],table_z:0,points:[{u:0,v:0,x:0,y:0,z:0},{u:600,v:0,x:.2,y:0,z:0},{u:600,v:400,x:.2,y:.2,z:0},{u:0,v:400,x:0,y:.2,z:0}]}}});
 if(['POST','DELETE','PUT'].includes(req.method())&&path!=='/api/config'){
   hardware.push(path);
   if(path==='/api/grasp/plan')return route.fulfill({json:{ok:true,plan_id:'browser-test-only',side:'left',xyz:[.2,.1,.03],rpy:[0,.7,0],joints:[0,0,0,0,0,0],ik_error:.0001}});
   if(path==='/api/yolo/predict')return route.fulfill({json:{ok:true,image:'data:image/svg+xml;base64,'+image.toString('base64'),detections:[{name:'盒子',conf:.95,xyxy:[20,20,200,160]}]}});
   if(path==='/api/devices/disconnect-all')return route.fulfill({json:{ok:true,released:true,note:'测试设备已释放',killed:[{pid:99999,name:'test-only',devices:['/dev/test-only']}],remaining:[],errors:[]}});
   return route.fulfill({json:{ok:true}});
 }
 return route.continue();
});
try{
 await page.goto(base+'/p/home');const frame=page.frameLocator('#workspaceFrame');await frame.locator('#kvEnv').waitFor();
 await page.locator('.persistent-monitor').evaluate(n=>n.dataset.testIdentity='persisted');
 assert.equal(await page.locator('#monitorArmScene').count(),1);
 for(const size of [{width:1440,height:1000},{width:390,height:844}]){
  await page.setViewportSize(size);
  for(const name of ['home','connect','teleop','dataset','train','eval','setup','yolo','viewer','logs','camera']){
   if(size.width<900&&!await page.locator('body').evaluate(n=>n.classList.contains('nav-open')))await page.locator('#btnNavToggle').click();
   await page.locator('.nav a[href="/p/'+name+'"]').click();await page.waitForURL(base+'/p/'+name);await frame.locator('body').waitFor();await page.waitForTimeout(500);console.log('PASS page',name,size.width);
   assert.equal(await page.locator('.persistent-monitor').getAttribute('data-test-identity'),'persisted');
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),name+' outer overflow');
   assert.ok(await frame.locator('html').evaluate(n=>n.scrollWidth<=innerWidth),name+' inner overflow '+size.width);
   assert.ok(await page.locator('.persistent-monitor').isVisible());
   if(['teleop','dataset','camera'].includes(name))await page.screenshot({path:'/tmp/rebot-monitor-'+name+'-'+size.width+'.png'});
  }
 }
 await page.locator('#btnDisconnectAll').click();await page.locator('#deviceReleaseReport').waitFor();assert.match(await page.locator('#deviceReleaseReport').innerText(),/99999/);await page.locator('#deviceReleaseReport button').click();
 assert.deepEqual(errors,[]);assert.deepEqual(dialogs,[]);assert.ok(!hardware.includes('/api/grasp/execute'));console.log('PASS 11 desktop/mobile pages, persistent models/cameras survive navigation, disconnect result dialog, no native unsaved-changes prompt, no JavaScript errors');
}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
