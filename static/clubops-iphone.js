// ClubOps 登录转发 v1 — Scriptable on iPhone
// No credentials are embedded. Configure in the app after import.
'use strict';
// Shared by the generated Scriptable client and isolated protocol tests.
const PhoneForwarder = (() => {
  const messages = {
    ignored:'未匹配本次抖音验证码，未转发。', not_configured:'请先在 Scriptable 中配置手机连接。',
    no_pending:'电脑当前没有等待验证码的登录任务。', wrong_account:'电脑等待的账号与手机配置不一致，未转发。',
    expired:'本次登录任务已过期，或手机时间不匹配。请查看电脑状态。',
    received:'验证码已转发，请以电脑的身份核对结果为准。',
    already_attempted:'本任务已尝试转发，请查看电脑状态；不会自动重复提交。',
    unknown:'提交结果尚未确认，请查看电脑状态；不会自动重发。',
    unauthorized:'手机配对未通过鉴权，请重新核对手机配置。',
    unavailable:'中转暂时不可用，请检查网络和电脑状态。',
    invalid_response:'中转响应不符合预期，未继续处理。', connected:'手机已连接中转；尚未完成验证码转发测试。'
  };
  const outcome = status => ({status, message:messages[status] || messages.unavailable});
  function configuration(value) {
    if (!value || typeof value!=='object' || Array.isArray(value)) throw Error('invalid_config');
    const {origin, account, authorization}=value;
    if (typeof origin!=='string' || origin.length>253 ||
        !/^https:\/\/(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(origin) ||
        /\.(?:local|localhost|internal|test|invalid)$/.test(origin)) throw Error('invalid_origin');
    if (typeof account!=='string' || !/^[A-Za-z0-9_.-]{2,64}$/.test(account)) throw Error('invalid_account');
    if (typeof authorization!=='string' || !/^Bearer [A-Za-z0-9_-]{40,128}$/.test(authorization)) throw Error('invalid_credential');
    return {origin, account, authorization};
  }
  function extract(input, startedAt) {
    let text=input, receivedAt=startedAt;
    if (input && typeof input==='object' && !Array.isArray(input)) {
      if (Object.keys(input).some(k=>!['text','received_at'].includes(k))) return null;
      text=input.text;
      if (input.received_at!==undefined) receivedAt=input.received_at;
    }
    if (typeof text!=='string' || text.length>2000 || !text.includes('抖音') || !text.includes('验证码')) return null;
    const matches=(text.match(/[0-9]+/g)||[]).filter(x=>x.length>=4 && x.length<=8);
    if (matches.length!==1 || !Number.isSafeInteger(receivedAt) || receivedAt<=0) return null;
    return {code:matches[0], received_at:receivedAt};
  }
  async function pending(settings, io) {
    let response;
    try { response=await io.request(settings, '/v1/pending'); } catch { return {error:'unavailable'}; }
    if (response.status===401) return {error:'unauthorized'};
    if (response.status!==200) return {error:'unavailable'};
    const job=response.data;
    if (!job || typeof job!=='object' || Array.isArray(job)) return {error:'invalid_response'};
    if (job.id===null) return {job:null};
    if (typeof job.id!=='string' || !/^[a-f0-9]{32}$/.test(job.id) || typeof job.account!=='string' ||
        !Number.isSafeInteger(job.created_at) || !Number.isSafeInteger(job.expires_at) ||
        job.created_at<=0 || job.expires_at<=job.created_at || job.expires_at-job.created_at>180000)
      return {error:'invalid_response'};
    if (job.account!==settings.account) return {error:'wrong_account'};
    return {job};
  }
  async function check(settings, io) {
    try { settings=configuration(settings); } catch { return outcome('not_configured'); }
    const result=await pending(settings, io);
    return outcome(result.error || 'connected');
  }
  async function forward(input, settings, io) {
    const payload=extract(input, io.now());
    if (!payload) return outcome('ignored');
    try { settings=configuration(settings); } catch { return outcome('not_configured'); }
    const result=await pending(settings,io);
    if (result.error) return outcome(result.error);
    if (!result.job) return outcome('no_pending');
    const job=result.job, now=io.now();
    if (now>=job.expires_at || payload.received_at<job.created_at || payload.received_at>now+30000 ||
        now-payload.received_at>180000) return outcome('expired');
    try {
      // Persist only the task id and deadline BEFORE posting. Never persist the SMS/code.
      if (!await io.admit(job.id,job.expires_at)) return outcome('already_attempted');
    } catch { return outcome('unavailable'); }
    let response;
    try { response=await io.request(settings,'/v1/otp',{id:job.id,code:payload.code,received_at:payload.received_at}); }
    catch { return outcome('unknown'); }
    if (response.status===200 && response.data?.status==='received') return outcome('received');
    if (response.status===401) return outcome('unauthorized');
    if (response.status===409 && ['no_matching_login','stale_otp'].includes(response.data?.error)) return outcome('expired');
    if (response.status===409 && response.data?.error==='already_received') return outcome('already_attempted');
    return outcome('unknown');
  }
  return {configuration,extract,forward,check,outcome};
})();


// Bundled after forwarder-core.cjs. Scriptable APIs are supplied by the iPhone app.
const CLUBOPS_CONFIG_KEY='clubops.iphone.login.config.v1';
const CLUBOPS_ATTEMPTS_KEY='clubops.iphone.login.attempts.v1';
const CLUBOPS_STARTED_AT=Date.now();
function readPhoneConfig(){
  try { return PhoneForwarder.configuration(JSON.parse(Keychain.get(CLUBOPS_CONFIG_KEY))); }
  catch { return null; }
}
const phoneIO={
  now:()=>Date.now(),
  async admit(id,expiresAt){
    let rows=[];
    if(Keychain.contains(CLUBOPS_ATTEMPTS_KEY)){
      rows=JSON.parse(Keychain.get(CLUBOPS_ATTEMPTS_KEY));
      if(!Array.isArray(rows)||rows.some(r=>!r||typeof r.id!=='string'||!Number.isSafeInteger(r.expires_at)))throw Error('invalid_ledger');
    }
    rows=rows.filter(r=>r.expires_at>Date.now());
    if(rows.some(r=>r.id===id))return false;
    rows.push({id,expires_at:expiresAt});
    Keychain.set(CLUBOPS_ATTEMPTS_KEY,JSON.stringify(rows.slice(-8)));
    return true;
  },
  async request(settings,path,body){
    if(!['/v1/pending','/v1/otp'].includes(path))throw Error('invalid_route');
    const url=settings.origin+path, request=new Request(url);
    request.method=body?'POST':'GET';
    request.headers={Authorization:settings.authorization,Accept:'application/json','Content-Type':'application/json'};
    request.timeoutInterval=8;
    request.allowInsecureRequest=false;
    request.onRedirect=()=>null;
    if(body)request.body=JSON.stringify(body);
    const raw=await request.loadString();
    if(request.response?.url!==url||typeof raw!=='string'||raw.length>4096)throw Error('invalid_response');
    return {status:request.response.statusCode,data:JSON.parse(raw)};
  }
};
async function phoneAlert(message){
  const alert=new Alert();alert.title='ClubOps 登录转发';alert.message=message;alert.addAction('好');await alert.presentAlert();
}
async function setupPhone(){
  const alert=new Alert();alert.title='配置手机连接';
  alert.message='在电脑账号登录页点“显示手机配置”，粘贴 Scriptable 配置 JSON。凭证保存在手机 Keychain，不写入脚本。';
  alert.addSecureTextField('配置 JSON','');alert.addAction('检查并保存');alert.addCancelAction('取消');
  if(await alert.presentAlert()!==0)return;
  let settings;
  try{settings=PhoneForwarder.configuration(JSON.parse(alert.textFieldValue(0)));}
  catch{await phoneAlert('配置格式无效，请复制电脑显示的完整 Scriptable 配置。');return;}
  const result=await PhoneForwarder.check(settings,phoneIO);
  if(result.status!=='connected'){await phoneAlert(result.message);return;}
  Keychain.set(CLUBOPS_CONFIG_KEY,JSON.stringify(settings));
  // An update of the pairing must not erase prior attempt guards.
  await phoneAlert('手机配对已保存。请在电脑启动“测试手机转发”，再运行手机菜单中的手动连接测试。');
}
async function runPhoneMenu(){
  if(!readPhoneConfig()){await setupPhone();return;}
  const menu=new Alert();menu.title='ClubOps 登录转发';
  menu.message='日常短信由快捷指令传入；此菜单用于配置和合成连接测试。';
  menu.addAction('检查中转连接');menu.addAction('手动连接测试');menu.addAction('重新配置');
  menu.addDestructiveAction('移除手机配对');menu.addCancelAction('关闭');
  const action=await menu.presentSheet();
  if(action===0)await phoneAlert((await PhoneForwarder.check(readPhoneConfig(),phoneIO)).message);
  if(action===1){
    const prompt=new Alert();prompt.title='手动连接测试';
    prompt.message='先在电脑点击“测试手机转发”，再输入电脑显示的完整合成测试文字。';
    prompt.addSecureTextField('电脑显示的测试文字','');prompt.addAction('测试');prompt.addCancelAction('取消');
    if(await prompt.presentAlert()===0)await phoneAlert((await PhoneForwarder.forward(prompt.textFieldValue(0),readPhoneConfig(),phoneIO)).message);
  }
  if(action===2)await setupPhone();
  if(action===3){
    const confirm=new Alert();confirm.title='移除手机配对？';confirm.message='会停止本手机的自动转发；电脑及抖音登录态保留。';
    confirm.addDestructiveAction('移除');confirm.addCancelAction('取消');
    if(await confirm.presentAlert()===0 && Keychain.contains(CLUBOPS_CONFIG_KEY))Keychain.remove(CLUBOPS_CONFIG_KEY);
  }
}
try{
  const input=args.shortcutParameter;
  if(input!==null && input!==undefined){
    const receivedInput=typeof input==='string'?{text:input,received_at:CLUBOPS_STARTED_AT}:input;
    const result=await PhoneForwarder.forward(receivedInput,readPhoneConfig(),phoneIO);
    Script.setShortcutOutput(result); // Status only: no SMS, OTP, token or account id.
  }else if(config.runsInApp){await runPhoneMenu();}
  else Script.setShortcutOutput(PhoneForwarder.outcome('ignored'));
}catch{
  if(args.shortcutParameter===null||args.shortcutParameter===undefined){
    if(config.runsInApp)await phoneAlert('手机转发未完成，请检查配置或在电脑查看状态。');
  }
  Script.setShortcutOutput(PhoneForwarder.outcome('unavailable'));
}finally{Script.complete();}
