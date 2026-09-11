'use strict';
// User-authorized, project-owned login session only. Authentication may use Chrome;
// the sole comment request runs after Chrome has closed, using Node HTTP only.
// Cookies stay in this process's memory: no file, CLI/env argument, diagnostic or log.
const path=require('node:path');
const fs=require('node:fs');
const {probe,targetURL}=require('./probe-public-http.cjs');
const profile=path.resolve(process.env.CLUBOPS_DATA_DIR||path.resolve(__dirname,'../data'),'browser-profile');
function playwrightPackage(){
  if(process.env.CLUBOPS_PLAYWRIGHT)return process.env.CLUBOPS_PLAYWRIGHT;
  return require.resolve('playwright');
}
async function sessionProbe(value,{launchContext,probeImpl=probe}={}){
  const target=targetURL(value); // Never start authentication for arbitrary targets.
  let context,cookies=[],stage='load_dependency';
  try{
    if(!launchContext){
      if(!fs.existsSync(profile))return {status:'needs_login',requests:0,browser_started:false};
      const {chromium}=require(playwrightPackage());
      launchContext=()=>chromium.launchPersistentContext(profile,{...require('../browser_config.cjs')(),headless:false,timeout:25000,acceptDownloads:false});
    }
    stage='opening_session';context=await launchContext();stage='reading_session';
    cookies=await context.cookies('https://www.douyin.com/aweme/v1/web/comment/list/');
    stage='closing_session';await context.close();context=null;
    if(!cookies.length)return {status:'needs_login',requests:0,browser_started:true,browser_closed_before_request:true};
    const cookieHeader=cookies.map(c=>`${c.name}=${c.value}`).join('; ');
    stage='http_probe';const result=await probeImpl(target.href,{resource:'comments',cookieHeader});
    return {...result,browser_started:true,browser_started_for_auth:true,browser_used_for_http:false,browser_closed_before_request:true,
      session_cookies_attached:true,cookie_count:cookies.length,
      authentication_note:'附带本工具专用会话的 Cookie，不代表已经确认登录有效或接口权限'};
  }catch(error){
    return {status:stage==='load_dependency'&&error.code==='MODULE_NOT_FOUND'?'dependency_missing':'session_probe_failed',stage,
      requests:stage==='http_probe'?null:0,error_type:['Error','TypeError','TimeoutError'].includes(error.name)?error.name:'Error'};
  }finally{
    await context?.close().catch(()=>{});
    cookies=[]; // Release references; not a claim of cryptographic memory erasure.
  }
}
module.exports={sessionProbe,playwrightPackage};
if(require.main===module){
  if(process.argv.length!==3){process.stderr.write('Usage: node scripts/probe-session-http.cjs https://www.douyin.com/video/VIDEO_ID\n');process.exitCode=1;}
  else sessionProbe(process.argv[2]).then(result=>{process.stdout.write(JSON.stringify(result,null,2)+'\n');process.exitCode=result.status==='recognized_comment_json'?0:2;}).catch(()=>{process.stderr.write('Invalid public video URL\n');process.exitCode=1;});
}
