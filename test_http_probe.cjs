'use strict';
const assert=require('node:assert/strict');
const {probe,summarize,targetURL,MAX_BYTES}=require('./scripts/probe-public-http.cjs');
const {sessionProbe}=require('./scripts/probe-session-http.cjs');
const url='https://www.douyin.com/video/7664994032866659594';
async function main(){
  for(const bad of ['http://www.douyin.com/video/12345','https://example.com/video/12345',url+'?token=private',url+'#x','https://user:pass@www.douyin.com/video/12345','https://www.douyin.com/aweme/v1/web/comment/list/','https://www.douyin.com/video/not-an-id'])assert.throws(()=>targetURL(bad));
  let calls=0;
  await assert.rejects(probe('http://127.0.0.1/',{fetchImpl:async()=>{calls++;}}));assert.equal(calls,0);
  for(const [http,status] of [[401,'needs_login'],[403,'access_denied'],[429,'rate_limited'],[302,'redirect_not_followed'],[500,'http_error']]){
    let cancelled=false;
    const result=await probe(url,{fetchImpl:async(target,options)=>{
      calls++;assert.equal(target,url);assert.equal(options.redirect,'manual');assert.equal(options.method,'GET');
      assert.deepEqual(Object.keys(options.headers).sort(),['Accept','User-Agent']);
      return new Response(new ReadableStream({cancel(){cancelled=true;}}),{status:http,headers:{Location:'https://example.com/private?token=secret','Set-Cookie':'private=secret'}});
    }});
    assert.equal(result.status,status);assert.equal(cancelled,true);assert.equal(result.requests,1);
    assert.ok(!JSON.stringify(result).includes('secret'));
  }
  assert.equal(calls,5);
  await assert.rejects(probe(url,{resource:'private',fetchImpl:async()=>{calls++;}}));assert.equal(calls,5);
  const commentProbe=await probe(url,{resource:'comments',fetchImpl:async(target,options)=>{
    calls++;const u=new URL(target);assert.equal(u.origin,'https://www.douyin.com');assert.equal(u.pathname,'/aweme/v1/web/comment/list/');
    assert.deepEqual([...u.searchParams],[['aid','6383'],['aweme_id','7664994032866659594'],['cursor','0'],['count','10']]);
    assert.deepEqual(Object.keys(options.headers).sort(),['Accept','User-Agent']);assert.equal(options.redirect,'manual');
    return new Response('',{status:200,headers:{'Content-Type':'application/json'}});
  }});
  assert.equal(calls,6);assert.equal(commentProbe.resource,'comments');assert.equal(commentProbe.url,url);
  assert.equal(commentProbe.status,'empty_response');assert.equal(commentProbe.comment_count,null);assert.equal(commentProbe.complete_response,false);
  const html=await probe(url,{fetchImpl:async()=>new Response('<html><script>window.fake={comments:[]}; throw Error("Do not execute");</script></html>',{headers:{'Content-Type':'text/html'}})});
  assert.equal(html.status,'html_without_verified_comments');assert.equal(html.comment_count,null);assert.equal(html.browser_started,false);assert.equal(html.visible_text_present,false);
  const hints=summarize(Buffer.from('<script id="RENDER_DATA">{"comments":[]}</script>'),'text/html','7664994032866659594');
  assert.equal(hints.render_data_marker,true);assert.equal(hints.comments_key_marker,true);assert.equal(hints.complete_response,false);assert.equal(hints.comment_count,null);
  assert.equal(summarize(Buffer.from('<h1>安全验证</h1>'),'text/html','7664994032866659594').status,'needs_verification');
  assert.equal(summarize(Buffer.from('{'),'application/json','7664994032866659594').status,'invalid_json');
  assert.equal(summarize(Buffer.from('{}'),'application/json','7664994032866659594').status,'unrecognized_json');
  const rejected=summarize(Buffer.from('{"status_code":99,"comments":[],"secret":"not printed"}'),'application/json','7664994032866659594');
  assert.equal(rejected.platform_status,99);assert.equal(rejected.status,'unrecognized_json');assert.equal(rejected.comment_count,null);assert.ok(!JSON.stringify(rejected).includes('not printed'));
  const empty=summarize(Buffer.from('{"status_code":0,"comments":null,"total":0,"has_more":0}'),'application/json','7664994032866659594');
  assert.equal(empty.comment_count,0);assert.equal(empty.complete_response,true);
  const json=summarize(Buffer.from(JSON.stringify({comments:[{cid:'12345',text:'synthetic fixture',aweme_id:'7664994032866659594'}],has_more:1})),'application/json; charset=utf-8','7664994032866659594');
  assert.equal(json.status,'recognized_comment_json');assert.equal(json.comment_count,1);assert.equal(json.has_more,true);
  const mismatch=summarize(Buffer.from(JSON.stringify({comments:[{cid:'12345',text:'synthetic fixture',aweme_id:'11111'}]})),'application/json','7664994032866659594');
  assert.equal(mismatch.skipped,1);assert.equal(mismatch.complete_response,false);assert.equal(mismatch.status,'partial_comment_json');
  const oversizedList=summarize(Buffer.from(JSON.stringify({comments:Array.from({length:1001},(_,i)=>({cid:String(10000+i),text:'synthetic bounded item'}))})),'application/json','7664994032866659594');
  assert.equal(oversizedList.status,'partial_comment_json');assert.equal(oversizedList.comment_count,1000);assert.equal(oversizedList.complete_response,false);
  for(const headers of [{},{'Content-Length':String(MAX_BYTES+1)}]){
    const large=await probe(url,{fetchImpl:async()=>new Response(new Uint8Array(MAX_BYTES+1),{headers})});
    assert.equal(large.status,'body_budget_exceeded');
  }
  const timeout=await probe(url,{timeoutMs:10,fetchImpl:async(_,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('timeout')),{once:true}))});
  assert.equal(timeout.status,'timeout');
  const error=await probe(url,{fetchImpl:async()=>{throw new Error('secret request details');}});
  assert.equal(error.status,'network_error');assert.ok(!JSON.stringify(error).includes('secret'));
  let closed=false,sessionRequests=0;
  const session=await sessionProbe(url,{launchContext:async()=>({cookies:async(scope)=>{
    assert.equal(scope,'https://www.douyin.com/aweme/v1/web/comment/list/');return [{name:'synthetic_session',value:'secret-value'}];
  },close:async()=>{closed=true;}}),probeImpl:async(target,options)=>{
    sessionRequests++;assert.equal(closed,true);assert.equal(options.cookieHeader,'synthetic_session=secret-value');
    return probe(target,{...options,fetchImpl:async(_,request)=>{assert.equal(request.headers.Cookie,'synthetic_session=secret-value');return new Response('');}});
  }});
  assert.equal(sessionRequests,1);assert.equal(session.browser_closed_before_request,true);assert.ok(!JSON.stringify(session).includes('secret-value'));
  const closeFailure=await sessionProbe(url,{launchContext:async()=>({cookies:async()=>[{name:'x',value:'secret'}],close:async()=>{throw new Error('secret');}}),probeImpl:async()=>{sessionRequests++;}});
  assert.equal(closeFailure.status,'session_probe_failed');assert.equal(sessionRequests,1);assert.ok(!JSON.stringify(closeFailure).includes('secret'));
  const noSession=await sessionProbe(url,{launchContext:async()=>({cookies:async()=>[],close:async()=>{}}),probeImpl:async()=>{sessionRequests++;}});
  assert.equal(noSession.status,'needs_login');assert.equal(sessionRequests,1);
  await assert.rejects(probe(url,{cookieHeader:'a=secret\r\nx=test',fetchImpl:async()=>{sessionRequests++;}}));assert.equal(sessionRequests,1);
  console.log('HTTP probe: anonymous/scoped-session modes, browser-close barrier, URL scope, no redirects, secret redaction, inert HTML, JSON identity, limits and timeout passed (synthetic only).');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
