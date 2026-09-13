// Public workbench access explicitly selected by the owner. No additional login.
// Computer connections remain authenticated; business data is never persisted here.
const LIMIT=6*1024*1024;
const headers={'cache-control':'no-store','x-content-type-options':'nosniff','referrer-policy':'no-referrer',
  'content-security-policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"};
const reply=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{...headers,'content-type':'application/json; charset=utf-8'}});
const equal=(a,b)=>{if(typeof a!=='string'||typeof b!=='string'||a.length!==b.length)return false;let n=0;for(let i=0;i<a.length;i++)n|=a.charCodeAt(i)^b.charCodeAt(i);return n===0;};
async function decodeBody(value){
  const raw=Uint8Array.from(atob(value.body),c=>c.charCodeAt(0)),max=64*1024*1024;
  if(raw.length>4*1024*1024)throw Error('size');
  if(value.body_encoding===undefined){if(raw.length>max)throw Error('size');return raw;}
  if(value.body_encoding!=='gzip')throw Error('encoding');
  const reader=new Response(raw).body.pipeThrough(new DecompressionStream('gzip')).getReader();
  let count=0;const parts=[];
  try{while(true){const {done,value}=await reader.read();if(done)break;count+=value.length;if(count>max)throw Error('size');parts.push(value);}}
  finally{await reader.cancel().catch(()=>{});}
  const decoded=new Uint8Array(count);let offset=0;for(const part of parts){decoded.set(part,offset);offset+=part.length;}return decoded;
}
const GETS=new Set(['/', '/app.js','/app.css','/vendor/lucide.min.js','/login','/login.js','/login.css','/login-guide','/iphone-script',
  '/api/state','/api/collector','/api/monitor-comments','/api/login-recovery','/api/live-message','/api/live-history','/api/analysis-history','/api/lead-live-history','/api/collector-evidence','/api/export',
  '/api/uid-inbox','/api/asset-references','/api/asset-keywords','/api/asset-keyword']);
