'use strict';
// Own-account authentication helper. OTPs arrive only through stdin and never
// appear in output, process arguments, snapshots or diagnostic files.
const path=require('node:path');
const {performance}=require('node:perf_hooks');
const {prepare}=require('./prepare-collection-session.cjs');
const BASE=path.resolve(__dirname,'..');
const ORIGIN='https://www.douyin.com';
const PROFILE='/aweme/v1/web/user/profile/self/';

class Commands {
  queue=[]; cancelled=false;
  push(message){
    if(message?.command==='cancel'){this.cancelled=true;return;}
    if(!message||!['ready','otp','complete'].includes(message.command))return;
    if(message.command==='otp'&&(typeof message.code!=='string'||!/^\d{4,8}$/.test(message.code)))return;
    if(this.queue.length<4)this.queue.push(message);
  }
  take(command){const i=this.queue.findIndex(x=>x.command===command);return i<0?null:this.queue.splice(i,1)[0];}
  clear(){this.queue.length=0;}
}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function ownOrigin(page){try{return new URL(page.url()).origin===ORIGIN;}catch{return false;}}
async function uniqueVisible(locator){
  const n=await locator.count();if(n>10)return null;
  const visible=[];for(let i=0;i<n;i++){const item=locator.nth(i);if(await item.isVisible())visible.push(item);}
  return visible.length===1?visible[0]:null;
}
async function bodyWithin(response){
  if(response.status()!==200||Number(response.headers()['content-length']||0)>262144)return null;
  let timer;
  try{
    const raw=await Promise.race([response.body(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('response_timeout')),5000);})]);
    return raw.length<=262144?JSON.parse(raw.toString('utf8')):null;
  }catch{return null;}finally{clearTimeout(timer);}
}

function identityDiagnostic(result){
  const clean={};
  if(['session_ready','invalid_input','identity_failed','needs_login','account_mismatch','browser_identity_unverified','needs_login_or_verification','bootstrap_result_unknown','invalid_result','dependency_missing','bootstrap_failed'].includes(result.status))clean.preparation_status=result.status;
  if(['validation','cookie_header','identity_http','credential_save','complete'].includes(result.bootstrap_phase))clean.bootstrap_phase=result.bootstrap_phase;
  if(['invalid_structure','invalid_metadata','invalid_account','local_expiry','missing_cookie_scopes','invalid_cookie_list','invalid_cookie','invalid_cookie_value','cookie_domain_mismatch','cookie_path_mismatch','invalid_cookie_expiry','invalid_client_context','credential_path_unwritable','preparation_failed'].includes(result.bootstrap_error))clean.bootstrap_error=result.bootstrap_error;
  if(['invalid_input','needs_login','http_failed','http_rejected','unrecognized_response','account_mismatch','identity_verified'].includes(result.identity_status))clean.identity_status=result.identity_status;
  const input=result.identity_evidence||{},evidence={};
  for(const [key,low,high] of [['http_status',100,599],['response_bytes',0,262145],['business_code',-2147483648,2147483647]])
    if(Number.isSafeInteger(input[key])&&input[key]>=low&&input[key]<=high)evidence[key]=input[key];
  for(const key of ['user_present','verification_indicated'])if(typeof input[key]==='boolean')evidence[key]=input[key];
  if(typeof input.response_sha256==='string'&&/^[a-f0-9]{64}$/.test(input.response_sha256))evidence.response_sha256=input.response_sha256;
  if(['connect','request','response_headers','response_body'].includes(input.transport_phase))evidence.transport_phase=input.transport_phase;
  if(['connection_failed','tls_verification_failed','invalid_response','response_exceeds_bound','transport_failed','timeout'].includes(input.transport_error))evidence.transport_error=input.transport_error;
  if(Object.keys(evidence).length)clean.identity_evidence=evidence;
  return clean;
}

