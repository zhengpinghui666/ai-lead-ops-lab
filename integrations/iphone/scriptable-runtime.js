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
