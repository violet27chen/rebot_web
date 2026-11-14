(async function(){
 'use strict';var A=window.APP,busy=false,lastMode=null,stopped=false,serviceReady=false,checking=null;
 var buttons=['zeroRead','zeroRelease','zeroSet'];
 function status(s){
  var dual=!!(s&&s.dual_arm&&s.dual_arm.enabled),select=A.el('zeroArm');
  if(lastMode!==dual){var old=select.value;select.innerHTML=dual?'<option value="left">左从臂</option><option value="right">右从臂</option>':'<option value="left">当前从臂</option>';if(dual)select.value=old;lastMode=dual;}
  buttons.forEach(function(id){A.el(id).disabled=!serviceReady||busy||!!(s&&s.running);});
  select.disabled=busy;A.el('zeroSelectAll').disabled=busy;
  A.qa('#zeroJoints input').forEach(function(node){node.disabled=busy;});
 }
 function errorMessage(error,phase){
  if(error.status===404&&phase!=='result')return '零点调整接口未加载：当前后端与界面版本不一致。请重启控制服务并刷新界面，再点击“重新检测服务”。未发送设备动作。';
  if(error.status===404&&phase==='result')return '本次零点任务结果已失效，可能是服务重启导致。请重新读取实测角度；设零和释放力矩不会自动重发。';
  return error.message||'零点服务请求失败';
 }
 function checkService(){
  if(checking)return checking;
  A.el('zeroService').textContent='正在检查零点服务…';A.el('zeroRetryService').disabled=true;
  checking=(async function(){
   try{
    var result=await A.api.get('/api/devices/zero/capabilities',8000);
    if(result.api_version!==1||!Array.isArray(result.actions)||!['read','release','set-zero'].every(function(action){return result.actions.includes(action);}))throw new Error('零点服务接口不兼容，请更新并重启控制服务');
    if(!result.worker_available)throw new Error('零点操作程序缺失，请恢复 backend/zero_adjust_worker.py 后重试');
    serviceReady=true;A.el('zeroService').textContent='零点服务已就绪 · '+result.version+'+'+result.build;
    A.el('zeroRetryService').hidden=true;A.clearAlert('#zeroAlert');return true;
   }catch(error){
    serviceReady=false;A.el('zeroService').textContent='零点服务未就绪';A.el('zeroRetryService').hidden=false;
    A.setAlert('#zeroAlert',errorMessage(error,'service'),'err');return false;
   }finally{checking=null;A.el('zeroRetryService').disabled=false;status(A.status());}
  })();return checking;
 }
 function selected(){return A.qa('#zeroJoints input:checked').map(function(n){return Number(n.value);});}
 function paint(result){
  A.el('zeroReadings').innerHTML=(result.joints||[]).map(function(j){return '<tr><td>'+j.id+'</td><td>'+Number(j.degrees).toFixed(2)+'°</td><td>'+(j.disabled?'已失能':'已使能')+'</td></tr>';}).join('');
  A.el('zeroState').textContent=(result.side==='right'?'右从臂':'当前 / 左从臂')+' · '+result.port+' · '+new Date(result.timestamp*1000).toLocaleTimeString()+' 实测';
 }
 async function run(action){
  if(busy)return;
  if(!serviceReady){await checkService();return;}
  var joints=selected();if(!joints.length)throw new Error('请至少选择一个关节');
  await A.autosave.flush();
  var cfg=await A.api.get('/api/config'),side=A.el('zeroArm').value,dual=!!(cfg.dual_arm||{}).enabled;
  var arm=dual?(cfg.dual_arm[side]||{}):(cfg.follower||{}),port=dual?arm.follower_port:arm.port;
  var who=(dual?(side==='left'?'左从臂':'右从臂'):'当前从臂')+' · '+port;
  if(!port)throw new Error('请先配置目标从臂串口');
  if(action!=='read'){
   var text=action==='release'?'即将释放 '+who+' 的全部关节力矩。\n请先托住机械臂，失能后按官方零位姿态手动摆放。':
    '目标：'+who+'\n即将将关节 '+joints.join('、')+' 的当前位置设为 0，并保存到电机参数。\n请确认整臂已失能、所选关节处于正确物理零位，夹爪设零时完全闭合。此操作改变校准基准。';
   if(!await A.confirm(text,{title:action==='release'?'二次确认：释放力矩':'二次确认：写入零点'}))return;
  }
  busy=true;status(A.status());A.clearAlert('#zeroAlert');
  var phase='start';
  try{
   var r=await A.api.post('/api/devices/zero/start',{side:side,action:action,joints:joints,confirm:action!=='read',expected_port:port});
   if(!r||typeof r.job_id!=='string'||!r.job_id)throw new Error('后端没有返回零点任务编号，请更新控制服务后重试；不会自动重发操作');
   phase='result';
   A.el('zeroState').textContent='操作已启动，正在等待实测反馈…';
   var end=Date.now()+90000;
   while(!stopped&&Date.now()<end){
    var progress=await A.api.get('/api/devices/zero/result?job_id='+encodeURIComponent(r.job_id));
    if(progress.finished){
     if(progress.exit_code!==0||!progress.result)throw new Error(progress.last_line||'操作失败，请查看全局任务日志');
     paint(progress.result);A.setAlert('#zeroAlert',action==='set-zero'?'所选关节零点已写入、保存并回读确认。重新连接后再启动遥操作。':action==='release'?'已确认全部关节失能，可托住机械臂手动摆位。':'实测角度已更新。','ok');break;
    }
    A.el('zeroState').textContent=progress.last_line||'正在等待设备反馈…';
    await new Promise(function(resolve){setTimeout(resolve,400);});
   }
   if(!stopped&&Date.now()>=end)throw new Error('操作等待超时，请查看全局任务日志；未自动重发设零命令');
  }catch(error){
   if(error.status===404&&phase==='start'){serviceReady=false;A.el('zeroService').textContent='零点服务接口未加载';A.el('zeroRetryService').hidden=false;}
   A.el('zeroState').textContent=phase==='result'?'本次操作结果未确认，请查看错误说明':error.status===404?'操作未启动：零点接口不存在':'操作是否启动尚未确认，请查看全局任务日志，勿重复发送设零';
   throw new Error(errorMessage(error,phase));
  }finally{busy=false;await A.refreshStatus();status(A.status());}
 }
 A.onStatus(status);status(A.status());
 A.click('zeroRetryService',checkService);
 A.change('zeroArm',function(){A.el('zeroReadings').innerHTML='<tr><td colspan="3">目标已变化，请重新读取实测角度。</td></tr>';A.el('zeroState').textContent='未读取当前所选从臂';A.clearAlert('#zeroAlert');});
 [['zeroRead','read'],['zeroRelease','release'],['zeroSet','set-zero']].forEach(function(pair){A.click(pair[0],function(){A.action(function(){return run(pair[1]);},'#zeroAlert');});});
 A.click('zeroSelectAll',function(){A.qa('#zeroJoints input').forEach(function(n){n.checked=true;n.dispatchEvent(new Event('change',{bubbles:true}));});});
 window.addEventListener('pagehide',function(){stopped=true;});
 checkService();
 try{await A.autosave.preferences(['zeroArm','zeroJoint1','zeroJoint2','zeroJoint3','zeroJoint4','zeroJoint5','zeroJoint6','zeroJoint7'],'zero-adjust');}
 catch(error){A.setAlert('#zeroAlert',error.message,'err');}
})();
