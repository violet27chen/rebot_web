'use strict';
const fs=require('node:fs');const path=require('node:path');const {execFile,spawn}=require('node:child_process');const {promisify}=require('node:util');const run=promisify(execFile);
async function request(base,pathname,options={}){const response=await fetch(base+pathname,{...options,signal:options.signal?AbortSignal.any([options.signal,AbortSignal.timeout(options.timeout||3500)]):AbortSignal.timeout(options.timeout||3500)});if(!response.ok)throw new Error('控制服务返回 HTTP '+response.status);return response.json();}
async function probe(base,signal){const version=await request(base,'/api/version',{signal});if(!version.version)throw new Error('该地址不是 reBot 控制服务');let platform=version.platform;if(!platform)platform=(await request(base,'/api/status',{signal})).platform;if(!['Linux','Windows'].includes(platform))throw new Error('控制服务需要运行在 Windows 或 Linux 主机上');return version;}
async function ensureBackend(settings,report,signal){
 const check=()=>{if(signal&&signal.aborted)throw new Error('连接已取消');};check();
 try{const info=await probe(settings.serverUrl,signal);return info;}catch(error){check();if(!settings.autoStart)throw new Error('无法连接 '+settings.serverUrl+'：'+error.message+'。请确认本机控制服务已启动且地址可达。');}
 if(!['linux','win32'].includes(process.platform))throw new Error('当前系统暂不支持启动本机控制后端');
 const start=path.join(settings.projectPath,'start.py');
 if(!fs.existsSync(start))throw new Error('找不到本机项目 '+start+'。请在连接设置中选择项目目录。');
 if(!fs.existsSync(settings.pythonPath))throw new Error('找不到 Python 环境：'+settings.pythonPath);
 report('正在启动本机控制服务…','机械臂、串口与相机由本机后端直接控制。');
 const port=new URL(settings.serverUrl).port||'8000';
 if(process.platform==='win32'){
   await new Promise((resolve,reject)=>{
     const child=spawn(settings.pythonPath,[start,'--no-browser','--no-chmod','--host','127.0.0.1','--port',port],{cwd:settings.projectPath,detached:true,stdio:'ignore',windowsHide:true,env:{...process.env,PYTHONUNBUFFERED:'1'}});
     child.once('error',reject);
     child.once('spawn',()=>{child.unref();resolve();});
   });
 }else{
   let active=false;
   try{await run('systemctl',['--user','is-active','--quiet','rebot-web.service'],{timeout:3000});active=true;}catch{}
   if(!active){
     let started=false;
     try{await run('systemctl',['--user','start','rebot-web.service'],{timeout:10000});started=true;}catch{}
     if(!started)await run('systemd-run',['--user','--unit=rebot-web','--collect','--property=Restart=on-failure','--property=RestartSec=3','--working-directory='+settings.projectPath,settings.pythonPath,start,'--no-browser','--no-chmod','--host','127.0.0.1','--port',port],{timeout:10000});
   }
 }
 let lastError;const began=Date.now(),deadline=began+45000;
 while(Date.now()<deadline){
   check();
   try{return await probe(settings.serverUrl,signal);}catch(error){lastError=error;}
   report('正在等待控制服务就绪…','已用时 '+Math.round((Date.now()-began)/1000)+' 秒，可随时修改连接设置。');
   await new Promise(resolve=>setTimeout(resolve,1000));
 }
 throw new Error('本机控制服务未就绪：'+lastError.message+(process.platform==='linux'?'。可使用 journalctl --user -u rebot-web 查看日志。':'。请检查 Python 环境和启动日志。'));
}
module.exports={request,probe,ensureBackend};
