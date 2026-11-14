/* Fixed overhead calibration. No actuator commands are issued by this page. */
(function(){
  'use strict';
  document.addEventListener('rebot:camera-ready',mount,{once:true});
  async function mount(){
    var A=window.APP, frame=null, points={rgb:null,depth:null},images={},busy=false;
    var defaults={camera_source:'',control_python:'',calibration:{}};
    function pretty(x){return x===undefined||x===null?'':JSON.stringify(x,null,2);}
    function json(text,name){if(!text.trim())return null;try{return JSON.parse(text);}catch(e){throw new Error(name+'不是有效 JSON');}}
    function vector(text,name){if(!text.trim())return null;var a=text.split(/[,，\s]+/).filter(Boolean).map(Number);if(a.length!==3||!a.every(Number.isFinite))throw new Error(name+'应为三个有限数值（米）');return a;}
    function config(c){var s=Object.assign({},defaults,c.sorting||{}),cal=s.calibration||{};
      var selector=A.el('rgbdSource'),previous=selector.value;
      selector.innerHTML='<option value="">请选择固定俯视彩色相机</option>'+(c.cameras||[]).map(function(cam){return '<option value="'+A.esc(String(cam.index_or_path))+'">'+A.esc(cam.name+' · '+cam.index_or_path)+'</option>';}).join('');
      var source=String(s.camera_source);if(source&&!Array.from(selector.options).some(function(o){return o.value===source;})){var o=new Option(source,source);selector.add(o);}
      selector.value=source;A.el('rgbdPython').value=s.control_python||'';
      A.el('rgbdBounds').value=pretty(cal.workspace_bounds);A.el('rgbdPlane').value=pretty(cal.table_plane);
      render(cal);
    }
    function read(){return {source:A.el('rgbdSource').value,python:A.el('rgbdPython').value.trim(),bounds:A.el('rgbdBounds').value,plane:A.el('rgbdPlane').value};}
    function render(cal){
      var kinds=[['alignment','RGB 与深度对齐','alignment_fit','alignment_verified'],['workspace','深度到工作台','workspace_fit','workspace_verified'],['left','左从臂基座'],['right','右从臂基座']];
      A.el('rgbdSamples').textContent='棋盘 '+(cal.board_samples||[]).length+' 张 · 对应点 '+(cal.points||[]).length+' 个（RGB-D '+(cal.points||[]).filter(function(p){return p.rgb_uv;}).length+' 对）';
      A.el('rgbdFits').innerHTML=kinds.map(function(row){var arm=(cal.arms||{})[row[0]]||{},fit=row[2]?cal[row[2]]:arm.fit,verified=row[3]?cal[row[3]]:arm.verified;
        var residual=fit?'样本 '+fit.samples+' · RMS '+Number(fit.rms_px===undefined?fit.rms_m*1000:fit.rms_px).toFixed(2)+(fit.rms_px===undefined?' mm':' px'):'尚未求解';
        return '<article class="rgbd-fit"><h3>'+A.esc(row[1])+'</h3><p class="hint">'+residual+' · '+(verified?'已确认':'待验证')+'</p><div class="btn-row"><button class="btn" data-solve="'+row[0]+'">求解</button><button class="btn subtle" data-verify="'+row[0]+'" '+(!fit||verified?'disabled':'')+'>验证后确认</button>'+(fit&&fit.worst_index!==undefined?'<button class="btn subtle" data-drop="'+fit.worst_index+'">删除最大误差点 #'+(fit.worst_index+1)+'（'+Math.max.apply(null,fit.residuals||[0]).toFixed(fit.rms_px===undefined?4:2)+(fit.rms_px===undefined?' m':' px')+'）</button>':'')+'</div></article>';
      }).join('');
      A.el('rgbdState').textContent=cal.alignment_verified&&cal.workspace_verified?'定位坐标已确认':'尚未完成标定';
    }
    async function refresh(){var r=await A.api.get('/api/sorting/status',10000);render(r.settings.calibration||{});A.el('rgbdCalibrationId').textContent='当前标定版本：'+r.calibration_id;return r;}
    async function act(button,fn){if(busy)return;busy=true;var label=button.textContent;button.disabled=true;button.textContent='处理中…';A.clearAlert('#rgbdAlert');
      try{await A.autosave.flush();await fn();await refresh();}catch(e){A.setAlert('#rgbdAlert',e.message,'err');}finally{busy=false;button.disabled=false;button.textContent=label;}}
    function click(id,fn){A.el(id).onclick=function(){return act(this,fn);};}
    async function post(path,p){return A.api.post('/api/sorting/'+path,p||{},120000);}
    function paint(which){var canvas=A.el(which==='rgb'?'rgbdRgb':'rgbdDepthView'),img=images[which];if(!img)return;
      canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;var ctx=canvas.getContext('2d');ctx.drawImage(img,0,0);
      var p=points[which];if(p){ctx.strokeStyle='#f43f5e';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(p[0]-10,p[1]);ctx.lineTo(p[0]+10,p[1]);ctx.moveTo(p[0],p[1]-10);ctx.lineTo(p[0],p[1]+10);ctx.stroke();}
    }
    function image(which,url){return new Promise(function(resolve,reject){var img=new Image();img.onload=function(){images[which]=img;paint(which);resolve();};img.onerror=function(){reject(new Error('冻结画面解码失败'));};img.src=url;});}
    try{
      var initial=await A.api.get('/api/config');config(initial);
      A.autosave.bind({id:'rgbd:geometry-config',section:'sorting',root:'#rgbdConfig',read:read,apply:config,
        write:async function(delta,full){var patch={};if('source' in delta)patch.camera_source=full.source;if('python' in delta)patch.control_python=full.python;
          if('bounds' in delta||'plane' in delta){patch.calibration={};if('bounds' in delta)patch.calibration.workspace_bounds=json(full.bounds,'工作区范围');if('plane' in delta)patch.calibration.table_plane=json(full.plane,'桌面平面');patch.calibration.workspace_verified=false;}
          if('source' in delta){patch.calibration=Object.assign(patch.calibration||{},{alignment_verified:false,workspace_verified:false,arms:{left:{verified:false},right:{verified:false}}});frame=null;A.el('rgbdDepth').disabled=true;}
          await A.saveConfig({sorting:patch});}});
      await A.autosave.preferences(['rgbdColumns','rgbdRows','rgbdSquare','rgbdWorld','rgbdSide','rgbdBase'],'rgbd-sampling');
      click('rgbdCapture',async function(){var r=await post('capture');frame=r;points={rgb:null,depth:null};A.el('rgbdViews').hidden=false;
        await Promise.all([image('rgb',r.rgb),image('depth',r.depth)]);A.el('rgbdDepth').disabled=false;
        A.el('rgbdRgbUv').textContent=A.el('rgbdDepthUv').textContent='未选点';A.el('rgbdFrameInfo').textContent='深度 '+r.meta.width+' × '+r.meta.height+' · '+(r.meta.name||'')+' · 此画面用于采样，5 分钟后过期';});
      click('rgbdDepth',async function(){if(!frame)throw new Error('请先冻结画面');await post('calibration/depth',{frame_id:frame.frame_id});A.setAlert('#rgbdAlert','已读取当前深度模式内参；接下来采集 RGB 棋盘和对应点。','ok');});
      click('rgbdBoard',async function(){if(!frame)throw new Error('请先冻结棋盘画面');await post('calibration/board',{frame_id:frame.frame_id,columns:Number(A.el('rgbdColumns').value),rows:Number(A.el('rgbdRows').value),square_m:Number(A.el('rgbdSquare').value)});A.setAlert('#rgbdAlert','已记录棋盘，请移动棋盘后重新冻结。','ok');});
      click('rgbdSolveRgb',async function(){var r=await post('calibration/solve',{kind:'rgb'});A.setAlert('#rgbdAlert','RGB 内参 RMS：'+r.fit.rms_px.toFixed(3)+' px','ok');});
      click('rgbdFk',async function(){var r=await A.api.get('/api/sorting/fk?side='+A.el('rgbdSide').value,35000);A.el('rgbdBase').value=r.xyz.map(function(x){return x.toFixed(6);}).join(', ');A.el('rgbdBase').dispatchEvent(new Event('change',{bubbles:true}));});
      click('rgbdPoint',async function(){if(!frame||!points.depth)throw new Error('先冻结画面，再点击深度图中的物理点');var p={frame_id:frame.frame_id,depth_uv:points.depth,side:A.el('rgbdSide').value},world=vector(A.el('rgbdWorld').value,'工作台坐标'),base=vector(A.el('rgbdBase').value,'基座坐标');
        if(points.rgb)p.rgb_uv=points.rgb;if(world)p.workspace_xyz=world;if(base){if(!world)throw new Error('基座对应点还需要同一点的工作台坐标');p.base_xyz=base;}
        if(!p.rgb_uv&&!world)throw new Error('请再点击 RGB 对应点，或填写工作台坐标');await post('calibration/point',p);points={rgb:null,depth:null};paint('rgb');paint('depth');A.el('rgbdRgbUv').textContent=A.el('rgbdDepthUv').textContent='已记录，请选下一个点';});
      click('rgbdReset',async function(){if(!await A.confirm('将清空棋盘与对应点，并作废全部几何验证结果。已登记的 ACT 技能需要重新核对。',{title:'确认清空标定采样'}))return;await post('calibration/reset',{confirm:true});});
      ['rgb','depth'].forEach(function(which){var canvas=A.el(which==='rgb'?'rgbdRgb':'rgbdDepthView');canvas.onclick=function(e){if(!frame||busy)return;var box=canvas.getBoundingClientRect();points[which]=[Math.round((e.clientX-box.left)*canvas.width/box.width),Math.round((e.clientY-box.top)*canvas.height/box.height)];A.el(which==='rgb'?'rgbdRgbUv':'rgbdDepthUv').textContent='像素：'+points[which].join(', ');paint(which);};});
      A.el('rgbdFits').onclick=function(e){var button=e.target.closest('[data-solve],[data-verify],[data-drop]');if(!button||button.disabled)return;if(button.dataset.drop!==undefined){act(button,async function(){if(!await A.confirm('删除该对应点后相关求解结果需要重新求解并验证。',{title:'删除对应点'}))return;await post('calibration/point/delete',{index:Number(button.dataset.drop)});A.setAlert('#rgbdAlert','已删除，请重新求解。','ok');});return;}act(button,async function(){var kind=button.dataset.solve||button.dataset.verify;
        if(button.dataset.solve){await post('calibration/solve',{kind:kind});A.setAlert('#rgbdAlert','已求解。请用额外点检查误差，再点击“验证后确认”。','ok');}
        else{if(!await A.confirm('确认已用额外测量点检查“'+kind+'”变换方向、单位与对应位置？坐标错误会使后续机械臂目标错误。',{title:'确认标定结果已验证'}))return;await post('calibration/confirm',{kind:kind,confirm:true});}
      });};
      A.autosave.onConfig(function(c){render((c.sorting||{}).calibration||{});});await refresh();
    }catch(e){A.setAlert('#rgbdAlert','标定模块加载失败：'+e.message,'err');}
  }
})();
