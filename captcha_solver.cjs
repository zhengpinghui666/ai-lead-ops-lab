'use strict';
const {spawn}=require('node:child_process');
const path=require('node:path');

function solve(payload,{python,stopping=()=>false,timeoutMs=25000}={}){
  return new Promise(resolve=>{
    if(stopping())return resolve({status:'needs_review',reason:'cancelled'});
    if(typeof python!=='string'||!python)return resolve({status:'needs_review',reason:'dependency_missing'});
    const raw=JSON.stringify(payload);
    if(Buffer.byteLength(raw)>2800000)return resolve({status:'needs_review',reason:'input_too_large'});
    let child,output='',done=false,timer,poll;
    const finish=result=>{if(done)return;done=true;clearTimeout(timer);clearInterval(poll);resolve(result);};
    try{child=spawn(python,[path.join(__dirname,'captcha_engine.py')],{
      cwd:__dirname,windowsHide:true,stdio:['pipe','pipe','ignore'],
      env:{...process.env,PYTHONIOENCODING:'utf-8',OMP_NUM_THREADS:'1'}
    });}catch{return finish({status:'needs_review',reason:'dependency_missing'});}
    // Completion waits for process exit, so cancelling cannot leave a model worker running.
    let stoppedReason='';
    const stop=reason=>{if(stoppedReason)return;stoppedReason=reason;child.kill();};
    timer=setTimeout(()=>stop('solver_timeout'),timeoutMs);
    poll=setInterval(()=>{if(stopping())stop('cancelled');},100);
    child.on('error',()=>finish({status:'needs_review',reason:'dependency_missing'}));
    child.stdout.setEncoding('utf8');
    child.stdout.on('data',chunk=>{output+=chunk;if(output.length>16000)stop('invalid_solver_response');});
    child.stdin.on('error',()=>{});
    child.on('close',code=>{
      if(stoppedReason)return finish({status:'needs_review',reason:stoppedReason});
      try{
        const value=JSON.parse(output);
        finish(code===0&&['predicted','needs_review'].includes(value.status)?value:{status:'needs_review',reason:'invalid_solver_response'});
      }catch{finish({status:'needs_review',reason:'invalid_solver_response'});}
    });
    child.stdin.end(raw);
  });
}
module.exports={solve};
