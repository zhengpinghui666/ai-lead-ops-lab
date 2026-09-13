'use strict';
const path=require('node:path'),fs=require('node:fs');
async function follow(input){
 if(!input||!/^\d{5,19}$/.test(input.account)||!/^\d{5,19}$/.test(input.uid)||!/^[A-Za-z0-9_-]{10,200}$/.test(input.sec_uid)||input.account===input.uid)throw Error('invalid_identity');
 const folder=path.resolve(process.env.CLUBOPS_DATA_DIR||path.join(__dirname,'data'),'browser-profile');
 if(!fs.existsSync(folder))return {status:'not_submitted',proof:{submission_started:false,reason:'profile_missing'}};
 let ctx,submitted=false,dispatched=false,receipt=null,stage='open_profile';
 const timer=setTimeout(()=>ctx?.close().catch(()=>{}),55000);
 try{
  const {chromium}=require(process.env.CLUBOPS_PLAYWRIGHT||'playwright');
  ctx=await chromium.launchPersistentContext(folder,{...require('./browser_config.cjs')(),headless:true,timeout:20000,acceptDownloads:false,serviceWorkers:'block'});
  stage='identity';const own=await ctx.request.get('https://www.douyin.com/aweme/v1/web/user/profile/self/?aid=6383',{timeout:10000});
  const identity=await own.json();if(identity.status_code!==0||String(identity.user?.uid)!==input.account)throw Error('identity_not_verified');
  const page=ctx.pages()[0]||await ctx.newPage();
  await page.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());
   if(/\/message\/send|\/conversation\/add_participants|\/commit\/digg/.test(url.pathname))return route.abort();
   if(url.pathname==='/aweme/v1/web/commit/follow/user/'){
    const official=['https://www.douyin.com','https://www-hj.douyin.com'].includes(url.origin);
    if(official&&request.method()==='HEAD')return route.continue();
    const body=new URLSearchParams(request.postData()||'');
    if(!submitted||!official||dispatched||request.method()!=='POST'||body.get('user_id')!==input.uid||body.get('type')!=='1')return route.abort();
    dispatched=true;
   }
   return route.continue();
  });
  const pending=[];
  page.on('response',response=>{if(response.request().method()==='POST'&&new URL(response.url()).pathname==='/aweme/v1/web/commit/follow/user/')pending.push((async()=>{
   const value=await response.json().catch(()=>null);receipt={http_status:response.status(),platform_code:value?.status_code,follow_status:value?.follow_status};
  })());});
  stage='profile';await page.goto('https://www.douyin.com/user/'+input.sec_uid,{waitUntil:'domcontentloaded',timeout:20000});
  const button=page.getByRole('button',{name:'关注',exact:true});
  await button.waitFor({timeout:8000});if(await button.count()!==1)throw Error('ambiguous_follow');
  // The server-rendered button is visible before hydration attaches its handler.
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='关注'&&Object.keys(b).some(k=>k.startsWith('__reactProps$'))),null,{timeout:8000});
  await page.waitForTimeout(1500);
  // The only UI write is this exact profile's Follow button. Sender identity
  // and the resulting request's recipient/type are checked independently.
  stage='follow';submitted=true;await button.click({timeout:5000});
  await page.getByRole('button',{name:/^(已关注|互相关注|相互关注)$/}).waitFor({timeout:10000}).catch(()=>{});
  await Promise.allSettled(pending);
  return {status:receipt?.http_status===200&&receipt?.platform_code===0?'accepted':dispatched?'uncertain':'not_submitted',proof:{transport:'saved_profile_browser',browser_version:2,submission_started:dispatched,...receipt}};
 }catch{return {status:dispatched?'uncertain':'not_submitted',proof:{transport:'saved_profile_browser',browser_version:2,submission_started:dispatched,phase:stage}};}
 finally{clearTimeout(timer);await ctx?.close().catch(()=>{});}
}
module.exports={follow};
if(require.main===module){let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>{input+=s;if(input.length>4096)process.exit(2);});process.stdin.on('end',()=>follow(JSON.parse(input)).then(result=>process.stdout.write(JSON.stringify(result))).catch(()=>process.stdout.write(JSON.stringify({status:'not_submitted',proof:{submission_started:false,reason:'invalid_input'}}))));}
