// A single account's short-lived SMS handoff. No message bodies or OTPs are logged.
import iphoneScript from './iphone-client.mjs';
const encoder = new TextEncoder();
const json = (value, status=200) => new Response(JSON.stringify(value), {status, headers:{
  'content-type':'application/json', 'cache-control':'no-store', 'x-content-type-options':'nosniff'}});
const validToken = value => typeof value==='string' && /^[A-Za-z0-9_-]{40,128}$/.test(value);
class InvalidRequest extends Error {}
async function equalSecret(a,b){
  if(!validToken(b)||typeof a!=='string'||a.length>150)return false;
  const hashes=await Promise.all([a,b].map(x=>crypto.subtle.digest('SHA-256',encoder.encode(x))));
  const x=new Uint8Array(hashes[0]),y=new Uint8Array(hashes[1]);let diff=0;
  for(let i=0;i<x.length;i++)diff|=x[i]^y[i];return diff===0;
}
async function body(request){
  if(request.headers.get('content-type')?.split(';')[0].trim().toLowerCase()!=='application/json')throw new InvalidRequest();
  if(Number(request.headers.get('content-length')||0)>2048)throw new InvalidRequest();
  const reader=request.body?.getReader();if(!reader)throw new InvalidRequest();
  const parts=[];let size=0;
  try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;
    if(size>2048)throw new InvalidRequest();parts.push(value);}}
  finally{await reader.cancel().catch(()=>{});}
  const raw=new Uint8Array(size);let offset=0;for(const part of parts){raw.set(part,offset);offset+=part.length;}
  let result;try{result=JSON.parse(new TextDecoder().decode(raw));}catch{throw new InvalidRequest();}
  if(!result||Array.isArray(result)||typeof result!=='object')throw new InvalidRequest();return result;
}
const keys = (obj,names) => Object.keys(obj).sort().join(',')===names.sort().join(',');
async function cryptCode(value,secret,decrypt=false){
  const material=await crypto.subtle.digest('SHA-256',encoder.encode('clubops-otp-at-rest-v1:'+secret));
  const key=await crypto.subtle.importKey('raw',material,'AES-GCM',false,[decrypt?'decrypt':'encrypt']);
  if(decrypt){const {iv,data}=value;
    const plain=await crypto.subtle.decrypt({name:'AES-GCM',iv:new Uint8Array(iv)},key,new Uint8Array(data));
    return new TextDecoder().decode(plain);}
  const iv=crypto.getRandomValues(new Uint8Array(12));
  return {iv:[...iv],data:[...new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},key,encoder.encode(value)))]};
}

