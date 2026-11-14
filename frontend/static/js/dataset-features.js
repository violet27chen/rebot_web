(function(){
 'use strict';var A=window.APP,timer,request=0;
 async function check(){
  var box=A.el('datasetFeatures'),repo=A.el('l_repo_id').value,dir=A.el('l_dataset_dir').value,id=++request;
  if(!repo&&!dir){box.textContent='请选择已采集的数据集。';return;}
  box.textContent='正在读取数据集特征…';box.className='hint';
  try{var r=await A.api.get('/api/datasets/features?repo_id='+encodeURIComponent(repo)+'&dataset_dir='+encodeURIComponent(dir));if(id!==request)return;
    if(!r.found){box.textContent=r.note;box.className='hint warn';return;}
    box.textContent='动作 '+r.action_dim+' 维 · 状态 '+r.state_dim+' 维 · '+r.total_episodes+' 集 · 相机：'+((r.cameras||[]).join('、')||'无')+(A.isDual()&&r.action_dim!==14?'。当前双臂方案预期 14 维动作，请核对所选数据。':'');
    box.className='hint '+(A.isDual()&&r.action_dim!==14?'warn':'ok');
  }catch(e){if(id===request){box.textContent='特征检查失败：'+e.message;box.className='hint err';}}
 }
 function schedule(){clearTimeout(timer);timer=setTimeout(check,650);}
 A.click('btnCheckFeatures',function(){A.action(check,'#trainAlert');});
 ['l_repo_id','l_dataset_dir'].forEach(function(id){['input','change'].forEach(function(e){A.el(id).addEventListener(e,schedule);});});
 A.autosave.onConfig(schedule);A.onStatus(function(){if(!timer)schedule();});setTimeout(check,1500);
})();
