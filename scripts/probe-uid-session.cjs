'use strict';
// Only the project's own profile. Cookies remain in local process/pipe memory.
// Authentication Chrome closes before the one read-only Python HTTPS attempt.
const path=require('node:path');
const fs=require('node:fs');
const {spawn}=require('node:child_process');
const BASE=path.resolve(__dirname,'..');
const PROFILE=path.resolve(process.env.CLUBOPS_DATA_DIR||path.join(BASE,'data'),'browser-profile');

function runPython(input){
  return new Promise(resolve=>{
    let child,raw='',settled=false,timer;
    const finish=result=>{if(settled)return;settled=true;clearTimeout(timer);resolve(result);};
    const failure=()=>({status:'http_probe_failed',phase:'identity',can_send:false,live_verified:false,
      http_attempts:null,detail:'本机 HTTP 核对未正常返回；不自动重试。'});
    try{
      child=spawn(process.env.CLUBOPS_PYTHON||'python',[path.join(BASE,'uid_bootstrap.py')],
        {cwd:BASE,windowsHide:true,stdio:['pipe','pipe','ignore'],env:{...process.env,PYTHONIOENCODING:'utf-8'}});
      timer=setTimeout(()=>{child.kill();finish(failure());},20000);
      const abort=()=>{child.kill();finish(failure());};
      child.on('error',abort);
      child.stdin.on('error',abort);
      child.stdout.setEncoding('utf8');
      child.stdout.on('data',chunk=>{raw+=chunk;if(raw.length>8192){child.kill();finish(failure());}});
      child.on('close',()=>{
        try{
          const result=JSON.parse(raw);
          if(!['identity_verified','invalid_input','needs_login','http_failed','http_rejected','unrecognized_response','account_mismatch'].includes(result.status)
              ||result.can_send!==false||result.live_verified!==false)throw Error('shape');
          finish(result);
        }catch{finish(failure());}
      });
      child.stdin.end(JSON.stringify(input));
    }catch{child?.kill();finish(failure());}
  });
}

async function sessionIdentityProbe(expectedAccount,{launchContext,httpProbe=runPython}={}){
  if(typeof expectedAccount!=='string'||!/^[A-Za-z0-9_.-]{2,64}$/.test(expectedAccount))
    return {status:'invalid_input',http_attempts:0,can_send:false,live_verified:false};
  let context,cookies=[],stage='load_dependency',browserStarted=false,browserClosed=false;
  try{
    if(!launchContext){
      if(!fs.existsSync(PROFILE))return {status:'needs_login',http_attempts:0,browser_started:false,can_send:false,live_verified:false};
      const {chromium}=require(process.env.CLUBOPS_PLAYWRIGHT||'playwright');
      launchContext=()=>chromium.launchPersistentContext(PROFILE,{...require('../browser_config.cjs')(),headless:false,timeout:25000,acceptDownloads:false});
    }
    stage='opening_session';context=await launchContext();browserStarted=true;
    stage='reading_session';
    const blankPage=await context.newPage();
    const userAgent=await blankPage.evaluate(()=>navigator.userAgent);
    cookies=await context.cookies('https://www.douyin.com/aweme/v1/web/user/profile/self/');
    stage='closing_session';await context.close();context=null;browserClosed=true;
    if(!cookies.length)return {status:'needs_login',http_attempts:0,browser_started:true,browser_closed_before_request:true,can_send:false,live_verified:false};
    stage='identity';
    const result=await httpProbe({expected_account:expectedAccount,cookie:cookies.map(c=>`${c.name}=${c.value}`).join('; '),user_agent:userAgent});
    return {...result,browser_started:true,browser_closed_before_request:true,browser_used_for_http:false,
      cookie_count:cookies.length,credential_file_created:false,
      authentication_note:'只核对指定账号的资料；Cookie 存在不等于 IM 鉴权、签名或票据已具备。'};
  }catch(error){
    return {status:stage==='load_dependency'?'dependency_missing':'session_probe_failed',phase:stage,
      http_attempts:stage==='identity'?null:0,browser_started:browserStarted,browser_closed_before_request:browserClosed,
      can_send:false,live_verified:false,detail:'专用会话准备未完成；请检查依赖或当前专用浏览器使用状态。'};
  }finally{
    await context?.close().catch(()=>{});
    cookies=[];
  }
}

module.exports={sessionIdentityProbe};
if(require.main===module){
  if(process.argv.length!==3){process.stderr.write('Usage: node scripts/probe-uid-session.cjs EXPECTED_DOUYIN_ACCOUNT\n');process.exitCode=1;}
  else sessionIdentityProbe(process.argv[2]).then(result=>{process.stdout.write(JSON.stringify(result,null,2)+'\n');process.exitCode=result.status==='identity_verified'?0:2;})
    .catch(()=>{process.stderr.write('Identity check did not complete\n');process.exitCode=2;});
}
