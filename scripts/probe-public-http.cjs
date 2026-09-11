'use strict';
// Single-request feasibility check, not a production collector. No browser, profile,
// credential discovery, script execution, redirect following or retries. The
// separate authorized-session helper may supply an in-memory scoped Cookie header.
const {createHash}=require('node:crypto');
const parser=require('../collector_parser.cjs');
const MAX_BYTES=1048576;
function targetURL(value){
  const u=new URL(value);
  if(u.protocol!=='https:'||u.hostname!=='www.douyin.com'||u.port||u.username||u.password||u.search||u.hash||!/^\/video\/\d{5,30}$/.test(u.pathname))throw new Error('Expected a canonical public Douyin video URL, without credentials or parameters');
  return u;
}
function summarize(body,contentType,videoID){
  const raw=body.toString('utf8');
  const hash=createHash('sha256').update(body).digest('hex');
  if(!raw.trim())return {status:'empty_response',body_sha256:hash,comment_count:null,complete_response:false};
  if(/^application\/(?:[\w.+-]+\+)?json\b/i.test(contentType)){
    let data;try{data=JSON.parse(raw);}catch{return {status:'invalid_json',body_sha256:hash};}
    const parsed=parser.comments(data,videoID);
    const status=!parsed.recognized?'unrecognized_json':parsed.skipped||parsed.truncated?'partial_comment_json':'recognized_comment_json';
    return {status,body_sha256:hash,
      platform_status:Number.isSafeInteger(data?.status_code)?data.status_code:null,
      comment_count:parsed.recognized?parsed.rows.length:null,skipped:parsed.skipped,
      has_more:parsed.hasMore,complete_response:parsed.recognized&&!parsed.skipped&&!parsed.truncated};
  }
  if(!/^text\/html\b/i.test(contentType))return {status:'unsupported_content_type',body_sha256:hash};
  // Inspect inert text only; identifiers and key names are hints, not parsed data.
  const visible=raw.replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/gi,' ').replace(/<style\b[^>]*>[\s\S]*?<\/style\s*>/gi,' ').replace(/<[^>]*>/g,' ');
  const blocked=parser.blockFromText(visible);
  return {status:blocked||'html_without_verified_comments',body_sha256:hash,
    comment_count:null,complete_response:false,visible_text_present:!!visible.trim(),
    script_count:(raw.match(/<script\b/gi)||[]).length,
    render_data_marker:/\bid\s*=\s*["']RENDER_DATA["']/i.test(raw),
    next_data_marker:/\bid\s*=\s*["']__NEXT_DATA__["']/i.test(raw),
    comments_key_marker:/["']comments["']\s*:\s*\[/.test(raw)};
}
async function probe(value,{fetchImpl=fetch,timeoutMs=15000,resource='page',cookieHeader=''}={}){
  const target=targetURL(value); // Validate before any external access.
  if(!Number.isInteger(timeoutMs)||timeoutMs<1||timeoutMs>15000)throw new Error('Invalid timeout');
  if(!['page','comments'].includes(resource))throw new Error('Unsupported probe resource');
  if(typeof cookieHeader!=='string'||cookieHeader.length>65536||/[\r\n]/.test(cookieHeader))throw new Error('Invalid session header');
  const requestURL=resource==='page'?target:new URL('/aweme/v1/web/comment/list/',target.origin);
  if(resource==='comments'){
    // Fixed unsigned candidate, not a replay of a browser's signed request.
    // aid is documented in the author's 2023 API notes, not a current contract:
    // https://github.com/Johnserf-Seed/TikTokDownload/wiki/APIv1.0#作品评论信息
    // Ordinary routing parameters do not establish authorization or availability.
    requestURL.searchParams.set('aid','6383');
    requestURL.searchParams.set('aweme_id',target.pathname.split('/').pop());
    requestURL.searchParams.set('cursor','0');requestURL.searchParams.set('count','10');
  }
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);
  const start=Date.now(),base={url:target.href,request_url:requestURL.href,resource,observed_at:new Date().toISOString(),transport:'http',browser_started:false,requests:1};
  try{
    const response=await fetchImpl(requestURL.href,{method:'GET',redirect:'manual',signal:controller.signal,
      headers:{'User-Agent':'ClubOps-HTTP-Probe/1.0','Accept':'text/html,application/json',...(cookieHeader?{Cookie:cookieHeader}:{})}});
    const meta={http_status:response.status,content_type:response.headers.get('content-type')||''};
    const stop={401:'needs_login',403:'access_denied',429:'rate_limited'}[response.status]||
      (response.status>=300&&response.status<400?'redirect_not_followed':response.status!==200?'http_error':'');
    if(stop){await response.body?.cancel();return {...base,...meta,status:stop,elapsed_ms:Date.now()-start};}
    if(Number(response.headers.get('content-length'))>MAX_BYTES){await response.body?.cancel();return {...base,...meta,status:'body_budget_exceeded',elapsed_ms:Date.now()-start};}
    const chunks=[];let bytes=0;
    if(response.body){
      const reader=response.body.getReader();
      try{for(;;){const {done,value:chunk}=await reader.read();if(done)break;bytes+=chunk.byteLength;
        if(bytes>MAX_BYTES){await reader.cancel();return {...base,...meta,status:'body_budget_exceeded',elapsed_ms:Date.now()-start};}
        chunks.push(Buffer.from(chunk));
      }}finally{reader.releaseLock();}
    }
    return {...base,...meta,...summarize(Buffer.concat(chunks),meta.content_type,target.pathname.split('/').pop()),bytes,elapsed_ms:Date.now()-start};
  }catch(error){
    return {...base,status:controller.signal.aborted?'timeout':'network_error',error_type:error.name,
      elapsed_ms:Date.now()-start};
  }finally{clearTimeout(timer);}
}
module.exports={probe,summarize,targetURL,MAX_BYTES};
if(require.main===module){
  if(process.argv.length<3||process.argv.length>4||(process.argv[3]&&process.argv[3]!=='--comments')){process.stderr.write('Usage: node scripts/probe-public-http.cjs https://www.douyin.com/video/VIDEO_ID [--comments]\n');process.exitCode=1;}
  else probe(process.argv[2],{resource:process.argv[3]==='--comments'?'comments':'page'}).then(result=>{
    process.stdout.write(JSON.stringify(result,null,2)+'\n');
    process.exitCode=result.status==='recognized_comment_json'?0:2;
  }).catch(error=>{process.stderr.write(error.message+'\n');process.exitCode=1;});
}
