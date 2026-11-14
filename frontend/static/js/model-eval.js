/* Offline ACT validation: saved observations only, no hardware commands. */
(function(){
  'use strict';var A=window.APP,dialog,binding,datasets=[],timer=null,evaluationId='',busy=false;
  function field(id){return dialog.querySelector('#'+id);}
  function values(){
    var data=datasets.find(function(d){return d.path===field('me_dataset').value;});
    return {base_model:field('me_path').value.trim(),dataset_dir:field('me_dataset').value,
      repo_id:data?data.repo_id:'',episodes:field('me_episodes').value.trim(),
      max_samples:Number(field('me_samples').value),batch_size:Number(field('me_batch').value),device:field('me_device').value,video_backend:'pyav'};
  }
  function apply(cfg){var p=cfg.model_eval||{};
    var map={base_model:'me_path',dataset_dir:'me_dataset',max_samples:'me_samples',batch_size:'me_batch',device:'me_device'};
    Object.keys(map).forEach(function(k){if(p[k]!==undefined)field(map[k]).value=p[k];});
    field('me_episodes').value=Array.isArray(p.episodes)?p.episodes.join(', '):p.episodes||'';
    field('me_model').value=field('me_path').value;
  }
  async function close(){if(busy)return;try{if(binding)await binding.flush();dialog.close();}catch(e){field('me_error').textContent=e.message;}}
  function create(){
    if(dialog)return;
    var host=A.modalHost(),doc=host.ownerDocument;dialog=doc.createElement('dialog');dialog.className='model-eval-dialog';dialog.id='modelEvalDialog';dialog.setAttribute('aria-labelledby','me_title');
    dialog.innerHTML='<div class="notify-modal"><div class="notify-head"><strong id="me_title">ACT 离线模型评估</strong><button class="btn subtle sm" type="button" id="me_close" aria-label="关闭">×</button></div><div class="notify-msg">'+
      '<div class="field"><label for="me_model">选择已有模型 · 自动填入路径</label><select id="me_model"></select></div>'+
      '<div class="field"><label for="me_path">模型路径</label><input id="me_path" placeholder="pretrained_model 目录"></div>'+
      '<div class="field"><label for="me_dataset">测试数据集</label><select id="me_dataset"></select></div>'+
      '<div class="row"><div class="field"><label for="me_samples">最多采样帧数</label><input id="me_samples" type="number" min="1" max="10000" step="1" value="200"></div><div class="field"><label for="me_batch">批大小</label><input id="me_batch" type="number" min="1" max="32" step="1" value="4"></div><div class="field"><label for="me_device">计算设备</label><select id="me_device"><option value="cpu">CPU</option><option value="cuda">CUDA</option></select></div></div>'+
      '<div class="field"><label for="me_episodes">评估剧集编号（从 0 开始，留空为全部）</label><input id="me_episodes" placeholder="例如 0, 2, 4"></div>'+
      '<p class="hint">均匀采样所选剧集，使用检查点保存的归一化配置。建议选择未参与训练的数据；离线动作误差不能代表真实分拣成功率。</p><p id="me_error" class="hint err" role="alert"></p></div><div class="notify-foot"><button class="btn subtle" type="button" id="me_cancel">关闭</button><button class="btn primary" type="button" id="me_start">开始离线评估</button></div></div>';
    host.appendChild(dialog);
    field('me_close').onclick=field('me_cancel').onclick=close;
    dialog.addEventListener('cancel',function(e){e.preventDefault();close();});
    dialog.addEventListener('click',function(e){if(e.target===dialog)close();});
    field('me_model').onchange=function(){if(field('me_model').value)field('me_path').value=field('me_model').value;if(binding)binding.changed();};
    field('me_start').onclick=function(){A.action(async function(){
      if(busy)return;busy=true;field('me_start').disabled=true;field('me_error').textContent='正在检查模型和测试数据…';
      try{
        if(binding)await binding.flush();
        var r=await A.api.post('/api/model-eval/start',values());evaluationId=r.evaluation_id;
        dialog.close();A.el('modelEvalState').textContent='离线评估已启动，正在加载模型…';A.el('modelEvalMetrics').hidden=true;A.el('modelEvalJoints').hidden=true;
        poll();A.refreshStatus();
      }catch(e){field('me_error').textContent=e.message;throw e;}
      finally{busy=false;field('me_start').disabled=false;}
    },'#trainAlert');};
    window.addEventListener('pagehide',function(){clearTimeout(timer);dialog.remove();});
  }
  async function open(path){
    create();field('me_error').textContent='正在读取模型与数据集…';if(!dialog.open)dialog.showModal();
    try{
      var results=await Promise.all([A.api.get('/api/checkpoints'),A.api.get('/api/datasets'),A.api.get('/api/config')]);
      var previous=binding?{model_eval:values()}:results[2];
      datasets=(results[1].datasets||[]).filter(function(d){return d.episodes>0&&d.path;});
      var models=(results[0].checkpoints||[]).filter(function(c){return c.can_finetune!==false&&(!c.policy_type||c.policy_type==='act')&&/pretrained_model\/?$/.test(c.path);});
      field('me_model').innerHTML='<option value="">手动输入模型路径</option>'+models.map(function(c){return '<option value="'+A.esc(c.path)+'">'+A.esc(c.path)+'</option>';}).join('');
      field('me_dataset').innerHTML='<option value="">请选择测试数据集</option>'+datasets.map(function(d){return '<option value="'+A.esc(d.path)+'">'+A.esc(d.repo_id)+' · '+d.episodes+' 集</option>';}).join('');
      apply(previous);
      if(!binding)binding=A.autosave.bind({id:'offline-model-eval',root:dialog,read:function(){return {model_eval:values()};},apply:apply,
        validate:function(){dialog.querySelectorAll('input[type=number]').forEach(function(n){if(!n.checkValidity()||!n.value)throw new Error('请填写有效的采样帧数和批大小');});}});
      var train=results[2].train||{};
      if(path)field('me_path').value=path;
      if(!field('me_path').value)field('me_path').value=A.el('l_base_model').value||train.base_model||'';
      if(!field('me_dataset').value)field('me_dataset').value=train.dataset_dir||'';
      field('me_model').value=field('me_path').value;
      binding.changed();
      field('me_error').textContent='';
    }catch(e){field('me_error').textContent=e.message;}
  }
  function render(r){
    var state=A.el('modelEvalState'),report=r.result;
    if(report){
      state.textContent='评估完成 · '+report.samples+' 帧 · '+report.model_path;
      var labels=[['原始动作 MAE',report.mae],['原始动作 RMSE',report.rmse],['最大动作误差',report.max_error],['归一化 L1（不含 KL）',report.normalized_l1],['每帧推理耗时（ms）',report.inference_ms]];
      A.el('modelEvalMetrics').innerHTML=labels.map(function(v){return '<dt>'+A.esc(v[0])+'</dt><dd>'+Number(v[1]).toFixed(6)+'</dd>';}).join('');A.el('modelEvalMetrics').hidden=false;
      var names=Array.isArray(report.action_names)?report.action_names:[];
      A.el('modelEvalJoints').innerHTML='<table class="table"><thead><tr><th>动作维度</th><th>MAE</th><th>RMSE</th></tr></thead><tbody>'+(report.per_joint_mae||[]).map(function(v,i){return '<tr><td>'+A.esc(names[i]||'关节 '+(i+1))+'</td><td>'+Number(v).toFixed(6)+'</td><td>'+Number(report.per_joint_rmse[i]).toFixed(6)+'</td></tr>';}).join('')+'</tbody></table>';A.el('modelEvalJoints').hidden=false;
      A.el('modelEvalNote').textContent=report.note||'';
    }else state.textContent=r.finished?(r.exit_code?'离线评估失败：'+(r.last_line||'请查看任务日志'):'尚无评估报告，请选择已有模型和测试数据。'):'正在评估：'+(r.last_line||'加载模型与数据…');
  }
  async function poll(){
    clearTimeout(timer);
    try{var r=await A.api.get('/api/model-eval/result'+(evaluationId?'?evaluation_id='+encodeURIComponent(evaluationId):''));render(r);evaluationId=r.evaluation_id||evaluationId;if(!r.finished)timer=setTimeout(poll,1000);}
    catch(e){A.el('modelEvalState').textContent='评估结果读取失败：'+e.message;}
  }
  A.click('btnModelEval',function(){A.action(function(){return open();},'#trainAlert');});
  document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('[data-model-eval]');if(b){e.preventDefault();A.action(function(){return open(b.getAttribute('data-model-eval'));},'#trainAlert');}});
  A.onLog(function(e){if(e.type==='state'&&e.kind==='model-eval'&&e.action==='finish')poll();});
  poll();
})();
