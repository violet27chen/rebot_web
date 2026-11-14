/* One parcel cycle, placed beside existing policy evaluation. */
(function(){
  'use strict';document.addEventListener('rebot:eval-ready',mount,{once:true});
  async function mount(){
    var A=window.APP,scene=null,plan=null,calibrationId='',busy=false,refreshing=false;
    function pretty(v){return v===undefined||v===null?'':JSON.stringify(v,null,2);}
    function parsed(v,name,fallback){if(!v.trim())return fallback;try{return JSON.parse(v);}catch(e){throw new Error(name+'不是有效 JSON');}}
    function apply(c){var s=c.sorting||{},safe=s.safety||{},cyc=s.cycle||{};
      A.el('sortingYolo').value=s.yolo_model||'';A.el('sortingDevice').value=s.device||'cuda';A.el('sortingConfidence').value=s.confidence===undefined?.6:s.confidence;
      A.el('sortingSkills').value=pretty(s.skills||[]);A.el('sortingTorque').value=pretty(safe.torque_limits_nm);A.el('sortingLimits').value=pretty(safe.joint_limits_rad);
      A.el('sortingVelocity').value=safe.max_velocity_rad_s||.25;A.el('sortingClearance').value=safe.clearance_m||.10;A.el('sortingPush').value=safe.max_push_m||.12;
      if(A.el('sortingCycleArm'))A.el('sortingCycleArm').value=cyc.arm||'auto';
      if(A.el('sortingLeftDir'))A.el('sortingLeftDir').value=pretty(cyc.left_direction||[-1,0,0]).replace(/\s+/g,' ');
    }
    function read(){var r={};['Yolo','Device','Confidence','Skills','Torque','Limits','Velocity','Clearance','Push','CycleArm','LeftDir'].forEach(function(k){var el=A.el('sorting'+k);if(el)r[k]=el.value;});return r;}
    function invalidate(){plan=null;A.el('sortingExecute').disabled=true;A.el('sortingPlanDetails').hidden=true;}
    function selected(){return scene&&(scene.targets||[]).find(function(t){var n=A.q('input[name="sorting-target"]:checked');return n&&t.id===n.value;});}
    function status(r){calibrationId=r.calibration_id;A.el('sortingCalibId').textContent='标定版本：'+calibrationId;A.el('sortingState').textContent=r.ready?'配置齐全 · 等待预演':'配置未完成';
      A.el('sortingRequirements').textContent=r.errors.length?r.errors.join('\n'):'检测与预演可用。执行前核对路径与训练范围。当前仅检查末端路径，仍需确认臂身与周围设备无碰撞。';
      if(r.session&&r.session.phase&&A.status()&&A.status().running)A.el('sortingProgress').textContent='当前阶段：'+r.session.phase;
      else if(r.control_active)A.el('sortingProgress').textContent='动作已结束，独立 CAN 进程保持姿态。请从顶部收工归零并失能。';
    }
    async function refresh(){if(refreshing)return;refreshing=true;try{status(await A.api.get('/api/sorting/status',8000));}catch(e){A.el('sortingState').textContent='状态读取失败';A.setAlert('#sortingAlert',e.message,'err');}finally{refreshing=false;}}
    async function act(button,fn){if(busy)return;busy=true;var text=button.textContent;button.disabled=true;button.textContent='处理中…';A.clearAlert('#sortingAlert');
      try{await A.autosave.flush();await fn();}catch(e){A.setAlert('#sortingAlert',e.message,'err');}finally{busy=false;button.textContent=text;button.disabled=false;A.el('sortingPlan').disabled=!selected();A.el('sortingExecute').disabled=!plan;await refresh();}}
    function click(id,fn){A.el(id).onclick=function(){act(this,fn);};}
    async function post(p,v){return A.api.post('/api/sorting/'+p,v||{},150000);}
    async function draw(r){A.el('sortingPreview').hidden=false;A.el('sortingDepth').src=r.depth;var img=new Image();await new Promise(function(resolve,reject){img.onload=resolve;img.onerror=reject;img.src=r.rgb;});
      var canvas=A.el('sortingRgb');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;var ctx=canvas.getContext('2d');ctx.drawImage(img,0,0);ctx.lineWidth=2;ctx.font='14px sans-serif';
      r.targets.forEach(function(t){var b=t.xyxy;ctx.strokeStyle=t.valid?'#22c55e':'#ef4444';ctx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);ctx.fillStyle=ctx.strokeStyle;ctx.fillText(t.name+(t.valid?' · '+Math.round(t.height_m*1000)+' mm':''),b[0],Math.max(15,b[1]-4));});
    }
    try{
      var cfg=await A.api.get('/api/config');apply(cfg);
      A.autosave.bind({id:'sorting:skills-control',section:'sorting',root:'#sortingConfig',read:read,apply:apply,
        write:async function(delta,full){var p={},map={Yolo:'yolo_model',Device:'device',Confidence:'confidence'};Object.keys(map).forEach(function(k){if(k in delta)p[map[k]]=k==='Confidence'?Number(full[k]):full[k];});
          if('Skills' in delta){p.skills=parsed(full.Skills,'技能清单',[]);if(!Array.isArray(p.skills))throw new Error('技能清单需要 JSON 数组');}
          var safe={},sm={Torque:'torque_limits_nm',Limits:'joint_limits_rad',Velocity:'max_velocity_rad_s',Clearance:'clearance_m',Push:'max_push_m'};Object.keys(sm).forEach(function(k){if(k in delta)safe[sm[k]]=(k==='Torque'||k==='Limits')?parsed(full[k],k==='Torque'?'力矩上限':'关节限位',null):Number(full[k]);});if(Object.keys(safe).length)p.safety=safe;
          if('CycleArm' in delta||'LeftDir' in delta){var cyc={};if('CycleArm' in delta)cyc.arm=full.CycleArm;if('LeftDir' in delta)cyc.left_direction=parsed(full.LeftDir,'向左方向向量',[-1,0,0]);p.cycle=cyc;}
          invalidate();await A.saveConfig({sorting:p});await refresh();}});
      [['sortingScanLeft','left'],['sortingScanRight','right'],['sortingScanTop',null]].forEach(function(pair){var el=A.el(pair[0]);if(!el)return;click(pair[0],async function(){var r=await post('label/scan',pair[1]?{side:pair[1]}:{});var codes=(r.codes||[]).map(function(c){return c.text+'('+c.type+')';}).join(', ')||'无条码';A.el('sortingScanResult').textContent=(pair[1]==='left'?'左腕':pair[1]==='right'?'右腕':'俯视')+'：状态='+r.status+' · 清晰度='+Math.round(r.sharpness)+' · 结果='+codes;});});
      click('sortingDetect',async function(){invalidate();A.el('sortingProgress').textContent='加载本地 YOLO 并获取同步 RGB-D，首次加载可能较慢…';scene=await post('detect');await draw(scene);
        A.el('sortingTargets').innerHTML=scene.targets.length?scene.targets.map(function(t,index){return '<tr><td><label><input type="radio" name="sorting-target" value="'+A.esc(t.id)+'" '+(!t.valid?'disabled':'')+' '+(index===scene.targets.findIndex(function(x){return x.valid;})?'checked':'')+'> '+A.esc(t.name)+'</label></td><td>'+A.esc(t.valid?t.xyz_m.map(function(n){return n.toFixed(3);}).join(' / '):t.error)+'</td><td>'+(t.valid?Math.round(t.height_m*1000)+' mm':'—')+'</td><td>'+(t.points||0)+'</td><td>'+A.esc(t.skill||t.route_error||'深度无效')+'</td></tr>';}).join(''):'<tr><td colspan="5">未检测到包裹</td></tr>';
        A.el('sortingProgress').textContent='已更新冻结检测。选择目标后预演；检测结果 20 秒后需重新获取。';});
      A.el('sortingTargets').onchange=function(){invalidate();A.el('sortingPlan').disabled=!selected();};
      click('sortingPlan',async function(){invalidate();var target=selected();if(!target)throw new Error('请选择有效包裹');var action=A.el('sortingAction')?A.el('sortingAction').value:'';var payload={scene_id:scene.scene_id,target_id:target.id};if(action)payload.action=action;plan=await post('plan',payload);A.el('sortingPlanDetails').hidden=false;
        A.el('sortingPlanDetails').textContent=JSON.stringify({动作模式:plan.action||'legacy',选用从臂:plan.side||plan.skill,路径:plan.paths.map(function(p){return {从臂:p.side,工作台坐标:p.xyz_workspace,原始关节弧度:p.joints};}),有效秒数:plan.expires_in},null,2);
        A.el('sortingProgress').textContent='预演完成，尚未驱动电机。检查两臂路径和周围空间后确认执行。';});
      click('sortingExecute',async function(){if(!plan)throw new Error('请先预演');var modeDesc=plan.action==='cycle'?'将启动【持续分拣清桌】：悬停扫码 → 若朝下则翻面 → 向左推走 → 循环直到桌面清空。':plan.action==='inspect'?'将仅执行【单步悬停】到包裹正上方（爪头朝下）。':plan.action==='push_left'?'将仅执行【双臂配合向左推走】。':(plan.push?'将先用双臂规则推拢，并重新检查位置，再执行 ACT。':'将接近目标，并执行对应 ACT。');if(!await A.confirm('目标：'+plan.target.name+'；模式：'+(plan.action||'单次技能')+'。\n'+modeDesc+'\n确认真实标定、力矩上限、训练范围和完整双臂运动空间均已核对。保持此控制台在线；停止后机械臂保持姿态，随后使用顶部收工归零并失能。',{title:'确认真机执行包裹任务'}))return;
        await post('execute',{plan_id:plan.plan_id,confirm:true});invalidate();await A.refreshStatus();A.el('sortingProgress').textContent='已启动，实时阶段与输出见全局任务栏。';});
      A.el('sortingStop').onclick=function(){A.action(async function(){await A.stop();invalidate();await refresh();},'#sortingAlert');};
      A.el('sortingConfig').addEventListener('input',invalidate);
      var dialog=A.el('sortingTemplateDialog');dialog.addEventListener('click',function(e){if(e.target===dialog)dialog.close();});
      A.el('sortingTemplate').onclick=function(){A.el('sortingTemplateText').textContent=JSON.stringify([{name:'flip_box',class_name:'box',enabled:true,arm:'left',checkpoint:'/绝对路径/pretrained_model',dataset_info:'/该独立技能数据集/meta/info.json',calibration_id:calibrationId,training_bounds:null,start_pose_rad:null,start_tolerance_rad:.2,approach_rpy:{left:null,right:null},camera_map:{'observation.images.top':'/固定俯视RGB设备','observation.images.wrist':'/腕部RGB设备'},task:'填写训练时任务描述',duration_s:5}],null,2);A.modalHost().appendChild(dialog);dialog.showModal();};
      window.addEventListener('pagehide',function(){if(dialog.open)dialog.close();dialog.remove();});
      A.onLog(function(e){if(e.type==='state'&&e.kind==='sorting-run')refresh();});A.autosave.onConfig(function(){invalidate();refresh();});await refresh();
    }catch(e){A.setAlert('#sortingAlert','包裹技能模块加载失败：'+e.message,'err');}
  }
})();
