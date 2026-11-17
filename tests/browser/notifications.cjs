/* Static parent/iframe fixture only: every network request is intercepted. */
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'../..');
(async()=>{const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});try{
 const context=await browser.newContext(),errors=[];
 await context.route('**/*',async route=>{
  const p=new URL(route.request().url()).pathname;
  if(p==='/common.js')return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(root,'frontend/static/js/common.js'))});
  if(p==='/style.css')return route.fulfill({contentType:'text/css',body:fs.readFileSync(path.join(root,'frontend/static/css/style.css'))});
  if(p==='/'||p==='/child'||p==='/child-next')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><link rel="stylesheet" href="/style.css"></head><body><script src="/common.js"></script>'+(p==='/'?'<iframe id="child" src="/child"></iframe>':'')+'</body></html>'});
  return route.abort();
 });
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.clock.install({time:new Date('2026-11-15T00:00:00Z')});
 await page.goto('http://rebot.test/');const frame=page.frameLocator('#child');await frame.locator('body').waitFor({state:'attached'});
 await page.clock.pauseAt(new Date('2026-11-15T00:01:00Z'));
 await page.evaluate(()=>APP.toast('first','ok'));await page.clock.runFor(2000);
 await page.evaluate(()=>APP.toast('second','info'));
 await frame.locator('body').evaluate(()=>APP.toast('from-child','ok'));
 assert.equal(await page.locator('#toasts .toast').count(),3);assert.equal(await frame.locator('#toasts .toast').count(),0);
 await page.locator('.toast').filter({hasText:'first'}).evaluate(n=>{n.dispatchEvent(new Event('mouseenter'));n.querySelector('button').focus();n.dispatchEvent(new Event('focusin'));});
 await page.locator('#child').evaluate(n=>{n.src='/child-next';});
 await page.waitForFunction(()=>document.querySelector('#child').contentWindow.location.pathname==='/child-next'&&!!document.querySelector('#child').contentWindow.APP);
 await page.clock.runFor(3001);
 assert.equal(await page.locator('.toast').filter({hasText:'first'}).count(),0,'hover/focus must not suspend the deadline');
 assert.equal(await page.locator('.toast').filter({hasText:'second'}).count(),1);
 assert.equal(await page.locator('.toast').filter({hasText:'from-child'}).count(),1);
 await page.evaluate(()=>{APP.toast('third','warn');APP.toast('error-message','err');});
 await page.clock.runFor(1999);
 assert.equal(await page.locator('.toast').filter({hasText:'second'}).count(),0,'later notification cannot reset another deadline');
 assert.equal(await page.locator('.toast').filter({hasText:'from-child'}).count(),0,'iframe navigation cannot orphan a notification timer');
 await page.clock.runFor(3001);
 assert.equal(await page.locator('.toast').filter({hasText:'third'}).count(),0);assert.equal(await page.locator('.toast.err').count(),1);
 await page.clock.runFor(7000);assert.equal(await page.locator('.toast').count(),0,'each error also expires independently at 12 seconds');
 await page.evaluate(()=>{APP.toast('manual-close','ok');APP.toast('still-timed','ok');});
 await page.locator('.toast').filter({hasText:'manual-close'}).locator('button').evaluate(n=>n.click());
 assert.equal(await page.locator('.toast').count(),1);await page.clock.runFor(5000);assert.equal(await page.locator('.toast').count(),0);
 await page.evaluate(()=>{APP.confirm('explicit confirmation').then(v=>window.confirmResult=v);});
 await page.clock.runFor(20000);assert.equal(await page.locator('.notify-modal.confirm').count(),1,'explicit confirmation is not an expiring toast');
 await page.locator('[data-cancel]').evaluate(n=>n.click());await page.waitForFunction(()=>window.confirmResult===false);
 assert.deepEqual(errors,[]);console.log('PASS independent 5/12-second notification expiry, hover/focus, staggered arrivals, persistent parent ownership across iframe navigation, manual close, and explicit confirmation preserved; no live APIs');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