export class LoginRelay {
  constructor(ctx,env){this.ctx=ctx;this.env=env;}
  async alarm(){
    // A delayed alarm from an older task must not clear a newer login.
    await this.ctx.storage.transaction(async storage=>{
      const job=await storage.get('job');
      if(!job||job.expires_at<=Date.now()){
        await storage.delete('job');await this.ctx.storage.deleteAlarm();
      }else await this.ctx.storage.setAlarm(job.expires_at);
    });
  }
  async fetch(request){
    try{
      const url=new URL(request.url),method=request.method;
      if(url.search||url.username||url.password)return json({error:'invalid_route'},400);
      const authorization=request.headers.get('authorization')||'';
      const credential=authorization.startsWith('Bearer ')?authorization.slice(7):'';
      const backend=await equalSecret(credential,this.env.BACKEND_TOKEN);
      const phone=!backend&&await equalSecret(credential,this.env.PHONE_TOKEN);
      if(!backend&&!phone)return json({error:'unauthorized'},401);
      if(backend&&url.pathname==='/v1/health'&&method==='GET')return json({service:'clubops-login-relay',version:1});
      const data=method==='POST'?await body(request):{};
      return await this.ctx.storage.transaction(async storage=>{
        const now=Date.now();let job=await storage.get('job');
        if(job&&job.expires_at<=now){await storage.delete('job');job=null;}
        if(backend&&url.pathname==='/v1/login'&&method==='POST'){
          if(!keys(data,['id','account'])||typeof data.id!=='string'||typeof data.account!=='string'||
              !/^[a-f0-9]{32}$/.test(data.id)||!/^[A-Za-z0-9_.-]{2,64}$/.test(data.account))
            return json({error:'invalid_job'},400);
          if(job){if(job.id===data.id&&job.account===data.account)return json({id:job.id,expires_at:job.expires_at});
            return json({error:'login_in_progress'},409);}
          job={id:data.id,account:data.account,created_at:now,expires_at:now+180000,status:'waiting'};
          await storage.put('job',job);await this.ctx.storage.setAlarm(job.expires_at);
          return json({id:job.id,expires_at:job.expires_at},201);
        }
        if(phone&&url.pathname==='/v1/pending'&&method==='GET')
          return json(job&&job.status==='waiting'?{id:job.id,account:job.account,created_at:job.created_at,expires_at:job.expires_at}:{id:null});
        if(phone&&url.pathname==='/v1/otp'&&method==='POST'){
          if(!keys(data,['id','code','received_at'])||typeof data.code!=='string'||!/^\d{4,8}$/.test(data.code)||
              !Number.isSafeInteger(data.received_at))return json({error:'invalid_otp'},400);
          if(!job||job.id!==data.id)return json({error:'no_matching_login'},409);
          if(job.status!=='waiting')return json({error:'already_received'},409);
          if(data.received_at<job.created_at||data.received_at>now+30000||now-data.received_at>180000)
            return json({error:'stale_otp'},409);
          job.cipher=await cryptCode(data.code,this.env.BACKEND_TOKEN);job.status='received';
          await storage.put('job',job);return json({status:'received'});
        }
        if(backend&&url.pathname==='/v1/take'&&method==='POST'){
          if(!keys(data,['id'])||!job||job.id!==data.id)return json({error:'no_matching_login'},409);
          if(job.status==='taken')return json({status:'taken'});
          if(job.status!=='received')return json({status:'waiting'});
          const code=await cryptCode(job.cipher,this.env.BACKEND_TOKEN,true);
          delete job.cipher;job.status='taken';await storage.put('job',job);
          return json({status:'received',id:job.id,code});
        }
        if(backend&&url.pathname==='/v1/cancel'&&method==='POST'){
          if(!keys(data,['id']))return json({error:'invalid_job'},400);
          if(job&&job.id!==data.id)return json({error:'no_matching_login'},409);
          await storage.delete('job');await this.ctx.storage.deleteAlarm();return json({status:'cleared'});
        }
        return json({error:'not_found'},404);
      });
    }catch(error){return error instanceof InvalidRequest?json({error:'invalid_request'},400):json({error:'relay_unavailable'},503);}
  }
}
export default {async fetch(request,env){
  if(new URL(request.url).protocol!=='https:')return json({error:'https_required'},400);
  // Public, credential-free installer only. No state/OTP/configuration is exposed here.
  if(new URL(request.url).pathname==='/iphone-script' && request.method==='GET')
    return new Response(iphoneScript,{headers:{'content-type':'text/plain; charset=utf-8',
      'content-disposition':'attachment; filename="ClubOps-Login.js"',
      'cache-control':'no-store','x-content-type-options':'nosniff'}});
  if(!validToken(env.BACKEND_TOKEN)||!validToken(env.PHONE_TOKEN)||env.BACKEND_TOKEN===env.PHONE_TOKEN)
    return json({error:'not_configured'},503);
  const auth=request.headers.get('authorization')||'';
  const token=auth.startsWith('Bearer ')?auth.slice(7):'';
  // Reject unauthenticated traffic before it can wake the storage object.
  if(!await equalSecret(token,env.BACKEND_TOKEN)&&!await equalSecret(token,env.PHONE_TOKEN))
    return json({error:'unauthorized'},401);
  return env.LOGIN_RELAY.get(env.LOGIN_RELAY.idFromName('single-account')).fetch(request);
}};
