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
if (typeof module!=='undefined') module.exports=PhoneForwarder;