async function recover(account,{commands=new Commands(),emit=()=>{},launchContext,verify,
    timeoutMs=600000,initialWaitMs=8000,tickMs=400,armWaitMs=15000}={}){
  if(typeof account!=='string'||!/^[A-Za-z0-9_.-]{2,64}$/.test(account))throw Error('invalid_account');
  let context,identity=null,phase='',code=null,filled=false,armed=false;
  const status=value=>{if(phase!==value){phase=value;emit({type:'status',status:value});}};
  const started=performance.now(),deadline=started+timeoutMs;
  try{
    status('opening_browser');
    if(!launchContext){
      const {chromium}=require(process.env.CLUBOPS_PLAYWRIGHT||'playwright');
      launchContext=()=>chromium.launchPersistentContext(path.join(process.env.CLUBOPS_DATA_DIR||path.join(BASE,'data'),'browser-profile'),
        {...require('../browser_config.cjs')(),headless:false,acceptDownloads:false,timeout:25000});
    }
    context=await launchContext();
    const page=await context.newPage();
    const onResponse=async response=>{
      try{
        const u=new URL(response.url());if(u.origin!==ORIGIN||u.pathname!==PROFILE)return;
        const result=await bodyWithin(response);
        if(result?.status_code===0&&result.user){
          const id=String(result.user.unique_id||result.user.short_id||'');
          identity=id===account?'matched':'mismatch';
        }
      }catch{/* Never emit response bodies or browser errors. */}
    };
    page.on('response',onResponse);
    await page.goto(ORIGIN+'/',{waitUntil:'domcontentloaded',timeout:25000});
    const initialEnd=Math.min(deadline,performance.now()+initialWaitMs);
    while(!identity&&!commands.cancelled&&performance.now()<initialEnd)await sleep(tickMs);
    if(commands.cancelled)return {status:'cancelled'};
    if(identity==='mismatch')return {status:'account_mismatch'};
    if(identity!=='matched'){
      status('arming_relay');
      const armEnd=Math.min(deadline,performance.now()+armWaitMs);
      while(!armed&&!commands.cancelled&&performance.now()<armEnd){
        armed=!!commands.take('ready');if(!armed)await sleep(tickMs);
      }
      if(commands.cancelled)return {status:'cancelled'};
      if(!armed)return {status:'relay_unavailable'};
      // Arm the remote task before clicking a button that may send an SMS.
      if(ownOrigin(page)){
        const saved=await uniqueVisible(page.getByText('一键登录',{exact:true}));
        if(commands.cancelled)return {status:'cancelled'};
        if(saved)await saved.click({timeout:5000});
      }
      status('waiting_sms');
      while(performance.now()<deadline&&!commands.cancelled){
        if(identity==='mismatch')return {status:'account_mismatch'};
        if(identity==='matched')break;
        if(commands.take('complete'))break; // Explicit manual completion still needs independent HTTP identity.
        if(!filled&&!code)code=commands.take('otp')?.code||null;
        if(code&&!filled&&ownOrigin(page)){
          const input=await uniqueVisible(page.locator('input[autocomplete="one-time-code"], input[placeholder="请输入验证码"]'));
          if(input){
            const current=await input.inputValue();
            if(commands.cancelled)return {status:'cancelled'};
            if(current){code=null;status('manual_required');}
            else{
              await input.fill(code,{timeout:5000});code=null;filled=true;
              status('code_filled');
              const submit=await uniqueVisible(page.getByRole('button',{name:'登录',exact:true}));
              const enabled=submit&&await submit.isEnabled();
              if(commands.cancelled)return {status:'cancelled'};
              if(enabled){
                await submit.click({timeout:5000});status('checking_browser');
              }else status('manual_required');
            }
          }else status('manual_required');
        }
        await sleep(tickMs);
      }
      if(commands.cancelled)return {status:'cancelled'};
      if(performance.now()>=deadline)return {status:'timeout'};
    }
    code=null;commands.clear();status('checking_identity');
    // prepare() closes this browser before the independent HTTP check and only
    // persists the encrypted session after the expected account is confirmed.
    const result=await prepare(account,'',{launchContext:async()=>context,verify});
    context=null;
    return {status:result.status==='session_ready'?'completed':result.status,
      browser_closed_before_http:result.browser_closed_before_http===true,
      credential_file_created:result.credential_file_created===true,
      browser_identity_matched:identity==='matched',sms_step_used:armed,code_filled:filled,
      ...identityDiagnostic(result)};
  }catch{return {status:commands.cancelled?'cancelled':'browser_failed'};}
  finally{code=null;commands.clear();await context?.close().catch(()=>{});}
}
module.exports={Commands,recover,uniqueVisible};

if(require.main===module){
  const readline=require('node:readline');
  const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
  const commands=new Commands();let launched=false;
  const emit=message=>process.stdout.write(JSON.stringify(message)+'\n');
  input.on('line',line=>{
    if(line.length>4096){commands.cancelled=true;return;}
    let message;try{message=JSON.parse(line);}catch{commands.cancelled=true;return;}
    if(!launched){
      if(message?.command!=='start'||typeof message.account!=='string'){emit({type:'result',status:'invalid_input'});input.close();return;}
      launched=true;
      recover(message.account,{commands,emit}).then(result=>{
        emit({type:'result',...result});input.close();process.stdin.pause();
        process.exitCode=result.status==='completed'?0:2;
      }).catch(()=>{emit({type:'result',status:'browser_failed'});input.close();process.stdin.pause();process.exitCode=2;});
    }else commands.push(message);
  });
  input.on('close',()=>{commands.cancelled=true;});
}
