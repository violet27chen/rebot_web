'use strict';
const {contextBridge,ipcRenderer}=require('electron');
// The control server never receives filesystem/process IPC capabilities.
if(location.protocol==='file:')contextBridge.exposeInMainWorld('desktopSetup',{
 bootstrap:()=>ipcRenderer.invoke('setup:bootstrap'),
 save:settings=>ipcRenderer.invoke('setup:save',settings),
 connect:settings=>ipcRenderer.invoke('setup:connect',settings),
 pick:kind=>ipcRenderer.invoke('setup:pick',kind),
 onState:callback=>{const listener=(_event,state)=>callback(state);ipcRenderer.on('setup:state',listener);return ()=>ipcRenderer.removeListener('setup:state',listener);}
});
