'use strict';
// Explicit authentication only. The Python identity check starts after Chrome closes.
const path=require('node:path');
const fs=require('node:fs');
const {spawn}=require('node:child_process');
const BASE=path.resolve(__dirname,'..');
const DATA=path.resolve(process.env.CLUBOPS_DATA_DIR||path.join(BASE,'data'));
const paths={identity:'/aweme/v1/web/user/profile/self/',comments:'/aweme/v1/web/comment/list/',
  replies:'/aweme/v1/web/comment/list/reply/',search:'/aweme/v1/web/search/item/'};
async function bounded(promise,ms){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('response timeout')),ms);})]);}finally{clearTimeout(timer);}}
async function waitForConfirmation(){
  process.stdout.write(JSON.stringify({status:'waiting_for_manual_verification',browser_open:true})+'\n');
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('manual verification timed out')),600000);
    process.stdin.once('data',data=>{clearTimeout(timer);process.stdin.pause();String(data).trim()==='done'?resolve():reject(Error('not confirmed'));});
  });
}
async function prepare(account,verifySearch='',{refreshSession=false,resumeSavedLogin=false,launchContext,verify,confirm=waitForConfirmation}={}){
  if(!/^[A-Za-z0-9_.-]{2,64}$/.test(account||''))throw Error('invalid account');
  if(!launchContext&&!fs.existsSync(path.join(DATA,'browser-profile')))return {status:'needs_login'};
  if(refreshSession&&verifySearch)throw Error('Choose one preparation mode');
  if(resumeSavedLogin&&!refreshSession)throw Error('Saved login requires refresh mode');
  let context;
  try{
    if(!launchContext){
      const {chromium}=require(process.env.CLUBOPS_PLAYWRIGHT||'playwright');
      launchContext=()=>chromium.launchPersistentContext(path.join(DATA,'browser-profile'),
        {...require('../browser_config.cjs')(),headless:false,timeout:25000,acceptDownloads:false});
    }
    context=await launchContext();
    const page=await context.newPage();
    if(refreshSession){
      // Normal navigation may refresh the signed-in browser's cookies. Observe
      // its own profile response before attempting a new independent HTTP check.
      const pending=page.waitForResponse(r=>{try{const u=new URL(r.url());return u.origin==='https://www.douyin.com'&&u.pathname===paths.identity;}catch{return false;}},{timeout:30000}).catch(()=>null);
      await page.goto('https://www.douyin.com/',{waitUntil:'domcontentloaded',timeout:25000});
      if(resumeSavedLogin){
        const button=page.getByText('一键登录',{exact:true});
        await button.waitFor({state:'visible',timeout:8000});
        if(await button.count()!==1)throw Error('ambiguous saved login');
        await button.click({timeout:5000});
      }
      const response=await pending;
      if(!response&&resumeSavedLogin){
        // SMS or other interactive authentication must keep this same page
        // alive. Confirmation triggers the independent HTTP account check.
        await confirm();
      }else if(!response||response.status()!==200||Number(response.headers()['content-length']||0)>262144){
        return {status:'browser_identity_unverified',credential_file_created:false,browser_refreshed:true};
      }else{
        const raw=await bounded(response.body(),7000);
        if(raw.length>262144)return {status:'browser_identity_unverified',credential_file_created:false,browser_refreshed:true};
        const identity=JSON.parse(raw.toString('utf8'));
        if(identity?.status_code!==0||!identity.user){
          // The homepage can return an anonymous profile before the SMS step.
          // Keep the explicit login attempt open until the user finishes it.
          if(resumeSavedLogin)await confirm();
          else return {status:'needs_login_or_verification',credential_file_created:false,browser_refreshed:true};
        }else if(String(identity.user.unique_id||identity.user.short_id||'')!==account)
          return {status:'account_mismatch',credential_file_created:false,browser_refreshed:true};
      }
    }
    if(verifySearch){
      if(verifySearch.length>80||/[\x00-\x1f]/.test(verifySearch))throw Error('invalid keyword');
      await page.goto('https://www.douyin.com/search/'+encodeURIComponent(verifySearch)+'?type=video',
        {waitUntil:'domcontentloaded',timeout:25000});
      await confirm();
    }
    // Snapshot only URL-scoped cookies from this project's browser profile.
    const metadata=await page.evaluate(()=>({user_agent:navigator.userAgent,context:{platform:navigator.platform,
      width:screen.width,height:screen.height,cores:navigator.hardwareConcurrency||1,memory:navigator.deviceMemory||8}}));
    const cookies={};
    for(const [key,url] of Object.entries(paths))cookies[key]=(await context.cookies('https://www.douyin.com'+url))
      .map(({name,value,domain,path,expires})=>({name,value,domain,path,expires}));
    const input={format:'clubops-collection-session-1',account,captured_at:Date.now()/1000,...metadata,cookies};
    await context.close();context=null;
    if(verify)return {...await verify(input),browser_closed_before_http:true,browser_used_for_collection:false,browser_refreshed:refreshSession};
    return await new Promise(resolve=>{
      const child=spawn(process.env.CLUBOPS_PYTHON||'python',[path.join(BASE,'collector_http_session.py')],
        {cwd:BASE,windowsHide:true,stdio:['pipe','pipe','ignore'],env:{...process.env,PYTHONIOENCODING:'utf-8'}});
      let raw='',done=false;
      const finish=x=>{if(done)return;done=true;clearTimeout(timer);resolve({...x,browser_closed_before_http:true,browser_used_for_collection:false,browser_refreshed:refreshSession});};
      const timer=setTimeout(()=>{child.kill();finish({status:'bootstrap_result_unknown'});},25000);
      child.stdout.setEncoding('utf8');child.stdout.on('data',s=>{raw+=s;if(raw.length>8192){child.kill();finish({status:'invalid_result'});}});
      child.on('error',()=>finish({status:'dependency_missing'}));child.stdin.on('error',()=>finish({status:'bootstrap_failed'}));
      child.on('close',()=>{try{finish(JSON.parse(raw));}catch{finish({status:'bootstrap_failed'});}});
      child.stdin.end(JSON.stringify(input));
    });
  }finally{await context?.close().catch(()=>{});}
}
module.exports={prepare};
if(require.main===module)prepare(process.argv[2],process.argv[3]==='--verify-search'?process.argv[4]:'',{refreshSession:['--refresh-session','--resume-login'].includes(process.argv[3]),resumeSavedLogin:process.argv[3]==='--resume-login'}).then(r=>{process.stdout.write(JSON.stringify(r)+'\n');process.exitCode=r.status==='session_ready'?0:2;})
  .catch(()=>{process.stdout.write(JSON.stringify({status:'bootstrap_failed',can_send:false})+'\n');process.exitCode=2;});
