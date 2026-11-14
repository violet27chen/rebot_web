/* Persistent shared camera views and a single official-URDF robot scene. */
(function(){
  'use strict';
  var A=window.APP,panel,config={},streams=new Map(),world,latest={},paused=false,socket,retry;
  var preview={dialog:null,kind:null,record:null,frame:0,home:null,placeholder:null,returnFocus:null};
  var geometryBinding,dual=false,modelError='';
  function canonical(source){var s=String(source);return /^\d+$/.test(s)?'/dev/video'+Number(s):s;}
  function image(source){var r=streams.get(canonical(source));return r&&r.box.dataset.live==='true'&&r.img;}
  function stop(record){record.open=false;clearTimeout(record.timer);record.img.removeAttribute('src');record.box.dataset.live='false';record.note.textContent=paused?'设备已断开':'预览已暂停';}
  function start(record){
    if(paused||record.disabled)return;record.open=true;clearTimeout(record.timer);record.note.textContent='正在连接…';record.box.dataset.live='false';
    record.img.onerror=function(){if(paused)return;record.open=false;record.box.dataset.live='false';record.note.textContent='离线 · 正在重连';record.attempts=(record.attempts||0)+1;record.timer=setTimeout(function(){start(record);},Math.min(30000,1000*Math.pow(2,Math.min(record.attempts,5))));};
    record.img.onload=function(){if(!record.open)return;record.attempts=0;record.box.dataset.live='true';record.note.textContent='实时画面';};
    record.img.src=record.url+'&t='+Date.now();
    record.timer=setTimeout(function(){if(record.open&&!record.img.naturalWidth&&!paused)record.img.onerror();},12000);
  }
  function setPaused(value){paused=!!value;panel.classList.toggle('devices-paused',paused);A.el('monitorResume').hidden=!paused;
    streams.forEach(function(r){if(paused)stop(r);else if(!r.img.getAttribute('src'))start(r);});
    if(paused)Object.keys(latest).forEach(function(side){latest[side]=null;});
  }
  function setupPreview(){
    var dialog=document.createElement('dialog');dialog.id='monitorPreview';dialog.className='monitor-preview-dialog';dialog.setAttribute('aria-labelledby','monitorPreviewTitle');
    dialog.innerHTML='<section class="monitor-preview-panel"><header><div><h2 id="monitorPreviewTitle"></h2><p id="monitorPreviewSubtitle"></p></div><button class="monitor-preview-close" type="button" aria-label="关闭放大预览" title="关闭 (Esc)">×</button></header><div id="monitorPreviewView" class="monitor-preview-view"><canvas id="monitorPreviewImage"></canvas><div id="monitorPreviewEmpty" class="monitor-preview-empty" hidden>等待实时画面</div></div><footer id="monitorPreviewFooter"></footer></section>';
    document.body.appendChild(dialog);preview.dialog=dialog;
    dialog.querySelector('button').onclick=function(){dialog.close();};
    dialog.addEventListener('click',function(e){if(e.target===dialog)dialog.close();});
    dialog.addEventListener('close',function(){
      cancelAnimationFrame(preview.frame);
      if(preview.kind==='model'&&preview.home){var canvas=A.el('monitorArmScene');preview.home.insertBefore(canvas,preview.placeholder);preview.placeholder.remove();if(world)world.resize();}
      preview.kind=preview.record=preview.home=preview.placeholder=null;A.el('monitorPreviewImage').hidden=false;
      if(preview.returnFocus&&preview.returnFocus.isConnected)preview.returnFocus.focus({preventScroll:true});
    });
    A.el('monitorPreviewImage').onclick=async function(e){
      if(preview.kind!=='depth'||!preview.record||preview.record.box.dataset.live!=='true')return;
      var canvas=this,rect=canvas.getBoundingClientRect(),scale=Math.min(rect.width/canvas.width,rect.height/canvas.height);
      var x=Math.floor((e.clientX-rect.left-(rect.width-canvas.width*scale)/2)/scale),y=Math.floor((e.clientY-rect.top-(rect.height-canvas.height*scale)/2)/scale);
      if(x<0||y<0||x>=canvas.width||y>=canvas.height)return;
      try{var r=await A.api.get('/api/depth/point?x='+x+'&y='+y);if(preview.kind==='depth')A.el('monitorPreviewFooter').textContent='像素 ('+x+', '+y+') · '+(r.depth_mm===null?'无有效深度':r.depth_mm+' mm')+' · 深度未与 RGB 对齐';}
      catch(error){A.el('monitorPreviewFooter').textContent=error.message;}
    };
  }
  function openPreview(record){
    if(preview.dialog.open)preview.dialog.close();preview.kind=record.id==='@depth'?'depth':'rgb';preview.record=record;preview.returnFocus=document.activeElement;
    A.el('monitorPreviewTitle').textContent=record.title;A.el('monitorPreviewSubtitle').textContent=record.id==='@depth'?'真实深度 · 原生毫米数据的实时色彩图':'实时 RGB · 与常驻预览共用同一帧';
    A.el('monitorPreviewFooter').textContent=record.id==='@depth'?'点击深度图读取毫米值；点击 ×、空白遮罩或按 Esc 关闭。':'点击 ×、空白遮罩或按 Esc 关闭。';
    A.el('monitorPreviewImage').hidden=false;preview.dialog.showModal();
    var canvas=A.el('monitorPreviewImage'),context=canvas.getContext('2d'),empty=A.el('monitorPreviewEmpty');
    function paint(){
      if(!preview.dialog.open||preview.record!==record)return;
      var valid=!paused&&record.box.dataset.live==='true'&&record.img.naturalWidth;
      empty.hidden=!!valid;empty.textContent=paused?'设备已断开':record.note.textContent;
      if(valid){if(canvas.width!==record.img.naturalWidth||canvas.height!==record.img.naturalHeight){canvas.width=record.img.naturalWidth;canvas.height=record.img.naturalHeight;}context.drawImage(record.img,0,0);}
      else context.clearRect(0,0,canvas.width,canvas.height);
      preview.frame=requestAnimationFrame(paint);
    }paint();
  }
  function openModel(){
    if(preview.dialog.open)return;
    preview.kind='model';preview.returnFocus=document.activeElement;var canvas=A.el('monitorArmScene');
    preview.home=canvas.parentNode;preview.placeholder=document.createElement('div');preview.placeholder.className='monitor-model-away';preview.placeholder.textContent='正在放大查看';preview.home.insertBefore(preview.placeholder,canvas);
    A.el('monitorPreviewTitle').textContent=dual?'双臂 · 同一坐标场景':'从臂 · 官方模型';
    A.el('monitorPreviewSubtitle').textContent=A.el('monitorBaseline').textContent;
    A.el('monitorPreviewFooter').textContent='拖动旋转 · 滚轮 / 双指缩放 · 点击 ×、空白遮罩或按 Esc 关闭';
    A.el('monitorPreviewImage').hidden=true;A.el('monitorPreviewEmpty').hidden=true;A.el('monitorPreviewView').appendChild(canvas);preview.dialog.showModal();if(world)world.resize();
  }
  function tile(id,title,source,url){
    var box=document.createElement('article');box.className='monitor-feed';box.dataset.source=source;
    box.innerHTML='<header><strong></strong><span class="monitor-feed-state">正在连接…</span></header><div class="monitor-image" role="button" tabindex="0"><img alt=""><span class="monitor-empty">等待设备画面</span><span class="monitor-expand" aria-hidden="true">放大 ↗</span></div><footer></footer>';
    box.querySelector('strong').textContent=title;var img=box.querySelector('img');img.alt=title+' 实时画面';box.querySelector('footer').textContent=id==='@depth'?'真实深度 · mm · 点击放大':source;
    var record={box:box,img:img,note:box.querySelector('.monitor-feed-state'),url:url,id:id,title:title,disabled:false};
    var button=box.querySelector('.monitor-image');button.setAttribute('aria-label','放大查看 '+title);button.onclick=function(){openPreview(record);};button.onkeydown=function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();openPreview(record);}};
    return record;
  }
  function resolveGeometry(settings){
    var g=settings||{},mode=g.mode||'coordinates',left=g.left_base_mm,right=g.right_base_mm;
    function vector(v){return Array.isArray(v)&&v.length===3&&v.every(function(n){return typeof n==='number'&&Number.isFinite(n);});}
    if(mode==='coordinates'&&vector(left)&&vector(right)){
      var d=Math.hypot(right[0]-left[0],right[1]-left[1],right[2]-left[2]);
      if(d>0)return {distance_mm:d,base_positions_mm:{left:left,right:right},source:'coordinates'};
    }
    if(mode==='distance'&&typeof g.base_center_distance_mm==='number'&&g.base_center_distance_mm>0)return {distance_mm:g.base_center_distance_mm,base_positions_mm:null,source:'distance'};
    return {distance_mm:null,base_positions_mm:null,source:'unset'};
  }
  function layoutWorld(settings){
    var geometry=resolveGeometry(settings);if(world)world.layout(Object.assign({dual:dual},geometry));
    var note=!dual?'单臂 · 官方 URDF':geometry.source==='unset'?'未提供共同坐标或实测中心距 · 当前仅为布局示意':(geometry.source==='coordinates'?'共同坐标计算':'实测中心距')+' · '+geometry.distance_mm.toFixed(1)+' mm · 同向朝前';
    A.el('monitorBaseline').textContent=modelError||note;A.el('monitorBaseline').classList.toggle('is-uncalibrated',dual&&geometry.source==='unset');A.el('monitorModeLabel').textContent=dual?'双臂 · 同一场景':'单臂';
    A.el('monitorLayoutButton').hidden=!dual;if(preview.kind==='model')A.el('monitorPreviewSubtitle').textContent=note;
    var mode=A.el('monitorGeometryMode').value;A.el('monitorCoordinateFields').hidden=mode!=='coordinates';A.el('monitorDistanceField').hidden=mode!=='distance';
    return geometry;
  }
  function readGeometry(){
    function number(id){var value=A.el(id).value.trim();return value===''?null:Number(value);}
    var g={mode:A.el('monitorGeometryMode').value,frame_id:'workspace',unit:'mm',left_base_mm:['X','Y','Z'].map(function(axis){return number('monitorLeft'+axis);}),right_base_mm:['X','Y','Z'].map(function(axis){return number('monitorRight'+axis);}),base_center_distance_mm:number('monitorBaseDistance')};
    return {monitor_layout:g};
  }
  function applyGeometry(cfg){var g=cfg.monitor_layout||{};A.el('monitorGeometryMode').value=g.mode||'coordinates';
    ['left','right'].forEach(function(side){['X','Y','Z'].forEach(function(axis,i){var n=(g[side+'_base_mm']||[])[i];A.el('monitor'+(side==='left'?'Left':'Right')+axis).value=n===null||n===undefined?'':n;});});
    A.el('monitorBaseDistance').value=g.base_center_distance_mm==null?'':g.base_center_distance_mm;layoutWorld(g);
  }
  function setupGeometry(){
    var dialog=document.createElement('dialog');dialog.id='monitorGeometry';dialog.className='monitor-geometry-dialog';dialog.setAttribute('aria-labelledby','monitorGeometryTitle');
    dialog.innerHTML='<header><h2 id="monitorGeometryTitle">双臂基座位置</h2><button class="monitor-preview-close" type="button" aria-label="关闭基座设置">×</button></header><p class="hint">共同坐标系：X 向右、Y 向前、Z 向上，单位毫米。填写左右底座旋转中心的位置，自动计算间距并按比例摆放。</p><form id="monitorGeometryForm"><div class="field"><label for="monitorGeometryMode">位置数据来源</label><select id="monitorGeometryMode"><option value="coordinates">共同坐标系下的左右底座坐标</option><option value="distance">直接填写实测中心距</option></select></div><div id="monitorCoordinateFields">'+['Left','Right'].map(function(side){return '<fieldset><legend>'+(side==='Left'?'左':'右')+'从臂底座</legend><div class="row">'+['X','Y','Z'].map(function(axis){return '<div class="field narrow"><label for="monitor'+side+axis+'">'+axis+' (mm)</label><input id="monitor'+side+axis+'" type="number" step="any" min="-1000000" max="1000000" inputmode="decimal"></div>';}).join('')+'</div></fieldset>';}).join('')+'</div><div class="field" id="monitorDistanceField" hidden><label for="monitorBaseDistance">两底座旋转中心之间的实测距离 (mm)</label><input id="monitorBaseDistance" type="number" min="1" max="10000" step="any" inputmode="decimal" placeholder="填写实际测量值"></div><div class="hint" id="monitorGeometryState">修改自动保存。</div><p class="hint">各臂局部原点、末端坐标和图像像素不属于同一工作台坐标系，不能直接混用。未填完整时只显示未标定的布局示意。</p></form><button class="btn primary" id="monitorGeometryDone" type="button">完成</button>';
    document.body.appendChild(dialog);dialog.querySelector('header button').onclick=function(){dialog.close();};A.el('monitorGeometryDone').onclick=function(){dialog.close();};dialog.onclick=function(e){if(e.target===dialog){var r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}};
    A.el('monitorLayoutButton').onclick=function(){dialog.showModal();};
    A.el('monitorGeometryForm').onsubmit=function(event){event.preventDefault();};
    A.el('monitorGeometryForm').addEventListener('input',function(){var geometry=layoutWorld(readGeometry().monitor_layout);A.el('monitorGeometryState').textContent=geometry.distance_mm===null?'等待完整位置数据；输入会自动保存。':'计算中心距 '+geometry.distance_mm.toFixed(1)+' mm · 正在自动保存…';});
    A.el('monitorGeometryMode').addEventListener('change',function(){layoutWorld(readGeometry().monitor_layout);});
  }
  function configure(next){
    config=next;dual=!!(next.dual_arm||{}).enabled;A.el('modelRightStatus').hidden=!dual;
    ['left','right'].forEach(function(side){var arm=dual?((next.dual_arm||{})[side]||{}):(next.follower||{});A.el('modelLabel'+side).textContent=side==='left'?(dual?'左从臂':'从臂'):'右从臂';A.el('modelPort'+side).textContent=arm.follower_port||arm.port||'未设置串口';});
    if(!geometryBinding)applyGeometry(next);else layoutWorld(readGeometry().monitor_layout);
    var wanted=new Set(),grid=A.el('monitorFeeds');
    (next.cameras||[]).forEach(function(c){var id=canonical(c.index_or_path);wanted.add(id);var signature=JSON.stringify([c.index_or_path,c.width,c.height,c.fps,c.fourcc]),record=streams.get(id);
      if(!record){record=tile(id,c.name,id,'/api/camera/stream?device='+encodeURIComponent(c.index_or_path));streams.set(id,record);grid.appendChild(record.box);record.signature=signature;start(record);}
      else{record.title=c.name;record.box.querySelector('strong').textContent=c.name;if(record.signature!==signature){record.signature=signature;record.url='/api/camera/stream?device='+encodeURIComponent(c.index_or_path);stop(record);start(record);}}
    });
    wanted.add('@depth');streams.forEach(function(record,id){if(!wanted.has(id)){if(preview.record===record)preview.dialog.close();stop(record);record.box.remove();streams.delete(id);}});
    if(!streams.has('@depth')){var d=tile('@depth','深度相机 · Depth','@depth','/api/depth/stream?view=color');streams.set('@depth',d);grid.appendChild(d.box);start(d);}
  }
  function applyArm(arm){if(!arm||!world)return;latest[arm.side]=arm;world.set(arm.side,arm.joints);}
  function connect(){socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/monitor');socket.onopen=function(){A.el('monitorConnection').textContent='反馈已连接';};
    socket.onmessage=function(event){var p=JSON.parse(event.data);
      if(p.type==='snapshot'){setPaused(p.released||p.paused);Object.values(p.arms||{}).forEach(applyArm);}if(p.type==='feedback')applyArm(p.arm);if(p.type==='device-session')setPaused(p.paused);
      if(p.type==='health'){A.el('monitorConnection').textContent='v3.3.0 · '+(p.cameras&&p.cameras.capture_pid?'共享视觉 PID '+p.cameras.capture_pid:'视觉服务待机');if(paused!==!!p.released)setPaused(p.released);
        (p.cameras&&p.cameras.sessions||[]).forEach(function(s){var r=streams.get(canonical(s.source))||streams.get(canonical(s.device));if(r&&r.open&&(s.error||!s.active||s.age_ms>2000))r.img.onerror();});if(p.depth&&p.depth.error){var d=streams.get('@depth');if(d)d.note.textContent=p.depth.error;}}
    };socket.onclose=function(){A.el('monitorConnection').textContent='反馈离线 · 正在重连';clearTimeout(retry);retry=setTimeout(connect,1500);};
  }
  async function mount(){
    panel=document.createElement('aside');panel.className='persistent-monitor';panel.setAttribute('aria-label','常驻设备监视');panel.innerHTML='<header class="monitor-heading"><div><h2>实时监视</h2><span id="monitorConnection">连接反馈中…</span></div><button class="btn sm" id="monitorResume" hidden>恢复监视</button></header><section class="monitor-world"><header><strong id="monitorModeLabel">官方模型</strong><button class="btn subtle sm" id="monitorLayoutButton" type="button">基座位置</button></header><div class="monitor-world-canvas"><canvas id="monitorArmScene" tabindex="0" role="button" aria-label="放大机械臂官方模型场景"></canvas><span class="monitor-world-expand" aria-hidden="true">点击放大 ↗</span></div><div class="monitor-arm-status"><div id="modelLeftStatus"><strong id="modelLabelleft">从臂</strong><span id="modelStateleft">等待反馈</span><small id="modelPortleft"></small></div><div id="modelRightStatus"><strong id="modelLabelright">右从臂</strong><span id="modelStateright">等待反馈</span><small id="modelPortright"></small></div></div><div id="monitorBaseline" class="monitor-layout"></div></section><div id="monitorFeeds" class="monitor-feeds"></div><div class="monitor-footnote">点击任意画面放大。模型随实测反馈更新；断流标记过期。</div>';
    A.q('.app').appendChild(panel);setupPreview();setupGeometry();
    try{world=window.ARM3D.initWorkspace('monitorArmScene','/static/arm_urdf/reBot-DevArm_fixend.urdf');if(world){world.onLoad(function(){Object.values(latest).forEach(applyArm);layoutWorld(readGeometry().monitor_layout);});world.onError(function(text){modelError=text;A.el('monitorBaseline').textContent=text;});}}catch(error){A.el('monitorBaseline').textContent='3D 不可用：'+error.message;}
    var sceneCanvas=A.el('monitorArmScene'),down=null,moved=false;
    sceneCanvas.addEventListener('pointerdown',function(e){down=[e.clientX,e.clientY];moved=false;});sceneCanvas.addEventListener('pointermove',function(e){if(down&&Math.hypot(e.clientX-down[0],e.clientY-down[1])>5)moved=true;});sceneCanvas.addEventListener('click',function(){if(!moved&&preview.kind!=='model')openModel();down=null;});sceneCanvas.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();openModel();}});
    A.el('monitorResume').onclick=function(){A.action(async function(){await A.api.post('/api/devices/resume',{});setPaused(false);});};
    setInterval(function(){['left','right'].forEach(function(side){var arm=latest[side],node=A.el('modelState'+side),age=arm?Math.max(0,Date.now()-arm.timestamp*1000):null;node.textContent=paused?'已断开':age===null?'等待实测反馈':age>1200?'反馈已过期':Math.round(age)+' ms 前';node.classList.toggle('is-stale',age===null||age>1200||paused);});},200);
    try{var initial=await A.api.get('/api/monitor/status');paused=!!initial.released;configure(await A.api.get('/api/config'));setPaused(paused);Object.values(initial.arms||{}).forEach(applyArm);
      geometryBinding=A.autosave.bind({id:'monitor-layout',section:'monitor_layout',root:'#monitorGeometryForm',read:readGeometry,apply:applyGeometry,
        validate:function(){if(!A.el('monitorGeometryForm').checkValidity())throw new Error('请填写有效坐标或正数间距');},
        write:async function(delta){await A.saveConfig(delta);A.el('monitorGeometryState').textContent='位置设置已自动保存。';}});
    }catch(error){A.el('monitorConnection').textContent=error.message;}
    function readyImages(){streams.forEach(function(r){if(r.open&&r.img.naturalWidth&&r.img.currentSrc===r.img.src&&r.box.dataset.live!=='true'){r.box.dataset.live='true';r.note.textContent='实时画面';r.attempts=0;}});requestAnimationFrame(readyImages);}readyImages();A.autosave.onConfig(configure);connect();
  }
  window.MONITOR={mount:mount,getImage:image,paused:function(){return paused;},pause:setPaused,startSource:function(source){var r=streams.get(canonical(source));if(r){r.disabled=false;start(r);}},stopSource:function(source){var r=streams.get(canonical(source));if(r){r.disabled=true;stop(r);}}};
})();
