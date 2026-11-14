/* Device mode, ports and calibration live together; configuration autosaves. */
(async function(){
 'use strict';
 var A=window.APP;
 await A.init({active:'connect',title:'设备连接与校准',subtitle:'单臂 / 双臂配置 · 串口连接 · 分别校准'});
  /* Windows：COM 端口由驱动提供，没有 sudo / chmod；本页改为「串口检测」 */
  var winMode = !!(A.status() && A.status().is_windows);
  function applyConnWinUI() {
    if (!winMode) return;
    var t = A.el("permTitle"); if (t) t.textContent = "串口检测（Windows · COM 端口）";
    var s = A.el("permSub"); if (s) s.textContent = "无需 sudo 授权，端口由设备驱动提供";
    var n = A.el("permNote");
    if (n) n.innerHTML = "在「设备管理器 → 端口 (COM 和 LPT)」确认 COM 编号与 USB 转串口驱动；" +
      "端口打不开时，先关闭占用它的其它程序再重新扫描。";
    var f = A.el("sudoPwdField"); if (f) f.hidden = true;
    var u = A.el("btnChmodUsb"); if (u) u.style.display = "none";
    var c = A.el("btnChmod"); if (c) c.textContent = "重新扫描串口";
    var k = A.el("btnCheckPerm"); if (k) k.textContent = "检查串口状态";
    var m = A.el("permModeTh"); if (m) m.textContent = "类型";
    var st = A.el("permStateTh"); if (st) st.textContent = "状态";
    var pt = A.el("portPermTh"); if (pt) pt.textContent = "可用性";
  }

  async function loadPerm(quiet) {
    var body = A.q("#permTable tbody");
    A.showLoading(body, {
      title: winMode ? "正在扫描 COM 端口…" : "正在检查串口权限…",
      detail: winMode ? "正在枚举 Windows 设备管理器中的真实串口"
        : "正在读取 /dev/ttyUSB* 与 /dev/ttyACM* 的真实权限", rows: 3
    });
    try {
      var p = await A.api.get("/api/system/permissions");
      winMode = p.requires_chmod === false;
      applyConnWinUI();
      if (p.devices.length) {
        body.innerHTML = p.devices.map(function (d) {
          return "<tr><td><code>" + A.esc(d.device) + "</code></td><td>" + A.esc(d.mode || "-") +
            "</td><td>" + (d.writable
              ? (winMode ? '<span class="badge ok">可用 · 无需授权</span>'
                : '<span class="badge ok">可读写</span>')
              : (winMode ? '<span class="badge err">不可用</span>'
                : '<span class="badge err">无权限，请输入 sudo 密码</span>')) + "</td></tr>";
        }).join("");
      } else {
        A.loadEmpty(body, winMode ? "未检测到 COM 端口" : "未发现串口设备",
          p.note || "请确认机械臂已接入主机", function () { loadPerm(true); });
      }
      if (!quiet) A.ok(winMode ? "COM 端口扫描完成" : "权限检查完成");
    } catch (e) {
      A.loadError(body, e, function () { loadPerm(true); });
      A.setAlert("#connAlert", e.message, "err");
      A.toast(e.message, "err");
    }
  }

  var cfg = {}, activeDual = false, ports = [], binding = null, ready = false;
  var ids = ['leaderPort','followerPort','left_follower','left_leader','right_follower','right_leader'];
  function value(id) { return A.el(id).value.trim(); }
  function put(id, value) { var n = A.el(id); var v = String(value === undefined ? '' : value); if (n.value !== v) n.value = v; }
  function ratio(id) { return Number(A.el(id).value); }
  function sidePayload(side) {
    return {follower_port:value(side+'_follower'), leader_port:value(side+'_leader'),
      follower_type:value(side+'FollowerType'), follower_id:value(side+'FollowerId'),
      leader_type:value(side+'LeaderType'), leader_id:value(side+'LeaderId'),
      can_adapter:value(side+'Can'), gripper_torque_ratio:ratio(side+'GripForce')};
  }
  function readDevices() {
    return {dual_arm:{enabled:value('robotMode')==='dual', robot_type:value('dualRobotType'),teleop_type:value('dualTeleopType'),
      left_prefix:value('leftPrefix'),right_prefix:value('rightPrefix'),left:sidePayload('left'),right:sidePayload('right')},
      follower:{id:value('followerId'),port:value('followerPort'),can_adapter:value('singleCan'),gripper_torque_ratio:ratio('singleGripForce')},
      leader:{id:value('leaderId'),port:value('leaderPort')}};
  }
  function fillPort(id, saved) {
    var n=A.el(id), selected=saved===undefined?n.value:String(saved||'');
    A.clearState(n);
    n.innerHTML='<option value="">选择串口</option>'+ports.map(function(p){return '<option value="'+A.esc(p.device)+'">'+A.esc(p.device+' · '+(p.description||'未知设备'))+'</option>';}).join('');
    if(selected&&!ports.some(function(p){return p.device===selected;}))n.insertAdjacentHTML('beforeend','<option value="'+A.esc(selected)+'">'+A.esc(selected+'（当前未发现）')+'</option>');
    n.value=selected;
  }
  function applyDevices(c) {
    var d=c.dual_arm||{}, f=c.follower||{}, l=c.leader||{};
    put('robotMode',d.enabled?'dual':'single');
    [['dualRobotType',d.robot_type],['dualTeleopType',d.teleop_type],['leftPrefix',d.left_prefix||'left_arm'],['rightPrefix',d.right_prefix||'right_arm'],
     ['followerId',f.id],['leaderId',l.id],['singleCan',f.can_adapter||'damiao'],['singleGripForce',f.gripper_torque_ratio===undefined?.12:f.gripper_torque_ratio]].forEach(function(p){put(p[0],p[1]);});
    fillPort('leaderPort',l.port);fillPort('followerPort',f.port);
    ['left','right'].forEach(function(side){var v=d[side]||{};fillPort(side+'_follower',v.follower_port);fillPort(side+'_leader',v.leader_port);
      [['FollowerId',v.follower_id],['FollowerType',v.follower_type],['LeaderId',v.leader_id],['LeaderType',v.leader_type],['Can',v.can_adapter||'damiao'],['GripForce',v.gripper_torque_ratio===undefined?.12:v.gripper_torque_ratio]].forEach(function(p){put(side+p[0],p[1]);});});
    paint();
  }
  function paint() {
    if(!ready)return;
    var selected=value('robotMode')==='dual', s=A.status()||{}, connected=activeDual?!!s.dual_connected:!!s.connected;
    A.qa('[data-mode]').forEach(function(n){n.hidden=n.dataset.mode!==(selected?'dual':'single');});
    A.el('currentMode').textContent='当前：'+(activeDual?'双臂':'单臂');
    A.el('connectionTitle').textContent=selected?'左右主从臂连接':'主从臂连接';
    A.el('modeHint').textContent=selected!==activeDual?'正在应用模式，完成后请重新连接设备…':'模式与设备参数自动保存；更换控制对象后需要重新连接。';
    A.el('connState').textContent=connected?'已连接':'未连接';A.el('connState').className='badge '+(connected?'ok':'');
    A.el('btnConnect').textContent=selected?'连接四路串口':'连接主从臂';
    A.el('btnConnect').disabled=selected!==activeDual||!!s.running;
    A.el('btnDisconnect').disabled=!connected||!!s.running;
    A.el('robotMode').disabled=!!s.running;
    A.qa('[data-calibrate]').forEach(function(b){b.disabled=!connected||!!s.running||selected!==activeDual;});
  }
  function renderPorts() {
    var labels={leaderPort:'主臂',followerPort:'从臂',left_follower:'左从臂',left_leader:'左主臂',right_follower:'右从臂',right_leader:'右主臂'};
    var visibleIds=value('robotMode')==='dual'?ids.slice(2):ids.slice(0,2);
    A.el('portCount').textContent='· '+ports.length+' 个';
    A.q('#portTable tbody').innerHTML=ports.map(function(p){return '<tr><td><code>'+A.esc(p.device)+'</code></td><td>'+A.esc(p.description||'-')+'</td><td><span class="badge '+(p.writable?'ok':'err')+'">'+(p.writable?'可读写':'无权限')+'</span></td><td>'+A.esc(visibleIds.filter(function(id){return value(id)===p.device;}).map(function(id){return labels[id];}).join('、')||'未分配')+'</td></tr>';}).join('')||'<tr><td colspan="4">未发现外接串口设备</td></tr>';
  }
  async function loadPorts(quiet) {
    var data=await A.api.get('/api/ports');
    var selected=ids.map(function(id){return value(id);});
    ports=(data.ports||[]).filter(function(p){return /tty(USB|ACM)|\/dev\/serial\/|^COM/i.test(p.device)||(p.description&&p.description!=='n/a')||selected.indexOf(p.device)>=0;});
    ids.forEach(function(id){fillPort(id);});renderPorts();
    if(!quiet)A.toast('发现 '+ports.length+' 个外接串口设备','ok');
  }
  A.onStatus(function(s){if(!s)return;activeDual=!!(s.dual_arm&&s.dual_arm.enabled);paint();});
  A.el('robotMode').onchange=function(){paint();if(binding)binding.changed();};
  A.el('deviceSettings').addEventListener('change',renderPorts);
  A.click('btnScanPorts',function(){A.action(function(){return loadPorts(false);},'#connAlert');});
  A.click('btnConnect',function(){A.action(async function(){
    await binding.flush();var dual=value('robotMode')==='dual',payload={};
    if(dual){payload.dual=true;ids.slice(2).forEach(function(id){payload[id]=value(id);});}
    else {payload.leader_port=value('leaderPort');payload.follower_port=value('followerPort');}
    var keys=dual?ids.slice(2):['leader_port','follower_port'], selected=keys.map(function(k){return payload[k];});
    if(selected.some(function(v){return !v;})||new Set(selected).size!==selected.length)throw new Error('请为每条主臂和从臂选择不同的串口');
    await A.api.post('/api/connect',payload);await A.refreshStatus();A.ok(dual?'左右四路串口已连接':'主从臂已连接','#connAlert');
  },'#connAlert');});
  A.click('btnDisconnect',function(){A.action(async function(){await A.api.post('/api/disconnect',{});await A.refreshStatus();A.ok('设备已断开','#connAlert');},'#connAlert');});
  A.qa('[data-calibrate]').forEach(function(b){b.onclick=function(){A.action(async function(){await binding.flush();var parts=b.dataset.calibrate.split(':');
    if(parts.length===2)await A.api.post('/api/dual/calibrate',{side:parts[0],arm:parts[1]});else await A.api.post('/api/calibrate/'+parts[0],{});
    A.ok(b.textContent+'已启动，可在任务日志中查看进度','#connAlert');await A.refreshStatus();},'#connAlert');};});
  var detectedTypes=null;
  A.click('btnProbeTypes',function(){A.action(async function(){
    var button=A.el('btnProbeTypes');button.disabled=true;button.textContent='正在检测…';
    A.el('typeProbeHint').textContent='正在读取当前 Python 环境的插件注册表…';
    try{
      var r=await A.api.get('/api/dual/types',45000);
      if(r.error)throw new Error(r.error);
      detectedTypes=r;
      [['robotTypeList',r.dual_robots||[]],['teleopTypeList',r.dual_teleops||[]]].forEach(function(pair){A.el(pair[0]).innerHTML=pair[1].map(function(v){return '<option value="'+A.esc(v)+'"></option>';}).join('');});
      var robot=(r.dual_robots||[]).includes('bi_seeed_b601_dm_follower'),leader=(r.dual_teleops||[]).includes('bi_rebot_arm_102_leader');
      A.el('btnApplyTypes').hidden=!(robot&&leader);
      A.el('typeProbeHint').textContent='机器人：'+((r.dual_robots||[]).join('、')||'未发现')+'；主臂：'+((r.dual_teleops||[]).join('、')||'未发现')+(robot&&leader?'。点击“应用匹配的双臂类型”自动填入并保存。':'。请检查当前环境的双臂插件安装。');
    }catch(error){A.el('typeProbeHint').textContent='检测失败：'+error.message;throw error;}
    finally{button.disabled=false;button.textContent='检测双臂类型';}
  },'#connAlert');});
  A.click('btnApplyTypes',function(){A.action(async function(){
    if(!detectedTypes)throw new Error('请先检测双臂类型');
    put('dualRobotType','bi_seeed_b601_dm_follower');put('dualTeleopType','bi_rebot_arm_102_leader');
    put('leftPrefix','left_arm');put('rightPrefix','right_arm');
    binding.changed();await binding.flush();A.ok('已应用并保存真实注册的双臂类型','#connAlert');
  },'#connAlert');});
  A.click('btnChmod',function(){A.action(async function(){if(winMode)return loadPorts(false);var pwd=A.el('sudoPwd');try{await A.api.post('/api/system/chmod',{password:pwd.value,usb:false},35000);await loadPerm(true);await loadPorts(true);A.ok('串口授权完成','#connAlert');}finally{pwd.value='';}},'#connAlert');});
  A.click('btnChmodUsb',function(){A.action(async function(){var pwd=A.el('sudoPwd');try{await A.api.post('/api/system/chmod',{password:pwd.value,usb:true},35000);A.ok('USB 设备授权完成','#connAlert');}finally{pwd.value='';}},'#connAlert');});
  A.click('btnCheckPerm',function(){A.action(function(){return loadPerm(false);},'#connAlert');});
  A.click('btnCheckEnv',function(){A.action(function(){return A.api.post('/api/system/check-env',{});},'#connAlert');});
  [['btnClearRobots','robots'],['btnClearTeleop','teleoperators']].forEach(function(pair){A.click(pair[0],function(){A.action(async function(){if(!await A.confirm('确认清理校准缓存？清理后需要重新校准。',{title:'清理校准缓存'}))return;await A.api.post('/api/system/clear-calibration',{target:pair[1]});A.ok('校准缓存已清理','#connAlert');},'#connAlert');});});
  async function initializeDevices(){
   try {
    cfg=await A.api.get('/api/config');activeDual=!!(cfg.dual_arm&&cfg.dual_arm.enabled);
    A.clearState(A.el('deviceSettings'));A.clearAlert('#connAlert');
    applyDevices(cfg);ready=true;A.el('robotMode').disabled=false;paint();renderPorts();
    binding=A.autosave.bind({id:'devices',root:'#deviceSettings',read:readDevices,apply:applyDevices,
      validate:function(){A.qa('#deviceSettings input[type=number]').forEach(function(n){if(!n.checkValidity()||n.value==='')throw new Error('夹爪力矩上限须为 0.01～1');});},
      write:async function(delta,full){
        if((A.status()||{}).running)throw new Error('任务运行期间不能更改设备配置，请先停止任务');
        var enabled=full.dual_arm.enabled;
        await A.saveConfig(delta);activeDual=enabled;await A.refreshStatus();
        paint();renderPorts();
      }});
    A.autosave.onConfig(function(c){cfg=c;activeDual=!!(c.dual_arm&&c.dual_arm.enabled);paint();});
    loadPorts(true).catch(function(error){A.el('portCount').textContent='· 扫描失败';A.setAlert('#connAlert','串口扫描失败：'+error.message+'。已保存的设备配置仍可查看，可点击“扫描串口”重试。','warn');});
    loadPerm(true);
   } catch(e){ready=false;A.loadError(A.el('deviceSettings'),e,initializeDevices,{attach:true,title:'设备配置读取失败'});A.setAlert('#connAlert','设备信息加载失败：'+e.message,'err');ids.forEach(function(id){A.loadError(A.el(id),e,initializeDevices,{title:'设备配置读取失败'});});}
  }
  await initializeDevices();
  setInterval(function(){if(ready&&!document.hidden&&(!document.activeElement||document.activeElement.tagName!=='SELECT'))loadPorts(true).catch(function(e){A.el('portCount').textContent='· 扫描失败：'+e.message;});},3000);
})();
