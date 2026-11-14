'use strict';
const {app,BrowserWindow,Menu,ipcMain,dialog,shell,session}=require('electron');
const path=require('node:path');const fs=require('node:fs');const {pathToFileURL}=require('node:url');
const Settings=require('./settings.cjs');const Backend=require('./backend.cjs');
let window,settings,file,connecting=false,activeConnection=null,closing=false,uiState={kind:'idle',title:'连接本机控制服务',detail:''},bootReady=false;
app.setName('rebot-console');
const launcher=path.join(__dirname,'../renderer/launcher.html');const launcherURL=pathToFileURL(launcher).href;
if(!app.requestSingleInstanceLock()){app.quit();}else{
 app.on('second-instance',()=>{if(window){if(window.isMinimized())window.restore();window.show();window.focus();}});
 app.whenReady().then(start).catch(error=>{dialog.showErrorBox('reBot 启动失败',error.message);app.quit();});
}
function isSetup(event){return window&&!window.isDestroyed()&&event.sender===window.webContents&&event.senderFrame&&event.senderFrame.url===launcherURL;}
function state(kind,title,detail=''){uiState={kind,title,detail};if(window&&!window.isDestroyed()&&window.webContents.getURL()===launcherURL)window.webContents.send('setup:state',uiState);}
async function showLauncher(){bootReady=false;await window.loadFile(launcher);bootReady=true;state(uiState.kind,uiState.title,uiState.detail);}
async function connect(input){
 if(activeConnection)activeConnection.controller.abort();
 const current={controller:new AbortController(),target:null};activeConnection=current;connecting=true;
 try{
   const target=Settings.save(file,{...settings,...input});settings=target;current.target=target;state('loading','正在连接控制服务…',target.serverUrl);
   const info=await Backend.ensureBackend(target,(title,detail)=>{if(activeConnection===current)state('loading',title,detail);},current.controller.signal);
   if(current.controller.signal.aborted||activeConnection!==current)return {ok:false,canceled:true};
   state('loading','控制服务已就绪','正在打开桌面工作区 · 后端 v'+info.version);
   await window.loadURL(target.serverUrl+'/p/home');return {ok:true};
 }catch(error){if(current.controller.signal.aborted||activeConnection!==current)return {ok:false,canceled:true};state('error','暂时无法连接',error.message);return {ok:false,error:error.message};}
 finally{if(activeConnection===current){connecting=false;activeConnection=null;}}
}
function go(page){if(!window||window.isDestroyed())return;const route='/p/'+page;
 if(window.webContents.getURL().startsWith(settings.serverUrl+'/'))window.webContents.executeJavaScript('document.querySelector('+JSON.stringify('.nav a[href="'+route+'"]')+')?.click()').catch(()=>{});
}
async function flush(){if(!window||window.isDestroyed())return;await Promise.race([window.webContents.executeJavaScript('(async()=>{if(window.APP?.autosave)await window.APP.autosave.flush();else if(window.flushDesktopSettings)await window.flushDesktopSettings();})().catch(()=>{})').catch(()=>{}),new Promise(resolve=>setTimeout(resolve,1800))]);}
async function reload(){await flush();window.webContents.reloadIgnoringCache();}
async function start(){
 if(process.platform==='win32')app.setAppUserModelId('local.rebot.console');
 file=path.join(app.getPath('userData'),'connection.json');settings=Settings.load(file);
 const b=settings.bounds||{};
 window=new BrowserWindow({width:Math.max(1000,Math.min(b.width||1520,3000)),height:Math.max(680,Math.min(b.height||980,2000)),minWidth:900,minHeight:620,title:'reBot 机械臂控制台 v'+app.getVersion(),backgroundColor:'#f5f7fb',icon:path.join(__dirname,'../build/icon.png'),show:false,
   webPreferences:{preload:path.join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true,backgroundThrottling:false,webviewTag:false}});
 window.once('ready-to-show',()=>window.show());
 session.defaultSession.setPermissionRequestHandler((contents,permission,callback)=>{let allowed=false;try{allowed=new URL(contents.getURL()).origin===new URL(settings.serverUrl).origin;}catch{}callback(allowed&&['clipboard-sanitized-write','fullscreen'].includes(permission));});
 window.webContents.on('will-navigate',(event,url)=>{let allowed=url===launcherURL;try{allowed=allowed||new URL(url).origin===new URL(settings.serverUrl).origin;}catch{}if(!allowed){event.preventDefault();if(/^https?:\/\//.test(url))shell.openExternal(url);}});
 window.webContents.setWindowOpenHandler(({url})=>{try{if(['http:','https:'].includes(new URL(url).protocol)&&new URL(url).hostname===new URL(settings.serverUrl).hostname)return {action:'allow',overrideBrowserWindowOptions:{webPreferences:{nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true}}};if(/^https?:\/\//.test(url))shell.openExternal(url);}catch{}return {action:'deny'};});
 window.webContents.on('did-fail-load',(_event,code,description,_url,isMainFrame)=>{if(isMainFrame&&code!==-3&&bootReady){state('error','控制服务连接中断',description);showLauncher().catch(()=>{});}});
 window.webContents.on('render-process-gone',()=>{state('error','界面进程已退出','可以重新连接；控制服务在独立进程中运行。');showLauncher().catch(()=>{});});
 window.webContents.on('page-title-updated',(event,title)=>{event.preventDefault();window.setTitle((title||'reBot 控制台')+' · 桌面 v'+app.getVersion());});
 window.on('close',event=>{if(closing)return;event.preventDefault();closing=true;try{Settings.save(file,{...settings,bounds:window.getNormalBounds()});}catch{}flush().finally(()=>{if(window&&!window.isDestroyed())window.destroy();});});
 ipcMain.handle('setup:bootstrap',event=>{if(!isSetup(event))throw new Error('仅连接设置窗口可以读取本地配置');return {settings,platform:process.platform,version:app.getVersion(),state:uiState};});
 ipcMain.handle('setup:save',(event,input)=>{
   if(!isSetup(event))throw new Error('非法配置来源');const next=Settings.save(file,{...settings,...input});settings=next;
   if(activeConnection&&activeConnection.target&&['serverUrl','projectPath','pythonPath','autoStart'].some(k=>next[k]!==activeConnection.target[k])){activeConnection.controller.abort();state('idle','连接设置已更新','点击“连接工作区”应用新设置。');}
   return {ok:true};
 });
 ipcMain.handle('setup:connect',(event,input)=>{if(!isSetup(event))throw new Error('非法配置来源');return connect(input);});
 ipcMain.handle('setup:pick',async(event,kind)=>{if(!isSetup(event))throw new Error('非法配置来源');if(!['project','python'].includes(kind))throw new Error('非法选择类型');const result=await dialog.showOpenDialog(window,{title:kind==='project'?'选择 reBot 项目目录':'选择 LeRobot Python',properties:[kind==='project'?'openDirectory':'openFile']});return result.canceled?'':result.filePaths[0];});
 Menu.setApplicationMenu(Menu.buildFromTemplate([
  {label:'文件',submenu:[{label:'连接设置…',accelerator:'CmdOrCtrl+,',click:async()=>{await flush();state('idle','连接控制服务');await showLauncher();}},{label:'重新加载界面',accelerator:'CmdOrCtrl+R',click:reload},{type:'separator'},{label:'断开设备并退出',click:async()=>{try{const report=await Backend.request(settings.serverUrl,'/api/devices/disconnect-all',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',timeout:30000});if(report.released!==true){const remaining=(report.remaining||[]).map(p=>'PID '+p.pid+' · '+p.devices.join(', ')).join('\n');dialog.showErrorBox('设备未完全释放',(report.note||'仍有设备被占用')+(remaining?'\n'+remaining:''));return;}window.close();}catch(error){dialog.showErrorBox('设备断开失败',error.message);}}},{label:'退出客户端',accelerator:'CmdOrCtrl+Q',click:()=>window.close()}]},
  {label:'工作区',submenu:[['总览','home'],['设备连接与校准','connect'],['摄像头','camera'],['遥操作','teleop'],['数据采集','dataset'],['模型训练','train'],['视觉抓取','grasp'],['从臂控制','arm']].map(([label,page])=>({label,click:()=>go(page)}))},
  {label:'界面',submenu:[{role:'zoomIn',label:'放大'},{role:'zoomOut',label:'缩小'},{role:'resetZoom',label:'重置缩放'},{role:'togglefullscreen',label:'全屏'},{type:'separator'},{label:'开发者工具',accelerator:'CmdOrCtrl+Shift+I',click:()=>window.webContents.toggleDevTools()}]},
  {label:'帮助',submenu:[{label:'关于 reBot',click:()=>dialog.showMessageBox(window,{type:'info',title:'关于 reBot',message:'reBot 机械臂控制台 v'+app.getVersion(),detail:'Windows 与 Linux 均可本机运行控制服务；串口、相机和机械臂由本机后端访问。'})}]}
 ]));
 await showLauncher();await connect(settings);
}
app.on('window-all-closed',()=>app.quit());
