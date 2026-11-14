'use strict';const fs=require('node:fs');const path=require('node:path');const {Resvg}=require('@resvg/resvg-js');
const svg=fs.readFileSync(path.join(__dirname,'../frontend/static/img/logo.svg'),'utf8');
fs.mkdirSync(path.join(__dirname,'build'),{recursive:true});
const png=new Resvg(svg,{fitTo:{mode:'width',value:512}}).render().asPng();fs.writeFileSync(path.join(__dirname,'build/icon.png'),png);
const small=new Resvg(svg,{fitTo:{mode:'width',value:256}}).render().asPng(),header=Buffer.alloc(22);header.writeUInt16LE(1,2);header.writeUInt16LE(1,4);header.writeUInt16LE(1,10);header.writeUInt16LE(32,12);header.writeUInt32LE(small.length,14);header.writeUInt32LE(22,18);fs.writeFileSync(path.join(__dirname,'build/icon.ico'),Buffer.concat([header,small]));
