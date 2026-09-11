'use strict';
// Explicit authentication preparation using only the project's own profile.
// After Chrome closes, Python verifies identity and one IM read before saving
// current-user DPAPI ciphertext. This command never creates or sends messages.
const path=require('node:path');
const fs=require('node:fs');
const {spawn}=require('node:child_process');
const BASE=path.resolve(__dirname,'..');
const DATA=path.resolve(process.env.CLUBOPS_DATA_DIR||path.join(BASE,'data'));
const ENDPOINT='https://imapi.douyin.com/v1/stranger/get_conversation_list';
const HEADER_NAMES=new Set(['accept','accept-language','content-type','cookie','origin','priority','referer',
  'sec-ch-ua','sec-ch-ua-mobile','sec-ch-ua-platform','sec-fetch-dest','sec-fetch-mode','sec-fetch-site','user-agent']);

function verifyAndSave(input){
  return new Promise(resolve=>{
    let child,raw='',finished=false,timer;
    const failure=()=>({status:'bootstrap_result_unknown',can_send:false,live_verified:false,
      credential_file_created:null,detail:'会话准备未正常返回；检查本机状态，不自动重试。'});
    const finish=result=>{if(finished)return;finished=true;clearTimeout(timer);resolve(result);};
    try{
      child=spawn(process.env.CLUBOPS_PYTHON||'python',[path.join(BASE,'uid_session.py'),'bootstrap'],
        {cwd:BASE,windowsHide:true,stdio:['pipe','pipe','ignore'],env:{...process.env,PYTHONIOENCODING:'utf-8'}});
      const abort=()=>{child.kill();finish(failure());};
      timer=setTimeout(abort,45000);child.on('error',abort);child.stdin.on('error',abort);
      child.stdout.setEncoding('utf8');child.stdout.on('data',s=>{raw+=s;if(raw.length>8192)abort();});
      child.on('close',()=>{try{
        const result=JSON.parse(raw);
        if(result.can_send!==false||result.live_verified!==false)throw Error('invalid response');
        finish(result);
      }catch{finish(failure());}});
      child.stdin.end(JSON.stringify(input));
    }catch{child?.kill();finish(failure());}
  });
}

async function bootstrap(expectedAccount,{launchContext,verify=verifyAndSave}={}){
  if(typeof expectedAccount!=='string'||!/^[A-Za-z0-9_.-]{2,64}$/.test(expectedAccount))
    return {status:'invalid_account',can_send:false,live_verified:false,credential_file_created:false};
  let context,stage='dependencies',closed=false;
  try{
    if(!launchContext){
      if(!fs.existsSync(path.join(DATA,'browser-profile')))return {status:'needs_login',can_send:false,live_verified:false,credential_file_created:false};
      const {chromium}=require(process.env.CLUBOPS_PLAYWRIGHT||'playwright');
      launchContext=()=>chromium.launchPersistentContext(path.join(DATA,'browser-profile'),
        {...require('../browser_config.cjs')(),headless:false,timeout:25000,acceptDownloads:false});
    }
    stage='opening_session';context=await launchContext();
    const page=await context.newPage();
    const pending=page.waitForRequest(r=>r.method()==='POST'&&r.url().split('?')[0]===ENDPOINT,{timeout:15000})
      .then(request=>request,()=>null);
    stage='observing_read_context';
    await page.goto('https://www.douyin.com/',{waitUntil:'domcontentloaded',timeout:25000});
    const request=await pending;
    if(!request)throw Error('read context unavailable');
    const url=new URL(request.url());
    if(url.search)throw Error('unexpected signed query');
    const headers={};
    for(const [key,value] of Object.entries(await request.allHeaders()))if(HEADER_NAMES.has(key.toLowerCase()))headers[key.toLowerCase()]=value;
    const payload=request.postDataBuffer();
    if(!payload||payload.length>16384)throw Error('invalid context');
    const cookies=await context.cookies('https://www.douyin.com/aweme/v1/web/user/profile/self/');
    const input={expected_account:expectedAccount,identity_cookie:cookies.map(c=>`${c.name}=${c.value}`).join('; '),
      headers,payload:payload.toString('base64')};
    stage='closing_session';await context.close();context=null;closed=true;
    stage='http_verification';
    return {...await verify(input),browser_closed_before_http:true,browser_used_for_http:false,
      browser_started:true,script_message_actions:0};
  }catch{
    return {status:'bootstrap_incomplete',phase:stage,can_send:false,live_verified:false,
      credential_file_created:stage==='http_verification'?null:false,browser_closed_before_http:closed,
      detail:'专用认证会话准备未完成；已有业务数据保留，没有发送消息。'};
  }finally{await context?.close().catch(()=>{});}
}

module.exports={bootstrap};
if(require.main===module){
  if(process.argv.length!==3){process.stderr.write('Usage: node scripts/bootstrap-uid-session.cjs EXPECTED_DOUYIN_ACCOUNT\n');process.exitCode=2;}
  else bootstrap(process.argv[2]).then(result=>{process.stdout.write(JSON.stringify(result,null,2)+'\n');process.exitCode=result.status==='session_ready'?0:2;})
    .catch(()=>{process.stderr.write('Session bootstrap did not complete\n');process.exitCode=2;});
}
