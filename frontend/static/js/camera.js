/* Auto-saved camera settings; previews mirror the persistent monitor. */
(async function () {
  'use strict';
  var A = window.APP;
  await A.init({ active: 'camera', title: '摄像头', subtitle: '多路视觉 · 设备与实时画面' });
  var cameras = [], devices = [], serial = 0, editing = null, dirty = false, saving = false, cameraBinding=null, cameraSaveError="";
  var resumeAfterSave = new Set(), desired=new Set(), reconnectTimers=new Map(), retries=new Map(), frameSeen=new Map();
  var running = new Set(), pending = new Set(), streamTimers = new Map();
  var grid = A.el('camGrid'), dialog = A.el('cameraDialog');
  if (dialog && A.modalHost() !== dialog.ownerDocument.body) A.modalHost().appendChild(dialog);
  var shared = window.parent!==window && window.parent.MONITOR;
  var mirrorFrames = new Map();
  function key(source) {
    var s = String(source).trim(), d = devices.find(function (d) {
      return String(d.index) === s || d.path === s || d.aliases.indexOf(s) >= 0;
    });
    return d ? d.path : (/^\d+$/.test(s) ? '/dev/video' + Number(s) : s);
  }
  function sourceLabel(c) { return /^\d+$/.test(String(c.index_or_path)) ? '/dev/video' + c.index_or_path : c.index_or_path; }
  function state() {
    A.el('camState').textContent = cameras.length + ' 路配置 · ' + running.size + ' 路预览';
    A.el('saveState').textContent = cameraSaveError ? '自动保存失败，内容已保留' : dirty ? '正在自动保存…' : '配置已自动保存';
    A.el('btnSaveCam').disabled = !dirty || saving;
    A.el('btnSaveCam').textContent = '重试保存';
    A.el('btnSaveCam').hidden = !cameraSaveError;
  }
  function setDirty() { dirty = true; state(); if(cameraBinding)cameraBinding.changed(); }
  function tile(id) { return grid.querySelector('[data-camera="' + id + '"]'); }
  function stop(c, preserveIntent) {
    clearTimeout(reconnectTimers.get(c._id)); reconnectTimers.delete(c._id);
    if(!preserveIntent){desired.delete(c._id);retries.delete(c._id);}
    var t = tile(c._id);
    cancelAnimationFrame(mirrorFrames.get(c._id)); mirrorFrames.delete(c._id);
    if(t&&t.querySelector('canvas'))t.querySelector('canvas').hidden=true;
    clearTimeout(streamTimers.get(c._id)); streamTimers.delete(c._id);
    if (t) {
      var img = t.querySelector('img'); img.onload = img.onerror = null; img.removeAttribute('src'); img.hidden = true;
      t.querySelector('.camera-placeholder').hidden = false;
      t.querySelector('.camera-placeholder p').textContent = pending.has(c._id) ? '保存配置后即可预览' : '预览已暂停';
      t.querySelector('[data-toggle]').textContent = '启动预览';
      t.querySelector('[data-state]').textContent = '未连接'; t.querySelector('[data-state]').className = 'badge';
    }
    running.delete(c._id); state();
  }
  async function failed(c, message) {
    if (!running.has(c._id)) return;
    stop(c,true);
    var t = tile(c._id); if (!t) return;
    t.querySelector('[data-state]').textContent = '连接失败'; t.querySelector('[data-state]').className = 'badge err';
    t.querySelector('.camera-placeholder p').textContent = message || '设备未返回画面，请检查连接后重试';
    t.querySelector('[data-toggle]').textContent = '立即重试';
    var attempts=(retries.get(c._id)||0)+1;retries.set(c._id,attempts);
    var seconds=Math.min(30,Math.pow(2,Math.min(attempts,5)));
    t.querySelector('[data-state]').textContent='离线 · '+seconds+' 秒后重连';
    reconnectTimers.set(c._id,setTimeout(function(){if(desired.has(c._id)&&cameras.indexOf(c)>=0)start(c,true);},seconds*1000));
    try {
      var s = await A.api.get('/api/cameras/status');
      var detail = s.sessions.find(function (s) { return s.device === key(c.index_or_path); });
      if (detail && detail.error && t.isConnected) t.querySelector('.camera-placeholder p').textContent = detail.error;
    } catch (e) { /* The inline connection error remains visible. */ }
  }
  function start(c, retry) {
    if (pending.has(c._id)) return;
    stop(c,!!retry); desired.add(c._id); var t = tile(c._id); if (!t) return;
    running.add(c._id); state();
    var img = t.querySelector('img'), placeholder = t.querySelector('.camera-placeholder'), previewBox = t.querySelector('.camera-preview');
    if (previewBox && c.width && c.height) previewBox.style.aspectRatio = Number(c.width) + ' / ' + Number(c.height);
    t.querySelector('[data-state]').textContent = '连接中';
    placeholder.querySelector('p').textContent = '正在等待首帧…';
    t.querySelector('[data-toggle]').textContent = '暂停预览';
    if(shared){
      if(shared.ensureSource)shared.ensureSource(c.index_or_path);
      img.hidden=true;var canvas=t.querySelector('canvas');if(!canvas){canvas=document.createElement('canvas');canvas.style.cssText='position:absolute;inset:0;display:block;width:100%;height:100%;object-fit:fill';img.after(canvas);}canvas.hidden=false;
      var lastEnsure=Date.now();
      function draw(){if(!running.has(c._id))return;var source=shared.getImage(c.index_or_path);
        if(shared.paused()){placeholder.hidden=false;placeholder.querySelector('p').textContent='设备已断开，可在常驻监视区恢复';}
        else if(source&&source.naturalWidth&&source.naturalHeight&&source.getAttribute('src')){if(canvas.width!==source.naturalWidth||canvas.height!==source.naturalHeight){canvas.width=source.naturalWidth;canvas.height=source.naturalHeight;}if(previewBox)previewBox.style.aspectRatio=source.naturalWidth+' / '+source.naturalHeight;canvas.getContext('2d').drawImage(source,0,0,canvas.width,canvas.height);placeholder.hidden=true;t.querySelector('[data-state]').textContent='共享实时画面';t.querySelector('[data-state]').className='badge ok';}
        else {placeholder.hidden=false;var detail=shared.sourceState?shared.sourceState(c.index_or_path):null;placeholder.querySelector('p').textContent=detail?detail.message:'正在等待共享相机首帧…';if(Date.now()-lastEnsure>1000){lastEnsure=Date.now();if(shared.ensureSource)shared.ensureSource(c.index_or_path);}}
        mirrorFrames.set(c._id,requestAnimationFrame(draw));
      }draw();return;
    }
    img.hidden = false;
    img.onload = function () {
      if (!running.has(c._id)) return;
      clearTimeout(streamTimers.get(c._id)); placeholder.hidden = true; retries.delete(c._id);
      if (previewBox && img.naturalWidth && img.naturalHeight) previewBox.style.aspectRatio = img.naturalWidth + ' / ' + img.naturalHeight;
      t.querySelector('[data-state]').textContent = '实时画面'; t.querySelector('[data-state]').className = 'badge ok';
    };
    img.onerror = function () { failed(c); };
    img.src = '/api/camera/stream?device=' + encodeURIComponent(c.index_or_path) + '&t=' + Date.now();
    streamTimers.set(c._id, setTimeout(function () {
      // Some browsers do not fire load until a multipart stream finishes.
      if (img.naturalWidth) { img.onload(); return; }
      failed(c, '等待图像超时，请检查 USB 带宽、设备占用及 MJPG 设置');
    }, 12000));
  }
  var RES_PRESETS = [[320,240],[640,480],[800,600],[1024,768],[1280,720],[1920,1080],[2560,1440]];
  function resOptions(w, h) {
    var list = RES_PRESETS.slice();
    if (!list.some(function (r) { return r[0] === Number(w) && r[1] === Number(h); })) list.unshift([Number(w), Number(h)]);
    return list.map(function (r) {
      return '<option value="' + r[0] + 'x' + r[1] + '"' + (r[0] === Number(w) && r[1] === Number(h) ? ' selected' : '') + '>' + r[0] + ' × ' + r[1] + '</option>';
    }).join('');
  }
  function render() {
    // Detach old HTTP streams before rebuilding cards; keep per-device intent.
    grid.querySelectorAll('img').forEach(function (img) { img.onload = img.onerror = null; img.removeAttribute('src'); });
    streamTimers.forEach(clearTimeout); streamTimers.clear();
    grid.innerHTML = cameras.map(function (c, index) {
      var isPending = pending.has(c._id), sdk = false, ar = (Number(c.width) || 640) + ' / ' + (Number(c.height) || 480);
      return '<article class="camera-tile" data-camera="' + c._id + '"><header><div><h3>' + A.esc(c.name) +
        '</h3><div class="camera-source">第 ' + (index + 1) + ' 路 · ' + A.esc(sourceLabel(c)) + '</div></div><span class="badge" data-state>未连接</span></header>' +
        '<div class="camera-preview" style="aspect-ratio:' + ar + '"><img hidden alt="' + A.esc(c.name) + ' 实时画面"><div class="camera-placeholder"><p>' +
        (isPending ? '保存配置后即可预览' : sdk ? 'SDK 相机请使用下方高级诊断' : '预览已暂停') + '</p></div></div><footer>' +
        '<div class="camera-meta">' + A.esc(c.width) + ' × ' + A.esc(c.height) + ' · 采集 ' + A.esc(c.fps) + ' FPS · ' + A.esc(c.fourcc || 'MJPG') + '</div>' +
        '<div class="camera-res"><label>分辨率 <select data-res aria-label="修改 ' + A.esc(c.name) + ' 分辨率">' + resOptions(c.width, c.height) + '</select></label></div>' +
         '<div class="camera-actions"><button class="btn sm" data-toggle data-app-nofb' + (isPending || sdk ? ' disabled' : '') + '>启动预览</button>' +
        '<button class="btn subtle sm" data-snapshot data-app-nofb' + (isPending || sdk ? ' disabled' : '') + '>抓拍</button>' +
        '<button class="btn subtle sm" data-move="-1" data-app-nofb aria-label="将 ' + A.esc(c.name) + ' 画面前移" title="画面前移"' + (index === 0 ? ' disabled' : '') + '>前移</button>' +
        '<button class="btn subtle sm" data-move="1" data-app-nofb aria-label="将 ' + A.esc(c.name) + ' 画面后移" title="画面后移"' + (index === cameras.length - 1 ? ' disabled' : '') + '>后移</button>' +
        '<button class="btn subtle sm" data-edit data-app-nofb>设置</button><button class="btn subtle sm text-danger remove-camera" data-remove data-app-nofb aria-label="移除 ' + A.esc(c.name) + '">移除</button></div></footer></article>';
    }).join('') || '<div class="camera-empty"><h3>尚未添加相机</h3><p>扫描已连接的 USB 视频设备，然后添加到工作区。</p><button class="btn primary" data-empty-add data-app-nofb>添加相机</button></div>';
    cameras.forEach(function (c) { if (running.has(c._id)) start(c); }); state();
  }
  async function discover(announce) {
    var b = A.el('btnDiscover'); b.disabled = true;
    try {
      var data = await A.api.get('/api/cameras/devices'); devices = data.devices || [];
      A.el('deviceReport').textContent = devices.map(function (d) { return d.path + ' · ' + d.name + ' · ' + d.bus; }).join('\n') || '未发现视频采集设备';
      if ((data.errors||[]).length) A.el('deviceReport').textContent += '\n' + data.errors.map(function (e) { return e.path + '：' + e.error; }).join('\n');
      if (announce) A.toast('发现 ' + devices.length + ' 个视频采集设备', 'ok');
    } finally { b.disabled = false; }
  }
  function openEditor(c) {
    editing = c || null; var defaults = c || {name: '', index_or_path: '', width: 640, height: 480, fps: 30, fourcc: 'MJPG', type: 'opencv', preview: true};
    A.el('cameraDialogTitle').textContent = c ? '相机设置 · 自动保存' : '添加相机';
    A.clearAlert('#dialogAlert');
    var available = devices.filter(function (d) { return !cameras.some(function (other) { return other !== c && key(other.index_or_path) === d.path; }); });
    A.el('devicePicker').innerHTML = '<option value="">手动填写设备路径</option>' + available.map(function (d) {
      return '<option value="' + A.esc(d.source) + '">' + A.esc(d.path + ' · ' + d.name) + '</option>';
    }).join('');
    A.el('devicePicker').value = '';
    var initial = defaults.index_or_path;
    if (!c && available.length) { initial = available[0].source; A.el('devicePicker').value = String(initial); }
    A.el('camName').value = defaults.name;
    A.el('camSource').value = initial;
    [['camWidth','width'],['camHeight','height'],['camFps','fps'],['camType','type'],['camFourcc','fourcc']].forEach(function (p) { A.el(p[0]).value = defaults[p[1]] || (p[1] === 'fourcc' ? 'MJPG' : 'opencv'); });
    A.el('camPreview').checked = defaults.preview !== false;
    A.el('camPreset').innerHTML = '<option value="">自定义（手动填写宽高）</option>' + resOptions(A.el('camWidth').value, A.el('camHeight').value);
    A.el('camPreset').value = A.el('camWidth').value + 'x' + A.el('camHeight').value;
    dialog.showModal();
  }
  A.el('camPreset').onchange = function () {
    if (!this.value) return; var m = this.value.split('x');
    A.el('camWidth').value = m[0]; A.el('camHeight').value = m[1]; editChanged();
  };
  ['camWidth','camHeight'].forEach(function (id) { A.el(id).addEventListener('input', function () {
    var v = A.el('camWidth').value + 'x' + A.el('camHeight').value, p = A.el('camPreset');
    p.value = Array.from(p.options).some(function (o) { return o.value === v; }) ? v : '';
  }); });
  A.el('devicePicker').onchange = function () { if (this.value) A.el('camSource').value = this.value; };
  A.el('btnCancelCamera').onclick = function () { dialog.close(); };
  dialog.addEventListener('click', function (e) { if (e.target === dialog) { var r = dialog.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dialog.close(); } });
  A.el('cameraForm').onsubmit = function (e) {
    e.preventDefault(); A.clearAlert('#dialogAlert');
    if(editing){editChanged();if(!A.el('dialogAlert').textContent)dialog.close();return;}
    var source = A.el('camSource').value.trim(), name = A.el('camName').value.trim();
    if (cameras.some(function (c) { return c !== editing && (c.name === name || key(c.index_or_path) === key(source)); })) { A.setAlert('#dialogAlert', '相机名称或物理设备已在列表中，请选择其他设备', 'err'); return; }
    var c = Object.assign({}, editing || {}, {name: name, index_or_path: /^\d+$/.test(source) ? Number(source) : source,
      width: Number(A.el('camWidth').value), height: Number(A.el('camHeight').value), fps: Number(A.el('camFps').value),
      type: A.el('camType').value, fourcc: A.el('camFourcc').value.trim().toUpperCase(), preview: A.el('camPreview').checked});
    if (editing) { stop(editing); cameras[cameras.indexOf(editing)] = c; }
    else { c._id = ++serial; cameras.push(c); }
    pending.add(c._id); if(c.preview!==false)resumeAfterSave.add(c._id); setDirty(); dialog.close(); render(); A.el('btnAddCam').focus();
  };
  A.el('btnDiscover').onclick = function () { A.action(function () { return discover(true); }, '#camAlert'); };
  A.el('btnAddCam').onclick = function () { openEditor(); };
  A.el('btnSaveCam').onclick = function(){cameraBinding.flush().catch(function(){});};
  A.el('btnPreviewAll').onclick = function () { A.action(async function(){
    var button=A.el('btnPreviewAll');button.disabled=true;button.textContent='正在启动全部预览…';
    try{
      if(shared&&shared.paused()){await A.api.post('/api/devices/resume',{});shared.pause(false);}
      cameras.forEach(function(c){start(c);});
      if(!cameras.length)A.setAlert('#camAlert','尚未配置相机，请先添加已连接的视频设备','info');
    }finally{button.disabled=false;button.textContent='全部预览';}
  },'#camAlert'); };
  A.el('btnPauseAll').onclick = function () { cameras.forEach(function(c){stop(c);}); };
  function setResolution(c, w, h) {
    if (!c || !(w >= 16 && h >= 16) || (Number(c.width) === w && Number(c.height) === h)) return;
    if (running.has(c._id)) resumeAfterSave.add(c._id);
    stop(c); pending.add(c._id);
    c.width = w; c.height = h; setDirty(); render();
  }
  grid.onchange = function (e) {
    var sel = e.target.closest('select[data-res]'); if (!sel) return;
    var box = sel.closest('[data-camera]'), c = cameras.find(function (x) { return String(x._id) === box.dataset.camera; });
    var m = sel.value.split('x'); setResolution(c, Number(m[0]), Number(m[1]));
  };
  grid.onclick = function (e) {
    var b = e.target.closest('button'); if (!b) return;
    if (b.hasAttribute('data-empty-add')) { openEditor(); return; }
    var box = b.closest('[data-camera]'), c = cameras.find(function (c) { return String(c._id) === box.dataset.camera; });
    if (b.hasAttribute('data-move')) {
      var from = cameras.indexOf(c), to = from + Number(b.getAttribute('data-move'));
      if (from < 0 || to < 0 || to >= cameras.length || saving) return;
      cameras.splice(to, 0, cameras.splice(from, 1)[0]);
      setDirty(); render(); return;
    }
    if (b.hasAttribute('data-toggle')) { if (running.has(c._id)) stop(c); else start(c); }
    if (b.hasAttribute('data-edit')) openEditor(c);
    if (b.hasAttribute('data-remove')) A.confirm('移除相机「' + c.name + '」？更改会自动保存。', {title: '移除相机'}).then(function (yes) {
      if (!yes || saving) return; stop(c); cameras.splice(cameras.indexOf(c), 1); pending.delete(c._id); setDirty(); render();
    });
    if (b.hasAttribute('data-snapshot')) A.action(async function () {
      b.disabled = true;
      try {
        var r = await fetch('/api/camera/snapshot?device=' + encodeURIComponent(c.index_or_path));
        if (!r.ok) { var d = await r.json(); throw new Error(d.error || '抓拍失败'); }
        var url = URL.createObjectURL(await r.blob()), link = document.createElement('a');
        link.href = url; link.download = c.name + '-' + Date.now() + '.jpg'; document.body.appendChild(link); link.click(); link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000); A.toast('截图已下载', 'ok');
      } finally { b.disabled = false; }
    }, '#camAlert');
  };
  ['Opencv:opencv','Realsense:realsense','Orbbec:orbbec'].forEach(function (p) {
    var parts = p.split(':'); A.click('btnScan' + parts[0], function () { A.action(async function () {
      await A.api.post('/api/cameras/scan', {backend: parts[1]}); A.toast('设备扫描完成，画面继续运行', 'ok');
    }, '#camAlert'); });
  });
  function clean(c){var out=Object.assign({},c);delete out._id;return out;}
  function profile(c){return JSON.stringify([key(c.index_or_path),c.type,c.width,c.height,c.fps,c.fourcc]);}
  function refreshCards(){
    var current=Array.from(grid.querySelectorAll('[data-camera]')).map(function(n){return Number(n.dataset.camera);});
    if(JSON.stringify(current)!==JSON.stringify(cameras.map(function(c){return c._id;}))){render();return;}
    cameras.forEach(function(c){var t=tile(c._id);var pv=t.querySelector('.camera-preview');if(pv&&c.width&&c.height)pv.style.aspectRatio=Number(c.width)+' / '+Number(c.height);t.querySelector('[data-toggle]').disabled=pending.has(c._id);t.querySelector('[data-snapshot]').disabled=pending.has(c._id);t.querySelector('h3').textContent=c.name;t.querySelector('.camera-source').textContent=sourceLabel(c);t.querySelector('.camera-meta').textContent=c.width+' × '+c.height+' · 采集 '+c.fps+' FPS · '+(c.fourcc||'MJPG');var rs=t.querySelector('[data-res]');if(rs)rs.innerHTML=resOptions(c.width,c.height);});
  }
  function applyCameraConfig(cfg){
    var next=cfg.cameras||[], previous=cameras.slice(), restart=[];
    previous.forEach(function(c){if(!next.some(function(n){return key(n.index_or_path)===key(c.index_or_path);}))stop(c);});
    cameras=next.map(function(c){var old=previous.find(function(n){return key(n.index_or_path)===key(c.index_or_path);});
      if(old){if(profile(old)!==profile(c)&&running.has(old._id)){stop(old);restart.push(old._id);}Object.assign(old,c);return old;}
      return Object.assign({},c,{_id:++serial});});
    refreshCards();restart.forEach(function(id){var c=cameras.find(function(c){return c._id===id;});if(c)start(c);});state();
  }
  function editChanged(){
    if(!editing)return;
    var form=A.el('cameraForm');if(!form.checkValidity()){A.setAlert('#dialogAlert','请填写有效参数，修正后会自动保存','warn');return;}
    var source=A.el('camSource').value.trim(),name=A.el('camName').value.trim();
    if(!name||cameras.some(function(c){return c!==editing&&(c.name===name||key(c.index_or_path)===key(source));})){A.setAlert('#dialogAlert','名称或物理设备重复，当前更改尚未保存','warn');return;}
    var next=Object.assign({},editing,{name:name,index_or_path:/^\d+$/.test(source)?Number(source):source,width:Number(A.el('camWidth').value),height:Number(A.el('camHeight').value),fps:Number(A.el('camFps').value),type:A.el('camType').value,fourcc:A.el('camFourcc').value.trim().toUpperCase(),preview:A.el('camPreview').checked});
    if(profile(next)!==profile(editing)){if(running.has(editing._id))resumeAfterSave.add(editing._id);stop(editing);pending.add(editing._id);}
    Object.assign(editing,next);A.clearAlert('#dialogAlert');refreshCards();setDirty();
  }
  ['input','change'].forEach(function(event){A.el('cameraForm').addEventListener(event,editChanged);});
  setInterval(async function(){
    if(shared||document.hidden||!running.size)return;
    try{var info=await A.api.get('/api/cameras/status',6000);cameras.forEach(function(c){if(!running.has(c._id)||pending.has(c._id))return;
      var session=info.sessions.find(function(s){return s.device===key(c.index_or_path);});if(!session)return;
      if(session.error||!session.active){failed(c,session.error||'设备连接已结束，正在重新连接');return;}
      var seen=frameSeen.get(c._id);if(!seen||seen.frames!==session.frames)frameSeen.set(c._id,{frames:session.frames,at:Date.now()});
      else if(Date.now()-seen.at>8000)failed(c,'连续 8 秒未收到新画面，正在重新连接');
    });}catch(e){/* Server connection status appears in the global header. */}
  },2000);
  window.addEventListener('pagehide', function () {
    cameras.forEach(function(c){stop(c);});
    if (dialog && dialog.open) dialog.close();
    if (dialog && dialog.parentNode === A.modalHost()) dialog.remove();
  });
  try {
    var results = await Promise.allSettled([A.api.get('/api/config'), discover(false)]);
    if (results[0].status !== 'fulfilled') throw results[0].reason;
    if (results[1].status !== 'fulfilled') A.setAlert('#camAlert', '设备扫描失败，可手动填写路径：' + results[1].reason.message, 'warn');
    cameras = (results[0].value.cameras || []).map(function (c) { return Object.assign({}, c, {_id: ++serial}); });
    cameraBinding=A.autosave.bind({id:'cameras',atomicArray:true,read:function(){return {cameras:cameras.map(clean)};},apply:applyCameraConfig,
      write:async function(delta,full){saving=true;cameraSaveError='';state();
        try{await A.saveConfig({cameras:full.cameras});
          cameras.forEach(function(c){if(full.cameras.some(function(row){return JSON.stringify(row)===JSON.stringify(clean(c));})){pending.delete(c._id);if(resumeAfterSave.has(c._id)){resumeAfterSave.delete(c._id);start(c);}}});
          dirty=JSON.stringify(cameras.map(clean))!==JSON.stringify(full.cameras);
        }catch(e){cameraSaveError=e.message;throw e;}finally{saving=false;refreshCards();state();}
      }});
    render(); cameras.filter(function (c) { return c.preview !== false; }).forEach(function(c){start(c);});
  } catch (e) { A.setAlert('#camAlert', e.message, 'err'); grid.innerHTML = '<div class="camera-empty">配置加载失败，请刷新页面重试。</div>'; }
document.dispatchEvent(new Event('rebot:camera-ready'));
})();