export function permitted(method,path){return method==='GET'?GETS.has(path):method==='POST'&&/^\/api\/[a-z][a-z-]{0,64}$/.test(path)&&path!=='/api/service-stop';}
async function boundedBody(request,max){if(Number(request.headers.get('content-length')||0)>max)throw Error('size');
  const reader=request.body?.getReader();if(!reader)return '';let count=0,parts=[];
  try{while(true){const {done,value}=await reader.read();if(done)break;count+=value.length;if(count>max)throw Error('size');parts.push(value);}}
  finally{await reader.cancel().catch(()=>{});}
  const result=new Uint8Array(count);let at=0;for(const part of parts){result.set(part,at);at+=part.length;}return new TextDecoder().decode(result);
}
export default {async fetch(request,env){
  if(new URL(request.url).origin!==env.PUBLIC_ORIGIN)return reply({error:'入口地址不正确'},403);
  if(!env.CONNECTOR_TOKEN)return reply({error:'入口尚未完成配置'},503);
  try{return await env.TEAM.get(env.TEAM.idFromName('clubops-workbench-v2')).fetch(request);}
  catch{return reply({error:'工作台连接暂时不可用'},503);}
}};
export class TeamGateway {
  constructor(state,env){this.state=state;this.env=env;this.pending=new Map();
    this.replies=new WeakMap();
    this.state.setWebSocketAutoResponse(new WebSocketRequestResponsePair('ping','pong'));
  }
  sockets(){return this.state.getWebSockets('computer').filter(s=>s.readyState===1&&!s.deserializeAttachment()?.expired);}
  async fetch(request){
    const u=new URL(request.url),path=u.pathname;
    if(u.origin!==this.env.PUBLIC_ORIGIN)return reply({error:'入口地址不正确'},403);
    if(!this.env.CONNECTOR_TOKEN)return reply({error:'入口尚未完成配置'},503);
    if(path==='/_access/connect'){
      if(request.method!=='GET'||request.headers.get('Upgrade')?.toLowerCase()!=='websocket'||
        !equal(request.headers.get('Authorization'),`Bearer ${this.env.CONNECTOR_TOKEN}`))return reply({error:'unauthorized'},401);
      if(this.sockets().length)return reply({error:'已有电脑连接'},409);
      const [client,server]=Object.values(new WebSocketPair());this.state.acceptWebSocket(server,['computer']);
      return new Response(null,{status:101,webSocket:client});
    }
    if(request.method==='POST'&&request.headers.get('Origin')!==this.env.PUBLIC_ORIGIN)return reply({error:'请求来源不匹配'},403);
    if(path==='/_access/status'&&request.method==='GET')return reply({online:!!this.sockets().length,access:'public',login_required:false,revision:'connector-expiry-v1'});
    if(!permitted(request.method,path)||u.search.length>4000)return reply({error:'not_found'},404);
    const socket=this.sockets()[0];
    if(!socket)return reply({error:'电脑工作台离线，请确认电脑已开机并运行 ClubOps'},503);
    if(this.pending.size>=12)return reply({error:'工作台繁忙，请稍后重试'},503);
    let body='';try{if(request.method==='POST')body=await boundedBody(request,262144);}catch{return reply({error:'请求内容过大'},413);}
    const id=crypto.randomUUID(),replyVersion=this.replies.get(socket)||0;let resolve;const promise=new Promise(r=>resolve=r);
    const timer=setTimeout(()=>{
      this.pending.delete(id);resolve(reply({error:request.method==='POST'?'操作结果尚未确认，请查看记录；不要重复提交。':'电脑响应超时，请稍后查看。'},504));
      // A protocol-level open socket can outlive the computer process. If no
      // response arrived during this entire request, release it so the existing
      // authenticated connector can reconnect. Never replay pending operations.
      if((this.replies.get(socket)||0)===replyVersion){
        try{socket.close(1011,'computer response timeout');}catch{}
        this.webSocketClose(socket);
      }
    },20000);
    this.pending.set(id,{resolve,timer,socket});
    try{socket.send(JSON.stringify({id,method:request.method,path:path+u.search,body,headers:{'content-type':request.headers.get('Content-Type')||'', 'x-clubops-token':request.headers.get('X-ClubOps-Token')||''}}));}
    catch{clearTimeout(timer);this.pending.delete(id);return reply({error:'电脑连接已断开'},503);}
    return await promise;
  }
  async webSocketMessage(socket,message){
    if(typeof message!=='string'||message.length>LIMIT){socket.close(1009,'size');return;}
    let value;try{value=JSON.parse(message);}catch{return;}
    const pending=this.pending.get(value.id);if(!pending||pending.socket!==socket)return;
    this.replies.set(socket,(this.replies.get(socket)||0)+1);
    this.pending.delete(value.id);clearTimeout(pending.timer);
    try{
      if(!Number.isInteger(value.status)||value.status<200||value.status>599||typeof value.body!=='string')throw Error('invalid');
      const bytes=await decodeBody(value),mime=String(value.content_type||'application/octet-stream').slice(0,100);
      pending.resolve(new Response([204,205,304].includes(value.status)?null:bytes,{status:value.status,headers:{...headers,'content-type':mime}}));
    }catch{pending.resolve(reply({error:'电脑响应格式不正确'},502));}
  }
  webSocketClose(socket){
    // Cloudflare can retain a socket while its close handshake is pending.
    // Persist retirement so hibernation/reconnection cannot revive that slot.
    try{socket.serializeAttachment({expired:true});}catch{}
    try{socket.close(1000,'connection closed');}catch{}
    for(const [id,p] of this.pending){if(p.socket!==socket)continue;clearTimeout(p.timer);p.resolve(reply({error:'电脑连接中断；若刚提交操作，请核对记录。'},503));this.pending.delete(id);}
  }
  webSocketError(socket){this.webSocketClose(socket);}
}
