'use strict';
// Explicit per-account login. Never requests SMS, sends chat, or changes roles.
const readline=require('node:readline');
const {recover,Commands}=require('./login-recovery.cjs');
const {bootstrap}=require('./bootstrap-uid-session.cjs');
const input=readline.createInterface({input:process.stdin});
const commands=new Commands();let started=false;
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
input.on('line',line=>{
 let event;try{event=JSON.parse(line);}catch{return;}
 if(started){if(['complete','cancel'].includes(event.command))commands.push(event);return;}
 if(event.command!=='start'||typeof event.account!=='string')return;
 started=true;
 void (async()=>{
  const result=await recover(event.account,{commands,emit,manualOnly:true});
  if(result.status==='completed'&&!commands.cancelled){
   emit({type:'status',status:'preparing_im'});
   // Reuses this same account's saved profile; HTTP identity is checked again.
   await bootstrap(event.account);
  }
  emit({type:'result',status:commands.cancelled?'cancelled':result.status});
 })().catch(()=>emit({type:'result',status:'browser_failed'})).finally(()=>{input.close();process.stdin.pause();});
});
input.on('close',()=>{if(!started)process.exitCode=2;});
