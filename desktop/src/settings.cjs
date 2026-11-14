'use strict';
const fs=require('node:fs');const path=require('node:path');const os=require('node:os');
function defaults(){const win=process.platform==='win32';return {serverUrl:'http://127.0.0.1:8000',projectPath:path.join(os.homedir(),'rebot_web'),pythonPath:win?path.join(os.homedir(),'miniconda3','envs','lerobot','python.exe'):path.join(os.homedir(),'miniconda3','envs','lerobot','bin','python'),autoStart:true,bounds:{width:1520,height:980}};}
function validate(input){
 const d=defaults();const url=new URL(String(input.serverUrl||d.serverUrl));
 if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash||url.pathname!=='/')throw new Error('请输入控制服务的 HTTP(S) 地址，例如 http://127.0.0.1:8000');
 const local=['127.0.0.1','localhost','[::1]'].includes(url.hostname);
 const autoStart=local&&url.protocol==='http:'&&!!input.autoStart;
 return {serverUrl:url.origin,projectPath:String(input.projectPath||d.projectPath).replace(/^~(?=\/)/,os.homedir()),pythonPath:String(input.pythonPath||d.pythonPath).replace(/^~(?=\/)/,os.homedir()),autoStart,bounds:{width:Number.isFinite(input.bounds?.width)?input.bounds.width:d.bounds.width,height:Number.isFinite(input.bounds?.height)?input.bounds.height:d.bounds.height}};
}
function load(file){try{return validate(JSON.parse(fs.readFileSync(file,'utf8')));}catch{return defaults();}}
function save(file,value){const clean=validate(value);fs.mkdirSync(path.dirname(file),{recursive:true});const temp=file+'.tmp';fs.writeFileSync(temp,JSON.stringify(clean,null,2),{mode:0o600});fs.renameSync(temp,file);return clean;}
module.exports={defaults,validate,load,save};
