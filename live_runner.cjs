'use strict';
// Observes incoming data on one normal live page. Never sends chat messages.
const readline=require('node:readline');
const {decodeFrame,decodeHttpResponse,socketRoom,httpRoom,MAX_FRAME}=require('./live_protocol.cjs');
const input=readline.createInterface({input:process.stdin});
let config,context,stopping=false,finish,queued=0,tail=Promise.resolve(),received=0,frames=0,invalid=0,connected=0,boundRoom=null;
let reason='completed',timer,guardTimer,closeTimer,activeSocket;const methodCounts={},errorCounts={},timestamps={};
const pageInfo={navigation:'not_started',http_status:null,body_chars:0,gate:'unknown'},socketTargets=[];
const networkInfo={requests:0,failures:{},script_errors:{},live_responses:{},live_routes:{}};
let pollTail=Promise.resolve(),pollQueued=0;const pollInfo={responses:0,decoded:0,missing_room:0,decode_errors:0,body_errors:0,oversized:0,empty_bodies:0,empty_messages:0,max_bytes:0};const pollFields={},transports=new Set();
let validMessages=false,connectionLost=false,lastTransport=null;
function countDiagnostic(group,key){if(Object.hasOwn(group,key)||Object.keys(group).length<20)group[key]=Math.min(1000000,(group[key]||0)+1);}
function bindRoom(room,transport){
  if(boundRoom&&room!==boundRoom){stop('room_changed');return false;}boundRoom=room;
  if(!transports.has(transport)){transports.add(transport);void emit({type:'stream',room_id:room,transport});}return true;
}
function acceptParsed(parsed,transport){
  const known=Object.entries(parsed.methods).reduce((sum,[name,count])=>sum+(name==='unknown'?0:count),0);
  if(known>parsed.invalid){validMessages=true;connectionLost=false;lastTransport=transport;}
  invalid+=parsed.invalid;
  for(const [name,count] of Object.entries(parsed.methods))methodCounts[name]=(methodCounts[name]||0)+count;
  for(const [name,count] of Object.entries(parsed.errors))errorCounts[name]=(errorCounts[name]||0)+count;
  for(const [name,count] of Object.entries(parsed.timestamps))timestamps[name]=(timestamps[name]||0)+count;
  for(const record of parsed.records){if(stopping)break;received++;void emit({type:'message',record});if(received>=config.max_messages)stop('completed');}
  if(parsed.ended)stop('ended');else if(invalid>=10)stop('schema_changed');
}
function stop(status){if(stopping)return;stopping=true;reason=status;finish?.();}
function budgetStatus(){return connectionLost?'disconnected':invalid&&!received?'schema_changed':validMessages?'completed':'no_data';}
function emit(value){
  if(queued>=128){stop('resource_limited');return Promise.resolve();}
  queued++;tail=tail.then(()=>new Promise((resolve,reject)=>process.stdout.write(JSON.stringify(value)+'\n',error=>error?reject(error):resolve()))).finally(()=>queued--);
  tail.catch(()=>stop('interrupted'));return tail;
}
input.on('line',line=>{try{const v=JSON.parse(line);if(!config){config=v;void main();}else if(v.command==='stop')stop('cancelled');}catch{stop('failed');}});
input.on('close',()=>stop('interrupted'));
async function main(){
  try{
    if(!/^https:\/\/live\.douyin\.com\/[1-9][0-9]{2,29}$/.test(config.room_url)||!config.profile_dir||!Number.isInteger(config.duration_seconds)||config.duration_seconds<10||config.duration_seconds>3600||!Number.isInteger(config.max_messages)||config.max_messages<1||config.max_messages>5000)throw Error('config');
    let chromium,devices;try{({chromium,devices}=require(process.env.CLUBOPS_PLAYWRIGHT||'playwright'));}catch{stop('dependency_missing');return;}
    const ended=new Promise(resolve=>finish=resolve);
    timer=setTimeout(()=>stop(budgetStatus()),config.duration_seconds*1000+25000);
    await emit({type:'status',status:'connecting'});
    // Keep the documented desktop client identity in both background and visible sessions.
    context=await chromium.launchPersistentContext(config.profile_dir,{...require('./browser_config.cjs')(),userAgent:devices['Desktop Chrome'].userAgent,headless:!config.interactive,acceptDownloads:false,viewport:{width:1280,height:800},timeout:25000});
    if(config.session_cookies){
      if(!Array.isArray(config.session_cookies)||config.session_cookies.length>150)throw Error('account_session');
      await context.clearCookies();
      await context.addCookies(config.session_cookies);
      delete config.session_cookies;
    }
    if(stopping)return;
    context.on('close',()=>stop('interrupted'));
    const page=context.pages()[0]||await context.newPage();
    page.on('request',()=>networkInfo.requests=Math.min(1000000,networkInfo.requests+1));
    page.on('requestfailed',request=>{try{const code=request.failure()?.errorText?.match(/^net::(ERR_[A-Z_]{1,50})$/)?.[1]||'other';countDiagnostic(networkInfo.failures,code);}catch{}});
    page.on('pageerror',error=>countDiagnostic(networkInfo.script_errors,/^(?:TypeError|ReferenceError|SyntaxError|RangeError|Error|NotSupportedError|NotAllowedError|AbortError|SecurityError|NetworkError|InvalidStateError)$/.test(error.name)?error.name:'other'));
    page.on('response',response=>{try{const u=new URL(response.url());if((u.hostname==='live.douyin.com'||u.hostname.endsWith('.douyin.com'))&&u.pathname.startsWith('/webcast/')){countDiagnostic(networkInfo.live_responses,String(response.status()));if(/^\/webcast\/[a-zA-Z0-9/_-]{1,100}$/.test(u.pathname))countDiagnostic(networkInfo.live_routes,u.pathname);}}catch{}});
    page.on('response',response=>{try{const u=new URL(response.url());if(u.hostname==='live.douyin.com'&&response.status()===429)stop('rate_limited');if(u.hostname==='live.douyin.com'&&response.status()===403)stop('access_denied');}catch{}});
    // The normal page may use HTTP polling instead of opening a WebSocket.
    // Observe only responses the page already requested; never issue extra fetches.
    page.on('response',response=>{
      let url;try{url=new URL(response.url());}catch{return;}
      if(stopping||url.protocol!=='https:'||!url.hostname.endsWith('.douyin.com')||url.pathname!=='/webcast/im/fetch/'||response.status()!==200)return;
      pollInfo.responses++;const room=httpRoom(response.url());if(!room){pollInfo.missing_room++;return;}
      if(pollQueued>=8){stop('resource_limited');return;}pollQueued++;
      pollTail=pollTail.then(async()=>{
        if(stopping)return;
        let raw;
        try{const size=Number(await response.headerValue('content-length'));if(size>MAX_FRAME){pollInfo.oversized++;stop('resource_limited');return;}raw=await response.body();}
        catch{if(!stopping)pollInfo.body_errors++;return;}
        if(stopping)return;
        if(raw.length>MAX_FRAME){pollInfo.oversized++;stop('resource_limited');return;}
        if(!raw.length){pollInfo.empty_bodies++;pollInfo.empty_messages++;return;}
        try{
          const parsed=decodeHttpResponse(raw,room);
          if(!parsed.response_bytes){pollInfo.empty_bodies++;pollInfo.empty_messages++;return;}
          if(!bindRoom(room,'browser_http_poll'))return;
          pollInfo.decoded++;pollInfo.max_bytes=Math.max(pollInfo.max_bytes,parsed.response_bytes);
          if(!Object.keys(parsed.methods).length)pollInfo.empty_messages++;
          for(const field of parsed.response_fields)countDiagnostic(pollFields,'f'+field);
          if(pollInfo.decoded===1)void emit({type:'status',status:'running'});acceptParsed(parsed,'browser_http_poll');
        }
        catch{pollInfo.decode_errors++;invalid++;if(invalid>=10)stop('schema_changed');}
      }).finally(()=>pollQueued--);
      pollTail.catch(()=>stop('failed'));
    });
    page.on('websocket',ws=>{
      try{const address=new URL(ws.url());if(socketTargets.length<8&&/^[a-z0-9.-]{1,100}$/.test(address.hostname)&&/^[/a-zA-Z0-9_-]{1,200}$/.test(address.pathname)&&!socketTargets.some(x=>x.host===address.hostname&&x.path===address.pathname))socketTargets.push({host:address.hostname,path:address.pathname});}catch{}
      if(stopping)return;const room=socketRoom(ws.url());if(!room)return;
      if(!bindRoom(room,'browser_websocket'))return;
      if(++connected>3){stop('disconnected');return;}
      activeSocket=ws;lastTransport='browser_websocket';const connection=connected;
      let valid=false;
      ws.on('framereceived',event=>{
        if(stopping||activeSocket!==ws)return;
        try{
          const parsed=decodeFrame(event.payload,room);frames++;
          if(!valid){valid=true;void emit({type:'status',status:connection>1?'reconnected':'running'});}
          acceptParsed(parsed,'browser_websocket');
        }catch{invalid++;if(invalid>=10)stop('schema_changed');}
      });
      const lost=()=>{if(!stopping&&activeSocket===ws&&lastTransport!=='browser_http_poll'){connectionLost=true;void emit({type:'status',status:'disconnected'});}};
      ws.on('close',lost);
      ws.on('socketerror',lost);
    });
    pageInfo.navigation='loading';
    const navigation=await page.goto(config.room_url,{waitUntil:'domcontentloaded',timeout:25000});
    pageInfo.navigation='loaded';pageInfo.http_status=navigation?.status()||null;
    if(stopping)return;
    clearTimeout(timer);timer=setTimeout(()=>stop(budgetStatus()),config.duration_seconds*1000);
    // Only visible gate text is inspected; no browser storage or cookies exported.
    guardTimer=setInterval(async()=>{
      if(stopping)return;
      try{
        const current=new URL(page.url()),expected=new URL(config.room_url);
        if(current.origin!==expected.origin||current.pathname.replace(/\/$/,'')!==expected.pathname)return stop('room_changed');
        const body=(await page.locator('body').innerText({timeout:1000})).slice(0,25000);
        pageInfo.body_chars=body.length;pageInfo.gate='none';
        if(/完成验证后继续|请完成下方验证|拖动滑块|安全验证/.test(body)){pageInfo.gate='verification';stop('needs_verification');}
        else if(!boundRoom&&/登录后观看|请登录后观看/.test(body)){pageInfo.gate='login';stop('needs_login');}
        else if(!boundRoom&&/直播已结束|主播已下播/.test(body)){pageInfo.gate='ended';stop('ended');}
        else if(/聊天功能不可用/.test(body))pageInfo.gate='chat_unavailable';
      }catch{}
    },4000);
    await ended;
  }catch(error){if(pageInfo.navigation==='loading')pageInfo.navigation=error?.name==='TimeoutError'?'timeout':'failed';if(!stopping)stop('failed');}
  finally{
    clearTimeout(timer);clearInterval(guardTimer);
    // Close our own context before reporting terminal state; bounded fallback.
    closeTimer=setTimeout(()=>process.exit(2),12000);closeTimer.unref();
    try{await context?.close();}catch{}
    await pollTail.catch(()=>{});
    await tail.catch(()=>{});
    await emit({type:'diagnostic',methods:methodCounts,errors:errorCounts,timestamps,page:pageInfo,sockets:socketTargets,network:networkInfo,poll:pollInfo,poll_fields:pollFields});
    await emit({type:'final',status:reason,frames,invalid,received,connections:connected});
    await tail.catch(()=>{});clearTimeout(closeTimer);input.close();
  }
}
