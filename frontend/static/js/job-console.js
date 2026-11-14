/* Persistent task interaction: independent of iframe pages and log panels. */
(function () {
  'use strict';
  var A=window.APP, current=null, dialog, button, homeButton, openedKey='', dismissedKey='', sending=false;
  var mounted=false, boot=Date.now()/1000, lastEvent=0, openedAwaiting=false, homingRequest=false;
  function key(job){return job ? String(job.job_id||job.name||job.job)+'/'+String(job.prompt_id||0) : '';}
  function close(){if(dialog&&dialog.open)dialog.close();openedKey='';}
  function update(job){
    current=job||null;
    if(!mounted)return;
    var waiting=!!(current&&current.awaiting_input), nextKey=key(current);
    button.disabled=!current;
    button.textContent=waiting?'等待输入 · 点击处理':'命令输入';
    button.classList.toggle('primary',waiting);
    homeButton.disabled=!!current||homingRequest;
    homeButton.title=current?'先结束当前动作，再收工归零并失能':'按当前单臂 / 双臂配置，低速归零、检查到位、失能并释放串口';
    if(dialog.open&&(!current||openedKey!==nextKey||(openedAwaiting&&!waiting)))close();
    if(waiting&&nextKey!==dismissedKey&&!dialog.open)open();
  }
  function open(){
    if(!current||sending)return;
    openedKey=key(current);
    openedAwaiting=!!current.awaiting_input;
    A.el('jobInputTitle').textContent=current.awaiting_input?'命令等待输入':'向当前命令输入';
    A.el('jobInputTask').textContent=current.name||current.job||'后台任务';
    A.el('jobInputPrompt').textContent=current.prompt||current.last_line||'填写要发送的内容，留空发送回车。';
    var input=A.el('jobInputValue');input.value='';
    input.type=/(password|密码)/i.test(current.prompt||'')?'password':'text';
    A.el('jobInputError').textContent='';
    if(!dialog.open)dialog.showModal();
    requestAnimationFrame(function(){input.focus();});
  }
  async function send(event){
    event.preventDefault();
    if(sending||!current||!dialog.open)return;
    var job=current, original=openedKey, payload={text:A.el('jobInputValue').value,job_id:job.job_id};
    if(openedAwaiting)payload.prompt_id=job.prompt_id;
    sending=true;A.el('jobInputSend').disabled=true;
    try{
      await A.api.post('/api/job/input',payload,8000);
      dismissedKey=original;
      if(openedKey===original)close();
      await A.refreshStatus();
    }catch(error){A.el('jobInputError').textContent=error.message;}
    finally{sending=false;A.el('jobInputSend').disabled=false;
      // A second prompt may arrive before the first POST completes.
      if(current&&current.awaiting_input&&key(current)!==dismissedKey&&!dialog.open)open();}
  }
  // Only the persistent browser shell grants movement heartbeat. The runner
  // cannot renew its own permission after the browser/network disconnects.
  var heartbeatPending=false;
  setInterval(async function(){
    if(heartbeatPending||!current||current.kind!=='sorting-run'||!current.job_id)return;
    heartbeatPending=true;
    try{await A.api.post('/api/sorting/heartbeat',{job_id:current.job_id},1200);}
    catch(e){/* CAN watchdog freezes targets when this heartbeat expires. */}
    finally{heartbeatPending=false;}
  },350);
  function mount(){
    if(mounted)return;mounted=true;
    dialog=document.createElement('dialog');dialog.id='jobInputDialog';dialog.className='job-input-dialog';
    dialog.setAttribute('aria-labelledby','jobInputTitle');
    dialog.innerHTML='<form id="jobInputForm"><header><div><h2 id="jobInputTitle">命令等待输入</h2><p id="jobInputTask"></p></div><button type="button" class="btn subtle icon" id="jobInputClose" aria-label="关闭输入框">×</button></header>'+
      '<pre id="jobInputPrompt"></pre><label for="jobInputValue">输入内容</label><input id="jobInputValue" autocomplete="off" maxlength="4095" placeholder="留空发送回车；需要重新校准时输入 c">'+
      '<p class="hint">内容直接发送给当前命令，不作为系统命令执行。收起弹窗后，可从顶部“命令输入”重新打开。</p><p id="jobInputError" role="alert"></p>'+
      '<footer><button type="button" class="btn subtle" id="jobInputLater">稍后输入</button><button type="submit" class="btn primary" id="jobInputSend">发送并继续</button></footer></form>';
    document.body.appendChild(dialog);
    function dismiss(){dismissedKey=openedKey;close();}
    A.el('jobInputClose').onclick=dismiss;A.el('jobInputLater').onclick=dismiss;
    dialog.addEventListener('cancel',function(e){e.preventDefault();dismiss();});
    dialog.addEventListener('click',function(e){if(e.target===dialog)dismiss();});
    dialog.addEventListener('close',function(){A.el('jobInputValue').value='';});
    A.el('jobInputForm').addEventListener('submit',send);
    button=document.createElement('button');button.id='btnJobInput';button.className='btn';button.setAttribute('data-app-nofb','');button.onclick=open;
    homeButton=document.createElement('button');homeButton.id='btnHomeDisable';homeButton.className='btn';homeButton.textContent='收工归零并失能';homeButton.setAttribute('data-app-nofb','');
    homeButton.onclick=function(){A.action(async function(){
      if(current||homingRequest)throw new Error('请先结束当前动作，再执行收工归零');
      if(window.WORKSPACE)await window.WORKSPACE.flush();
      var cfg=await A.api.get('/api/config'), dual=!!(cfg.dual_arm||{}).enabled;
      var ports=dual?['left','right'].map(function(side){return (cfg.dual_arm[side]||{}).follower_port||'';}):[(cfg.follower||{}).port||''];
      if(!ports.every(Boolean))throw new Error('请先配置从臂串口');
      if(!await A.confirm('目标：'+ports.join('、')+'\n即将低速回到既有零位，确认到位后失能并释放串口。\n请确认回零路径无障碍，机械臂归零后有支撑，夹爪内没有包裹。\n此操作不会改写零点。',{title:'确认收工归零并失能'}))return;
      homingRequest=true;update(current);
      try{
        if(window.WORKSPACE)await window.WORKSPACE.flush();
        await A.autosave.flush();
        await A.api.post('/api/devices/home-and-disable',{confirm:true,expected_ports:ports},15000);
        await A.refreshStatus();A.toast('已开始收工归零，到位后失能并释放串口；进度见任务日志','ok');
      }finally{homingRequest=false;update(current);}
    });};
    var host=A.q('.top-right');host.insertBefore(button,A.el('railStop'));host.insertBefore(homeButton,A.el('railStop'));
    A.onLog(function(event){
      if(event.type!=='state'||(event.ts&&event.ts<boot)||event.running===undefined)return;
      if(event.running&&!event.job_id){A.refreshStatus();return;}
      if(event.ts&&event.ts<lastEvent)return;
      lastEvent=event.ts||lastEvent;
      update(event.running?event:null);
    });
    A.onStatus(function(status){if(status&&status.status_ts&&status.status_ts<lastEvent)return;update(status&&status.running?status.job:null);});
    var status=A.status();update(status&&status.running?status.job:null);
  }
  window.JOB_CONSOLE={mount:mount,open:open};
})();
