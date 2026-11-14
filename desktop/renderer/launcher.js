(async()=>{
 'use strict';const api=window.desktopSetup,$=id=>document.getElementById(id),boot=await api.bootstrap();
 $('version').textContent='v'+boot.version;
 for(const id of ['serverUrl','projectPath','pythonPath'])$(id).value=boot.settings[id]||'';$('autoStart').checked=!!boot.settings.autoStart;
 $('serverLabel').textContent=boot.platform==='win32'?'本机或远程控制服务地址':'本机或远程控制服务地址';
 $('serverHelp').textContent='本机运行使用 http://127.0.0.1:8000；也可填写局域网内另一台控制主机的地址。';
 $('autoStartLabel').textContent='服务未启动时，自动启动本机控制后端';
 $('projectLabel').textContent='reBot 项目目录';
 $('pythonLabel').textContent='LeRobot 环境 Python';
 $('platformNote').textContent='启用本机自动启动后，平台、串口、相机和机械臂控制均由这台电脑上的后端执行。';
 function paint(state){$('status').className=state.kind;$('statusTitle').textContent=state.title;$('statusDetail').textContent=state.detail||'';$('connect').disabled=false;$('connect').textContent=state.kind==='loading'?'重新连接':'连接工作区';}
 api.onState(paint);paint(boot.state);
 function values(){return {serverUrl:$('serverUrl').value.trim(),projectPath:$('projectPath').value.trim(),pythonPath:$('pythonPath').value.trim(),autoStart:$('autoStart').checked};}
 let saveTimer;
 async function save(){clearTimeout(saveTimer);if(!$('serverUrl').checkValidity()){ $('saved').textContent='请输入完整的服务地址';return; }try{await api.save(values());$('saved').textContent='连接设置已自动保存';}catch(error){$('saved').textContent='未保存：'+error.message;}}
 window.flushDesktopSettings=save;
 ['input','change'].forEach(event=>$('connectionForm').addEventListener(event,()=>{$('saved').textContent='正在自动保存…';clearTimeout(saveTimer);saveTimer=setTimeout(save,350);}));
 $('connectionForm').onsubmit=async event=>{event.preventDefault();if(!$('connectionForm').checkValidity())return;
 clearTimeout(saveTimer);const current=values();
 try{const result=await api.connect(current);if(!result.ok&&!result.canceled)paint({kind:'error',title:'连接失败',detail:result.error});}catch(error){paint({kind:'error',title:'连接失败',detail:error.message});}
 };
 $('pickProject').onclick=async()=>{const p=await api.pick('project');if(p){$('projectPath').value=p;await save();}};$('pickPython').onclick=async()=>{const p=await api.pick('python');if(p){$('pythonPath').value=p;await save();}};
})();
