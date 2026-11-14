/* Keep the monitor DOM, WebSockets and WebGL scenes alive across page changes. */
(async function(){
  'use strict';
  var A=window.APP;
  var pages={home:['总览','平台状态与快捷入口'],connect:['设备连接与校准','单臂 / 双臂配置 · 串口连接 · 分别校准'],
    camera:['摄像头','多路视觉与真实深度'],teleop:['遥操作','双主臂示教与实时监视'],dataset:['训练数据','独立技能采集与数据管理'],
    train:['模型训练','数据集与模型训练'],eval:['快递分拣','面单朝上 · 单号确认 · 按类别分流'],setup:['环境安装','运行环境与依赖'],
    viewer:['数据查看','录制数据回放'],yolo:['视觉数据','视觉数据与标注'],logs:['日志与命令','任务输出与诊断']};
  function route(path){var key=path.split('?')[0].split('/').pop();return pages[key]?key:'home';}
  var initial=location.pathname+location.search, active=route(initial);
  if('scrollRestoration' in history)history.scrollRestoration='manual';
  await A.init({active:active,title:pages[active][0],subtitle:pages[active][1],gate:false});
  document.body.classList.add('workspace-console');
  var content=A.q('.content'), frame=document.createElement('iframe');
  frame.id='workspaceFrame';frame.title=pages[active][0];frame.setAttribute('allow','fullscreen');
  content.querySelectorAll('#page,#robotContext,.runtime-details').forEach(function(n){n.remove();});
  content.classList.add('workspace-content');content.appendChild(frame);
  var pending=new Map(),serial=0,navigating=false;
  function resetPageScroll(){
    // Only the page area resets. The persistent camera/model rail stays alive.
    window.scrollTo({top:0,left:0,behavior:'instant'});
    content.scrollTop=0;var main=A.q('.main');if(main)main.scrollTop=0;
    try{frame.contentWindow.scrollTo({top:0,left:0,behavior:'instant'});}catch(e){}
  }
  function loadPage(target){
    frame.style.height='500px';frame.src=target;resetPageScroll();
  }
  function flushChild(){
    if(!frame.contentWindow)return Promise.resolve();
    var id=++serial;return new Promise(function(resolve,reject){
      var timer=setTimeout(function(){if(pending.has(id)){pending.delete(id);reject(new Error('页面配置保存未完成，请稍后重试'));}},8000);
      pending.set(id,{resolve:resolve,reject:reject,timer:timer});
      frame.contentWindow.postMessage({type:'rebot:flush',id:id},location.origin);
    });
  }
  function paint(path){
    var key=route(path),title=pages[key];document.title=title[0]+' · reBot 控制台';
    A.q('.top h1').textContent=title[0];A.q('.top p').textContent=title[1];frame.title=title[0];
    A.qa('.nav a').forEach(function(a){var match=new URL(a.href).pathname==='/p/'+key;a.classList.toggle('active',match);if(match)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
    document.body.dataset.page=key;
  }
  async function navigate(path,replace){
    if(navigating)return;var url=new URL(path,location.origin);if(url.origin!==location.origin)return;
    if(url.pathname==='/p/dual')url.pathname='/p/connect';
    navigating=true;
    try{
      await flushChild();
      var target=url.pathname+url.search+url.hash;paint(target);
      if(replace)history.replaceState({},'',target);else history.pushState({},'',target);
      loadPage(target);document.body.classList.remove('nav-open');
      var backdrop=A.el('navBackdrop');if(backdrop)backdrop.hidden=true;
    }catch(error){A.toast('保存未完成，已保留当前页面：'+error.message,'err');}
    finally{navigating=false;}
  }
  window.addEventListener('message',function(event){
    if(event.origin!==location.origin||event.source!==frame.contentWindow||!event.data)return;
    var data=event.data;
    if(data.type==='rebot:navigate')navigate(data.path);
    if(data.type==='rebot:flush-result'&&pending.has(data.id)){
      var request=pending.get(data.id);clearTimeout(request.timer);pending.delete(data.id);
      if(data.ok===false)request.reject(new Error(data.error||'配置保存失败'));else request.resolve();
    }
    if(data.type==='rebot:save-state')A.autosave.track('workspace',{dirty:data.dirty,saving:data.saving,error:data.error,flush:flushChild});
    if(data.type==='rebot:layout'){
      // Ignore delayed size messages from the document being navigated away.
      if(data.path&&data.path!==new URL(frame.src,location.origin).pathname+new URL(frame.src,location.origin).search)return;
      if(Number.isFinite(data.height)&&data.height>0)frame.style.height=Math.max(500,Math.ceil(data.height))+'px';
    }
  });
  document.addEventListener('click',function(event){var a=event.target.closest('a[href]');if(!a||event.ctrlKey||event.metaKey||event.shiftKey||a.target==='_blank')return;
    var url=new URL(a.href,location.origin);if(url.origin===location.origin&&(url.pathname.startsWith('/p/')||url.pathname==='/')){event.preventDefault();navigate(url.pathname+url.search+url.hash);}
  });
  window.addEventListener('popstate',function(){paint(location.pathname+location.search);loadPage(location.pathname+location.search);});
  frame.onload=function(){
    resetPageScroll();
    try{var path=frame.contentWindow.location.pathname+frame.contentWindow.location.search;if(path!==location.pathname+location.search){history.replaceState({},'',path);paint(path);}}catch(e){}
  };
  var disconnect=document.createElement('button');disconnect.id='btnDisconnectAll';disconnect.className='btn';disconnect.textContent='断开全部设备';disconnect.setAttribute('data-app-nofb','');
  disconnect.title='立即停止任务，并杀死所有占用机械臂串口、相机和深度设备的进程（包括 Web 外的进程）';
  disconnect.onclick=function(){A.action(async function(){
    disconnect.disabled=true;disconnect.textContent='正在释放设备…';
    try{var report=await A.api.post('/api/devices/disconnect-all',{},30000);window.MONITOR.pause(true);
      var text=report.note+'\n已终止 '+(report.killed||[]).length+' 个占用进程';
      (report.killed||[]).forEach(function(p){text+='\nPID '+p.pid+' · '+p.name+' · '+p.devices.join(', ');});
      (report.remaining||[]).forEach(function(p){text+='\n仍被占用：PID '+p.pid+' · '+p.name+' · '+p.devices.join(', ');});
      (report.errors||[]).forEach(function(e){text+='\nPID '+e.pid+' · '+e.error;});
      var result=document.getElementById('deviceReleaseReport');if(!result){result=document.createElement('dialog');result.id='deviceReleaseReport';result.className='device-release-dialog';result.innerHTML='<h2>设备释放结果</h2><pre class="release-report"></pre><button class="btn primary" type="button">关闭</button>';result.querySelector('button').onclick=function(){result.close();};document.body.appendChild(result);}result.querySelector('pre').textContent=text;result.showModal();await A.refreshStatus();
    }finally{disconnect.disabled=false;disconnect.textContent='断开全部设备';}
  },null);};
  A.q('.top-right').insertBefore(disconnect,A.el('railStop'));
  window.WORKSPACE={flush:flushChild};
  window.JOB_CONSOLE.mount();window.MONITOR.mount();paint(initial);loadPage(initial);
})();
